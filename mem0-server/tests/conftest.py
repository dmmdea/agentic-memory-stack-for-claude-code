"""conftest.py — make sibling test helpers importable.

This package has an `__init__.py`, so pytest's default (prepend) import mode inserts the
package's PARENT directory on sys.path, not this directory. A bare sibling import such as

    from _debris_patterns import delete_goal_rows

therefore fails at collection with ModuleNotFoundError depending on which files are selected
in the run — collecting several files together happened to work while running one of them
alone did not, which is a confusing way to discover the problem.

Inserting this directory makes bare sibling imports resolve identically no matter how the
suite is invoked (whole directory, single file, single test id).

Scope note: the maintainer-side copy of this file also carries a session-scoped cleanup
backstop that sweeps live-store debris left by a crashed run. That is deliberately not
reproduced here — it is tied to maintainer-only tooling. The public live-stack suites clean
up inline via `_debris_patterns.delete_goal_rows`, so a normal completed run leaves nothing
behind; a run that dies mid-test may leave rows for manual cleanup.
"""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
