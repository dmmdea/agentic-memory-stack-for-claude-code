# mem0-server/tests/test_ams_chain.py
"""One nightly chain on the authority (spec §4): timer → target, steps hang off it with
WantedBy=/After= (never Requires=), each step runs through ams-step.sh which writes a receipt."""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = REPO_ROOT / "systemd"
SCRIPTS = REPO_ROOT / "scripts" / "wsl"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

STEPS = ["stack-backup", "health-stamp", "rtcwake"]


def test_chain_units_exist_and_never_require():
    timer = (SYSTEMD / "ams-nightly.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03:00:00" in timer and "Persistent=true" in timer and "OnBootSec=15min" in timer
    assert "Unit=ams-nightly.target" in timer
    for s in STEPS:
        text = (SYSTEMD / f"ams-step-{s}.service").read_text(encoding="utf-8")
        assert "Requires=" not in text
        assert "WantedBy=ams-nightly.target" in text
        assert "PartOf=ams-nightly.target" in text
        assert "ams-step.sh " in text and (" " + s + " ") in text
    assert "After=ams-step-stack-backup.service" in (SYSTEMD / "ams-step-health-stamp.service").read_text(encoding="utf-8")
    assert "After=ams-step-health-stamp.service" in (SYSTEMD / "ams-step-rtcwake.service").read_text(encoding="utf-8")
    for f in ("ams-step.sh", "ams-rtcwake-arm.sh", "ams-health-stamp.sh"):
        r = subprocess.run([BASH, "-n", str(SCRIPTS / f)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr


def _step(tmp_path, args):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([BASH, str(SCRIPTS / "ams-step.sh"), *args], capture_output=True, text=True, env=env, timeout=60)
    rp = home / ".mem0" / "maintenance" / "receipts.jsonl"
    rows = [json.loads(ln) for ln in rp.read_text(encoding="utf-8").splitlines() if ln.strip()] if rp.exists() else []
    return r, rows, home


def test_step_writes_a_receipt_with_duration_and_exit(tmp_path):
    r, rows, _ = _step(tmp_path, ["demo", "bash", "-c", "sleep 0.2; echo hi"])
    assert r.returncode == 0, r.stderr
    assert rows[-1]["step"] == "demo" and rows[-1]["ok"] is True and rows[-1]["duration_ms"] >= 150
    assert re.fullmatch(r"demo-\d{8}T\d{6}Z-[0-9a-f]{6}", rows[-1]["receipt_id"])
    r, rows, _ = _step(tmp_path, ["demo", "bash", "-c", "echo bad >&2; exit 23"])
    assert r.returncode == 23
    assert rows[-1]["ok"] is False and "bad" in rows[-1]["note"] and rows[-1]["exit"] == 23


def test_receipt_note_keeps_the_tail_of_a_long_stderr(tmp_path):
    """The receipt note is the only diagnostic of an unattended 3 am failure; a stderr of
    thousands of lines must still leave its last 400 bytes in the note (no substitution race)."""
    r, rows, _ = _step(tmp_path, ["demo", "bash", "-c", "for i in $(seq 1 5000); do echo line-$i >&2; done; exit 7"])
    assert r.returncode == 7
    assert "line-5000" in rows[-1]["note"] and rows[-1]["ok"] is False
    assert "line-5000" in r.stderr, "stderr is still echoed to the caller (the journal)"


def test_guard_skips_a_second_run_inside_20h(tmp_path):
    r, rows, home = _step(tmp_path, ["--guard", "demo", "true"])
    assert r.returncode == 0 and rows[-1]["ok"] is True
    r, rows, _ = _step(tmp_path, ["--guard", "demo", "bash", "-c", "exit 9"])
    assert r.returncode == 0 and rows[-1]["note"].startswith("guard: chain succeeded")
    stamp = home / ".mem0" / "maintenance" / "last-chain-success"
    fresh = int(stamp.read_text())
    # 10 h old: still inside the 20 h window -> still a no-op (pins the window's width, not just its existence)
    stamp.write_text(str(fresh - 10 * 3600))
    r, rows, _ = _step(tmp_path, ["--guard", "demo", "bash", "-c", "exit 9"])
    assert r.returncode == 0 and rows[-1]["note"].startswith("guard: chain succeeded")
    # 21 h old: past the window -> the step runs and its exit code is the step's
    stamp.write_text(str(fresh - 21 * 3600))
    r, rows, _ = _step(tmp_path, ["--guard", "demo", "bash", "-c", "exit 9"])
    assert r.returncode == 9


def test_rtcwake_arm_computes_next_0245():
    r = subprocess.run([BASH, str(SCRIPTS / "ams-rtcwake-arm.sh"), "--dry-run", "02:45"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    epoch = int(r.stdout.strip().split()[-1])
    assert 0 < epoch - int(time.time()) <= 24 * 3600
