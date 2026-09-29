"""ams-health-stamp.sh and ams-morning-summary.sh print the honest health verdict.

The stamp is the chain's LAST reading step: it exits non-zero when a step's latest run failed or the
pool is unhealthy, so a bad night ends in a red unit instead of a green `ok=False` line nobody reads.
The morning summary lists degraded steps with their notes and the work counts a step reported."""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

BASE = {"ok": True, "steps": {}, "stale_steps": [], "failed_steps": [], "degraded_steps": [],
        "pool": {"used_pct": 71.4, "alarm": False, "threshold_pct": 85, "health": "ONLINE", "health_alarm": False},
        "usage": {"used_percent": 12}, "boots_7d": ["a", "b"]}


def _stamp(tmp_path, payload, curl_exit=0):
    """Run the stamp against a fake `curl` that writes `payload` to its -o target."""
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    canned = tmp_path / "canned.json"
    canned.write_text(json.dumps(payload), encoding="utf-8")
    fake = bindir / "curl"
    fake.write_text('#!/bin/bash\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                    f'[ {curl_exit} -eq 0 ] || exit {curl_exit}\ncp "{canned}" "$out"\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}")
    r = subprocess.run([BASH, str(SCRIPTS / "ams-health-stamp.sh")], capture_output=True, text=True, env=env, timeout=60)
    return r, home / ".mem0" / "maintenance" / "health-maintenance.json"


def test_green_night_prints_the_verdict_and_exits_zero(tmp_path):
    r, stamp = _stamp(tmp_path, BASE)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == "health ok=True failed=- degraded=- pool 71.4% ONLINE"
    assert json.loads(stamp.read_text(encoding="utf-8"))["ok"] is True


def test_failed_step_prints_and_turns_the_stamp_red(tmp_path):
    p = dict(BASE, ok=False, failed_steps=[{"step": "wiki-index", "ts": "t", "note": "n"}, {"step": "stack-backup", "ts": "t", "note": ""}],
             degraded_steps=[{"step": "dream", "ts": "t", "note": "posted-0-of-3"}])
    r, stamp = _stamp(tmp_path, p)
    assert r.returncode != 0, "a bad night must end the chain in a red step"
    assert r.stdout.splitlines()[-1] == "health ok=False failed=wiki-index,stack-backup degraded=dream pool 71.4% ONLINE"
    assert stamp.exists(), "the stamp file is written before the verdict exits (the morning summary and Gatus read it)"


def test_degraded_pool_turns_the_stamp_red(tmp_path):
    p = dict(BASE, ok=False, pool=dict(BASE["pool"], health="DEGRADED", health_alarm=True))
    r, _ = _stamp(tmp_path, p)
    assert r.returncode != 0 and r.stdout.splitlines()[-1] == "health ok=False failed=- degraded=- pool 71.4% DEGRADED"


def test_degraded_step_alone_is_reported_not_red(tmp_path):
    """The brief's exit rule is failed_steps or pool.health_alarm; degraded is loud in the line and the summary."""
    r, _ = _stamp(tmp_path, dict(BASE, ok=False, degraded_steps=[{"step": "dream", "ts": "t", "note": "x"}]))
    assert r.returncode == 0 and "degraded=dream" in r.stdout


def test_payload_from_an_older_server_still_prints(tmp_path):
    """A rolling deploy: the stamp may meet a server that predates failed_steps and pool.health."""
    old = {"ok": True, "stale_steps": [], "pool": {"used_pct": 50.0}, "boots_7d": []}
    r, _ = _stamp(tmp_path, old)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == "health ok=True failed=- degraded=- pool 50.0% unknown"


def test_curl_failure_is_still_a_failed_step(tmp_path):
    r, _ = _stamp(tmp_path, BASE, curl_exit=7)
    assert r.returncode != 0


def _summary(tmp_path, receipts, health=None):
    home = tmp_path / "home"
    d = home / ".mem0" / "maintenance"
    d.mkdir(parents=True, exist_ok=True)
    (d / "receipts.jsonl").write_text("\n".join(json.dumps(x) for x in receipts) + "\n", encoding="utf-8")
    if health is not None:
        (d / "health-maintenance.json").write_text(json.dumps(health), encoding="utf-8")
    env = dict(os.environ, HOME=str(home), AMS_SUMMARY_NOW="2099-01-01T09:00:00Z")
    r = subprocess.run([BASH, str(SCRIPTS / "ams-morning-summary.sh")], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return (d / "morning-summary.md").read_text(encoding="utf-8")


def _row(step, ok=True, status="ok", note="", work=None, ms=1000):
    return {"ts": "2099-01-01T08:00:00Z", "step": step, "ok": ok, "status": status, "exit": 0 if ok else 1,
            "duration_ms": ms, "receipt_id": "x", "note": note, "work": work or {}}


def test_summary_lists_degraded_steps_with_note_and_work(tmp_path):
    t = _summary(tmp_path, [
        _row("dream", status="degraded", note="posted-0-of-3", work={"consolidated": 3, "posted": 0}, ms=40000),
        _row("semantic-dedup", work={"scanned": 16383, "deleted": 0}),
        _row("stack-backup", ok=False, status="failed", note="manifest missing"),
        _row("index-refresh"),
    ], health=dict(BASE, ok=False, degraded_steps=[{"step": "dream", "ts": "t", "note": "posted-0-of-3"}]))
    assert "- dream DEGRADED 40000ms -- posted-0-of-3 [consolidated=3 posted=0]" in t
    assert "- semantic-dedup ok 1000ms [scanned=16383 deleted=0]" in t
    assert "- stack-backup FAILED 1000ms -- manifest missing" in t
    assert "- index-refresh ok 1000ms\n" in t, "no work object, no brackets"
    assert "degraded=dream" in t and "pool 71.4% usage 12%" in t and "ONLINE" in t


def test_summary_health_line_survives_an_old_stamp(tmp_path):
    t = _summary(tmp_path, [_row("dream")], health={"ok": True, "stale_steps": [], "pool": {"used_pct": 78.0}, "usage": {"used_percent": 2}})
    assert "- health ok=True stale=[] " in t and "pool 78.0% usage 2%" in t


def test_summary_marks_a_receipt_without_status_by_its_ok_flag(tmp_path):
    """Receipts written before the outcome contract carry no status/work."""
    legacy = {"ts": "2099-01-01T08:00:00Z", "step": "dream", "ok": True, "exit": 0, "duration_ms": 5, "receipt_id": "x", "note": ""}
    t = _summary(tmp_path, [legacy])
    assert "- dream ok 5ms" in t


def test_red_night_receipt_note_carries_the_verdict_line(tmp_path):
    """Run through ams-step: the stamp exits 2 with empty stderr, so the receipt note is its LAST stdout line,
    which must be the verdict (with the step names), not the legacy detail line."""
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    canned = tmp_path / "canned.json"
    canned.write_text(json.dumps(dict(BASE, ok=False, failed_steps=[{"step": "wiki-index", "ts": "t", "note": "n"}])), encoding="utf-8")
    fake = bindir / "curl"
    fake.write_text('#!/bin/bash\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n' f'cp "{canned}" "$out"\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}")
    env.pop("MEM0_URL", None)
    r = subprocess.run([BASH, str(SCRIPTS / "ams-step.sh"), "health-stamp", BASH, str(SCRIPTS / "ams-health-stamp.sh")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2
    row = json.loads((home / ".mem0" / "maintenance" / "receipts.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["ok"] is False and row["status"] == "failed"
    assert row["note"] == "health ok=False failed=wiki-index degraded=- pool 71.4% ONLINE"
