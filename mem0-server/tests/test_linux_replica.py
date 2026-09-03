# mem0-server/tests/test_linux_replica.py
"""The Linux replica role: offline-watcher.py (state machine, authority resolution, plan),
restore-replica.sh (One-Brain guards, run for real in a scratch HOME), install/linux-replica.sh
(argument contract, dry-run writes nothing, single-source parity with the WSL installer)."""
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHER = REPO_ROOT / "scripts" / "travel" / "offline-watcher.py"
RESTORE = REPO_ROOT / "scripts" / "travel" / "restore-replica.sh"
INSTALLER = REPO_ROOT / "install" / "linux-replica.sh"
WSL_INSTALLER = REPO_ROOT / "install" / "1-wsl-services.sh"
BASH = shutil.which("bash")


@pytest.fixture(scope="module")
def ow():
    spec = importlib.util.spec_from_file_location("offline_watcher_ut", WATCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- watcher: pure logic
def test_three_down_ticks_go_offline_two_up_ticks_go_online(ow):
    """Parity with offline-watcher.ps1's Step-OfflineState (N=3, M=2) and its flap debounce."""
    s = {"mode": "online", "consecutive_down": 0, "consecutive_up": 0}
    s = ow.step_offline_state(s, False); assert (s["mode"], s["transition"]) == ("online", "none")
    s = ow.step_offline_state(s, False); assert (s["mode"], s["transition"]) == ("online", "none")
    s = ow.step_offline_state(s, False); assert (s["mode"], s["transition"]) == ("offline", "go_offline")
    s = ow.step_offline_state(s, False); assert (s["mode"], s["transition"]) == ("offline", "none")
    s = ow.step_offline_state(s, True);  assert (s["mode"], s["transition"]) == ("offline", "none")
    s = ow.step_offline_state(s, True);  assert (s["mode"], s["transition"]) == ("online", "go_online")


def test_a_single_up_tick_resets_the_down_count(ow):
    s = {"mode": "online", "consecutive_down": 2, "consecutive_up": 0}
    s = ow.step_offline_state(s, True)
    assert s["consecutive_down"] == 0 and s["mode"] == "online"
    s = ow.step_offline_state(s, False)
    assert s["consecutive_down"] == 1 and s["mode"] == "online"


@pytest.mark.parametrize("url,expected_local", [
    ("http://127.0.0.1:18791", True), ("http://localhost:18791", True), ("http://x.localhost", True),
    ("http://0.0.0.0:18791", True), ("http://[::1]:18791", True), ("", True), ("not a url", True),
    ("http://brain-host:18791", False), ("https://brain.example.net", False), ("http://203.0.113.9:18791", False),
])
def test_is_local_url_fails_closed(ow, url, expected_local):
    assert ow.is_local_url(url) is expected_local


def test_resolve_authority_precedence(ow, tmp_path):
    f = tmp_path / "authority-url"
    f.write_text("# comment\nhttp://brain-host:18791/\n", encoding="utf-8")
    assert ow.resolve_authority("http://explicit:1/", None, f) == "http://explicit:1"
    assert ow.resolve_authority("", "http://env-host:2", f) == "http://env-host:2"
    assert ow.resolve_authority("", "http://127.0.0.1:2", f) == "http://brain-host:18791"   # local env ignored
    f.write_text("http://127.0.0.1:18791\n", encoding="utf-8")
    assert ow.resolve_authority("", None, f) is None                                     # local file refused
    assert ow.resolve_authority("", None, tmp_path / "missing") is None


def test_plan_actions(ow):
    assert ow.plan_actions({"mode": "offline", "transition": "go_offline"}, False, 1.0) == ["start_services"]
    assert ow.plan_actions({"mode": "online", "transition": "go_online"}, True, 1.0) == ["drain_outbox", "stop_services"]
    assert ow.plan_actions({"mode": "online", "transition": "none"}, True, 30.0) == ["refresh_replica"]
    assert ow.plan_actions({"mode": "online", "transition": "none"}, True, None) == ["refresh_replica"]
    assert ow.plan_actions({"mode": "online", "transition": "none"}, True, 1.0) == []
    assert ow.plan_actions({"mode": "online", "transition": "none"}, False, 30.0) == []   # unreachable: no refresh
    assert ow.plan_actions({"mode": "offline", "transition": "none"}, False, 30.0) == []
    # a failed refresh backs off: an attempt 10 minutes ago blocks the retry, one 7 hours ago allows it
    assert ow.plan_actions({"mode": "online", "transition": "none"}, True, None, attempt_age_h=0.17) == []
    assert ow.plan_actions({"mode": "online", "transition": "none"}, True, None, attempt_age_h=7.0) == ["refresh_replica"]


def test_dry_run_tick_refuses_without_a_remote_authority(tmp_path):
    r = subprocess.run(["python3" if shutil.which("python3") else "python", str(WATCHER), "--dry-run", "--home", str(tmp_path)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "refusing" in r.stderr


def test_dry_run_tick_probes_and_persists_nothing(tmp_path):
    (tmp_path / ".mem0").mkdir()
    r = subprocess.run(["python3" if shutil.which("python3") else "python", str(WATCHER), "--dry-run", "--home", str(tmp_path),
                        "--authority", "http://192.0.2.1:18791"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    line = json.loads(r.stdout.strip())
    assert line["reachable"] is False and line["mode"] == "online" and line["actions"] == []
    assert not (tmp_path / ".mem0" / "offline-mode.json").exists()


# ---------------------------------------------------------------- shell scripts
pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


def _scratch_home(tmp_path, role="replica", authority="http://brain-host:18791"):
    home = tmp_path / "home"; (home / ".mem0").mkdir(parents=True, exist_ok=True)
    (home / ".mem0" / "role").write_text(role + "\n", encoding="utf-8")
    (home / ".mem0" / "authority-url").write_text(authority + "\n", encoding="utf-8")
    bindir = tmp_path / "bin"; bindir.mkdir(exist_ok=True)
    for name in ("claude", "ssh", "systemctl", "jq", "loginctl", "curl"):
        s = bindir / name
        s.write_text("#!/usr/bin/env bash\necho stub-$0\nexit 0\n", encoding="utf-8")
        s.chmod(s.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, HOME=str(home), PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return home, env


@pytestmark_bash
def test_scripts_parse():
    for s in (RESTORE, INSTALLER):
        r = subprocess.run([BASH, "-n", str(s)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (s.name, r.stderr)


@pytestmark_bash
def test_restore_refuses_on_a_brain_and_on_a_loopback_authority(tmp_path):
    """The One-Brain guard, exercised for real: restoring on a brain-role box would overwrite the
    live store with a day-old snapshot. Both refusals must fire BEFORE any ssh/systemctl call."""
    home, env = _scratch_home(tmp_path, role="brain")
    r = subprocess.run([BASH, str(RESTORE), "--dry-run"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0 and "One-Brain" in r.stderr
    home, env = _scratch_home(tmp_path, role="replica", authority="http://127.0.0.1:18791")
    r = subprocess.run([BASH, str(RESTORE), "--dry-run"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0 and "REMOTE Brain" in r.stderr
    home, env = _scratch_home(tmp_path)   # replica + remote authority but no replica.env yet
    r = subprocess.run([BASH, str(RESTORE), "--dry-run"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0 and "replica.env" in r.stderr


@pytestmark_bash
def test_restore_stops_services_on_every_failure_path():
    """The EXIT trap is the dormancy guarantee: a restore that dies after starting qdrant must
    not leave it running while the Brain is reachable. Pin the trap and its success-only escape."""
    sh = RESTORE.read_text(encoding="utf-8")
    assert 'trap \'[ "$LEAVE_RUNNING" = 1 ] && [ "$RESTORED" = 1 ] || systemctl --user stop mem0.service qdrant.service' in sh
    assert sh.index("trap '") < sh.index("systemctl --user start qdrant.service"), "the trap must be armed before any service starts"
    assert "
RESTORED=1
" in sh and sh.index("log_line ok") < sh.index("
RESTORED=1
")


@pytestmark_bash
def test_installer_contract_and_dry_run(tmp_path):
    home, env = _scratch_home(tmp_path)
    r = subprocess.run([BASH, str(INSTALLER), "--authority", "http://brain-host:18791", "--dry-run"], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120)
    assert r.returncode != 0 and "--brain-ssh" in r.stderr
    r = subprocess.run([BASH, str(INSTALLER), "--authority", "http://127.0.0.1:18791", "--brain-ssh", "brain", "--dry-run"], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120)
    assert r.returncode != 0 and "REMOTE Brain" in r.stderr
    r = subprocess.run([BASH, str(INSTALLER), "--authority", "http://brain-host:18791", "--brain-ssh", "brain", "--brain-wsl", "distro:user", "--user-id", "tenant-a", "--dry-run"],
                       capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120)
    assert r.returncode == 0, r.stderr + r.stdout
    for step in ("[1] thin client", "[2] role=replica", "[3] python", "[4] qdrant", "[5] mem0 server", "[6] systemd user units", "[7] first restore"):
        assert step in r.stdout, step
    assert "tenant tenant-a" in r.stdout and "WSL distro:user" in r.stdout
    assert not (home / "apps").exists() and not (home / "qdrant-server").exists() and not (home / ".config").exists()
    assert not (home / ".mem0" / "replica.env").exists()


def test_installer_reads_server_lists_from_the_wsl_installer_not_a_copy():
    """MEM0_MODULES, QDRANT_VERSION and the mem0 pip line have one owner: install/1-wsl-services.sh.
    A second copy would drift the day a module is added there (the class of bug that crash-looped
    fresh installs on ModuleNotFoundError)."""
    sh = INSTALLER.read_text(encoding="utf-8")
    for key in ("MEM0_MODULES=", "QDRANT_VERSION=", "pip install --quiet 'mem0ai"):
        assert key in sh
    wsl = WSL_INSTALLER.read_text(encoding="utf-8")
    assert re.search(r'^MEM0_MODULES="[^"]+"$', wsl, re.M)
    assert re.search(r"^QDRANT_VERSION=\S+$", wsl, re.M)
    assert re.search(r"pip install --quiet 'mem0ai", wsl)
    assert not re.search(r'^MEM0_MODULES="[a-z]', sh, re.M), "the replica installer must not carry its own module list"


def test_units_and_scripts_ship_together():
    for f in ("systemd/offline-watcher.service", "systemd/offline-watcher.timer", "scripts/travel/restore-replica.sh", "scripts/travel/offline-watcher.py"):
        assert (REPO_ROOT / f).is_file(), f
    unit = (REPO_ROOT / "systemd" / "offline-watcher.service").read_text(encoding="utf-8")
    assert "offline-watcher.py" in unit and "%h/apps/mem0-client/.venv/bin/python" in unit
    sh = INSTALLER.read_text(encoding="utf-8")
    assert "systemctl --user disable mem0.service qdrant.service" in sh, "the local store must be dormant while online"
    assert "systemctl --user enable --now offline-watcher.timer" in sh
