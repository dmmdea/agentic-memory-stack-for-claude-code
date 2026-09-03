# mem0-server/tests/test_linux_client_installer.py
"""install/linux-client.sh — the Linux thin-client installer.

These run the script itself (bash) in a scratch HOME with a stub `claude` on PATH, so they
exercise the real argument parsing, the loopback refusal, and the dry-run contract; and they
pin the file-list parity with the Windows installer's WSL-side deploy list.
"""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "install" / "linux-client.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _run(args, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "claude"
    stub.write_text("#!/usr/bin/env bash\necho 'stub 0.0.0'\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    r = subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, env=env,
                       cwd=str(REPO_ROOT), timeout=120)
    return r, home


def test_script_parses():
    r = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_requires_an_authority(tmp_path):
    r, _ = _run([], tmp_path)
    assert r.returncode != 0
    assert "--authority" in r.stderr


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:18791", "http://localhost:18791", "http://0.0.0.0:18791", "http://[::1]:18791", "not a url",
])
def test_refuses_a_loopback_or_malformed_authority(tmp_path, url):
    """A client has no local store: a loopback authority would queue every write forever, and
    replay-ops.py refuses to drain into loopback for this role. Fail closed at install time."""
    r, home = _run(["--authority", url, "--dry-run"], tmp_path)
    assert r.returncode != 0
    assert "REMOTE authority" in r.stderr
    assert not (home / ".mem0").exists()


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path):
    r, home = _run(["--authority", "http://brain-host:18791", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for step in ("[1] per-host files", "[2] authority health", "[3] client venv", "[4] Claude Code MCP entry",
                 "[5] CLAUDE.md", "[6] receipt", "[7] end-to-end"):
        assert step in out, step
    assert "[dry-run]" in out
    assert "role=client" in out
    assert not (home / ".mem0").exists()
    assert not (home / ".claude").exists()
    assert not (home / "apps").exists()


def test_deploys_the_same_client_files_as_the_windows_installer():
    """The shim spawns its SIBLING replay-ops.py to drain the outbox, so both must be deployed
    together; the Windows installer's $wslScripts list is the reference (minus l10-audit.py, a
    store-side audit a client cannot run)."""
    sh = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'^CLIENT_FILES="([^"]+)"', sh, re.M)
    assert m, "CLIENT_FILES must be a single quoted list"
    client = set(m.group(1).split())
    ps1 = (REPO_ROOT / "install" / "2-windows-config.ps1").read_text(encoding="utf-8")
    w = re.search(r"\$wslScripts\s*=\s*@\(([^)]*)\)", ps1)
    assert w, "$wslScripts list not found in the Windows installer"
    windows = set(re.findall(r"'([^']+)'", w.group(1)))
    assert client == windows - {"l10-audit.py"}, (client, windows)
    for f in client:
        assert (REPO_ROOT / "scripts" / "wsl" / f).is_file(), f


def test_role_file_and_protocol_marker_match_the_shared_contract():
    sh = SCRIPT.read_text(encoding="utf-8")
    assert "printf 'client\\n' > \"$MEM0_DIR/role\"" in sh
    assert "## Memory tier protocol (agentic-memory-stack)" in sh
    assert "claude-config/claude-md-memory-protocol.md" in sh
    assert (REPO_ROOT / "claude-config" / "claude-md-memory-protocol.md").is_file()
