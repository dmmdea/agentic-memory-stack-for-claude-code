"""Tests redirect the home directory on every platform, not just under POSIX HOME semantics.

Windows resolves `~` from USERPROFILE, so a HOME-only redirect leaves a child process writing into
the real profile. These tests pin the shared helper and, structurally, that no suite in this
directory goes back to setting HOME alone.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from _home_isolation import apply_home, home_env

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent


def test_home_env_sets_every_variable_a_platform_reads(tmp_path):
    env = home_env(tmp_path, base={"PATH": "p"})
    assert env["HOME"] == str(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path)
    assert env["PATH"] == "p"


def test_home_env_does_not_mutate_the_caller(tmp_path):
    base = {"HOME": "/real"}
    home_env(tmp_path, base=base)
    assert base == {"HOME": "/real"}


def test_isolated_home_fixture_redirects_this_process(isolated_home):
    assert Path.home() == isolated_home
    assert os.environ["HOME"] == str(isolated_home)
    assert os.environ["USERPROFILE"] == str(isolated_home)
    assert Path(os.path.expanduser("~")) == isolated_home


def test_isolated_home_is_undone_afterwards(monkeypatch, tmp_path):
    before = Path.home()
    with monkeypatch.context() as m:
        apply_home(m, tmp_path)
        assert Path.home() == tmp_path
    assert Path.home() == before


def test_a_child_process_sees_the_redirected_home(tmp_path):
    """The Windows-semantics assertion: USERPROFILE is set in the child, and the child's ~ is the
    sandbox (on Windows because expanduser reads USERPROFILE, elsewhere because it reads HOME)."""
    code = "import os,pathlib;print(os.environ.get('USERPROFILE'));print(pathlib.Path.home())"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=home_env(tmp_path), timeout=60)
    assert r.returncode == 0, r.stderr
    userprofile, home = r.stdout.split()
    assert userprofile == str(tmp_path)
    assert Path(home) == tmp_path


_HOME_ONLY = re.compile(r"""(?:"HOME"\s*[:,]|'HOME'\s*[:,]|\bHOME\s*=\s*str\(|\[["']HOME["']\]\s*=)""")


# Every directory whose pytest files CI runs: a HOME-only child env in any of them is the same
# defect, so the static guard scans them all (not just this directory).
_SCANNED_DIRS = (
    HERE,
    REPO_ROOT / "claude-config" / "tests",
    REPO_ROOT / "scripts" / "wsl",
)


def _home_only_offenders():
    offenders = []
    for base in _SCANNED_DIRS:
        for path in sorted(base.glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if not _HOME_ONLY.search(line):
                    continue
                window = " ".join(lines[max(0, i - 3): i + 4])
                if "USERPROFILE" not in window:
                    offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{i + 1}")
    return offenders


def test_scanned_dirs_exist_and_hold_suites():
    """A moved directory must not turn the static guard below into a silent no-op."""
    for base in _SCANNED_DIRS:
        assert base.is_dir(), base
        assert any(base.glob("test_*.py")), base


def test_no_suite_redirects_HOME_alone():
    """A test that sets HOME by hand and never USERPROFILE is the class that leaked a receipt into
    the real Windows profile. Redirecting goes through _home_isolation (home_env / apply_home /
    the isolated_home fixture), or sets USERPROFILE and HOMEDRIVE/HOMEPATH inline where the suite
    lives outside this directory. Scans mem0-server/tests, claude-config/tests and scripts/wsl."""
    offenders = _home_only_offenders()
    assert not offenders, f"HOME-only redirect (use tests/_home_isolation.py): {offenders}"
