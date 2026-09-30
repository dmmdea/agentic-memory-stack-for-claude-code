"""_home_isolation.py - redirect a test's notion of "home" on every platform.

`HOME` alone is a POSIX-only redirect. On Windows Python resolves `~` from USERPROFILE (then
HOMEDRIVE+HOMEPATH), so a test that sets only HOME and runs a script that calls Path.home() writes
into the real profile - the goal-recurrence receipt leak. Every test that exercises code which
touches `~` goes through this module instead of setting HOME by hand:

    env = home_env(tmp_path)             # a child-process env with the home redirected
    apply_home(monkeypatch, tmp_path)    # the current process (env + Path.home)
    def test_x(isolated_home): ...       # the conftest fixture: apply_home on tmp_path/home
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional


def home_env(home: "os.PathLike | str", base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """A copy of `base` (default: os.environ) whose home directory is `home` on POSIX and Windows."""
    env = dict(os.environ if base is None else base)
    h = str(home)
    env["HOME"] = h
    env["USERPROFILE"] = h
    drive, tail = os.path.splitdrive(h)
    if drive:
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail
    return env


def apply_home(monkeypatch, home: "os.PathLike | str") -> Path:
    """Redirect the current process: HOME, USERPROFILE, HOMEDRIVE/HOMEPATH and Path.home()."""
    h = Path(home)
    for key, val in home_env(h, base={}).items():
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(h)))
    return h
