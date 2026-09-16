# mem0-server/tests/test_linux_client_store.py
"""The fleet-store block of install/linux-client.sh and its hook merger (register P4-3).

A Linux client joins the fleet store by holding three things: the binary, the hub transport,
and the hooks that drive it. Two properties matter more than the rest and both are asserted
here rather than described:

  * a client is ROLELESS - the `role` file is what makes judge-apply willing to decide, and a
    PC that carried `hub` would start applying the nightly's plans to everyone's memory;
  * registering hooks must never delete a hook this installer does not own (the 2026-06-08
    audit: an installer replaced whole event arrays and silently dropped user hooks).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "install" / "linux-client.sh"
HOOKS = REPO_ROOT / "claude-config" / "register-ams-hooks.py"
BASH = shutil.which("bash")

BIN = "/home/u/.local/bin/ams-store"
HUB = "hub-host"


def run_hooks(settings: Path, *extra, binary=BIN, hub=HUB):
    return subprocess.run(
        [sys.executable, str(HOOKS), "--settings", str(settings),
         "--binary", binary, "--hub-host", hub, *extra],
        capture_output=True, text=True, timeout=60,
    )


def events(settings: Path):
    return json.loads(settings.read_text(encoding="utf-8"))["hooks"]


def commands(settings: Path, event: str):
    out = []
    for block in events(settings).get(event, []):
        for hook in block.get("hooks", []):
            out.append(hook.get("command", ""))
    return out


# --------------------------------------------------------------- the hook merger


def test_registers_the_gate_and_both_syncs(tmp_path):
    s = tmp_path / "settings.json"
    r = run_hooks(s)
    assert r.returncode == 0, r.stderr
    assert commands(s, "PostToolUse") == [f"{BIN} gate"]
    sync = f"{BIN} sync --once --hub-host {HUB}"
    assert commands(s, "SessionStart") == [sync]
    assert commands(s, "SessionEnd") == [sync]
    # The gate only fires on writes, and the start sync must not hold the session open.
    gate_block = events(s)["PostToolUse"][0]
    assert gate_block["matcher"] == "Write|Edit"
    assert events(s)["SessionStart"][0]["hooks"][0]["async"] is True
    assert "async" not in events(s)["SessionEnd"][0]["hooks"][0]


def test_preserves_hooks_it_does_not_own(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "my-own-thing.sh"}]}],
            "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "prettier"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "stop-extract.sh"}]}],
        }
    }), encoding="utf-8")
    assert run_hooks(s).returncode == 0
    assert "my-own-thing.sh" in commands(s, "SessionStart")
    assert "prettier" in commands(s, "PostToolUse")
    assert commands(s, "Stop") == ["stop-extract.sh"]


def test_rerun_replaces_rather_than_duplicates_and_is_byte_identical(tmp_path):
    s = tmp_path / "settings.json"
    assert run_hooks(s).returncode == 0
    first = s.read_text(encoding="utf-8")
    r = run_hooks(s)
    assert r.returncode == 0
    assert s.read_text(encoding="utf-8") == first, "a second run rewrote the file"
    assert "already current" in r.stdout
    assert len(commands(s, "SessionStart")) == 1, "the entry was duplicated"


def test_a_stale_registration_is_replaced_not_kept(tmp_path):
    """The marker, not the exact command, is what identifies our entry - so an entry from an
    older install (different path, different hub) is replaced instead of left behind to run."""
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"hooks": {"SessionEnd": [
        {"hooks": [{"type": "command", "command": "/old/path/ams-store sync --once --hub-host oldhub"}]}
    ]}}), encoding="utf-8")
    assert run_hooks(s).returncode == 0
    assert commands(s, "SessionEnd") == [f"{BIN} sync --once --hub-host {HUB}"]


def test_the_powershell_gate_registration_is_replaced(tmp_path):
    """Cutover: the box was running the PowerShell write gate. Its markers are ours, so the
    ams-store gate must REPLACE it - two gates on one event would double-lint every write."""
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "bash ~/.claude/scripts/memory-index-write-lint.sh"}]}
    ]}}), encoding="utf-8")
    assert run_hooks(s).returncode == 0
    assert commands(s, "PostToolUse") == [f"{BIN} gate"]


def test_refuses_a_dotted_hub_host(tmp_path):
    s = tmp_path / "settings.json"
    r = run_hooks(s, hub="hub-host.tailnet.invalid")
    assert r.returncode == 2
    assert "single-label" in r.stderr
    assert not s.exists(), "a refused invocation still wrote the file"


def test_refuses_a_relative_binary_path(tmp_path):
    s = tmp_path / "settings.json"
    r = run_hooks(s, binary="ams-store")
    assert r.returncode == 2
    assert not s.exists()


def test_refuses_a_settings_file_that_is_not_an_object(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text("[1, 2, 3]", encoding="utf-8")
    r = run_hooks(s)
    assert r.returncode == 1
    assert s.read_text(encoding="utf-8") == "[1, 2, 3]", "it rewrote a file it could not parse"


# --------------------------------------------------------------- the installer block


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_installer_parses_and_documents_its_flags():
    r = subprocess.run([BASH, "-n", str(INSTALLER)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    head = INSTALLER.read_text(encoding="utf-8")[:4000]
    for flag in ("--ams-hub", "--ams-store-binary", "--ams-store-sums", "--ams-release-repo"):
        assert flag in head, f"{flag} is not documented in the usage header"


def test_the_client_never_writes_a_role_file():
    """Only the authority's checkout carries role=hub. linux-authority.sh writes it; this
    installer must refuse to run where one exists and must never create one."""
    body = INSTALLER.read_text(encoding="utf-8")
    assert "printf 'hub" not in body and 'printf "hub' not in body
    assert "$state/role" in body and "exists on a client" in body


def test_the_store_block_is_skipped_without_a_hub():
    body = INSTALLER.read_text(encoding="utf-8")
    assert 'if [ -z "$AMS_HUB" ]; then' in body
    assert "does not join the fleet store (skipped)" in body


def test_the_binary_is_verified_against_the_release_sums():
    body = INSTALLER.read_text(encoding="utf-8")
    assert "SHA256SUMS" in body
    assert "checksum mismatch" in body, "an unverified binary must not be installed"
    assert "ams-store-linux-arm64" in body, "arm64 boxes would install an amd64 binary"
    block = body.split("[5b] ams-store")[1].split("6. receipt")[0]
    invocations = [ln for ln in block.splitlines()
                   if ln.strip().startswith("sudo ") or " sudo " in ln.split("#")[0]]
    assert not invocations, f"a client installs into its own bin, never through sudo: {invocations}"


# --------------------------------------------------------------- the replica forwards the flags


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_replica_installer_forwards_the_hub_to_the_client(tmp_path):
    """A replica is a client plus a dormant brain, and it builds the client's arguments
    EXPLICITLY - so a flag it does not name is silently dropped. Before this test, --ams-hub was
    dropped: the replica install reported success and the box never joined the fleet store."""
    replica = REPO_ROOT / "install" / "linux-replica.sh"
    home = tmp_path / "home"
    home.mkdir()
    # The client's prerequisite step needs the editor CLI, which a test box need not have.
    binp = tmp_path / "bin"
    binp.mkdir()
    stub = binp / "claude"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=f"{binp}:{os.environ.get('PATH', '')}")

    r = subprocess.run(
        [BASH, str(replica), "--authority", "http://192.0.2.9:18791", "--brain-ssh", "nobrain",
         "--user-id", "t", "--ams-hub", "ams-hub@hub-host:ams-store.git", "--dry-run"],
        capture_output=True, text=True, timeout=300, env=env,
    )
    out = r.stdout + r.stderr
    assert "install/linux-client.sh" in out, f"the replica never reached the client step:\n{out[:800]}"
    assert "ams-hub@hub-host:ams-store.git" in out, (
        "the replica did not forward --ams-hub to the client, so the box would never "
        f"join the fleet store:\n{out[:1200]}"
    )


def test_the_replica_documents_the_forwarded_flags():
    body = (REPO_ROOT / "install" / "linux-replica.sh").read_text(encoding="utf-8")[:4000]
    for flag in ("--ams-hub", "--ams-store-binary", "--ams-store-sums"):
        assert flag in body, f"{flag} is not documented in the replica usage header"
