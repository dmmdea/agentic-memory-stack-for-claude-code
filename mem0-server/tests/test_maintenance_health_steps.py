# mem0-server/tests/test_maintenance_health_steps.py
"""GET /health/maintenance folds each step's LATEST outcome and the pool's HEALTH into `ok` (C2).

Headless (no `import app`): the pure build() with injected readers. The wider endpoint contract
(last success, pool alarm, boots, usage) stays in test_maintenance_health.py."""
import datetime as dt
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import maintenance_health as mh  # noqa: E402

FRIDAY = dt.datetime(2026, 9, 11, 8, 0, tzinfo=dt.timezone.utc)   # 2026-09-06 was a Sunday


def _receipts(tmp_path, rows):
    p = tmp_path / "receipts.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""), encoding="utf-8")
    return p


def _build(tmp_path, rows, now=FRIDAY, **kw):
    kw.setdefault("pool_reader", lambda: (10, 90))
    kw.setdefault("boots_reader", lambda: [])
    kw.setdefault("judge_transport", lambda: "native")
    return mh.build(_receipts(tmp_path, rows), now, **kw)


def _r(ts, step, ok=True, note="", status=None, **extra):
    row = {"ts": ts, "step": step, "ok": ok, "exit": 0 if ok else 1, "duration_ms": 5, "receipt_id": f"{step}-{ts}", "note": note}
    if status:
        row["status"] = status
    row.update(extra)
    return row


# ---- (a) a failed latest run is not hidden by an older success ---------------------------
def test_failed_latest_run_flips_ok_despite_a_recent_success(tmp_path):
    out = _build(tmp_path, [
        _r("2026-09-10T08:02:44Z", "wiki-index"),
        _r("2026-09-11T07:02:37Z", "wiki-index", ok=False, note="no wiki source reachable", status="failed"),
        _r("2026-09-11T07:03:00Z", "dream"),
    ])
    assert out["stale_steps"] == [], "the 24 h old success keeps the 48 h staleness rule quiet: that was the hole"
    assert out["ok"] is False
    assert out["failed_steps"] == [{"step": "wiki-index", "ts": "2026-09-11T07:02:37Z", "note": "no wiki source reachable"}]
    assert out["degraded_steps"] == []
    assert out["steps"]["wiki-index"]["status"] == "failed"


def test_legacy_receipt_without_status_counts_by_its_ok_flag(tmp_path):
    out = _build(tmp_path, [_r("2026-09-11T07:00:00Z", "stack-backup", ok=False, note="rsync 23")])
    assert [f["step"] for f in out["failed_steps"]] == ["stack-backup"] and out["ok"] is False


# ---- (b) degraded is ok:true for the step and still turns the verdict red ---------------
def test_degraded_step_flips_ok(tmp_path):
    out = _build(tmp_path, [
        _r("2026-09-10T07:00:00Z", "dream"),
        _r("2026-09-11T07:00:00Z", "dream", ok=True, note="posted-0-of-3", status="degraded", work={"consolidated": 3, "posted": 0}),
    ])
    assert out["degraded_steps"] == [{"step": "dream", "ts": "2026-09-11T07:00:00Z", "note": "posted-0-of-3"}]
    assert out["failed_steps"] == [] and out["stale_steps"] == []
    assert out["ok"] is False
    assert out["steps"]["dream"]["ok"] is True and out["steps"]["dream"]["status"] == "degraded"


# ---- (c) a later ok run clears both -------------------------------------------------------
def test_later_ok_run_clears_failed_and_degraded(tmp_path):
    rows = [
        _r("2026-09-09T07:00:00Z", "wiki-index", ok=False, status="failed"),
        _r("2026-09-09T07:01:00Z", "dream", status="degraded", note="posted-0-of-3"),
        _r("2026-09-10T07:00:00Z", "wiki-index"),
        _r("2026-09-10T07:01:00Z", "dream", status="ok"),
    ]
    out = _build(tmp_path, rows)
    assert out["failed_steps"] == [] and out["degraded_steps"] == [] and out["ok"] is True


def test_no_op_receipts_do_not_clear_a_real_failure(tmp_path):
    """Sunday's failed sweep must stay visible on Monday: the weekly/guard no-ops are ok:true rows,
    not runs."""
    rows = [_r("2026-09-06T03:00:00Z", "contradiction-sweep", ok=False, status="failed", note="judge unreachable"),
            _r("2026-09-07T03:00:00Z", "contradiction-sweep", note="weekly: not Sun; no-op"),
            _r("2026-09-08T03:00:00Z", "contradiction-sweep", note="guard: chain succeeded since the last 03:00 boundary (5s ago); no-op")]
    out = _build(tmp_path, rows)
    assert [f["step"] for f in out["failed_steps"]] == ["contradiction-sweep"]
    assert out["failed_steps"][0]["ts"] == "2026-09-06T03:00:00Z"


def test_health_stamp_never_feeds_on_its_own_verdict(tmp_path):
    """health-stamp exits non-zero on a bad night, and the endpoint reads the PREVIOUS night's receipt
    before tonight's stamp runs: without this exclusion one red night would keep itself red forever."""
    out = _build(tmp_path, [_r("2026-09-10T08:00:00Z", "health-stamp", ok=False, status="failed", note="exit 2")])
    assert out["failed_steps"] == [] and out["ok"] is True


# ---- (d) weekly steps are judged against 8 days ---------------------------------------------
def test_weekly_step_not_stale_midweek(tmp_path):
    out = _build(tmp_path, [_r("2026-09-06T03:00:00Z", "decay-scan")])   # Sunday run, judged on a Friday
    assert out["stale_steps"] == [] and out["ok"] is True, "5 days old, but it is a Sunday-only step"


def test_weekly_step_judged_on_its_real_runs_not_its_no_ops(tmp_path):
    rows = [_r("2026-09-06T03:00:00Z", "decay-scan")]
    rows += [_r(f"2026-09-{d:02d}T03:00:00Z", "decay-scan", note="weekly: not Sun; no-op") for d in (7, 8, 9, 10, 11)]
    assert _build(tmp_path, rows)["stale_steps"] == []
    # the Sunday run never happened: nine days of no-ops must read stale, not "alive"
    rows = [_r("2026-08-30T03:00:00Z", "decay-scan")]
    rows += [_r(f"2026-09-{d:02d}T03:00:00Z", "decay-scan", note="weekly: not Sun; no-op") for d in (7, 8, 9, 10, 11)]
    out = _build(tmp_path, rows)
    assert out["stale_steps"] == ["decay-scan"] and out["ok"] is False


def test_weekly_step_that_never_really_ran_is_not_stale_before_its_first_sunday(tmp_path):
    rows = [_r(f"2026-09-{d:02d}T03:00:00Z", "decay-scan", note="weekly: not Sun; no-op") for d in (8, 9, 10)]
    assert _build(tmp_path, rows)["stale_steps"] == []


def test_daily_step_keeps_the_48h_rule(tmp_path):
    out = _build(tmp_path, [_r("2026-09-08T03:00:00Z", "dream"), _r("2026-09-09T03:00:00Z", "dream", note="guard: chain succeeded; no-op")])
    assert out["stale_steps"] == ["dream"]   # a 53 h old success is stale, guard no-op or not
    out = _build(tmp_path, [_r("2026-09-10T03:00:00Z", "dream")])
    assert out["stale_steps"] == []


# ---- (e)/(f) pool health -----------------------------------------------------------------
def test_pool_health_degraded_flips_ok(tmp_path):
    out = _build(tmp_path, [], pool_health_reader=lambda: "DEGRADED")
    assert out["pool"]["health"] == "DEGRADED" and out["pool"]["health_alarm"] is True
    assert out["pool"]["alarm"] is False, "capacity is fine; the alarm is about health"
    assert out["ok"] is False
    out = _build(tmp_path, [], pool_health_reader=lambda: "ONLINE")
    assert out["pool"]["health"] == "ONLINE" and out["pool"]["health_alarm"] is False and out["ok"] is True
    assert out["pool"]["threshold_pct"] == 85 and out["pool"]["used_pct"] == 10.0


def test_pool_health_reader_failure_reads_unknown_and_fails_open(tmp_path):
    def boom():
        raise OSError("no zpool")
    out = _build(tmp_path, [], pool_health_reader=boom)
    assert out["pool"]["health"] == "unknown" and out["pool"]["health_alarm"] is False and out["ok"] is True
    out = _build(tmp_path, [], pool_health_reader=None)
    assert out["pool"]["health"] == "unknown" and out["pool"]["health_alarm"] is False
    out = _build(tmp_path, [], pool_health_reader=lambda: "  ")
    assert out["pool"]["health"] == "unknown" and out["pool"]["health_alarm"] is False


def test_any_pool_state_other_than_online_alarms(tmp_path):
    for state in ("FAULTED", "UNAVAIL", "SUSPENDED", "degraded"):
        out = _build(tmp_path, [], pool_health_reader=lambda s=state: s)
        assert out["pool"]["health_alarm"] is True, state


def test_zpool_health_reader_asks_zpool_for_the_pool_root(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="DEGRADED\n", stderr="")
    monkeypatch.setattr(mh.subprocess, "run", run)
    assert mh.zpool_health_reader("tank/apps/ams")() == "DEGRADED"
    assert calls == [["zpool", "list", "-H", "-o", "health", "tank"]]


# ---- pool-health acknowledgment: a known, dated DEGRADED pool (planned maintenance) --------
# MEM0_POOL_HEALTH_ACK=<STATE>:<YYYY-MM-DD>. Active iff it parses, today's UTC date <= the date and the
# live health equals STATE. Active: reported, health_alarm false, `ok` ignores the pool health.
def _ack(tmp_path, live, ack, now=FRIDAY):
    return _build(tmp_path, [], now=now, pool_health_reader=lambda: live, pool_ack_reader=lambda: ack)


def test_acked_degraded_pool_is_reported_but_does_not_flip_ok(tmp_path):
    out = _ack(tmp_path, "DEGRADED", "DEGRADED:2026-10-06")
    assert out["pool"]["health"] == "DEGRADED", "the live value stays visible"
    assert out["pool"]["health_alarm"] is False and out["ok"] is True
    assert out["pool"]["health_ack"] == {"state": "DEGRADED", "until": "2026-10-06", "active": True}


def test_ack_is_case_insensitive_and_active_on_its_last_day(tmp_path):
    out = _ack(tmp_path, "degraded", "degraded:2026-09-11")   # FRIDAY is 2026-09-11 UTC
    assert out["pool"]["health_alarm"] is False and out["pool"]["health_ack"]["active"] is True


def test_expired_ack_alarms_and_says_why(tmp_path):
    out = _ack(tmp_path, "DEGRADED", "DEGRADED:2026-09-10")
    assert out["pool"]["health_alarm"] is True and out["ok"] is False
    assert out["pool"]["health_ack"] == {"state": "DEGRADED", "until": "2026-09-10", "active": False, "reason": "expired"}


def test_ack_for_a_different_state_than_the_live_one_alarms(tmp_path):
    out = _ack(tmp_path, "FAULTED", "DEGRADED:2026-10-06")
    assert out["pool"]["health_alarm"] is True and out["ok"] is False
    assert out["pool"]["health_ack"] == {"state": "DEGRADED", "until": "2026-10-06", "active": False, "reason": "mismatch"}


def test_ack_naming_the_exact_state_may_mask_faulted(tmp_path):
    out = _ack(tmp_path, "FAULTED", "FAULTED:2026-10-06")
    assert out["pool"]["health_alarm"] is False and out["pool"]["health_ack"]["active"] is True


def test_malformed_ack_alarms_and_says_why(tmp_path):
    for bad in ("DEGRADED", "DEGRADED:soon", "DEGRADED:2026-13-40", "DEGRADED:20261006", ":2026-10-06", "DEGRADED 2026-10-06",
                "DEGRADED:2026-10-06:x"):
        out = _ack(tmp_path, "DEGRADED", bad)
        assert out["pool"]["health_alarm"] is True and out["ok"] is False, bad
        ack = out["pool"]["health_ack"]
        assert ack["active"] is False and ack["reason"] == "malformed" and ack["value"] == bad, bad


def test_an_ack_never_makes_an_online_pool_alarm(tmp_path):
    for ack in ("DEGRADED:2026-10-06", "DEGRADED:2026-09-01", "garbage"):
        out = _ack(tmp_path, "ONLINE", ack)
        assert out["pool"]["health_alarm"] is False and out["ok"] is True, ack
        assert out["pool"]["health_ack"]["active"] is False
    out = _build(tmp_path, [], pool_health_reader=lambda: "unknown", pool_ack_reader=lambda: "DEGRADED:2026-10-06")
    assert out["pool"]["health_alarm"] is False and out["pool"]["health_ack"]["active"] is False


def test_an_acked_pool_still_counts_capacity_and_steps(tmp_path):
    out = _build(tmp_path, [_r("2026-09-11T07:00:00Z", "dream", ok=False, status="failed")], pool_reader=lambda: (90, 10),
                 pool_health_reader=lambda: "DEGRADED", pool_ack_reader=lambda: "DEGRADED:2026-10-06")
    assert out["pool"]["alarm"] is True and out["ok"] is False and out["pool"]["health_alarm"] is False


def test_no_ack_key_changes_nothing(tmp_path):
    for reader in (None, lambda: None, lambda: "", lambda: "   "):
        out = _build(tmp_path, [], pool_health_reader=lambda: "DEGRADED", pool_ack_reader=reader)
        assert "health_ack" not in out["pool"] and out["pool"]["health_alarm"] is True and out["ok"] is False


def test_an_ack_reader_that_raises_reads_as_no_ack(tmp_path):
    def boom():
        raise OSError("unreadable")
    out = _build(tmp_path, [], pool_health_reader=lambda: "DEGRADED", pool_ack_reader=boom)
    assert "health_ack" not in out["pool"] and out["pool"]["health_alarm"] is True


# ---- where the ack comes from: env first, then stack.env, read on EACH call -----------------
@pytest.fixture
def stack_env(tmp_path, monkeypatch):
    """A sandboxed ~/.mem0/stack.env: the server unit does not load it into its environment, so the
    reader must open it itself. Nothing here touches the real home."""
    import job_liveness
    path = tmp_path / "stack.env"
    monkeypatch.setattr(job_liveness, "STACK_ENV_PATH", path)
    monkeypatch.delenv("MEM0_POOL_HEALTH_ACK", raising=False)
    return path


def test_ack_from_stack_env_alone_works(stack_env):
    stack_env.write_text("MEM0_ROLE=brain\nMEM0_POOL_HEALTH_ACK=DEGRADED:2026-10-06\n", encoding="utf-8")
    assert mh.read_pool_ack() == "DEGRADED:2026-10-06"


def test_env_beats_stack_env(stack_env, monkeypatch):
    stack_env.write_text("MEM0_POOL_HEALTH_ACK=DEGRADED:2026-10-06\n", encoding="utf-8")
    monkeypatch.setenv("MEM0_POOL_HEALTH_ACK", "DEGRADED:2026-11-01")
    assert mh.read_pool_ack() == "DEGRADED:2026-11-01"


def test_no_key_anywhere_reads_none(stack_env):
    assert mh.read_pool_ack() is None                      # no file
    stack_env.write_text("MEM0_ROLE=brain\n", encoding="utf-8")
    assert mh.read_pool_ack() is None                      # file without the key


def test_the_ack_is_read_on_each_call_never_cached(stack_env, tmp_path):
    reader = mh.read_pool_ack
    stack_env.write_text("MEM0_POOL_HEALTH_ACK=DEGRADED:2026-10-06\n", encoding="utf-8")
    assert _build(tmp_path, [], pool_health_reader=lambda: "DEGRADED", pool_ack_reader=reader)["pool"]["health_alarm"] is False
    stack_env.write_text("MEM0_ROLE=brain\n", encoding="utf-8")   # the operator cleared it
    assert _build(tmp_path, [], pool_health_reader=lambda: "DEGRADED", pool_ack_reader=reader)["pool"]["health_alarm"] is True


def test_the_route_wires_the_ack_reader():
    """app.py cannot be imported headless (it needs the mem0 library), so pin the wiring by its source:
    without this argument the ack is a silent no-op in production."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
    route = src[src.index("def health_maintenance"):]
    route = route[:route.index("@app.get", 1)]
    assert "pool_ack_reader=_mh.read_pool_ack" in route


# ---- the rest of C2: drift and wiki are reported, never folded into ok ----------------------
def test_drift_is_reported_but_not_folded(tmp_path):
    out = _build(tmp_path, [], drift_reader=lambda: {"alarm": True, "before_retrievable": 6, "n_total": 7, "hwm": 7})
    assert out["drift"] == {"alarm": True, "before": 6, "n_total": 7} and out["ok"] is True
    out = _build(tmp_path, [], drift_reader=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert out["drift"] == {"alarm": None, "before": None, "n_total": None}
    assert _build(tmp_path, [])["drift"] == {"alarm": None, "before": None, "n_total": None}


def test_wiki_freshness_reads_the_newer_of_pull_and_build(tmp_path):
    d = tmp_path / "wiki-index"
    d.mkdir()
    now_epoch = int(FRIDAY.timestamp())
    (d / "last-pull").write_text(f"{now_epoch - 48 * 3600}\n")
    (d / "last-build").write_text(f"{now_epoch - 720}\n")
    out = _build(tmp_path, [], wiki_stamp_dir=d)
    assert out["wiki"] == {"last_pull_age_h": 48.0, "last_build_age_h": 0.2, "fresh_age_h": 0.2}
    assert out["ok"] is True, "wiki freshness rides the wiki-index step's own status, not ok"
    (d / "last-build").unlink()
    out = _build(tmp_path, [], wiki_stamp_dir=d)
    assert out["wiki"] == {"last_pull_age_h": 48.0, "last_build_age_h": None, "fresh_age_h": 48.0}
    (d / "last-pull").write_text("garbage")
    assert _build(tmp_path, [], wiki_stamp_dir=d)["wiki"] == {"last_pull_age_h": None, "last_build_age_h": None, "fresh_age_h": None}
    assert _build(tmp_path, [])["wiki"] == {"last_pull_age_h": None, "last_build_age_h": None, "fresh_age_h": None}
