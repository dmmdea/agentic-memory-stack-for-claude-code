"""Headless truth-table pins for capabilities.py (the capability manifest).

evaluate() is pure (checks dict + role in, verdict out), so the review-bound
honesty rules pin headless: F9 (a zero-signal activity counter is never
'alive'), F8 (an unknown role never convicts role-scoped rows), probe-less
rows always 'unknown', retired rows always 'retired'. The doc test pins
docs/capability-manifest.md's table ids against the CAPABILITIES literal so the
human mirror cannot drift from the single source.

W4 retired the eight 'none -- W4' rows into real verdicts and added their
red cases -- above all F12's: an l1a success stamp that is 11 minutes stale
while attempts keep arriving must NOT convict (that is the commonest HEALTHY
state -- sessions ran and had nothing durable to extract).
"""
import copy
import datetime
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capabilities import (  # noqa: E402
    CAPABILITIES,
    L1A_CONVICT_H,
    admission_selfprobe,
    evaluate,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.datetime(2026, 8, 7, 12, 0, tzinfo=datetime.timezone.utc)
NOW_S = NOW.timestamp()


# The exact check shapes /health/deep assembles (app.py) on an all-green box.
GREEN_CHECKS = {
    "qdrant": {"ok": True, "points": 100, "status": "green"},
    "embedder": {"ok": True, "dim": 768},
    "sparse_leg": {"ok": True, "points": 100, "with_bm25": 100, "coverage": 1.0,
                   "canary": {"ran": True, "hit": True, "token": "qdrant"}},
    # Real /health/deep shape (canonical_key_health): ok/present/source/dpapi_blob.
    # The fixture previously carried a "provider" key that production never
    # emits — a fixture that does not mirror the real shape cannot catch a
    # verdict that reads the real keys.
    "canonical_key": {"ok": True, "present": True, "source": "runtime",
                      "dpapi_blob": True},
    "put_carryover_today": {"date": "2026-08-07", "puts": 3,
                            "keys_restored": 0, "keys_lost": 0},
    "mojibake": {"ok": True, "scanned": 100, "hits": 0, "sample_ids": [],
                 "elapsed_ms": 12},
    "pending_contradiction_reviews": 0,
    "reranker": {"last_rerank_ok_ts": NOW_S - 3600, "consecutive_rerank_failures": 0,
                 "ok_total": 12, "fail_total": 0, "last_error": None},
    "admission_probe": {"ok": True, "tier_rejected": True, "brand_rejected": True,
                        "neutral_admitted": True, "query_class": "durable"},
    "job_liveness": {
        "role": "brain",
        "last_dream_age_h": 10.0, "prune_age_h": 10.0, "gather_age_h": 10.0,
        "backup_manifest_age_h": 12.0, "dedup_report_age_h": 12.0,
        "morning_summary_age_h": 10.0, "morning_summary_sections_48h": 2,
        "l1a_attempt_age_h": 0.5, "l1a_success_age_h": 2.0,
        "sessionstart_banner_age_h": 1.0,
        "mcp_shim_receipt_age_h": 1.0, "mcp_shim_host_match": True,
        "mcp_shim_stack_version": "1.18.0",
        "brand_scope_age_h": 12.0, "brand_scope_misscoped": 0,
        "outbox_depth": 0, "outbox_replayed_age_h": 100.0,
        "outbox_drain_log_age_h": 100.0,
        # W6 D5: fresh queue mirror, nothing failed/stuck -> alive.
        "jobs_heartbeat_age_h": 0.5, "jobs_queued": 0, "jobs_running": 0,
        "jobs_failed_24h": 0, "jobs_reaped_24h": 0,
        "jobs_oldest_running_age_h": None, "jobs_oldest_queued_age_h": None,
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
# W4: formerly 'none -- W4' (always unknown), now each with a real evaluator.
W4_REVIVED = [
    "reranker", "l1a-extraction", "sessionstart-banner", "mcp-shim",
    "admission-gate", "tier-policy", "brand-isolation", "offline-outbox",
]


def _checks(**overrides):
    c = copy.deepcopy(GREEN_CHECKS)
    c.update(overrides)
    return c


def _jl(**overrides):
    """GREEN_CHECKS with job_liveness fields overridden."""
    jl = dict(GREEN_CHECKS["job_liveness"], **overrides)
    return _checks(job_liveness=jl)


def _ev(checks, role="brain", **kw):
    kw.setdefault("now", NOW)
    return evaluate(checks, role, **kw)


def test_GREEN_CHECKS_truth_table():
    out = _ev(GREEN_CHECKS)
    assert out["role"] == "brain"
    for cid in PROBE_BACKED + W4_REVIVED:
        assert out["states"][cid] == "alive", cid
    assert out["dead_required"] == []
    assert out["unknown"] == []


def test_no_row_is_probeless_anymore():
    """The wave gate: after W4 no capability may still say 'designed, unproven'.
    A new row added without a probe fails HERE, not silently in production."""
    blind = [r["id"] for r in CAPABILITIES if (r["probe"] or "").startswith("none")]
    assert blind == [], f"probe-less rows left in the manifest: {blind}"


def test_sparse_leg_dead_gates_dead_required():
    checks = _checks(sparse_leg={"ok": False, "fastembed": False,
                                 "bm25_slot": True, "coverage": 0.0})
    out = _ev(checks)
    assert out["states"]["bm25-sparse-leg"] == "dead"
    assert "bm25-sparse-leg" in out["dead_required"]


def test_sparse_leg_low_coverage_degraded_not_dead():
    checks = _checks(sparse_leg={"ok": True, "coverage": 0.5,
                                 "canary": {"ran": True, "hit": True}})
    out = _ev(checks)
    assert out["states"]["bm25-sparse-leg"] == "degraded"
    assert "bm25-sparse-leg" not in out["dead_required"]


def test_zero_signal_put_carryover_is_unknown_not_alive():
    # The F9 red case: a quiet day (no PUTs) is silence, not health.
    checks = _checks(put_carryover_today={"date": "2026-08-07", "puts": 0,
                                          "keys_restored": 0, "keys_lost": 0})
    out = _ev(checks)
    assert out["states"]["put-carryover"] == "unknown"
    assert "put-carryover" in out["unknown"]
    assert "put-carryover" not in out["dead_required"]


def test_keys_lost_is_dead_even_with_activity():
    checks = _checks(put_carryover_today={"date": "2026-08-07", "puts": 5,
                                          "keys_restored": 0, "keys_lost": 2})
    out = _ev(checks)
    assert out["states"]["put-carryover"] == "dead"
    assert "put-carryover" in out["dead_required"]  # required 'both'


def test_unknown_role_never_convicts_role_scoped_rows():
    # F8: dream-cycle (brain-scoped) is DEAD, but the role is unknown, so it
    # must not enter dead_required; the 'both'-scoped dead row still does.
    jl = dict(GREEN_CHECKS["job_liveness"], last_dream_age_h=200.0)
    checks = _checks(job_liveness=jl,
                     sparse_leg={"ok": False, "coverage": 0.0})
    out = _ev(checks, None)
    assert out["role"] is None
    assert out["states"]["dream-cycle"] == "dead"
    assert "dream-cycle" not in out["dead_required"]
    assert "bm25-sparse-leg" in out["dead_required"]


def test_replica_role_brain_rows_not_required():
    jl = dict(GREEN_CHECKS["job_liveness"], last_dream_age_h=200.0,
              backup_manifest_age_h=500.0)
    out = _ev(_checks(job_liveness=jl), "replica")
    assert out["states"]["dream-cycle"] == "dead"
    assert out["states"]["backup-pipeline"] == "dead"
    assert out["dead_required"] == []


def test_probe_none_rows_always_unknown():
    # The mechanism itself, pinned with an INJECTED row now that the literal has
    # no probe-less rows left: a future capability named before it is probed must
    # still evaluate 'unknown' on an otherwise all-green box, never 'alive'.
    rows = [{"id": "future-cap", "what": "named but unprobed",
             "probe": "none -- W5", "required": "both"}]
    out = _ev(GREEN_CHECKS, capabilities=rows)
    assert out["states"] == {"future-cap": "unknown"}
    assert out["unknown"] == ["future-cap"]
    assert out["dead_required"] == []      # blindness never convicts


def test_retired_rows_evaluate_retired():
    rows = [{"id": "old-cap", "what": "w",
             "probe": "retired -- superseded", "required": "both"}]
    out = _ev(GREEN_CHECKS, capabilities=rows)
    assert out["states"] == {"old-cap": "retired"}
    assert out["dead_required"] == []
    assert out["unknown"] == []


def test_drift_state_absent_is_unknown():
    checks = _checks(retrieval_drift={"state_present": False,
                                      "last_compare_ts": None,
                                      "age_hours": None})
    out = _ev(checks)
    assert out["states"]["drift-guard"] == "unknown"
    assert "drift-guard" not in out["dead_required"]


def test_drift_snapshot_failures_dead():
    rd = dict(GREEN_CHECKS["retrieval_drift"], consecutive_snapshot_failures=2)
    out = _ev(_checks(retrieval_drift=rd))
    assert out["states"]["drift-guard"] == "dead"
    assert "drift-guard" in out["dead_required"]


def test_nightly_receipt_ladder():
    # alive <= 48h, degraded <= 96h, dead beyond, unknown on no signal.
    for age, want in ((10.0, "alive"), (60.0, "degraded"),
                      (200.0, "dead"), (None, "unknown")):
        jl = dict(GREEN_CHECKS["job_liveness"], dedup_report_age_h=age)
        out = _ev(_checks(job_liveness=jl))
        assert out["states"]["dedup-job"] == want, (age, want)


def test_codex_auth_derived_never_dead():
    # A stale dream proves nothing about the token: unknown, never dead.
    jl = dict(GREEN_CHECKS["job_liveness"], last_dream_age_h=500.0)
    out = _ev(_checks(job_liveness=jl))
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


# ======================================================================
# W4 -- the eight revived rows
# ======================================================================

# ---- l1a-extraction: the F12 two-signal rule ----

def test_l1a_success_stale_by_eleven_minutes_does_not_convict():
    """THE F12 RED CASE (mutation M4).

    The extractor throttles at 10 minutes between SUCCESSFUL extractions, so a
    success stamp 11 minutes old is the single most ordinary healthy state
    there is. The pre-review rule ('last-l1a is stale => dead') convicted it.
    Attempts are arriving, a real extraction landed minutes ago: alive, and in
    no case dead_required."""
    out = _ev(_jl(l1a_attempt_age_h=0.05, l1a_success_age_h=11.0 / 60.0))
    assert out["states"]["l1a-extraction"] == "alive"
    assert "l1a-extraction" not in out["dead_required"]


def test_l1a_quiet_day_is_not_a_conviction():
    # Sessions ran all day; nothing durable came out of them. Inside the fresh
    # window this is still 'alive' -- and never dead.
    out = _ev(_jl(l1a_attempt_age_h=1.0, l1a_success_age_h=40.0))
    assert out["states"]["l1a-extraction"] == "alive"


def test_l1a_convicts_only_across_a_multi_day_window_and_caps_at_degraded():
    # Attempts still arriving, success stale by more than the multi-day window:
    # the one shape that IS evidence of a broken extractor. Capped at degraded,
    # so it can never enter dead_required even though the row is brain-required.
    out = _ev(_jl(l1a_attempt_age_h=0.5, l1a_success_age_h=L1A_CONVICT_H + 1))
    assert out["states"]["l1a-extraction"] == "degraded"
    assert out["dead_required"] == []
    # ...and just inside the window it still does not convict.
    ok = _ev(_jl(l1a_attempt_age_h=0.5, l1a_success_age_h=L1A_CONVICT_H - 1))
    assert ok["states"]["l1a-extraction"] == "unknown"


def test_l1a_attempts_stopped_too_is_unknown_not_dead():
    # Nothing has fired at all: an idle/powered-off box, not a broken one.
    out = _ev(_jl(l1a_attempt_age_h=500.0, l1a_success_age_h=500.0))
    assert out["states"]["l1a-extraction"] == "unknown"
    assert out["dead_required"] == []


def test_l1a_zero_signal_is_unknown():
    out = _ev(_jl(l1a_attempt_age_h=None, l1a_success_age_h=None))
    assert out["states"]["l1a-extraction"] == "unknown"
    assert "l1a-extraction" in out["unknown"]


def test_l1a_never_succeeded_is_unknown_not_convicted():
    # One attempt stamp cannot distinguish 'broken' from 'first session had
    # nothing to keep'.
    out = _ev(_jl(l1a_attempt_age_h=0.2, l1a_success_age_h=None))
    assert out["states"]["l1a-extraction"] == "unknown"


# ---- sessionstart-banner ----

def test_sessionstart_banner_fresh_is_alive():
    assert _ev(GREEN_CHECKS)["states"]["sessionstart-banner"] == "alive"


def test_sessionstart_banner_zero_signal_on_idle_box_is_unknown():
    # No banner stamp AND no session evidence: silence, not death (F9).
    out = _ev(_jl(sessionstart_banner_age_h=None, l1a_attempt_age_h=None))
    assert out["states"]["sessionstart-banner"] == "unknown"
    assert out["dead_required"] == []


def test_sessionstart_banner_stale_while_sessions_run_is_degraded():
    # Sessions are demonstrably happening (l1a fired minutes ago) but the
    # unconditional banner stamp is missing -> the hook stopped running.
    # Capped at degraded: a 'both'-required row must not FAIL the verifier on
    # a two-signal inference.
    out = _ev(_jl(sessionstart_banner_age_h=None, l1a_attempt_age_h=0.3))
    assert out["states"]["sessionstart-banner"] == "degraded"
    assert out["dead_required"] == []
    stale = _ev(_jl(sessionstart_banner_age_h=300.0, l1a_attempt_age_h=0.3))
    assert stale["states"]["sessionstart-banner"] == "degraded"


# ---- mcp-shim: host-keyed + version-skew (F13) ----

def test_mcp_shim_row_is_brain_scoped():
    # F13: a replica's shim writes to the REPLICA's ~/.mem0, whose server may
    # not be running -- so the row is scoped to brain, not both.
    row = next(r for r in CAPABILITIES if r["id"] == "mcp-shim")
    assert row["required"] == "brain"


def test_mcp_shim_foreign_host_receipt_does_not_count():
    # ~/.mem0 travels in stack backups; a restored receipt from another box
    # must read as absent, not as local liveness.
    out = _ev(_jl(mcp_shim_host_match=False, l1a_attempt_age_h=None))
    assert out["states"]["mcp-shim"] == "unknown"
    convicted = _ev(_jl(mcp_shim_host_match=False, l1a_attempt_age_h=0.3))
    assert convicted["states"]["mcp-shim"] == "degraded"
    assert convicted["dead_required"] == []       # never dead


def test_mcp_shim_stack_version_skew_is_degraded():
    # The AMS-02 class: the Windows launch-path copy is refreshed only by
    # install/2-windows-config.ps1, so a deployed server can outrun the shim
    # the CLI actually launches. Version skew says so instead of hiding it.
    out = _ev(GREEN_CHECKS, stack_version="1.19.0")
    assert out["states"]["mcp-shim"] == "degraded"
    same = _ev(GREEN_CHECKS, stack_version="1.18.0")
    assert same["states"]["mcp-shim"] == "alive"
    # No server version supplied -> comparison skipped, not failed.
    assert _ev(GREEN_CHECKS)["states"]["mcp-shim"] == "alive"


def test_mcp_shim_zero_signal_is_unknown():
    out = _ev(_jl(mcp_shim_receipt_age_h=None, mcp_shim_host_match=None,
                  mcp_shim_stack_version=None, l1a_attempt_age_h=None))
    assert out["states"]["mcp-shim"] == "unknown"


def test_shim_literal_matches_repo_version():
    """The shim's SHIM_STACK_VERSION is a hand-pinned literal (nothing beside
    the deployed copies carries a VERSION file, and reading the SERVER's would
    always agree and detect nothing). Pin it to the repo VERSION so the skew
    check keeps meaning what it claims."""
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    shim = (REPO_ROOT / "scripts" / "wsl" / "mem0-mcp-shim.py").read_text(encoding="utf-8")
    m = re.search(r'^SHIM_STACK_VERSION\s*=\s*"([^"]+)"', shim, re.M)
    assert m, "mem0-mcp-shim.py must define SHIM_STACK_VERSION"
    assert m.group(1) == version, (
        f"shim stamp {m.group(1)!r} != repo VERSION {version!r} -- bump both together")


def test_shim_receipt_is_written_before_run_and_never_to_stdout():
    """Contract pins for the receipt: it must be written in __main__ BEFORE
    mcp.run (a receipt written after the server exits proves nothing), and the
    writer must not print -- stdout is the MCP JSON-RPC channel."""
    shim = (REPO_ROOT / "scripts" / "wsl" / "mem0-mcp-shim.py").read_text(encoding="utf-8")
    main = shim.split('if __name__ == "__main__":', 1)[1]
    assert "_write_start_receipt()" in main
    assert main.index("_write_start_receipt()") < main.index("mcp.run(")
    body = shim.split("def _write_start_receipt", 1)[1].split("\nif __name__", 1)[0]
    assert "print(" not in body, "the receipt writer must never touch stdout"
    assert "socket.gethostname()" in body, "the receipt must be host-keyed (F13)"


# ---- admission-gate / tier-policy / brand-isolation ----

def test_admission_selfprobe_is_pure_and_correct():
    """The probe exercises the three dimensions the manifest names, through
    AdmissionPolicy.evaluate() directly."""
    out = admission_selfprobe()
    assert out["ok"] is True
    assert out["tier_rejected"] is True       # canonical is not in the durable allowlist
    assert out["brand_rejected"] is True      # v0.19 M4 fail-closed brandless scope
    assert out["neutral_admitted"] is True    # negative control: not a reject-all gate
    assert "error" not in out


def test_admission_selfprobe_does_not_touch_counters_or_audit_log(tmp_path,
                                                                  monkeypatch):
    """NEVER apply_admission. It bumps the MEM-8 daily counters that
    /health/deep reports and appends to ~/.mem0/admission-rejected.jsonl -- so
    probing through it would corrupt the starvation metrics and write audit
    lines on EVERY health read (deploy gate and session banner included)."""
    import admission_gate

    before = copy.deepcopy(admission_gate.admission_rejection_stats)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _explode(*a, **k):                    # any disk write is a defect here
        raise AssertionError("admission_selfprobe wrote to the audit log")

    monkeypatch.setattr(admission_gate, "log_rejected", _explode)
    out = admission_selfprobe()

    assert out["ok"] is True
    assert admission_gate.admission_rejection_stats == before
    assert not list(tmp_path.rglob("admission-rejected.jsonl"))


def test_admission_gate_row_dead_when_probe_fails():
    out = _ev(_checks(admission_probe={"ok": False, "tier_rejected": True,
                                       "brand_rejected": False,
                                       "neutral_admitted": True}))
    assert out["states"]["admission-gate"] == "dead"
    assert "admission-gate" in out["dead_required"]        # required 'both'
    assert out["states"]["brand-isolation"] == "dead"      # the leaking dimension
    assert out["states"]["tier-policy"] == "alive"         # that leg still holds


def test_canonical_key_plaintext_is_degraded_never_alive():
    """AMS-13 / review F22-1: a working key resting as PLAINTEXT is a weaker
    posture, not a healthy one. This row reporting 'alive' on plaintext is
    exactly the drift AMS-13 exists to end — the docs claimed the DPAPI cutover
    was complete while this agreed with them."""
    checks = dict(GREEN_CHECKS)
    checks["canonical_key"] = {"ok": True, "present": True,
                               "source": "plaintext", "dpapi_blob": False}
    out = evaluate(checks, "brain")
    assert out["states"]["canonical-key"] == "degraded"
    # degraded is not dead: promotions still work, so it must not convict.
    assert "canonical-key" not in out["dead_required"]


def test_canonical_key_alive_only_on_a_real_dpapi_posture():
    for src, blob, expected in (
        ("runtime", True, "alive"),      # tmpfs key injected from the blob
        ("dpapi", False, "alive"),       # served directly from the blob
        ("plaintext", False, "degraded"),
        # A blob on disk while the server SERVES plaintext is the cutover-ran-
        # then-broke state (ExecStartPre fails soft). It is a stronger degraded
        # signal than plain plaintext, never alive. This row previously asserted
        # 'alive' with the comment "blob exists and served the key" — which
        # source=plaintext contradicts; a green suite would never have
        # self-corrected it.
        ("plaintext", True, "degraded"),
        ("none", True, "degraded"),      # latent, but never alive on a blob alone
    ):
        checks = dict(GREEN_CHECKS)
        checks["canonical_key"] = {"ok": True, "present": True,
                                   "source": src, "dpapi_blob": blob}
        assert evaluate(checks, "brain")["states"]["canonical-key"] == expected, (src, blob)


def test_tier_policy_dead_when_canonical_admitted_in_durable():
    out = _ev(_checks(admission_probe={"ok": False, "tier_rejected": False,
                                       "brand_rejected": True,
                                       "neutral_admitted": True}))
    assert out["states"]["tier-policy"] == "dead"
    assert "tier-policy" in out["dead_required"]


def test_admission_rows_unknown_when_the_probe_did_not_run():
    # Zero signal (an older server, or the probe itself erroring) is unknown for
    # all three rows -- never 'alive', never a conviction.
    for probe in (None, {"ok": False, "error": "boom", "tier_rejected": None,
                         "brand_rejected": None, "neutral_admitted": None}):
        out = _ev(_checks(admission_probe=probe))
        for cid in ("admission-gate", "tier-policy", "brand-isolation"):
            assert out["states"][cid] == "unknown", (cid, probe)
        assert out["dead_required"] == []


def test_tier_policy_probe_string_names_the_write_half_exerciser():
    # The read half is server-probed; the write half is not. The row must say
    # so rather than implying full coverage.
    row = next(r for r in CAPABILITIES if r["id"] == "tier-policy")
    assert "read-half server-probed" in row["probe"]
    assert "Test-MemoryStack" in row["probe"]


def test_brand_isolation_degrades_on_misscoped_canonical_facts():
    out = _ev(_jl(brand_scope_misscoped=3))
    assert out["states"]["brand-isolation"] == "degraded"
    assert out["dead_required"] == []


def test_brand_isolation_degrades_when_the_audit_goes_stale():
    out = _ev(_jl(brand_scope_age_h=200.0))
    assert out["states"]["brand-isolation"] == "degraded"
    # An audit that was never deployed here is not a degradation -- the read
    # half still proved itself.
    absent = _ev(_jl(brand_scope_age_h=None, brand_scope_misscoped=None))
    assert absent["states"]["brand-isolation"] == "alive"


# ---- reranker: passive counters only (F11) ----

def test_reranker_zero_signal_is_unknown_not_alive():
    out = _ev(_checks(reranker={"last_rerank_ok_ts": None,
                                "consecutive_rerank_failures": 0,
                                "ok_total": 0, "fail_total": 0}))
    assert out["states"]["reranker"] == "unknown"


def test_reranker_failure_ladder():
    for fails, last_ok, want in (
        (0, NOW_S - 60, "alive"),
        (1, NOW_S - 60, "degraded"),
        (1, None, "degraded"),
        (3, None, "dead"),
        (5, NOW_S - 60, "dead"),
        (0, NOW_S - 200 * 3600, "unknown"),   # a days-old success is silence
    ):
        out = _ev(_checks(reranker={"last_rerank_ok_ts": last_ok,
                                    "consecutive_rerank_failures": fails}))
        assert out["states"]["reranker"] == want, (fails, last_ok, want)


def test_reranker_is_optional_and_never_convicts():
    out = _ev(_checks(reranker={"last_rerank_ok_ts": None,
                                "consecutive_rerank_failures": 99}))
    assert out["states"]["reranker"] == "dead"
    assert out["dead_required"] == []          # 'optional' never enters it


def test_no_active_reranker_probe_reaches_health_deep():
    """F11 (BLOCKER) as an executable rule: the row must read the PASSIVE
    counters. deploy.sh gates on /health/deep right after a restart, so an
    active CPU cross-encoder call there would hang deploys."""
    row = next(r for r in CAPABILITIES if r["id"] == "reranker")
    assert "checks.reranker" in row["probe"]
    assert "PASSIVE" in row["probe"] and "Test-MemoryStack L5" in row["probe"]
    deploy = (REPO_ROOT / "scripts" / "wsl" / "deploy.sh").read_text(encoding="utf-8")
    assert "--max-time" in deploy.split("/health/deep | python3", 1)[0].rsplit("curl", 1)[1]


# ---- offline-outbox: replica-scoped ----

def test_offline_outbox_role_scoping():
    """Required on the replica only. A stranded outbox is DEAD there, and F8
    keeps the identical state out of a brain box's dead_required."""
    stranded = _jl(outbox_depth=7, outbox_drain_log_age_h=None,
                   outbox_replayed_age_h=None)
    on_replica = _ev(stranded, "replica")
    assert on_replica["states"]["offline-outbox"] == "dead"
    assert "offline-outbox" in on_replica["dead_required"]

    on_brain = _ev(stranded, "brain")
    assert on_brain["states"]["offline-outbox"] == "dead"
    assert on_brain["dead_required"] == []          # not required on a brain

    unknown_role = _ev(stranded, None)
    assert unknown_role["dead_required"] == []      # F8


def test_offline_outbox_draining_is_degraded_not_dead():
    out = _ev(_jl(outbox_depth=3, outbox_drain_log_age_h=0.5), "replica")
    assert out["states"]["offline-outbox"] == "degraded"
    assert out["dead_required"] == []


def test_offline_outbox_empty_queue_needs_a_real_replay_to_be_alive():
    # F9: an empty outbox on a box that never went offline proves nothing.
    never = _ev(_jl(outbox_depth=0, outbox_replayed_age_h=None,
                    outbox_drain_log_age_h=None), "replica")
    assert never["states"]["offline-outbox"] == "unknown"
    exercised = _ev(_jl(outbox_depth=0, outbox_replayed_age_h=48.0), "replica")
    assert exercised["states"]["offline-outbox"] == "alive"


def test_offline_outbox_zero_signal_is_unknown():
    out = _ev(_jl(outbox_depth=None, outbox_replayed_age_h=None,
                  outbox_drain_log_age_h=None), "replica")
    assert out["states"]["offline-outbox"] == "unknown"


# ---- the shape contract still holds with every new field ----

def test_new_rows_never_raise_on_a_legacy_checks_dict():
    """A server carrying the new manifest but an older /health/deep payload (no
    reranker/admission_probe keys, pre-W4 job_liveness) must degrade to
    'unknown', not explode."""
    legacy = {"qdrant": {"ok": True}, "job_liveness": {"role": "brain"}}
    out = evaluate(legacy, "brain", now=NOW)
    for cid in W4_REVIVED:
        assert out["states"][cid] == "unknown", cid
    assert out["dead_required"] == []
