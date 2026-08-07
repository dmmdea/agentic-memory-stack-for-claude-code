"""Headless pins for payload_carryover (AMS-01, P0 — audit 2026-08-07).

These tests are CI-gating (listed in .github/workflows/ci.yml). They pin the
carry-over contract the PUT handler relies on: everything custom survives,
everything mem0-owned is dropped, and the NLI check-markers never survive a
text change while the standing contradicts_canonical verdict does.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from payload_carryover import (  # noqa: E402
    MEM0_OWNED_KEYS, NLI_CHECK_MARKERS, DROPPED_KEYS, compute_carryover,
)

# The exact 8-key fingerprint the damaged records carry, plus the custom keys
# the audit proved destroyed (source/brand/workspace/project/lifecycle keys).
FULL_PAYLOAD = {
    # mem0-owned (must all drop)
    "data": "the fact text",
    "hash": "d41d8cd98f00b204e9800998ecf8427e",
    "text_lemmatized": "fact text",
    "created_at": "2026-07-01T00:00:00+00:00",
    "updated_at": "2026-07-02T00:00:00+00:00",
    "user_id": "u1",
    "agent_id": "a1",
    "run_id": "r1",
    "actor_id": "act1",
    "role": "user",
    # custom (must all survive)
    "tier": "evidence",
    "source": "l1a-extractor",
    "brand": "brand-x",
    "workspace": "ws",
    "project": "proj",
    "event": "Stop",
    "extracted_at": "2026-07-01T00:00:01+00:00",
    "retrievable": False,
    "retired_at": "2026-07-03T00:00:00+00:00",
    "superseded_by": "some-other-id",
    "replaces": "older-id",
    "confidence": 0.9,
    "tier_actor": "user-direct",
    # NLI: verdict survives, check-markers do not
    "contradicts_canonical": "canon-id",
    "nli_gate_checked_at": "2026-07-02T00:00:00+00:00",
    "contradiction_checked_at": "2026-07-02T00:00:00+00:00",
    "contradicts_canonical_pending": True,
}


def test_custom_keys_survive_and_owned_keys_drop():
    out = compute_carryover(FULL_PAYLOAD)
    for k in MEM0_OWNED_KEYS:
        assert k not in out, f"mem0-owned key {k!r} must not be re-supplied"
    for k in ("tier", "source", "brand", "workspace", "project", "event",
              "extracted_at", "retrievable", "retired_at", "superseded_by",
              "replaces", "confidence", "tier_actor"):
        assert out[k] == FULL_PAYLOAD[k], f"custom key {k!r} must survive"


def test_nli_check_markers_drop_but_verdict_survives():
    out = compute_carryover(FULL_PAYLOAD)
    for k in NLI_CHECK_MARKERS:
        assert k not in out, (
            f"{k!r} asserts 'this text was judged' — it must not survive a "
            "text change or the contradiction sweep skips the new text forever"
        )
    assert out["contradicts_canonical"] == "canon-id", (
        "the standing verdict is fail-closed: it holds until a re-judge clears it"
    )


def test_empty_and_none_payloads():
    assert compute_carryover({}) == {}
    assert compute_carryover(None) == {}


def test_unknown_keys_are_blind_preserved():
    # Allowlisting would re-create the defect the moment a new key ships.
    out = compute_carryover({"data": "x", "some_future_key": 1, "junk": "y"})
    assert out == {"some_future_key": 1, "junk": "y"}


def test_owned_and_marker_sets_are_exactly_the_contract():
    # Pin the LITERAL sets. A test that iterates over the imported set is
    # vacuous against a key being removed from it (the loop simply shrinks) —
    # the same mutation-blindness class the W1 ledger records three times.
    assert MEM0_OWNED_KEYS == {
        "data", "hash", "text_lemmatized", "created_at", "updated_at",
        "user_id", "agent_id", "run_id", "actor_id", "role",
    }
    assert NLI_CHECK_MARKERS == {
        "nli_gate_checked_at", "contradiction_checked_at",
        "contradicts_canonical_pending",
    }


def test_dropped_is_union_and_disjoint_sanity():
    assert DROPPED_KEYS == MEM0_OWNED_KEYS | NLI_CHECK_MARKERS
    assert "contradicts_canonical" not in DROPPED_KEYS


def test_carryover_does_not_alias_input():
    src = dict(FULL_PAYLOAD)
    out = compute_carryover(src)
    out["tier"] = "changed"
    assert src["tier"] == "evidence"
