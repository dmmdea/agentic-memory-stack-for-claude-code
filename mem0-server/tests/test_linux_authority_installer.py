# mem0-server/tests/test_linux_authority_installer.py
"""install/linux-authority.sh — the native Linux AUTHORITY installer (spec §4).

Runs the script itself (bash) in a scratch HOME. The native install must render a unit set
that carries none of the WSL-only lines (the DPAPI ExecStartPre, /mnt/c, cmd.exe,
powershell.exe), must load both secrets through systemd-creds, and must never bind 0.0.0.0.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "install" / "linux-authority.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

WSL_ONLY = re.compile(r"/mnt/c|cmd\.exe|powershell\.exe|dpapi-fetch-key\.sh|/run/WSL")


def _run(args, tmp_path, secrets=True):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    sec = tmp_path / "secrets"
    sec.mkdir(exist_ok=True)
    if secrets:
        (sec / "ams-api-key.cred").write_bytes(b"x" * 64)
        (sec / "ams-canonical-key.cred").write_bytes(b"y" * 64)
    env = dict(os.environ)
    env["HOME"] = str(home)
    r = subprocess.run([BASH, str(SCRIPT), *args, "--secrets-dir", str(sec)],
                       capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120)
    return r, home


def test_script_parses():
    r = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("ip", ["0.0.0.0", "::", "127.0.0.1", "localhost", "not-an-ip"])
def test_refuses_a_wildcard_loopback_or_malformed_bind(tmp_path, ip):
    r, home = _run(["--bind-ip", ip, "--dry-run"], tmp_path)
    assert r.returncode != 0
    assert "bind" in r.stderr.lower()
    assert not (home / ".mem0").exists()


def test_refuses_when_a_cred_file_is_missing(tmp_path):
    r, home = _run(["--bind-ip", "192.0.2.9", "--dry-run"], tmp_path, secrets=False)
    assert r.returncode != 0
    assert "ams-api-key.cred" in r.stderr
    assert not (home / ".mem0").exists()


def test_dry_run_writes_nothing(tmp_path):
    r, home = _run(["--bind-ip", "192.0.2.9", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "[dry-run]" in r.stdout
    assert not (home / ".mem0").exists()
    assert not (home / ".config").exists()


def test_render_only_unit_set_is_native(tmp_path):
    out = tmp_path / "render"
    r, home = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (home / ".mem0").exists()
    files = {p.relative_to(out).as_posix(): p.read_text(encoding="utf-8")
             for p in out.rglob("*") if p.is_file()}
    assert "mem0.service" in files and "qdrant.service" in files and "l10-audit.timer" in files
    assert "mem0.service.d/native.conf" in files
    for name, text in files.items():
        assert not WSL_ONLY.search(text), f"{name} carries a WSL-only line"
        assert not re.search(r"__[A-Z_]+__", text), f"{name} has an unresolved sentinel"
    conf = files["mem0.service.d/native.conf"]
    assert "ExecStartPre=\n" in conf, "the drop-in must CLEAR the WSL ExecStartPre before adding its own"
    assert "wait-for-bind.sh 192.0.2.9" in conf
    assert conf.count("LoadCredentialEncrypted=") == 2, "both keys come through systemd-creds"
    assert "ams-canonical-key.cred" in conf and "ams-api-key.cred" in conf
    assert "Environment=MEM0_API_KEY_FILE=%d/ams-api-key" in conf
    assert "Environment=MEM0_HOST_KIND=native" in conf
    assert "--host 192.0.2.9 --port 18791" in files["mem0.service"]
    assert "--host 0.0.0.0" not in files["mem0.service"]


def test_render_only_includes_the_chain_and_no_per_job_timers(tmp_path):
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    names = {p.name for p in out.rglob("*.timer")}
    assert names == {"l10-audit.timer", "ams-nightly.timer"}, names


def test_wait_for_bind_parses_and_refuses_wildcard():
    script = REPO_ROOT / "scripts" / "wsl" / "wait-for-bind.sh"
    r = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([BASH, str(script), "0.0.0.0", "1"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 78
