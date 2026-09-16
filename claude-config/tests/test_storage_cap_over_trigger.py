"""P4-1c (2026-09-16): the session-start line reports the G7 metric.

Design section 9: per store, "hours over trigger without an applied decision", alarm at 24 h,
read at session start by the one observer that does not depend on the authority being up. The
store lint (`ams-store lint --summary-out`) writes it as `stores[].over_trigger_hours` in
lint-summary.json; the banner prints it quiet below 24 h and as an ALARM line at or above.

These tests run the REAL script under bash with HOME pointed at a fixture dir (every path in the
script is $HOME-derived); the summary file is the only fixture. Requires bash - the suite runs in
the WSL gate env and on the ubuntu CI runner.
"""
from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "storage-cap-check.sh"


def _run_with_fake_home(home: Path) -> str:
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "CLAUDE_CWD": "/tmp"},
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, f"script must always exit 0: {res.stderr}"
    return res.stdout


def _write_summary(home: Path, stores, *, age_hours: float = 0.0, actionable: int = 0) -> None:
    state = home / ".claude" / "state" / "automemory"
    state.mkdir(parents=True, exist_ok=True)
    gen = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=age_hours)
    (state / "lint-summary.json").write_text(json.dumps({
        "generated_at": gen.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stores": [{"workspace": w, "bytes": 1000, "lines": 10, "entries": 3, "files": 3,
                    "over_trigger": h is not None and h > 0, "skip_streak": 0, "last_status": "no-op",
                    "over_trigger_hours": h} for w, h in stores],
        "findings": [],
        "counts": {"total": 0, "actionable": actionable},
        "last_receipt_age_hours": 0,
    }), encoding="utf-8")


def test_quiet_line_below_24h(tmp_path):
    _write_summary(tmp_path, [("ws-a", 4.7), ("ws-b", None), ("ws-c", 0)])
    out = _run_with_fake_home(tmp_path)
    assert "auto-memory G7: over trigger ws-a 4.7h (alarm at 24h; the hub judge decides nightly)" in out
    assert "ALARM" not in out
    assert "ws-b" not in out and "ws-c" not in out, "stores at 0 or null are not listed"


def test_alarm_line_at_or_above_24h_lists_worst_first(tmp_path):
    _write_summary(tmp_path, [("ws-a", 4.7), ("ws-b", 31.5), ("ws-c", 24)])
    out = _run_with_fake_home(tmp_path)
    assert "AUTO-MEMORY G7 ALARM: over trigger without an applied decision for 31.5h (ws-b 31.5h, ws-c 24h, ws-a 4.7h)" in out


def test_silent_when_no_store_is_over_trigger(tmp_path):
    _write_summary(tmp_path, [("ws-a", None), ("ws-b", 0)])
    out = _run_with_fake_home(tmp_path)
    assert "G7" not in out


def test_stale_summary_reports_staleness_not_the_metric(tmp_path):
    """A lint that stopped completing must not present an old clock as this morning's."""
    _write_summary(tmp_path, [("ws-a", 40)], age_hours=13)
    out = _run_with_fake_home(tmp_path)
    assert "auto-memory lint: STALE (13" in out
    assert "the store lint has not completed since then" in out
    assert "G7" not in out


def test_bash_syntax():
    res = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
