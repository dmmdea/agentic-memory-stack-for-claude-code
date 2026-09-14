"""v1.23 P2-8 (spec §7 Y7): mem0-canonize.sh executes only on the authority; a replica forwards over
SSH or queues a canonize op; the authority-side executor refuses off the brain."""
from __future__ import annotations
import json, os, shutil, stat, subprocess
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CANON = REPO_ROOT / "scripts" / "wsl" / "mem0-canonize.sh"
EXEC = REPO_ROOT / "scripts" / "wsl" / "ams-canonize.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _home(tmp_path, role, brain_ssh="fakebrain"):
    (tmp_path / ".mem0").mkdir(exist_ok=True)
    (tmp_path / ".mem0" / "role").write_text(role + "\n", encoding="utf-8")
    (tmp_path / ".mem0" / "api-key").write_text("k\n", encoding="utf-8")
    (tmp_path / ".mem0" / "replica.env").write_text(f"BRAIN_SSH='{brain_ssh}'\n", encoding="utf-8")
    return tmp_path


def _fake_ssh(tmp_path, rc, record):
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    s = b / "ssh"
    s.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > " + str(record) + f"\nexit {rc}\n", encoding="utf-8")
    s.chmod(s.stat().st_mode | stat.S_IEXEC)
    return b


def _run(script, home, path_prefix, *argv):
    # Sandbox EVERY key source and the server: on a developer box $XDG_RUNTIME_DIR/mem0/canonical-key
    # holds the LIVE key while mem0.service runs and MEM0_URL defaults to the live loopback server —
    # the first run of this test signed a real PATCH against production (401 only because the API
    # key was fake). Never again.
    rt = Path(home) / "runtime"; rt.mkdir(exist_ok=True)
    env = {"HOME": str(home), "PATH": f"{path_prefix}:{os.environ['PATH']}",
           "XDG_RUNTIME_DIR": str(rt), "MEM0_URL": "http://authority.invalid:1"}
    return subprocess.run(["bash", str(script), *argv], env=env, capture_output=True, text=True)


def test_replica_forwards_over_ssh_with_quoted_argv(tmp_path):
    home = _home(tmp_path, "replica"); rec = tmp_path / "ssh.args"
    r = _run(CANON, home, _fake_ssh(tmp_path, 0, rec), "abc-123", "reason with spaces")
    assert r.returncode == 0, r.stderr
    args = rec.read_text(encoding="utf-8").splitlines()
    assert args[:4] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"] and args[4] == "fakebrain"
    remote = args[5]
    assert remote.startswith("bash ~/apps/mem0-scripts/ams-canonize.sh ") and "abc-123" in remote
    assert "reason\\ with\\ spaces" in remote, remote     # printf %q quoting survives the hop
    assert not (home / ".mem0" / "outbox.jsonl").exists()


def test_replica_queues_when_ssh_unreachable(tmp_path):
    home = _home(tmp_path, "replica"); rec = tmp_path / "ssh.args"
    r = _run(CANON, home, _fake_ssh(tmp_path, 255, rec), "abc-123", "why")
    assert r.returncode == 0, r.stderr
    assert "QUEUED_OFFLINE" in r.stdout
    line = json.loads((home / ".mem0" / "outbox.jsonl").read_text(encoding="utf-8").strip())
    assert line["op"] == "canonize" and line["args"]["argv"] == ["abc-123", "why"]
    assert line["key"] and line["queued_ts"].endswith("Z") and line["args"]["requester"]
    assert line["args"]["requested_ts"] == line["queued_ts"]


def test_replica_passes_through_an_authority_refusal(tmp_path):
    home = _home(tmp_path, "replica"); rec = tmp_path / "ssh.args"
    r = _run(CANON, home, _fake_ssh(tmp_path, 4, rec), "abc-123", "why")
    assert r.returncode == 4 and not (home / ".mem0" / "outbox.jsonl").exists()


def test_replica_without_brain_ssh_fails_loudly(tmp_path):
    home = _home(tmp_path, "replica"); (home / ".mem0" / "replica.env").unlink()
    r = _run(CANON, home, str(tmp_path / "bin"), "abc-123", "why")
    assert r.returncode == 2 and "BRAIN_SSH" in r.stderr


def test_executor_refuses_off_the_brain(tmp_path):
    home = _home(tmp_path, "replica")
    r = _run(EXEC, home, str(tmp_path / "bin"), "abc-123", "why")
    assert r.returncode == 3 and "refusing" in r.stderr


def test_brain_runs_locally_without_forwarding(tmp_path):
    home = _home(tmp_path, "brain"); rec = tmp_path / "ssh.args"
    # no canonical key anywhere -> the LOCAL path fails at key resolution (exit 1); ssh is never called
    r = _run(CANON, home, _fake_ssh(tmp_path, 0, rec), "abc-123", "why")
    assert r.returncode == 1 and "canonical key unavailable" in r.stderr and not rec.exists()


def test_executor_on_a_wsl_brain_runs_the_script_directly(tmp_path):
    home = _home(tmp_path, "brain")
    (home / ".mem0" / "stack.env").write_text("MEM0_HOST_KIND=wsl\n", encoding="utf-8")
    scripts = home / "apps" / "mem0-scripts"; scripts.mkdir(parents=True)
    stub = scripts / "mem0-canonize.sh"
    stub.write_text("#!/usr/bin/env bash\necho \"ran:$MEM0_CANONIZE_NO_FORWARD:$*\"\n", encoding="utf-8")
    r = _run(EXEC, home, str(tmp_path / "bin"), "abc-123", "why")
    assert r.returncode == 0 and r.stdout.strip() == "ran:1:abc-123 why"
