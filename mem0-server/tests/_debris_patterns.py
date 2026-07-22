"""_debris_patterns.py — test-side cleanup helpers for the live-stack suites.

The live-stack suites (test_episodic, test_context_bundle, test_collection_drift_guard)
run against a REAL deployment: they create goals, episodes and memories in the operator's
own store. Goals have no DELETE endpoint, so a test that creates one has no supported way
to remove it and the rows accumulate in the live database forever. This module is the
inline cleanup those tests call to put the store back the way they found it.

It is deliberately small. The maintainer-side copy carries additional debris-classification
patterns used by an offline purge tool; those are not part of the test contract and are not
reproduced here.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Env-overridable so the suites can be pointed at a scratch database instead of the live
# one. Default mirrors episodic.py.
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.environ.get("MEM0_QDRANT_COLLECTION", "mem0_egemma_768")


def episodic_db_path() -> Path:
    """Resolve the live episodic DB. EPISODIC_DB_PATH env overrides (mirrors
    episodic.py's default of ~/.mem0/episodic.db)."""
    return Path(os.environ.get("EPISODIC_DB_PATH") or (Path.home() / ".mem0" / "episodic.db"))


def delete_goal_rows(goal_ids, db_path: Path | None = None) -> None:
    """Best-effort inline cleanup for tests that hold the goal ids they created.

    Goals have no DELETE endpoint, so this removes the rows (plus their episode_links)
    directly via sqlite. FTS stays in sync via the goals_ad trigger. Never raises — a
    failure prints a loud warning and leaves the rows for manual cleanup, because a
    cleanup helper that explodes would mask the assertion the test was actually making.
    """
    goal_ids = [g for g in goal_ids if g]
    if not goal_ids:
        return
    path = db_path or episodic_db_path()
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            qs = ",".join("?" * len(goal_ids))
            conn.execute(
                f"DELETE FROM episode_links WHERE target_kind='goal' AND target_id IN ({qs})",
                [str(g) for g in goal_ids],
            )
            conn.execute(f"DELETE FROM goals WHERE id IN ({qs})", list(goal_ids))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover — cleanup must never fail a test run
        print(
            f"\n[test-cleanup WARNING] inline goal delete failed ({goal_ids}): {exc}",
            file=sys.stderr,
        )
