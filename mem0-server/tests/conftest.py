"""conftest.py — make sibling test helpers importable.

This package has an `__init__.py`, so pytest's default (prepend) import mode inserts the
package's PARENT directory on sys.path, not this directory. A bare sibling import such as

    from _debris_patterns import delete_goal_rows

therefore fails at collection with ModuleNotFoundError depending on which files are selected
in the run — collecting several files together happened to work while running one of them
alone did not, which is a confusing way to discover the problem.

Inserting this directory makes bare sibling imports resolve identically no matter how the
suite is invoked (whole directory, single file, single test id).

Live-suite interlock: the suites that talk HTTP are written to run against a real deployment,
so this file refuses to start a session aimed at anything but a loopback MEM0_URL (unless
AMS_ALLOW_LIVE_PROD_TESTS=1, which is announced in the report header), and refuses any request
carrying the stack's own tenant. The logic lives in `_live_guard.py`; it is exercised by
`test_live_guard.py`. Home redirects go through `_home_isolation.py` (the `isolated_home` fixture
below): `HOME` alone does not move `~` on Windows.

Scope note: the maintainer-side copy of this file also carries a session-scoped cleanup
backstop that sweeps live-store debris left by a crashed run. That is deliberately not
reproduced here — it is tied to maintainer-only tooling. The public live-stack suites clean
up inline (`_debris_patterns.delete_goal_rows`, `_test_cleanup.delete_memory`), so a normal
completed run leaves nothing behind; a run that dies mid-test may leave rows for manual cleanup.
"""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest  # noqa: E402

import _live_guard as _guard  # noqa: E402
from _home_isolation import apply_home  # noqa: E402


def pytest_sessionstart(session):
    """Refuse a production target before collection imports any suite (no HTTP can have run)."""
    try:
        _guard.check_session()
    except _guard.LiveGuardRefused as exc:
        pytest.exit(f"live-suite guard: {exc}", returncode=2)


def pytest_report_header(config):
    line = _guard.announcement()
    return [line] if line else []


@pytest.fixture(scope="session", autouse=True)
def _live_suite_guard():
    """Belt and braces for runs where this conftest is loaded after session start, and the
    request-level tenant check for every httpx call the suites make."""
    try:
        _guard.check_session()
    except _guard.LiveGuardRefused as exc:
        pytest.exit(f"live-suite guard: {exc}", returncode=2)
    with _guard.request_guard():
        yield


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """A throwaway home for code that resolves `~`: HOME, USERPROFILE, HOMEDRIVE/HOMEPATH and
    Path.home() all point at tmp_path/home, and are restored afterwards."""
    home = tmp_path / "home"
    home.mkdir()
    return apply_home(monkeypatch, home)
