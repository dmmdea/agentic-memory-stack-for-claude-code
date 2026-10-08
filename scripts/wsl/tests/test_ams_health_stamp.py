"""ams-health-stamp.sh and ams-morning-summary.sh print the honest health verdict.

The stamp is the chain's LAST reading step: it exits non-zero when a step's latest run failed, the
pool is unhealthy or the write path is failing, so a bad night ends in a red unit instead of a green
`ok=False` line nobody reads. The morning summary lists degraded steps with their notes and the work
counts a step reported. Both name a failing write path on their health line and change no byte of a
healthy one."""
import json
import os
import re
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


def _home_env(home, **extra):
    """The child env with the home redirected on every platform: HOME alone leaves a child whose `~` is
    read from USERPROFILE (or HOMEDRIVE+HOMEPATH) writing into the real profile."""
    h = str(home)
    drive, tail = os.path.splitdrive(h)
    return dict(os.environ, HOME=h, USERPROFILE=h, HOMEDRIVE=drive, HOMEPATH=tail, **extra)


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
    env = _home_env(home, PATH=f"{bindir}:{os.environ['PATH']}")
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


def test_acked_degraded_pool_keeps_the_stamp_green_and_says_so(tmp_path):
    """Planned maintenance: the server clears health_alarm while the dated ack is active, so the stamp reads
    that flag (not the raw health) and ends green; the line names the ack so nobody reads it as ONLINE."""
    ack = {"state": "DEGRADED", "until": "2026-10-06", "active": True}
    p = dict(BASE, pool=dict(BASE["pool"], health="DEGRADED", health_alarm=False, health_ack=ack))
    r, _ = _stamp(tmp_path, p)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == "health ok=True failed=- degraded=- pool 71.4% DEGRADED (acked until 2026-10-06)"


def test_an_expired_ack_leaves_the_stamp_red(tmp_path):
    ack = {"state": "DEGRADED", "until": "2026-09-01", "active": False, "reason": "expired"}
    p = dict(BASE, ok=False, pool=dict(BASE["pool"], health="DEGRADED", health_alarm=True, health_ack=ack))
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


# The write path: /health/maintenance folds write_path.ok into `ok`, so a night whose only fault is a write
# path that failed its last real write printed `health ok=False failed=- degraded=- ...` with no cause and
# exited 0: a green step over an ok=False line, the exact shape this script exists to end.
WP_RED = {"ok": False, "last_ok_at": None, "last_error_at": "2099-01-01T04:05:00+00:00",
          "last_error": "503 upstream", "errors_1h": 4, "writes_1h": 4}
WP_OK = {"ok": True, "last_ok_at": "2099-01-01T04:06:00+00:00", "last_error_at": "2099-01-01T04:05:00+00:00",
         "last_error": "503 upstream", "errors_1h": 4, "writes_1h": 5}
GREEN_LINE = "health ok=True failed=- degraded=- pool 71.4% ONLINE"


def test_a_failing_write_path_alone_turns_the_stamp_red_and_names_it(tmp_path):
    r, stamp = _stamp(tmp_path, dict(BASE, ok=False, write_path=WP_RED))
    assert r.returncode == 2, "a broken write path IS a red night"
    assert r.stdout.splitlines()[-1] == "health ok=False failed=- degraded=- pool 71.4% ONLINE write-path 503 upstream"
    assert stamp.exists(), "the stamp file is written before the verdict exits"


def test_a_recovered_write_path_leaves_the_stamp_line_byte_identical(tmp_path):
    """ok:true keeps last_error on record; only ok:false is a fault."""
    r, _ = _stamp(tmp_path, dict(BASE, write_path=WP_OK))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == GREEN_LINE
    r0, _ = _stamp(tmp_path / "plain", BASE)
    assert r.stdout == r0.stdout, "a healthy write path changes not one byte of the output"


@pytest.mark.parametrize("wp", [{"ok": None, "note": "write-path reader failed"}, "garbage", ["ok", False], 7, {}],
                         ids=["unknown", "string", "list", "number", "empty"])
def test_a_write_path_that_is_unknown_or_malformed_is_silent_and_not_red(tmp_path, wp):
    """`ok:null` is the server saying it could not read the tracker: fail-open, loud in the value only."""
    r, _ = _stamp(tmp_path, dict(BASE, write_path=wp))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == GREEN_LINE


def test_the_write_path_is_named_beside_a_failed_step_and_an_acked_pool(tmp_path):
    ack = {"state": "DEGRADED", "until": "2026-10-06", "active": True}
    p = dict(BASE, ok=False, write_path=WP_RED, failed_steps=[{"step": "wiki-index", "ts": "t", "note": "n"}],
             pool=dict(BASE["pool"], health="DEGRADED", health_alarm=False, health_ack=ack))
    r, _ = _stamp(tmp_path, p)
    assert r.returncode == 2
    assert r.stdout.splitlines()[-1] == ("health ok=False failed=wiki-index degraded=- pool 71.4% DEGRADED "
                                         "(acked until 2026-10-06) write-path 503 upstream")


def test_a_failing_write_path_without_a_reason_still_says_so(tmp_path):
    r, _ = _stamp(tmp_path, dict(BASE, ok=False, write_path={"ok": False, "last_error": None}))
    assert r.returncode == 2 and r.stdout.splitlines()[-1].endswith(" write-path failing")


def test_the_verdict_stays_one_line_whatever_the_reason_holds(tmp_path):
    r, _ = _stamp(tmp_path, dict(BASE, ok=False, write_path=dict(WP_RED, last_error="503 up\nstream\t now")))
    assert r.returncode == 2
    assert len(r.stdout.splitlines()) == 2, "the first line, then exactly one verdict line"
    assert r.stdout.splitlines()[-1].endswith("write-path 503 up stream now")


def _summary(tmp_path, receipts, health=None):
    home = tmp_path / "home"
    d = home / ".mem0" / "maintenance"
    d.mkdir(parents=True, exist_ok=True)
    (d / "receipts.jsonl").write_text("\n".join(json.dumps(x) for x in receipts) + "\n", encoding="utf-8")
    if health is not None:
        (d / "health-maintenance.json").write_text(json.dumps(health), encoding="utf-8")
    env = _home_env(home, AMS_SUMMARY_NOW="2099-01-01T09:00:00Z")
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


def test_summary_names_an_active_pool_ack(tmp_path):
    pool = dict(BASE["pool"], health="DEGRADED", health_alarm=False,
                health_ack={"state": "DEGRADED", "until": "2026-10-06", "active": True})
    t = _summary(tmp_path, [_row("dream")], health=dict(BASE, pool=pool))
    assert "pool-health DEGRADED (acked until 2026-10-06)" in t
    (tmp_path / "second").mkdir()
    t2 = _summary(tmp_path / "second", [_row("dream")], health=BASE)   # the summary appends: a fresh HOME
    assert "pool-health ONLINE\n" in t2 and "acked" not in t2


def test_summary_health_line_survives_an_old_stamp(tmp_path):
    t = _summary(tmp_path, [_row("dream")], health={"ok": True, "stale_steps": [], "pool": {"used_pct": 78.0}, "usage": {"used_percent": 2}})
    assert "- health ok=True stale=[] " in t and "pool 78.0% usage 2%" in t


def test_summary_names_a_failing_write_path_and_only_then(tmp_path):
    t = _summary(tmp_path, [_row("dream")], health=dict(BASE, ok=False, write_path=WP_RED))
    assert "pool-health ONLINE write-path 503 upstream\n" in t
    (tmp_path / "second").mkdir()
    t2 = _summary(tmp_path / "second", [_row("dream")], health=dict(BASE, write_path=WP_OK))
    assert "pool-health ONLINE\n" in t2 and "write-path" not in t2, "a healthy line is byte-identical to before"
    (tmp_path / "third").mkdir()
    t3 = _summary(tmp_path / "third", [_row("dream")], health=dict(BASE, write_path={"ok": None, "note": "write-path reader failed"}))
    assert "pool-health ONLINE\n" in t3 and "write-path" not in t3, "an unreadable tracker is not a fault"


def test_summary_writes_the_write_path_after_an_active_pool_ack(tmp_path):
    pool = dict(BASE["pool"], health="DEGRADED", health_alarm=False,
                health_ack={"state": "DEGRADED", "until": "2026-10-06", "active": True})
    t = _summary(tmp_path, [_row("dream")], health=dict(BASE, ok=False, pool=pool, write_path=WP_RED))
    assert "pool-health DEGRADED (acked until 2026-10-06) write-path 503 upstream\n" in t


def test_summary_survives_a_malformed_write_path_and_a_missing_reason(tmp_path):
    t = _summary(tmp_path, [_row("dream")], health=dict(BASE, write_path="garbage"))
    assert "pool-health ONLINE\n" in t and "write-path" not in t
    (tmp_path / "second").mkdir()
    t2 = _summary(tmp_path / "second", [_row("dream")], health=dict(BASE, ok=False, write_path={"ok": False}))
    assert "pool-health ONLINE write-path failing\n" in t2


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
    env = _home_env(home, PATH=f"{bindir}:{os.environ['PATH']}")
    env.pop("MEM0_URL", None)
    r = subprocess.run([BASH, str(SCRIPTS / "ams-step.sh"), "health-stamp", BASH, str(SCRIPTS / "ams-health-stamp.sh")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2
    row = json.loads((home / ".mem0" / "maintenance" / "receipts.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["ok"] is False and row["status"] == "failed"
    assert row["note"] == "health ok=False failed=wiki-index degraded=- pool 71.4% ONLINE"


def test_red_night_receipt_note_names_a_failing_write_path(tmp_path):
    """The same run with only the write path red: the chain's last reading step fails, and the receipt note
    (the stamp's last stdout line) says why instead of a bare `ok=False`."""
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    canned = tmp_path / "canned.json"
    canned.write_text(json.dumps(dict(BASE, ok=False, write_path=WP_RED)), encoding="utf-8")
    fake = bindir / "curl"
    fake.write_text('#!/bin/bash\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n' f'cp "{canned}" "$out"\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = _home_env(home, PATH=f"{bindir}:{os.environ['PATH']}")
    env.pop("MEM0_URL", None)
    r = subprocess.run([BASH, str(SCRIPTS / "ams-step.sh"), "health-stamp", BASH, str(SCRIPTS / "ams-health-stamp.sh")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2
    row = json.loads((home / ".mem0" / "maintenance" / "receipts.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["ok"] is False and row["status"] == "failed"
    assert row["note"] == "health ok=False failed=- degraded=- pool 71.4% ONLINE write-path 503 upstream"


# ---- capture (audit CRIT-01): a stalled capture is named on the verdict line, never red -------------------------
CAP_STALLED = {"state": "stalled", "stalled": True, "success_at": "2099-01-01T00:00:00+00:00", "success_age_h": 120.0,
               "activity_at": "2099-01-05T04:00:00+00:00", "activity_age_h": 0.5, "quiet_after_h": 48.0, "stalled_after_h": 96.0}


def test_a_stalled_capture_is_named_on_the_stamp_line_and_the_stamp_stays_green(tmp_path):
    r, _ = _stamp(tmp_path, dict(BASE, capture=CAP_STALLED))
    assert r.returncode == 0, "the PCs being off is not a chain fault: a capture note never reddens the stamp"
    assert r.stdout.splitlines()[-1] == GREEN_LINE + " capture stalled 120.0h"


@pytest.mark.parametrize("cap", [{"state": "ok", "stalled": False}, {"state": "quiet", "stalled": False},
                                 {"state": "unknown", "stalled": False, "note": "capture reader failed"}, "garbage", ["x"], 7, {}],
                         ids=["ok", "quiet", "unknown", "string", "list", "number", "empty"])
def test_any_other_capture_reading_leaves_the_stamp_line_byte_identical(tmp_path, cap):
    r, _ = _stamp(tmp_path, dict(BASE, capture=cap))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[-1] == GREEN_LINE


def test_a_stalled_capture_rides_beside_a_failing_write_path(tmp_path):
    r, _ = _stamp(tmp_path, dict(BASE, ok=False, write_path=WP_RED, capture=CAP_STALLED))
    assert r.returncode == 2
    assert r.stdout.splitlines()[-1].endswith("write-path 503 upstream capture stalled 120.0h")


def test_summary_names_a_stalled_capture_and_only_then(tmp_path):
    t = _summary(tmp_path, [_row("dream")], health=dict(BASE, capture=CAP_STALLED))
    assert "pool-health ONLINE capture stalled 120.0h\n" in t
    (tmp_path / "second").mkdir()
    t2 = _summary(tmp_path / "second", [_row("dream")], health=dict(BASE, capture={"state": "quiet", "stalled": False}))
    assert "pool-health ONLINE\n" in t2 and "capture" not in t2, "a quiet capture is not a fault"


# ---- CM-01: the summary quotes the stamp, so the stamp runs first -------------------------------------------------
UNITS = SCRIPTS.parents[1] / "systemd"


def _after_graph():
    g = {}
    for p in UNITS.glob("ams-step-*.service"):
        step = p.name[len("ams-step-"):-len(".service")]
        m = re.search(r"^After=(.*)$", p.read_text(encoding="utf-8"), re.M)
        g[step] = {u[len("ams-step-"):-len(".service")] for u in (m.group(1).split() if m else []) if u.startswith("ams-step-")}
    return g


def _runs_after(g, a, b):
    seen, todo = set(), [a]
    while todo:
        for dep in g.get(todo.pop(), ()):
            if dep == b:
                return True
            if dep not in seen:
                seen.add(dep)
                todo.append(dep)
    return False


def test_the_morning_summary_runs_after_the_health_stamp_it_quotes():
    """The summary reads health-maintenance.json, which only the stamp writes. Live 2026-10-08: the summary line (03:04:50.33)
    came from the stamp BEFORE tonight's (03:04:50.65), so a night that recovered still read `ok=False degraded=wiki-index`,
    and a bad night reads green until the next one."""
    g = _after_graph()
    assert _runs_after(g, "morning-summary", "health-stamp")
    assert not _runs_after(g, "health-stamp", "morning-summary")
    assert _runs_after(g, "health-stamp", "pcloud-copy"), "the stamp still follows the last real step"
    assert _runs_after(g, "rtcwake", "morning-summary") and _runs_after(g, "rtcwake", "health-stamp"), "rtcwake stays last"
