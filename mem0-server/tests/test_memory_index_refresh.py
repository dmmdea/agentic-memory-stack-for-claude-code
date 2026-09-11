"""memory-index-refresh.py: twin of DreamCatchup.Tests.ps1 'memory-index-refresh: 6h throttle
honored' (register P1-3). The builder is injected; HOME lives under tmp_path."""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "wsl"
sys.path.insert(0, str(SCRIPTS))


def _mod():
    spec = importlib.util.spec_from_file_location("memory_index_refresh", SCRIPTS / "memory-index-refresh.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".mem0").mkdir()
    return tmp_path


def test_second_run_within_6h_is_a_noop(home):
    m = _mod()
    calls = []

    def ok(script):
        calls.append(script)
        return (0, "built")
    assert m.main([], runner=ok) == 0 and calls == ["memory-index-build.py"]
    assert m.main([], runner=ok) == 0 and calls == ["memory-index-build.py"], "throttled: no second build"
    assert m.main(["--force"], runner=ok) == 0 and len(calls) == 2
    assert (home / ".mem0" / "maintenance" / "last-index-refresh").exists()


def test_failure_does_not_mark_and_propagates_exit(home):
    m = _mod()
    assert m.main([], runner=lambda s: (3, "boom")) == 3
    assert not (home / ".mem0" / "maintenance" / "last-index-refresh").exists()
    assert not (home / ".mem0" / "maintenance" / "locks" / "index-refresh.lock").exists(), "the lock is released on failure"


def test_lock_held_is_a_quiet_skip_and_stale_lock_is_reclaimed(home):
    m = _mod()
    lock = home / ".mem0" / "maintenance" / "locks" / "index-refresh.lock"
    lock.mkdir(parents=True)
    calls = []
    assert m.main([], runner=lambda s: (calls.append(s), (0, ""))[1]) == 0 and calls == []
    import os
    import time
    old = time.time() - 40 * 60
    os.utime(lock, (old, old))
    assert m.main([], runner=lambda s: (calls.append(s), (0, ""))[1]) == 0 and calls == ["memory-index-build.py"]
