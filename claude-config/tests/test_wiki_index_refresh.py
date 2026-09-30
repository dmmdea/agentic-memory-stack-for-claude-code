"""wiki-index-refresh.sh: the operator-side session refresh of the wiki index (WP-8).

The REAL script under bash with HOME pointed at a fixture dir and `wsl.exe` replaced by a fake on
PATH that records what it was handed. Contract under test: the vault comes from an argument, the
environment or ~/.mem0/wiki-vault (never a baked-in path); a successful hand-off writes the local
refresh stamp (~/.claude/state/last-wiki-refresh) that the SessionStart catch-up compares vault
page times against; a failed hand-off leaves the stamp alone.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "wiki-index-refresh.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or shutil.which("tar") is None, reason="bash/tar not available")


def _fixture(tmp_path: Path, wsl_exit: int = 0):
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    vault = tmp_path / "vault"
    (vault / "wiki" / "entities").mkdir(parents=True)
    (vault / "wiki" / "entities" / "Page.md").write_text("# Page\n\ntext\n", encoding="utf-8")
    b = tmp_path / "bin"
    b.mkdir()
    fake = b / "wsl.exe"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "wsl $*" >> "$FAKE_LOG"\n'
        'tar -tf - >> "$FAKE_LOG" 2>/dev/null\n'
        f"exit {wsl_exit}\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {"HOME": str(home), "USERPROFILE": str(home),
           "HOMEDRIVE": os.path.splitdrive(str(home))[0], "HOMEPATH": os.path.splitdrive(str(home))[1],
           "PATH": f"{b}{os.pathsep}/usr/bin{os.pathsep}/bin", "FAKE_LOG": str(tmp_path / "wsl.log")}
    return home, vault, env


def _run(env, *args):
    return subprocess.run([BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60, check=False)


def test_refresh_hands_the_wiki_tree_to_wsl_and_stamps_success(tmp_path):
    home, vault, env = _fixture(tmp_path)
    r = _run(env, str(vault))
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "wsl.log").read_text(encoding="utf-8")
    assert "wiki-index.sh snapshot" in log and "wiki-index.sh build" in log
    assert "wiki/entities/Page.md" in log
    stamp = home / ".claude" / "state" / "last-wiki-refresh"
    assert stamp.read_text().strip().isdigit()


def test_a_failed_handoff_does_not_stamp(tmp_path):
    home, vault, env = _fixture(tmp_path, wsl_exit=1)
    r = _run(env, str(vault))
    assert r.returncode != 0
    assert not (home / ".claude" / "state" / "last-wiki-refresh").exists()


def test_vault_falls_back_to_env_then_the_config_file(tmp_path):
    home, vault, env = _fixture(tmp_path)
    r = _run(dict(env, WIKI_VAULT=str(vault)))
    assert r.returncode == 0, r.stderr
    (home / ".claude" / "state" / "last-wiki-refresh").unlink()
    (home / ".mem0" / "wiki-vault").write_text(str(vault) + "\n", encoding="utf-8")
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert (home / ".claude" / "state" / "last-wiki-refresh").exists()


def test_no_vault_configured_is_a_loud_failure_naming_the_config(tmp_path):
    home, _, env = _fixture(tmp_path)
    r = _run(env)
    assert r.returncode == 1
    assert "wiki-vault" in r.stderr
    assert not (tmp_path / "wsl.log").exists()


def test_a_vault_without_a_wiki_tree_is_refused(tmp_path):
    home, _, env = _fixture(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _run(env, str(empty))
    assert r.returncode == 1
    assert "wiki" in r.stderr
    assert not (tmp_path / "wsl.log").exists()


def test_a_tar_failure_is_not_masked_by_a_successful_wsl_side(tmp_path):
    """The pipeline's LAST stage (wsl) succeeds on whatever it was fed; a tar that died must still fail the run."""
    home, vault, env = _fixture(tmp_path)
    bad_tar = tmp_path / "bin" / "tar"
    bad_tar.write_text("#!/usr/bin/env bash\necho 'tar: boom' >&2\nexit 2\n", encoding="utf-8")
    bad_tar.chmod(bad_tar.stat().st_mode | stat.S_IEXEC)
    r = _run(env, str(vault))
    assert r.returncode != 0
    assert not (home / ".claude" / "state" / "last-wiki-refresh").exists()
