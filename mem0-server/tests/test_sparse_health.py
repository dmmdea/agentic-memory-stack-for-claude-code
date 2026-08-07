"""Headless pins for sparse_health (AMS-09 — audit 2026-08-07).

The pure evaluator and the deterministic canary-token rule are pinned here;
the I/O wrapper is exercised live by Test-MemoryStack and the deploy gate.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparse_health import evaluate_sparse_leg, pick_canary_token  # noqa: E402


CANARY_OK = {"ran": True, "hit": True, "token": "qdrant"}
CANARY_MISS = {"ran": True, "hit": False, "token": "qdrant"}
CANARY_NOT_RUN = {"ran": False, "hit": False, "token": None}


def test_all_green():
    out = evaluate_sparse_leg(True, True, 100, 100, CANARY_OK)
    assert out["ok"] is True and out["coverage"] == 1.0


def test_fastembed_missing_gates():
    # The exact AMS-09 production state: venv rebuilt without fastembed.
    out = evaluate_sparse_leg(False, True, 100, 25, CANARY_NOT_RUN)
    assert out["ok"] is False and out["fastembed"] is False


def test_slot_missing_gates():
    out = evaluate_sparse_leg(True, False, 100, 0, CANARY_NOT_RUN)
    assert out["ok"] is False


def test_canary_miss_gates():
    # Encoder loads but retrieval does not return the probed point — the
    # tokenizer/hash-drift case a coverage number can never catch.
    out = evaluate_sparse_leg(True, True, 100, 100, CANARY_MISS)
    assert out["ok"] is False


def test_no_canary_point_gates():
    # Empty bm25 population: nothing to probe IS a dead leg, not an unknown.
    out = evaluate_sparse_leg(True, True, 100, 0, CANARY_NOT_RUN,
                              error="no bm25-bearing point to canary")
    assert out["ok"] is False and "error" in out


def test_low_coverage_reports_but_does_not_gate_here():
    # Coverage gating is Test-MemoryStack's WARN (<0.95); a half-backfilled
    # corpus with a live encoder is degraded, not dead.
    out = evaluate_sparse_leg(True, True, 1000, 250, CANARY_OK)
    assert out["ok"] is True and out["coverage"] == 0.25


def test_zero_points_coverage_zero_no_crash():
    out = evaluate_sparse_leg(True, True, 0, 0, CANARY_NOT_RUN)
    assert out["coverage"] == 0.0 and out["ok"] is False


def test_canary_token_rule_is_deterministic():
    # Longest token wins; lexicographic tiebreak; stable across calls.
    assert pick_canary_token("alpha beta gamma-long zzz") == "gamma-long"
    assert pick_canary_token("bbb aaa ccc") == "aaa"  # equal length -> lexical
    assert pick_canary_token("") is None
    assert pick_canary_token(None) is None
    assert pick_canary_token("  one  ") == "one"
