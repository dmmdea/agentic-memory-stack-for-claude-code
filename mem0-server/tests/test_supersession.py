"""supersession.py: the refusal matrix, the payloads, and the hand-written marker parser."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import supersession as ss  # noqa: E402

L = "0220d8f1-96a0-44b5-8f18-7115953227d0"
W = "cbf2f1e0-f8fa-4c7e-8eb0-49f7c84f07e7"
X = "25feded5-d916-48e1-9daf-70f2ef8956dd"
NOW = "2026-10-01T08:00:00+00:00"


def _rec(**kw):
    base = {"tier": "evidence", "user_id": "u1", "data": "a fact"}
    base.update(kw)
    return base


def _code(refusal):
    return refusal.code if refusal else None


# ---- precheck -----------------------------------------------------------------------------------

def test_a_plain_full_supersession_passes():
    assert ss.precheck(L, W, _rec(), _rec()) is None


@pytest.mark.parametrize("tier", ["evidence", "stable", "temporal"])
def test_ordinary_tiers_may_be_superseded(tier):
    assert ss.precheck(L, W, _rec(tier=tier), _rec(tier="canonical")) is None


@pytest.mark.parametrize("loser,winner,scope,detail,status,code", [
    (_rec(), _rec(), "sideways", None, 400, "bad-scope"),
    (_rec(), _rec(), "partial", None, 400, "detail-required"),
    (_rec(), _rec(), "partial", "   ", 400, "detail-required"),
    (_rec(), _rec(), "partial", "x" * 301, 400, "detail-too-long"),
    (None, _rec(), "full", None, 404, "loser-not-found"),
    (_rec(), None, "full", None, 404, "winner-not-found"),
    (_rec(tier="canonical"), _rec(), "full", None, 403, "loser-canonical"),
    (_rec(tier=None), _rec(), "full", None, 403, "loser-canonical"),
    (_rec(tier="insight"), _rec(), "full", None, 403, "loser-insight"),
    (_rec(tier="insight"), _rec(), "partial", "the figure", 403, "loser-insight"),
    (_rec(retrievable=False), _rec(), "full", None, 409, "loser-retired"),
    (_rec(), _rec(retrievable=False), "full", None, 409, "winner-retired"),
    (_rec(), _rec(superseded_by=X), "full", None, 409, "winner-superseded"),
    (_rec(user_id="u1"), _rec(user_id="u2"), "full", None, 403, "cross-tenant"),
    (_rec(brand="brand-a"), _rec(brand="brand-b"), "full", None, 403, "cross-brand"),
    (_rec(superseded_by=X), _rec(), "full", None, 409, "already-superseded"),
    (_rec(superseded_by=W), _rec(), "partial", "the figure", 409, "already-superseded"),
])
def test_refusal_matrix(loser, winner, scope, detail, status, code):
    r = ss.precheck(L, W, loser, winner, scope=scope, detail=detail)
    assert r is not None and (r.status, r.code) == (status, code), r


def test_ids_are_validated_and_self_supersession_refused():
    assert _code(ss.precheck("not-an-id", W, _rec(), _rec())) == "bad-id"
    assert _code(ss.precheck(L, "../../etc", _rec(), _rec())) == "bad-id"
    assert _code(ss.precheck(L, L.upper(), _rec(), _rec())) == "self"


def test_brands_compare_case_insensitively_and_null_or_shared_brands_are_neutral():
    assert ss.precheck(L, W, _rec(brand="Brand-A"), _rec(brand="brand-a")) is None
    assert ss.precheck(L, W, _rec(brand="brand-a"), _rec(brand=None)) is None       # branded -> neutral
    assert ss.precheck(L, W, _rec(brand="brand-a"), _rec(brand="shared"),
                       shared_brands=("shared",)) is None
    assert ss.precheck(L, W, _rec(brand=None), _rec(brand="shared"), shared_brands=("shared",)) is None


@pytest.mark.parametrize("loser_brand", [None, "", "shared"])
def test_a_neutral_record_is_never_hidden_behind_a_branded_one(loser_brand):
    """The neutral fact is visible in every scope, the branded winner only in its own: hiding the
    neutral one would erase the fact for every other brand."""
    r = ss.precheck(L, W, _rec(brand=loser_brand), _rec(brand="brand-a"), shared_brands=("shared",))
    assert r is not None and (r.status, r.code) == (403, "cross-brand")


def test_the_same_winner_again_is_a_noop_not_a_refusal():
    loser = _rec(superseded_by=W)
    assert ss.precheck(L, W, loser, _rec()) is None
    assert ss.is_noop(loser, W, "full") is True
    assert ss.is_noop(_rec(), W, "full") is False


def test_partial_noop_needs_the_same_winner_and_detail():
    loser = _rec(partially_superseded_by=[{"winner_id": W, "detail": "the figure", "at": NOW}])
    assert ss.is_noop(loser, W, "partial", " the figure ") is True
    assert ss.is_noop(loser, W, "partial", "another claim") is False
    assert ss.is_noop(loser, X, "partial", "the figure") is False


# ---- payloads -----------------------------------------------------------------------------------

def test_full_payload_stamps_three_server_values_and_a_lowercase_winner():
    """superseded_via is the server's own stamp: a caller label could forge provenance, so it only
    reaches the ledger line, as a caller-declared `source`."""
    assert ss.full_payload(W.upper(), NOW) == {"superseded_by": W, "superseded_at": NOW,
                                               "superseded_via": ss.ENDPOINT_ACTOR}
    assert ss.clean_source("a b/../c\n") == "a-b-..-c"
    assert ss.clean_source("x" * 200) == "x" * ss.SOURCE_MAX_CHARS


def test_detail_and_reason_are_capped_for_every_scope():
    assert ss.precheck(L, W, _rec(), _rec(), scope="full", detail="d" * 301).code == "detail-too-long"
    assert ss.precheck(L, W, _rec(), _rec(), reason="r" * 501).code == "reason-too-long"
    assert ss.precheck(L, W, _rec(), _rec(), reason="r" * 500) is None
    assert ss.clear_precheck(L, _rec(superseded_by=W), "full", reason="r" * 501).code == "reason-too-long"


def test_a_winner_with_retired_at_counts_as_retired():
    assert ss.precheck(L, W, _rec(), _rec(retired_at="2026-09-01T00:00:00Z")).code == "winner-retired"


def test_partial_payload_appends_and_is_bounded():
    loser = _rec(partially_superseded_by=[{"winner_id": X, "detail": f"d{i}", "at": NOW}
                                          for i in range(ss.PARTIAL_MAX_ENTRIES)])
    entries = ss.partial_payload(loser, W, " the figure ", NOW)["partially_superseded_by"]
    assert len(entries) == ss.PARTIAL_MAX_ENTRIES
    assert entries[-1] == {"winner_id": W, "detail": "the figure", "at": NOW}
    assert entries[0]["detail"] == "d1"
    assert ss.partial_payload(_rec(partially_superseded_by="junk"), W, "x", NOW) == {
        "partially_superseded_by": [{"winner_id": W, "detail": "x", "at": NOW}]}


def test_partial_keys_are_not_a_hide_key():
    """The admission gate reads only superseded_by / contradicts_canonical: partial never hides."""
    import admission_gate
    policy = admission_gate.AdmissionPolicy(allowed_tiers=("evidence",), max_age_days=None)
    meta = {"tier": "evidence", **ss.partial_payload(_rec(), W, "the figure", NOW)}
    assert policy.evaluate({"id": L, "memory": "x", "metadata": meta}, {}, "durable").admit is True
    meta = {"tier": "evidence", **ss.full_payload(W, NOW)}
    assert policy.evaluate({"id": L, "memory": "x", "metadata": meta}, {}, "durable").admit is False


def test_clear_keys_and_precheck():
    assert ss.clear_keys("full") == ("superseded_by", "superseded_at", "superseded_via")
    assert ss.clear_keys("partial") == ("partially_superseded_by",)
    assert set(ss.clear_keys("all")) == ss.SUPERSEDE_KEYS
    assert _code(ss.clear_precheck(L, _rec(tier="canonical"), "full")) == "loser-canonical"
    assert _code(ss.clear_precheck(L, None, "full")) == "loser-not-found"
    assert _code(ss.clear_precheck(L, _rec(), "both")) == "bad-scope"
    assert ss.clear_precheck(L, _rec(superseded_by=W), "full") is None
    assert ss.has_supersession(_rec(superseded_by=W), "full") is True
    assert ss.has_supersession(_rec(), "all") is False


# ---- markers ------------------------------------------------------------------------------------

def test_full_marker_on_its_own_line():
    text = ("The fleet node runs in a hidden console.\n\nSUPERSEDED 2026-09-30 by mem0 "
            f"{W}: the node is launched detached since 2026-09-23.")
    m = ss.classify_text(text)
    assert (m.kind, m.winner_id) == ("full", W)


def test_partial_marker_with_a_parenthetical_never_hides():
    text = (f"Handoff text. SUPERSEDED 2026-09-30 by mem0 {X} (the 'Current main = abc, VERSION "
            "1.20.4' figure only): the repo released 1.32.3.")
    m = ss.classify_text(text)
    assert (m.kind, m.winner_id) == ("partial", X)


@pytest.mark.parametrize("text", [
    f"PARTIALLY SUPERSEDED by mem0 {W}: the port changed.",
    f"SUPERSEDED by mem0 {W} for the VERSION figure: now 1.32.3.",
    f"SUPERSEDED 2026-09-30 by mem0 {W} except the date: it moved.",
    f"SUPERSEDED by {W} in some way: see it.",
])
def test_qualified_or_ambiguous_markers_are_partial(text):
    assert ss.classify_text(text).kind == "partial"


def test_a_mid_sentence_mention_is_not_a_marker():
    text = f"Note that the plan was SUPERSEDED by mem0 {W} in August, then reinstated."
    assert ss.classify_text(text).kind == "mention"


def test_marker_without_an_id_is_no_target_and_plain_text_is_none():
    assert ss.classify_text("SUPERSEDED 2026-09-30: see the newer note").kind == "no-target"
    assert ss.classify_text("superseded by a later design") is None
    assert ss.classify_text("") is None and ss.classify_text(None) is None


def test_any_partial_marker_wins_over_a_full_one():
    text = (f"A.\nSUPERSEDED by mem0 {W}: whole thing.\n"
            f"SUPERSEDED by mem0 {X} (the second figure only): b.")
    assert ss.classify_text(text).kind == "partial"


def test_markers_accept_bullets_and_lowercase_ids():
    m = ss.classify_text(f"Fact.\n- **SUPERSEDED** 2026-09-30 by mem0 {W.upper()}: gone.")
    assert (m.kind, m.winner_id) == ("full", W)


def test_live_record_shapes():
    """The three full-shape records the 2026-10-01 audit saw and the partial handoff."""
    full = ("Only `blackwell-2x16` and `blackwell-3x16` declare vLLM seats, and both are one "
            "box's tiers.\n\nSUPERSEDED 2026-09-30 by mem0 cbf2f1e0-f8fa-4c7e-8eb0-49f7c84f07e7: four "
            "tiers declare a vllm_seat, not only the two.")
    assert ss.classify_text(full).kind == "full"
    partial = ("HISTORICAL HANDOFF captured 2026-08-29; created_at is a re-ingest stamp (class note "
               "mem0 111ade13-a8c1-43f4-bdc2-32a9c608962b). SUPERSEDED 2026-09-30 by mem0 "
               "25feded5-d916-48e1-9daf-70f2ef8956dd (the 'Current main = 6552b5e, VERSION 1.20.4' "
               "figure only): the repo released v1.32.1 to v1.32.3 on 2026-09-30.")
    assert ss.classify_text(partial).kind == "partial"


# ---- full is the narrow case (security review, 1.32.4) ------------------------------------------

@pytest.mark.parametrize("text", [
    f"Fact.\nSUPERSEDED (price figure only) by mem0 {W}: new price",
    f"Fact.\nSUPERSEDED in part by mem0 {W}",
    f"Fact.\nSUPERSEDED partially by mem0 {W}",
    f"Fact.\nSUPERSEDED the price only by mem0 {W}",
    f"Fact.\nSUPERSEDED, in part, by mem0 {W}",
    f"Fact.\nSUPERSEDED (but still valid for X) by mem0 {W}",
    f"Fact.\nSUPERSEDED 2026-09-30 by mem0 {W}: the 'X' figure only",
    f"Fact.\nSUPERSEDED 2026-09-30 by mem0 {W}: only the port number is stale, the rest still holds",
    f"Fact.\nSUPERSEDED by mem0 {W}: REVERTED, this record is current again",
    f"Fact.\nSUPERSEDED by mem0 {W}. Port only.",
    f"Fact.\nSUPERSEDED by mem0 {W}\nThe port line above still holds",
    f"Fact.\nSUPERSEDED 2026-09-30 by mem0 {W}: port number now 5; the rest remains valid",
])
def test_ambiguous_or_scoped_markers_are_never_full(text):
    m = ss.classify_text(text)
    assert m is not None and m.kind != "full", (text, m)


@pytest.mark.parametrize("text", [
    f"Fact.\nSUPERSEDED 2026-09-30 by mem0 {W}: four tiers declare it, not only the two.",
    f"Fact.\nSUPERSEDED 2026-09-30T13:58Z by mem0 {W}: launched detached (no shared console) since 09-23.",
    f"Fact. SUPERSEDED on 2026-09-30 by mem0 {W}: replaced.",
    f"Fact.\n[SUPERSEDED 2026-09-30 by mem0 {W}: replaced]",
    f"Fact — SUPERSEDED 2026-09-30 by mem0 {W}: replaced.",
])
def test_a_plain_dated_marker_is_full(text):
    m = ss.classify_text(text)
    assert (m.kind, m.winner_id) == ("full", W), (text, m)


# ---- the write transaction ------------------------------------------------------------------------

class _Store:
    def __init__(self, records, fail_set=False):
        self.records = {k: dict(v) for k, v in records.items()}
        self.fail_set = fail_set
        self.calls = []

    def read(self, mid):
        self.calls.append(("read", mid))
        rec = self.records.get(mid)
        return dict(rec) if rec is not None else None

    def set_payload(self, mid, payload):
        self.calls.append(("set", mid))
        if self.fail_set:
            raise RuntimeError("store down")
        self.records[mid].update(payload)

    def delete_keys(self, mid, keys):
        self.calls.append(("delete_keys", mid))
        for k in keys:
            self.records[mid].pop(k, None)


class _Ledger:
    def __init__(self, fail=False):
        self.lines, self.fail = [], fail

    def __call__(self, entry):
        if self.fail:
            raise OSError("disk full")
        self.lines.append(dict(entry))


def test_run_supersede_full_writes_after_the_intent_line():
    store, ledger = _Store({L: _rec(), W: _rec()}), _Ledger()
    out = ss.run_supersede(store, ledger, mid=L, winner_id=W.upper(), scope="full",
                           reason="r", source="memory_supersede", now_iso=NOW)
    assert out["hidden"] is True and out["noop"] is False and out["winner_id"] == W
    assert store.records[L]["superseded_by"] == W
    assert store.records[L]["superseded_via"] == ss.ENDPOINT_ACTOR
    assert [line["event"] for line in ledger.lines] == ["supersede-intent"]
    assert ledger.lines[0]["source"] == "memory_supersede" and ledger.lines[0]["actor"] == ss.ENDPOINT_ACTOR
    assert out["_entry"]["event"] == "supersede"
    assert store.calls.index(("set", L)) > store.calls.index(("read", W))


def test_run_supersede_refusal_writes_nothing():
    store, ledger = _Store({L: _rec(tier="canonical"), W: _rec()}), _Ledger()
    with pytest.raises(ss.Refused) as e:
        ss.run_supersede(store, ledger, mid=L, winner_id=W, now_iso=NOW)
    assert e.value.refusal.code == "loser-canonical"
    assert ledger.lines == [] and ("set", L) not in store.calls


def test_run_supersede_without_a_ledger_writes_nothing():
    store = _Store({L: _rec(), W: _rec()})
    with pytest.raises(ss.LedgerUnavailable):
        ss.run_supersede(store, _Ledger(fail=True), mid=L, winner_id=W, now_iso=NOW)
    assert "superseded_by" not in store.records[L] and ("set", L) not in store.calls


def test_run_supersede_store_error_propagates_after_the_intent():
    store, ledger = _Store({L: _rec(), W: _rec()}, fail_set=True), _Ledger()
    with pytest.raises(RuntimeError):
        ss.run_supersede(store, ledger, mid=L, winner_id=W, now_iso=NOW)
    assert [line["event"] for line in ledger.lines] == ["supersede-intent"]


def test_run_supersede_repeat_is_a_noop_and_partial_appends():
    store, ledger = _Store({L: _rec(superseded_by=W), W: _rec()}), _Ledger()
    out = ss.run_supersede(store, ledger, mid=L, winner_id=W, now_iso=NOW)
    assert out["noop"] is True and ledger.lines == [] and ("set", L) not in store.calls
    store2 = _Store({L: _rec(), W: _rec()})
    out2 = ss.run_supersede(store2, _Ledger(), mid=L, winner_id=W, scope="partial",
                            detail="the figure", now_iso=NOW)
    assert out2["hidden"] is False
    assert store2.records[L]["partially_superseded_by"][-1]["detail"] == "the figure"
    assert "superseded_by" not in store2.records[L]


def test_run_unsupersede_clears_after_the_intent_and_noops_when_clear():
    store, ledger = _Store({L: _rec(superseded_by=W, superseded_at=NOW, superseded_via="x")}), _Ledger()
    out = ss.run_unsupersede(store, ledger, mid=L, scope="full", reason="wrong winner", now_iso=NOW)
    assert out["noop"] is False and "superseded_by" not in store.records[L]
    assert ledger.lines[0]["event"] == "unsupersede-intent"
    assert ledger.lines[0]["cleared"]["superseded_by"] == W
    again = ss.run_unsupersede(store, _Ledger(), mid=L, scope="full", now_iso=NOW)
    assert again["noop"] is True
    with pytest.raises(ss.Refused):
        ss.run_unsupersede(_Store({L: _rec(tier="canonical", superseded_by=W)}), _Ledger(),
                           mid=L, now_iso=NOW)


# ---- the rules the delete and tier paths share ---------------------------------------------------

@pytest.mark.parametrize("payload,protected", [
    (None, True), ({"tier": None}, True), ({}, True), ({"tier": "canonical"}, True),
    ({"tier": "insight"}, True), ({"tier": "evidence"}, False), ({"tier": "stable"}, False),
])
def test_cascade_never_deletes_a_protected_or_unreadable_member(payload, protected):
    assert ss.cascade_protected(payload) is protected


def test_a_superseded_record_is_never_promoted_into_a_protected_tier():
    r = ss.promotion_refusal({"tier": "evidence", "superseded_by": W}, "canonical")
    assert r is not None and (r.status, r.code) == (409, "superseded-record")
    assert ss.promotion_refusal({"tier": "evidence", "superseded_by": W}, "insight") is not None
    assert ss.promotion_refusal({"tier": "evidence", "superseded_by": W}, "stable") is None
    assert ss.promotion_refusal({"tier": "evidence"}, "canonical") is None
    assert ss.promotion_refusal({"tier": "evidence", "partially_superseded_by": [{}]}, "canonical") is None
