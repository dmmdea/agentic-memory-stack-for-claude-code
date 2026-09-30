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


from _home_isolation import home_env  # noqa: E402


def _run(args, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "claude"
    stub.write_text("#!/usr/bin/env bash\necho 'stub 0.0.0'\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = home_env(home)
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


def test_resolves_the_tenant_sentinel_the_windows_installer_resolves():
    """Every deployed tool defaults its user_id to the __WSL_USER__ sentinel; the Windows
    installer resolves it at deploy time. If this installer copied the files verbatim, every
    write from a client would land under a literal placeholder tenant."""
    sh = SCRIPT.read_text(encoding="utf-8")
    assert 'sed "s|__WSL_USER__|$USER_ID|g"' in sh
    assert "--user-id" in sh
    # the substitution is load-bearing: the sources really carry the sentinel
    for f in ("mem0-mcp-shim.py", "replay-ops.py"):
        assert "__WSL_USER__" in (REPO_ROOT / "scripts" / "wsl" / f).read_text(encoding="utf-8"), f
    ps1 = (REPO_ROOT / "install" / "2-windows-config.ps1").read_text(encoding="utf-8")
    assert "$Text.Replace('__WSL_USER__',   $WslUser)" in ps1


def test_user_id_is_reported_and_validated(tmp_path):
    r, _ = _run(["--authority", "http://brain-host:18791", "--user-id", "tenant-a", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "user_id (mem0 tenant): tenant-a" in r.stdout
    assert "__WSL_USER__ -> tenant-a" in r.stdout
    r, _ = _run(["--authority", "http://brain-host:18791", "--user-id", "bad/one", "--dry-run"], tmp_path)
    assert r.returncode != 0 and "--user-id" in r.stderr


def test_role_file_and_protocol_marker_match_the_shared_contract():
    sh = SCRIPT.read_text(encoding="utf-8")
    assert "printf 'client\\n' > \"$MEM0_DIR/role\"" in sh
    assert "## Memory tier protocol (agentic-memory-stack)" in sh
    assert "claude-config/claude-md-memory-protocol.md" in sh
    assert (REPO_ROOT / "claude-config" / "claude-md-memory-protocol.md").is_file()


def test_tenant_inherits_from_the_client_receipt_on_a_rerun(tmp_path):
    """v1.23.2: an omitted --user-id takes the tenant in the previous client-receipt.json; only a
    first install falls back to the login name (a re-run used to rewrite the shim under the login)."""
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    (home / ".mem0" / "client-receipt.json").write_text(
        '{"role":"client","authority":"http://brain-host:18791","user_id":"tenant-old"}\n', encoding="utf-8")
    r, _ = _run(["--authority", "http://brain-host:18791", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "tenant inherited from" in r.stdout
    assert "user_id (mem0 tenant): tenant-old" in r.stdout
    r, _ = _run(["--authority", "http://brain-host:18791", "--user-id", "tenant-new", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "user_id (mem0 tenant): tenant-new" in r.stdout and "tenant inherited" not in r.stdout


# ---- --ams-hub inherits from the previous receipt (session-12 WP-12) ---------------------------
# The Windows installer inherits every flag it records; the Linux client inherited the tenant but
# not the hub, so a re-run without --ams-hub skipped the fleet store with exit 0 and rewrote the
# receipt with an empty hub. Explicit flag > receipt > nothing; an explicit empty value clears.
HUB = "ams-hub@hubbox:ams-store.git"


def _receipt(tmp_path, body):
    mem0 = tmp_path / "home" / ".mem0"
    mem0.mkdir(parents=True, exist_ok=True)
    (mem0 / "client-receipt.json").write_text(body, encoding="utf-8")


def test_ams_hub_inherits_from_the_receipt_when_the_flag_is_omitted(tmp_path):
    _receipt(tmp_path, '{"role":"client","user_id":"t","ams_hub":"%s","ams_store_sha256":"abc"}\n' % HUB)
    r, _ = _run(["--authority", "http://brain-host:18791", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert f"--ams-hub inherited from ~/.mem0/client-receipt.json: {HUB}" in r.stdout
    assert f"wire the hub transport for {HUB}" in r.stdout
    assert "skipped" not in r.stdout


def test_an_explicit_ams_hub_wins_over_the_receipt(tmp_path):
    _receipt(tmp_path, '{"user_id":"t","ams_hub":"%s"}\n' % HUB)
    r, _ = _run(["--authority", "http://brain-host:18791", "--ams-hub", "ams-hub@other:x.git", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--ams-hub inherited" not in r.stdout
    assert "wire the hub transport for ams-hub@other:x.git" in r.stdout


def test_an_explicit_empty_ams_hub_clears_the_recorded_hub_with_a_warning(tmp_path):
    _receipt(tmp_path, '{"user_id":"t","ams_hub":"%s"}\n' % HUB)
    r, _ = _run(["--authority", "http://brain-host:18791", "--ams-hub", "", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--ams-hub cleared (explicit empty value; not inherited)" in r.stdout
    assert "--ams-hub inherited" not in r.stdout
    assert "WARN: no --ams-hub" in r.stdout


def test_skipping_the_fleet_store_is_a_warning_not_a_quiet_line(tmp_path):
    r, _ = _run(["--authority", "http://brain-host:18791", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "WARN: no --ams-hub" in r.stdout and "skipped" in r.stdout
    assert "inherited" not in r.stdout


def test_a_recorded_hub_that_cannot_be_read_fails_instead_of_dropping_it(tmp_path):
    """The receipt names a hub but the value cannot be parsed out (truncated JSON): inheriting an
    empty string here would rewrite the receipt without the hub, exactly the silent loss this
    inheritance exists to stop."""
    _receipt(tmp_path, '{"user_id":"t","ams_hub":"%s"\n' % HUB)   # no closing brace
    r, _ = _run(["--authority", "http://brain-host:18791", "--dry-run"], tmp_path)
    assert r.returncode != 0
    assert "records an ams_hub" in r.stderr
    # ...and an explicit flag is the operator's way out, so it is not refused
    r, _ = _run(["--authority", "http://brain-host:18791", "--ams-hub", HUB, "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
