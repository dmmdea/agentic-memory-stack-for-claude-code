# mem0-server/tests/test_maintenance_health_steps.py
"""GET /health/maintenance folds each step's LATEST outcome and the pool's HEALTH into `ok` (C2).

Headless (no `import app`): the pure build() with injected readers. The wider endpoint contract
(last success, pool alarm, boots, usage) stays in test_maintenance_health.py."""
import datetime as dt
import json
import os
import subprocess
import sys

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
