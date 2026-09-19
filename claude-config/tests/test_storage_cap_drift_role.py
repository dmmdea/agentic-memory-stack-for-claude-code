"""1.28.4 (register P5-11): the drift heartbeat is the brain's.

`retrieval-drift-state.json` is written by the drift guard inside the brain's dream. A replica still
carries the copy from before the authority cutover, alarm and all, so the banner printed a permanent
"DRIFT GUARD DEAD" there. The banner now reads the file only when `~/.mem0/role` is `brain` (or
absent, which is the brain). Same harness as the other storage-cap tests: the REAL script under bash
with HOME pointed at a fixture dir.
"""
from __future__ import annotations

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


def _write_drift_state(home: Path, *, role: str | None) -> None:
    mem0 = home / ".mem0"
    mem0.mkdir(parents=True, exist_ok=True)
    (mem0 / "retrieval-drift-state.json").write_text(json.dumps({
        "hwm": 7, "consecutive_snapshot_failures": 12, "alarm": True,
        "before_retrievable": 7, "after_retrievable": 6, "n_total": 7,
        "last_compare_ts": "2026-09-14T13:32:29+00:00",
    }), encoding="utf-8")
    if role is not None:
        (mem0 / "role").write_text(role + "\n", encoding="utf-8")


def test_replica_never_prints_the_drift_lines(tmp_path):
    _write_drift_state(tmp_path, role="replica")
    out = _run_with_fake_home(tmp_path)
    assert "DRIFT GUARD DEAD" not in out
    assert "DRIFT ALARM" not in out


def test_client_never_prints_the_drift_lines(tmp_path):
    _write_drift_state(tmp_path, role="client")
    out = _run_with_fake_home(tmp_path)
    assert "DRIFT" not in out


def test_brain_still_prints_them(tmp_path):
    _write_drift_state(tmp_path, role="brain")
    out = _run_with_fake_home(tmp_path)
    assert "DRIFT GUARD DEAD (>=2 snapshot failures)" in out
    assert "DRIFT ALARM standing (7/7 retrievable, hwm 7)" in out


def test_absent_role_file_is_the_brain(tmp_path):
    _write_drift_state(tmp_path, role=None)
    out = _run_with_fake_home(tmp_path)
    assert "DRIFT GUARD DEAD (>=2 snapshot failures)" in out
