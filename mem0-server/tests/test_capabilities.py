"""Headless truth-table pins for capabilities.py (W3 -- the capability manifest).

evaluate() is pure (checks dict + role in, verdict out), so the review-bound
honesty rules pin headless: F9 (a zero-signal activity counter is never
'alive'), F8 (an unknown role never convicts role-scoped rows), probe-less
'none -- W4' rows always 'unknown', retired rows always 'retired'. The doc
test pins docs/capability-manifest.md's table ids against the CAPABILITIES
literal so the human mirror cannot drift from the single source.
"""
import copy
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capabilities import CAPABILITIES, evaluate  # noqa: E402


# The exact check shapes /health/deep assembles (app.py) on an all-green box.
GREEN_CHECKS = {
    "qdrant": {"ok": True, "points": 100, "status": "green"},
    "embedder": {"ok": True, "dim": 768},
    "sparse_leg": {"ok": True, "points": 100, "with_bm25": 100, "coverage": 1.0,
                   "canary": {"ran": True, "hit": True, "token": "qdrant"}},
    "canonical_key": {"ok": True, "present": True, "provider": "dpapi"},
    "put_carryover_today": {"date": "2026-08-07", "puts": 3,
                            "keys_restored": 0, "keys_lost": 0},
    "mojibake": {"ok": True, "scanned": 100, "hits": 0, "sample_ids": [],
                 "elapsed_ms": 12},
    "pending_contradiction_reviews": 0,
    "job_liveness": {
        "role": "brain",
        "last_dream_age_h": 10.0, "prune_age_h": 10.0, "gather_age_h": 10.0,
        "backup_manifest_age_h": 12.0, "dedup_report_age_h": 12.0,
        "morning_summary_age_h": 10.0, "morning_summary_sections_48h": 2,
    },
    "retrieval_drift": {
        "state_present": True, "last_compare_ts": "2026-08-07T03:00:00",
        "age_hours": 10.0, "before_retrievable": 25, "n_total": 25,
        "hwm": 25, "hwm_seeded": True, "consecutive_below_hwm": 0,
        "consecutive_snapshot_failures": 0, "alarm": False,
        "missing": [], "compat_fallback": False,
    },
}

PROBE_BACKED = [
    "mem0-api", "qdrant-store", "embedder", "bm25-sparse-leg", "canonical-key",
    "put-carryover", "mojibake-tripwire", "contradiction-review-queue",
    "dream-cycle", "drift-guard", "backup-pipeline", "dedup-job",
    "memory-index", "sweep-job", "codex-auth",
]
W4_BLIND = [
    "reranker", "l1a-extraction", "sessionstart-banner", "mcp-shim",
    "admission-gate", "tier-policy", "brand-isolation", "offline-outbox",
]


def _checks(**overrides):
    c = copy.deepcopy(GREEN_CHECKS)
    c.update(overrides)
    return c


def test_all_green_truth_table():
    out = evaluate(GREEN_CHECKS, "brain")
    assert out["role"] == "brain"
    for cid in PROBE_BACKED:
        assert out["states"][cid] == "alive", cid
    for cid in W4_BLIND:
        assert out["states"][cid] == "unknown", cid
    assert out["dead_required"] == []
    assert sorted(out["unknown"]) == sorted(W4_BLIND)


def test_sparse_leg_dead_gates_dead_required():
    checks = _checks(sparse_leg={"ok": False, "fastembed": False,
                                 "bm25_slot": True, "coverage": 0.0})
    out = evaluate(checks, "brain")
    assert out["states"]["bm25-sparse-leg"] == "dead"
    assert "bm25-sparse-leg" in out["dead_required"]


def test_sparse_leg_low_coverage_degraded_not_dead():
    checks = _checks(sparse_leg={"ok": True, "coverage": 0.5,
                                 "canary": {"ran": True, "hit": True}})
    out = evaluate(checks, "brain")
    assert out["states"]["bm25-sparse-leg"] == "degraded"
    assert "bm25-sparse-leg" not in out["dead_required"]


def test_zero_signal_put_carryover_is_unknown_not_alive():
    # The F9 red case: a quiet day (no PUTs) is silence, not health.
    checks = _checks(put_carryover_today={"date": "2026-08-07", "puts": 0,
                                          "keys_restored": 0, "keys_lost": 0})
    out = evaluate(checks, "brain")
    assert out["states"]["put-carryover"] == "unknown"
    assert "put-carryover" in out["unknown"]
    assert "put-carryover" not in out["dead_required"]


def test_keys_lost_is_dead_even_with_activity():
    checks = _checks(put_carryover_today={"date": "2026-08-07", "puts": 5,
                                          "keys_restored": 0, "keys_lost": 2})
    out = evaluate(checks, "brain")
    assert out["states"]["put-carryover"] == "dead"
    assert "put-carryover" in out["dead_required"]  # required 'both'


def test_unknown_role_never_convicts_role_scoped_rows():
    # F8: dream-cycle (brain-scoped) is DEAD, but the role is unknown, so it
    # must not enter dead_required; the 'both'-scoped dead row still does.
    jl = dict(GREEN_CHECKS["job_liveness"], last_dream_age_h=200.0)
    checks = _checks(job_liveness=jl,
                     sparse_leg={"ok": False, "coverage": 0.0})
    out = evaluate(checks, None)
    assert out["role"] is None
    assert out["states"]["dream-cycle"] == "dead"
    assert "dream-cycle" not in out["dead_required"]
    assert "bm25-sparse-leg" in out["dead_required"]


def test_replica_role_brain_rows_not_required():
    jl = dict(GREEN_CHECKS["job_liveness"], last_dream_age_h=200.0,
              backup_manifest_age_h=500.0)
    out = evaluate(_checks(job_liveness=jl), "replica")
    assert out["states"]["dream-cycle"] == "dead"
    assert out["states"]["backup-pipeline"] == "dead"
    assert out["dead_required"] == []


def test_probe_none_rows_always_unknown():
    # Even on an all-green box the W4 rows are named blindness, never alive.
    out = evaluate(GREEN_CHECKS, "brain")
    for row in CAPABILITIES:
        if (row["probe"] or "").startswith("none"):
            assert out["states"][row["id"]] == "unknown", row["id"]


def test_retired_rows_evaluate_retired():
    rows = [{"id": "old-cap", "what": "w",
             "probe": "retired -- superseded", "required": "both"}]
    out = evaluate(GREEN_CHECKS, "brain", capabilities=rows)
    assert out["states"] == {"old-cap": "retired"}
    assert out["dead_required"] == []
    assert out["unknown"] == []


def test_drift_state_absent_is_unknown():
    checks = _checks(retrieval_drift={"state_present": False,
                                      "last_compare_ts": None,
                                      "age_hours": None})
    out = evaluate(checks, "brain")
    assert out["states"]["drift-guard"] == "unknown"
    assert "drift-guard" not in out["dead_required"]


def test_drift_snapshot_failures_dead():
    rd = dict(GREEN_CHECKS["retrieval_drift"], consecutive_snapshot_failures=2)
    out = evaluate(_checks(retrieval_drift=rd), "brain")
    assert out["states"]["drift-guard"] == "dead"
    assert "drift-guard" in out["dead_required"]


def test_nightly_receipt_ladder():
    # alive <= 48h, degraded <= 96h, dead beyond, unknown on no signal.
    for age, want in ((10.0, "alive"), (60.0, "degraded"),
                      (200.0, "dead"), (None, "unknown")):
        jl = dict(GREEN_CHECKS["job_liveness"], dedup_report_age_h=age)
        out = evaluate(_checks(job_liveness=jl), "brain")
        assert out["states"]["dedup-job"] == want, (age, want)


def test_codex_auth_derived_never_dead():
    # A stale dream proves nothing about the token: unknown, never dead.
    jl = dict(GREEN_CHECKS["job_liveness"], last_dream_age_h=500.0)
    out = evaluate(_checks(job_liveness=jl), "brain")
    assert out["states"]["codex-auth"] == "unknown"


def test_evaluate_shape_and_injected_clock():
    now = datetime.datetime(2026, 8, 7, 12, 0, tzinfo=datetime.timezone.utc)
    out = evaluate(GREEN_CHECKS, "brain", now=now)
    assert set(out) == {"role", "states", "dead_required", "unknown",
                        "evaluated_at"}
    assert out["evaluated_at"] == now.isoformat()


def test_garbage_inputs_do_not_raise():
    out = evaluate(None, "  BRAIN ")
    assert out["role"] == "brain"
    assert out["dead_required"] == []  # nothing convicts from an empty dict
    out2 = evaluate({}, "not-a-role")
    assert out2["role"] is None


def _doc_table_ids():
    doc = Path(__file__).resolve().parents[2] / "docs" / "capability-manifest.md"
    ids = []
    for line in doc.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        first = line.split("|")[1].strip()
        if first.startswith("`") and first.endswith("`"):
            ids.append(first.strip("`"))
    return ids


def test_doc_manifest_ids_match_literal():
    # Same ids, same order: the doc is a generated mirror, not a fork.
    assert _doc_table_ids() == [row["id"] for row in CAPABILITIES]
