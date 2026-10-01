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
    assert ss.precheck(L, W, _rec(brand=None), _rec(brand="brand-a")) is None
    assert ss.precheck(L, W, _rec(brand="shared"), _rec(brand="brand-a"),
                       shared_brands=("shared",)) is None


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

def test_full_payload_stamps_three_keys_and_never_trusts_the_source_for_identity():
    p = ss.full_payload(W, NOW, source="memory_supersede")
    assert p == {"superseded_by": W, "superseded_at": NOW, "superseded_via": "memory_supersede"}
    assert ss.full_payload(W, NOW)["superseded_via"] == ss.ENDPOINT_ACTOR
    assert ss.full_payload(W, NOW, source="a b/../c\n")["superseded_via"] == "a-b-..-c"


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
