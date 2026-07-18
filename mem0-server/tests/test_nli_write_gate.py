"""Unit coverage for nli_write_gate.evaluate (v0.27.2 R5).

Pure: the search + judge are injected fakes, so no live server / Qdrant / Codex.
Asserts the fail-OPEN contract (the gate only acts on a CONFIDENT contradiction).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nli_write_gate as gate  # noqa: E402


def _neighbor(mid="can-1", text="the port is 8080"):
    return {"id": mid, "memory": text, "metadata": {"tier": "canonical"}}


def _search_hit(*neighbors):
    return lambda q, f, t, k: list(neighbors)


_SEARCH_NONE = lambda q, f, t, k: []
_JUDGE_YES = lambda a, b, t: {"ok": True, "contradicts": True}
_JUDGE_NO = lambda a, b, t: {"ok": True, "contradicts": False}
_JUDGE_UNPARSEABLE = lambda a, b, t: {"ok": True, "contradicts": None}
_JUDGE_DOWN = lambda a, b, t: {"ok": False, "error_type": "unreachable"}


def test_empty_text_admits_without_search():
    called = {"n": 0}
    def s(q, f, t, k):
        called["n"] += 1
        return []
    out = gate.evaluate("   ", "youruser", None, search_fn=s, judge_fn=_JUDGE_YES)
    assert out["action"] == "admit"
    assert called["n"] == 0


def test_no_canonical_neighbor_admits_without_judge():
    called = {"n": 0}
    def j(a, b, t):
        called["n"] += 1
        return {"ok": True, "contradicts": True}
    out = gate.evaluate("anything", "youruser", None, search_fn=_SEARCH_NONE, judge_fn=j)
    assert out["action"] == "admit"
    assert out["detail"] == "no-canonical-neighbor"
    assert called["n"] == 0  # the fast pre-filter spares the hot path the Codex call


def test_contradiction_returns_flag():
    out = gate.evaluate("the port is 9000", "youruser", None,
                        search_fn=_search_hit(_neighbor()), judge_fn=_JUDGE_YES)
    assert out["action"] == "flag"
    assert out["canonical_id"] == "can-1"


def test_no_contradiction_admits():
    out = gate.evaluate("a fresh unrelated fact", "youruser", None,
                        search_fn=_search_hit(_neighbor()), judge_fn=_JUDGE_NO)
    assert out["action"] == "admit"
    assert out["detail"] == "no-contradiction"


def test_unparseable_verdict_admits():
    out = gate.evaluate("x", "youruser", None,
                        search_fn=_search_hit(_neighbor()), judge_fn=_JUDGE_UNPARSEABLE)
    assert out["action"] == "admit"


def test_shim_down_fails_open():
    out = gate.evaluate("x", "youruser", None,
                        search_fn=_search_hit(_neighbor()), judge_fn=_JUDGE_DOWN)
    assert out["action"] == "admit"
    assert out["detail"].startswith("judge-unavailable")


def test_search_error_fails_open():
    def s(q, f, t, k):
        raise RuntimeError("qdrant boom")
    out = gate.evaluate("x", "youruser", None, search_fn=s, judge_fn=_JUDGE_YES)
    assert out["action"] == "admit"
    assert out["detail"] == "search-error"


def test_judge_raises_fails_open():
    def j(a, b, t):
        raise RuntimeError("kaboom")
    out = gate.evaluate("x", "youruser", None,
                        search_fn=_search_hit(_neighbor()), judge_fn=j)
    assert out["action"] == "admit"
    assert out["detail"] == "judge-error"


def test_neighbor_without_text_admits():
    out = gate.evaluate("x", "youruser", None,
                        search_fn=_search_hit({"id": "c2", "memory": ""}), judge_fn=_JUDGE_YES)
    assert out["action"] == "admit"
    assert out["detail"] == "neighbor-no-text"


def test_brand_is_passed_into_the_search_filters():
    captured = {}
    def s(q, f, t, k):
        captured.update(f)
        return []
    gate.evaluate("x", "youruser", "brand-a", search_fn=s, judge_fn=_JUDGE_YES)
    assert captured.get("user_id") == "youruser"
    assert captured.get("brand") == "brand-a"


def test_brandless_search_has_no_brand_filter():
    captured = {"set": False}
    def s(q, f, t, k):
        captured["filters"] = dict(f)
        return []
    gate.evaluate("x", "youruser", None, search_fn=s, judge_fn=_JUDGE_YES)
    assert "brand" not in captured["filters"]


def test_judge_receives_canonical_as_a_and_new_as_b():
    seen = {}
    def j(a, b, t):
        seen["a"] = a
        seen["b"] = b
        return {"ok": True, "contradicts": False}
    gate.evaluate("NEW fact text", "youruser", None,
                  search_fn=_search_hit(_neighbor(text="CANONICAL fact text")), judge_fn=j)
    assert seen["a"] == "CANONICAL fact text"
    assert seen["b"] == "NEW fact text"


# --- stamp_contradictions (the background-pass connective tissue used by app._nli_gate_stamp) ---

def test_stamp_contradictions_stamps_only_flagged_records():
    stamped_calls = []
    recs = [
        {"id": "m1", "memory": "the port is 9000"},   # contradicts -> flag -> stamp
        {"id": "m2", "memory": "unrelated trivia"},   # no neighbor -> admit -> no stamp
    ]
    def search_fn(q, f, t, k):
        return [_neighbor()] if "9000" in q else []
    out = gate.stamp_contradictions(recs, "youruser", None,
                                    search_fn=search_fn, judge_fn=_JUDGE_YES,
                                    stamp_fn=lambda mid, cid: stamped_calls.append((mid, cid)))
    assert stamped_calls == [("m1", "can-1")]
    assert out == [{"memory_id": "m1", "canonical_id": "can-1"}]


def test_stamp_contradictions_skips_records_without_id_or_text():
    calls = []
    recs = [{"id": None, "memory": "x"}, {"id": "m3", "memory": ""}, {"memory": "y"}]
    gate.stamp_contradictions(recs, "youruser", None,
                              search_fn=_search_hit(_neighbor()), judge_fn=_JUDGE_YES,
                              stamp_fn=lambda mid, cid: calls.append(mid))
    assert calls == []


def test_stamp_contradictions_fail_soft_when_stamp_raises():
    # A stamp_fn error on one record must not abort the others, and must not raise.
    seen = []
    def stamp_fn(mid, cid):
        seen.append(mid)
        if mid == "m1":
            raise RuntimeError("qdrant down")
    recs = [{"id": "m1", "memory": "the port is 9000"}, {"id": "m2", "memory": "the port is 9001"}]
    out = gate.stamp_contradictions(recs, "youruser", None,
                                    search_fn=_search_hit(_neighbor()), judge_fn=_JUDGE_YES,
                                    stamp_fn=stamp_fn)
    assert seen == ["m1", "m2"]          # both attempted
    assert out == [{"memory_id": "m2", "canonical_id": "can-1"}]  # only the successful one recorded


def test_stamp_contradictions_no_stamp_when_judge_says_no():
    calls = []
    gate.stamp_contradictions([{"id": "m1", "memory": "x"}], "youruser", None,
                              search_fn=_search_hit(_neighbor()), judge_fn=_JUDGE_NO,
                              stamp_fn=lambda mid, cid: calls.append(mid))
    assert calls == []
