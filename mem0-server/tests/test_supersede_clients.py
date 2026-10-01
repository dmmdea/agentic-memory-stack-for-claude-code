"""The supersede door's clients (1.32.4): the MCP shim tools, the outbox replay, the readers that must
not feed superseded facts into MEMORY.md or the dream, and the docs that must agree with the code.

POST /v1/memories/{id}/supersede is the only writer of superseded_by (mem0-server/supersession.py).
Everything here is headless: the shim and replay-ops modules load against an isolated home with a
throwaway key file, and every HTTP call is a recording fake, so nothing reaches a live service.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SCRIPTS = REPO_ROOT / "scripts" / "wsl"
sys.path.insert(0, str(HERE.parent))

from _home_isolation import apply_home  # noqa: E402

LOSER = "11111111-1111-4111-8111-111111111111"
WINNER = "22222222-2222-4222-8222-222222222222"


def _load(name: str, path: Path, monkeypatch, tmp_path: Path):
    """Import a hyphenated script under an isolated home that holds a throwaway api key."""
    monkeypatch.setenv("MEM0_URL", "http://authority.invalid:18791")
    apply_home(monkeypatch, tmp_path)
    (tmp_path / ".mem0").mkdir(exist_ok=True)
    (tmp_path / ".mem0" / "api-key").write_text("test-key", encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"{path.name} is not importable here: {e}")
    return mod


@pytest.fixture()
def shim(monkeypatch, tmp_path):
    return _load("shim_supersede_ut", SCRIPTS / "mem0-mcp-shim.py", monkeypatch, tmp_path)


@pytest.fixture()
def ro(monkeypatch, tmp_path):
    return _load("replay_ops_supersede_ut", SCRIPTS / "replay-ops.py", monkeypatch, tmp_path)


def _tool(shim, name):
    t = getattr(shim, name)
    return getattr(t, "fn", t)       # fastmcp 3.x wraps @mcp.tool functions in a FunctionTool


def _answer(monkeypatch, shim, status=200, body=None, raises=None):
    """Replace httpx.request with a recorder that answers `status`/`body` (or raises `raises`)."""
    seen = []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        seen.append({"method": method, "url": url, "json": json, "params": params})
        if raises:
            raise raises
        return httpx.Response(status, json={"ok": True} if body is None else body,
                              request=httpx.Request(method, url))
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    return seen


def _outbox(shim):
    if not shim.OUTBOX.exists():
        return []
    return [json.loads(ln) for ln in shim.OUTBOX.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---- memory_supersede -----------------------------------------------------------------------------

def test_memory_supersede_posts_the_endpoint_body(shim, monkeypatch):
    seen = _answer(monkeypatch, shim, body={"ok": True, "noop": False, "hidden": False})
    out = _tool(shim, "memory_supersede")(LOSER, WINNER, scope="partial",
                                          detail="the port figure only", reason="renumbered")
    assert out == {"ok": True, "noop": False, "hidden": False}
    assert seen[0]["method"] == "POST"
    assert seen[0]["url"].endswith(f"/v1/memories/{LOSER}/supersede")
    assert seen[0]["json"] == {"winner_id": WINNER, "scope": "partial",
                               "detail": "the port figure only", "reason": "renumbered",
                               "source": "memory_supersede"}


def test_memory_supersede_defaults_to_a_full_supersession(shim, monkeypatch):
    seen = _answer(monkeypatch, shim)
    _tool(shim, "memory_supersede")(LOSER, WINNER)
    assert seen[0]["json"] == {"winner_id": WINNER, "scope": "full", "detail": None,
                               "reason": None, "source": "memory_supersede"}


@pytest.mark.parametrize("failure", [
    {"raises": httpx.ConnectError("refused")},
    {"status": 503, "body": {"detail": "upstream embedder rate-limited"}},
])
def test_memory_supersede_queues_when_the_authority_is_unusable(shim, monkeypatch, failure):
    _answer(monkeypatch, shim, **failure)
    out = _tool(shim, "memory_supersede")(LOSER, WINNER, scope="partial", detail="d", reason="r")
    assert out["queued"] is True and out["op"] == "supersede" and out["event"] == "QUEUED_OFFLINE"
    (rec,) = _outbox(shim)
    assert rec["op"] == "supersede" and rec["key"] == out["key"]
    assert rec["args"] == {"memory_id": LOSER, "superseded_by": WINNER, "scope": "partial",
                           "detail": "d", "reason": "r"}


@pytest.mark.parametrize("status", [400, 403, 404, 409])
def test_a_refusal_from_the_door_propagates_and_is_never_queued(shim, monkeypatch, status):
    """The refusal matrix answers 4xx. Queueing one would replay a bad op forever; the caller has to
    read the code (loser-canonical, winner-superseded, ...) and act."""
    _answer(monkeypatch, shim, status=status, body={"detail": "loser-canonical: signed path only"})
    with pytest.raises(httpx.HTTPStatusError):
        _tool(shim, "memory_supersede")(LOSER, WINNER)
    assert _outbox(shim) == []


# ---- memory_unsupersede ---------------------------------------------------------------------------

def test_memory_unsupersede_deletes_with_scope_and_reason_as_query_params(shim, monkeypatch):
    seen = _answer(monkeypatch, shim, body={"ok": True, "noop": False})
    out = _tool(shim, "memory_unsupersede")(LOSER, scope="all", reason="wrong winner")
    assert out == {"ok": True, "noop": False}
    assert seen[0]["method"] == "DELETE"
    assert seen[0]["url"].endswith(f"/v1/memories/{LOSER}/supersede")
    assert seen[0]["params"] == {"scope": "all", "reason": "wrong winner"}
    assert seen[0]["json"] is None


def test_memory_unsupersede_omits_an_empty_reason(shim, monkeypatch):
    seen = _answer(monkeypatch, shim)
    _tool(shim, "memory_unsupersede")(LOSER)
    assert seen[0]["params"] == {"scope": "full"}


def test_memory_unsupersede_queues_offline_and_propagates_a_refusal(shim, monkeypatch):
    _answer(monkeypatch, shim, raises=httpx.ConnectError("refused"))
    out = _tool(shim, "memory_unsupersede")(LOSER, scope="partial", reason="r")
    assert out["queued"] is True and out["op"] == "unsupersede"
    (rec,) = _outbox(shim)
    assert rec["op"] == "unsupersede"
    assert rec["args"] == {"memory_id": LOSER, "scope": "partial", "reason": "r"}
    _answer(monkeypatch, shim, status=403, body={"detail": "loser-canonical"})
    with pytest.raises(httpx.HTTPStatusError):
        _tool(shim, "memory_unsupersede")(LOSER)
    assert len(_outbox(shim)) == 1, "a refusal queues nothing"


# ---- memory_update: text only, and the server's marker note reaches the caller --------------------

def test_memory_update_passes_the_servers_supersede_note_through_unchanged(shim, monkeypatch):
    answer = {"ok": True, "supersede_note": "This text carries a 'SUPERSEDED ... by mem0 <id>' marker",
              "supersede_marker": {"kind": "full", "winner_id": WINNER}}
    seen = _answer(monkeypatch, shim, body=answer)
    out = _tool(shim, "memory_update")(LOSER, f"SUPERSEDED 2026-09-30 by mem0 {WINNER}: old")
    assert out == answer
    assert seen[0]["method"] == "PUT" and seen[0]["json"] == {
        "text": f"SUPERSEDED 2026-09-30 by mem0 {WINNER}: old"}, "memory_update sends the text, nothing else"


# ---- search / recall: a partial supersession never hides, but the caller is told ------------------

_PARTIAL = [{"winner_id": WINNER, "detail": "the old port number", "at": "2026-09-30T00:00:00+00:00"}]


def test_memory_search_notes_results_that_carry_a_partial_supersession(shim, monkeypatch):
    _answer(monkeypatch, shim, body={"results": [
        {"memory": "a", "metadata": {"partially_superseded_by": _PARTIAL}},
        {"memory": "b", "partially_superseded_by": _PARTIAL},          # top-level, as recall reads created_at
        {"memory": "c", "metadata": {}},
    ], "rejected_superseded": 2, "rejected_contradicted": 0})
    out = _tool(shim, "memory_search")("ports")
    assert "2 result" in out["partial_supersession_note"]
    assert "partially_superseded_by" in out["partial_supersession_note"]
    assert "withheld_note" in out and "2 superseded" in out["withheld_note"], "the two notes sit side by side"


def test_memory_search_has_no_partial_note_without_a_partial_supersession(shim, monkeypatch):
    _answer(monkeypatch, shim, body={"results": [{"memory": "a", "metadata": {"tier": "stable"}},
                                                 {"memory": "b", "metadata": {"partially_superseded_by": []}}]})
    assert "partial_supersession_note" not in _tool(shim, "memory_search")("anything")
    _answer(monkeypatch, shim, body={})
    assert "partial_supersession_note" not in _tool(shim, "memory_search")("anything")


def test_memory_recall_notes_bundle_memories_that_carry_a_partial_supersession(shim, monkeypatch):
    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        if url.endswith("/bundle"):
            body = {"memories": [{"memory": "a", "metadata": {"partially_superseded_by": _PARTIAL}},
                                 {"memory": "b", "metadata": {}}],
                    "goals": [], "open_questions": [], "rejected_superseded": 1}
        else:
            body = {"results": []}
        return httpx.Response(200, json=body, request=httpx.Request(method, url))
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    out = _tool(shim, "memory_recall")("ports")
    assert "1 result" in out["partial_supersession_note"]
    assert "withheld_note" in out
    monkeypatch.setattr(shim.httpx, "request", lambda method, url, **kw: httpx.Response(
        200, json={"memories": [{"memory": "b", "metadata": {}}], "goals": [], "open_questions": [],
                   "results": []}, request=httpx.Request(method, url)))
    assert "partial_supersession_note" not in _tool(shim, "memory_recall")("ports")


# ---- the docstrings are the one instruction channel that ships with the deploy --------------------

def _doc(shim, name):
    return " ".join((_tool(shim, name).__doc__ or "").split())


def test_memory_search_lists_the_history_query_class(shim):
    assert "'history'" in _doc(shim, "memory_search")


def test_memory_update_says_text_only_and_never_to_append_a_marker(shim):
    doc = _doc(shim, "memory_update")
    assert "memory_supersede" in doc and "SUPERSEDED" in doc and "text" in doc.lower()


def test_memory_supersede_docstring_teaches_the_full_and_partial_cases_the_refusals_and_the_undo(shim):
    doc = _doc(shim, "memory_supersede")
    for needle in ("scope='full'", "scope='partial'", "detail", "canonical", "insight",
                   "memory_unsupersede", "history"):
        assert needle in doc, f"memory_supersede's docstring must mention {needle!r}"
    undo = _doc(shim, "memory_unsupersede")
    assert "history" in undo and "full" in undo and "partial" in undo


# ---- replay-ops: the outbox replays both ops ------------------------------------------------------

class _Replay:
    """Records the httpx calls replay-ops makes and answers each with `status`."""

    def __init__(self, ro, monkeypatch, status=200):
        self.calls = []
        self.status = status

        def respond(method, url, **kw):
            self.calls.append({"method": method, "url": url, **kw})
            return httpx.Response(self.status, json={"ok": True}, request=httpx.Request(method, url))
        monkeypatch.setattr(ro.httpx, "post", lambda url, **kw: respond("POST", url, **kw))
        monkeypatch.setattr(ro.httpx, "delete", lambda url, **kw: respond("DELETE", url, **kw))
        monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)


def test_replay_dispatches_a_supersede_to_the_endpoint(ro, monkeypatch):
    rec = _Replay(ro, monkeypatch)
    ro.AUTHORITY = "http://authority.invalid:18791"
    ro.dispatch("supersede", {"memory_id": LOSER, "superseded_by": WINNER, "scope": "partial",
                              "detail": "the old figure", "reason": "r"})
    (call,) = rec.calls
    assert call["method"] == "POST"
    assert call["url"] == f"http://authority.invalid:18791/v1/memories/{LOSER}/supersede"
    assert call["json"] == {"winner_id": WINNER, "scope": "partial", "detail": "the old figure",
                            "reason": "r", "source": "memory_supersede"}


def test_replay_dispatches_an_unsupersede_with_query_params(ro, monkeypatch):
    rec = _Replay(ro, monkeypatch)
    ro.AUTHORITY = "http://authority.invalid:18791"
    ro.dispatch("unsupersede", {"memory_id": LOSER, "scope": "all", "reason": "wrong winner"})
    (call,) = rec.calls
    assert call["method"] == "DELETE"
    assert call["url"] == f"http://authority.invalid:18791/v1/memories/{LOSER}/supersede"
    assert call["params"] == {"scope": "all", "reason": "wrong winner"}


def _queue(tmp_path, *ops, prefix="k"):
    """An outbox holding `ops`; the replayed-key ledger dedups by key, so two queues in one test
    need different prefixes."""
    ob = tmp_path / "outbox.jsonl"
    ob.write_text("\n".join(json.dumps({"op": op, "args": args, "key": f"{prefix}{i}"})
                            for i, (op, args) in enumerate(ops)) + "\n", encoding="utf-8")
    return ob


_SUP = ("supersede", {"memory_id": LOSER, "superseded_by": WINNER, "scope": "full",
                      "detail": None, "reason": None})


def test_replay_a_successful_supersede_is_replayed_and_a_permanent_4xx_goes_to_conflicts(
        ro, monkeypatch, tmp_path):
    rec = _Replay(ro, monkeypatch, status=200)
    stats = ro.replay(_queue(tmp_path, _SUP), "http://authority.invalid", "k")
    assert stats["replayed"] == 1 and stats["conflicts"] == 0 and len(rec.calls) == 1

    rec = _Replay(ro, monkeypatch, status=409)
    stats = ro.replay(_queue(tmp_path, _SUP, ("unsupersede", {"memory_id": LOSER}), prefix="b"),
                      "http://authority.invalid", "k")
    assert stats["conflicts"] == 2 and stats["kept"] == 0 and stats["replayed"] == 0
    confs = [json.loads(ln) for ln in (tmp_path / "mutation-conflicts.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [c["op"] for c in confs] == ["supersede", "unsupersede"] and all(c["status"] == 409 for c in confs)
    assert not (tmp_path / "outbox.replaying.jsonl").exists()


def test_replay_keeps_a_supersede_that_met_a_503_and_everything_behind_it(ro, monkeypatch, tmp_path):
    rec = _Replay(ro, monkeypatch, status=503)
    ob = _queue(tmp_path, _SUP, ("unsupersede", {"memory_id": LOSER}))
    stats = ro.replay(ob, "http://authority.invalid", "k")
    assert stats["replayed"] == 0 and stats["conflicts"] == 0 and stats["kept"] == 2
    assert stats["stopped_retryable"]["op"] == "supersede"
    kept = [json.loads(ln)["op"] for ln in (tmp_path / "outbox.replaying.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert kept == ["supersede", "unsupersede"], "503 is retryable: nothing is dropped, order is kept"
    assert not (tmp_path / "mutation-conflicts.jsonl").exists()
    assert len(rec.calls) == 1, "the drain stops at the first 503"


def test_replay_a_supersede_missing_its_ids_is_a_deterministic_conflict(ro, monkeypatch, tmp_path):
    _Replay(ro, monkeypatch)
    stats = ro.replay(_queue(tmp_path, ("supersede", {"memory_id": LOSER})), "http://authority.invalid", "k")
    assert stats["conflicts"] == 1 and stats["kept"] == 0


# ---- readers: a superseded fact must not feed MEMORY.md or the nightly dream ------------------------
#
# The admission gate hides superseded_by on a search; these two jobs read the store directly, so they
# apply the same rule themselves. A PARTIAL supersession (partially_superseded_by) never hides: the
# record still stands apart from the one claim it names. The audits (l10, brand-backfill,
# brand-scope-audit) deliberately keep seeing everything.

def test_memory_index_build_leaves_superseded_records_out_and_keeps_partial_ones(monkeypatch, tmp_path):
    m = _load("memory_index_build_ut", SCRIPTS / "memory-index-build.py", monkeypatch, tmp_path)
    live = {"data": "the live stable fact", "tier": "stable", "source": "src"}
    points = [
        {"id": "a" * 8 + "-live", "payload": live},
        {"id": "b" * 8 + "-gone", "payload": {**live, "data": "the superseded stable fact",
                                              "superseded_by": WINNER}},
        {"id": "c" * 8 + "-part", "payload": {**live, "data": "the partially superseded fact",
                                              "partially_superseded_by": _PARTIAL}},
        {"id": "d" * 8 + "-temp", "payload": {**live, "data": "the superseded temporal fact",
                                              "tier": "temporal", "superseded_by": WINNER}},
    ]
    out = tmp_path / "MEMORY.md"
    monkeypatch.setattr(m, "scroll_all", lambda: points)
    monkeypatch.setattr(m, "OUT", out)
    m.main()
    text = out.read_text(encoding="utf-8")
    assert "the live stable fact" in text
    assert "the partially superseded fact" in text, "a partial supersession never hides a record"
    assert "the superseded stable fact" not in text and "the superseded temporal fact" not in text


def test_dream_gather_leaves_superseded_records_out_and_keeps_partial_ones(monkeypatch, tmp_path):
    m = _load("dream_consolidate_ut", SCRIPTS / "dream-consolidate.py", monkeypatch, tmp_path)

    def answer(request):
        body = {"result": {"points": [
            {"id": "live", "payload": {"data": "live", "user_id": "u", "created_at": "2026-09-11T06:00:00Z",
                                       "tier": "evidence"}},
            {"id": "gone", "payload": {"data": "stale", "user_id": "u", "created_at": "2026-09-11T05:00:00Z",
                                       "tier": "evidence", "superseded_by": WINNER}},
            {"id": "part", "payload": {"data": "partly stale", "user_id": "u",
                                       "created_at": "2026-09-11T04:00:00Z", "tier": "evidence",
                                       "partially_superseded_by": _PARTIAL}},
        ], "next_page_offset": None}}
        return httpx.Response(200, json=body)
    client = m.Mem0Client("http://x", "k", "u", http=httpx.Client(transport=httpx.MockTransport(answer)))
    assert [p["id"] for p in client.all_points()] == ["live", "part"]
