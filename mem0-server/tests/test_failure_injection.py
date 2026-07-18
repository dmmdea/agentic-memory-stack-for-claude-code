"""v0.17 Phase C: failure injection tests.

The atomic episode POST refactor (v0.16 D HIGH-5) wrapped the entire episode
create flow in a single BEGIN/ROLLBACK transaction. These tests prove it holds:
monkey-patch each step to raise after partial work, then assert no orphan rows
remain in episodes/sessions/episode_links/goals.

Tests run against an in-process SQLite test database — NOT the running server.
This avoids the unsolvable problem of monkey-patching a separate process's imports.

Approach A (chosen): import episodic directly, construct the same call sequence
as app.py's create_episode handler, inject failures at each step, verify atomicity.
The transaction boundary in app.py is: `conn.commit()` only fires after ALL steps
succeed; any exception triggers `conn.rollback()`. We replicate that boundary here.
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

import sys
SERVER_DIR = str(Path(__file__).parent.parent)
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import episodic


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    """Fresh episodic.db in tmp_path; EPISODIC_DB_PATH patched to it."""
    db_path = tmp_path / "test_failure_inject.db"
    monkeypatch.setattr(episodic, "EPISODIC_DB_PATH", db_path)
    conn = episodic._connect_to(db_path)
    episodic.init_schema(conn)
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Helper: row counts snapshot
# ---------------------------------------------------------------------------

def _row_counts(db_path: Path) -> tuple[int, int, int, int]:
    """Return (sessions, episodes, episode_links, goals) row counts."""
    with sqlite3.connect(str(db_path)) as conn:
        s = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        e = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        lk = conn.execute("SELECT COUNT(*) FROM episode_links").fetchone()[0]
        g = conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    return s, e, lk, g


# ---------------------------------------------------------------------------
# Helper: replicate app.py's atomic episode POST transaction
# ---------------------------------------------------------------------------

def _atomic_episode_post(
    db_path: Path,
    session_id: str,
    goal_title: str = "Test goal",
    brand: str = "test-brand",
    create_goal_row: bool = True,
) -> int:
    """Replicate the atomic transaction from app.py create_episode.

    Creates session + episode + (optionally) goal + episode_link in ONE transaction.
    Returns episode_id on success. Rolls back and re-raises on any exception.

    This mirrors the structure of app.py create_episode (the HIGH-5 atomic refactor):
      1. create_session (commit=False)
      2. add_episode (commit=False)
      3. create_goal (commit=False) — if create_goal_row=True
      4. link_episode_to_goal (commit=False) — if create_goal_row=True
      5. conn.commit() — all-or-nothing
    On any exception: conn.rollback(), re-raise.
    """
    conn = episodic._connect_to(db_path)
    try:
        now = episodic._iso_now()
        episodic.create_session(
            conn, session_id, brand=brand, started_at=now, commit=False
        )
        episode_id = episodic.add_episode(
            conn, session_id,
            started_at=now, ended_at=now,
            goal_text=goal_title,
            summary_text="Test summary",
            state="complete",
            commit=False,
        )
        if create_goal_row:
            goal_id = episodic.create_goal(
                conn, title=goal_title, brand=brand,
                first_seen_session_id=session_id, commit=False
            )
            episodic.link_episode_to_goal(
                conn, episode_id, goal_id, link_type="advanced_goal", commit=False
            )
        conn.commit()
        return episode_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests: failure injection at each step
# ---------------------------------------------------------------------------

def test_rollback_after_create_session_fails(test_db):
    """If create_session raises, no session/episode/goal/link rows are created."""
    s0, e0, l0, g0 = _row_counts(test_db)
    with patch.object(episodic, "create_session", side_effect=RuntimeError("injected: create_session")):
        with pytest.raises(RuntimeError, match="create_session"):
            _atomic_episode_post(test_db, f"sid-{uuid.uuid4()}")
    s1, e1, l1, g1 = _row_counts(test_db)
    assert (s0, e0, l0, g0) == (s1, e1, l1, g1), \
        f"orphan rows after create_session failure: before={s0,e0,l0,g0} after={s1,e1,l1,g1}"


def test_rollback_after_add_episode_fails(test_db):
    """If add_episode raises after create_session, no rows committed."""
    s0, e0, l0, g0 = _row_counts(test_db)
    with patch.object(episodic, "add_episode", side_effect=RuntimeError("injected: add_episode")):
        with pytest.raises(RuntimeError, match="add_episode"):
            _atomic_episode_post(test_db, f"sid-{uuid.uuid4()}")
    s1, e1, l1, g1 = _row_counts(test_db)
    assert (s0, e0, l0, g0) == (s1, e1, l1, g1), \
        f"orphan rows after add_episode failure: before={s0,e0,l0,g0} after={s1,e1,l1,g1}"


def test_rollback_after_create_goal_fails(test_db):
    """If create_goal raises after episode is written, no rows committed (atomicity holds)."""
    s0, e0, l0, g0 = _row_counts(test_db)
    with patch.object(episodic, "create_goal", side_effect=RuntimeError("injected: create_goal")):
        with pytest.raises(RuntimeError, match="create_goal"):
            _atomic_episode_post(test_db, f"sid-{uuid.uuid4()}", create_goal_row=True)
    s1, e1, l1, g1 = _row_counts(test_db)
    assert (s0, e0, l0, g0) == (s1, e1, l1, g1), \
        f"orphan rows after create_goal failure: before={s0,e0,l0,g0} after={s1,e1,l1,g1}"


def test_rollback_after_link_episode_to_goal_fails(test_db):
    """If link_episode_to_goal raises, the preceding session/episode/goal writes are all rolled back."""
    s0, e0, l0, g0 = _row_counts(test_db)
    with patch.object(episodic, "link_episode_to_goal", side_effect=RuntimeError("injected: link_episode_to_goal")):
        with pytest.raises(RuntimeError, match="link_episode_to_goal"):
            _atomic_episode_post(test_db, f"sid-{uuid.uuid4()}", create_goal_row=True)
    s1, e1, l1, g1 = _row_counts(test_db)
    assert (s0, e0, l0, g0) == (s1, e1, l1, g1), \
        f"orphan rows after link_episode_to_goal failure: before={s0,e0,l0,g0} after={s1,e1,l1,g1}"


# ---------------------------------------------------------------------------
# Positive control: successful post increments all counts
# ---------------------------------------------------------------------------

def test_successful_episode_post_increments_counts(test_db):
    """Positive control: a fully successful atomic POST increments session+episode+goal+link."""
    s0, e0, l0, g0 = _row_counts(test_db)
    episode_id = _atomic_episode_post(test_db, f"sid-{uuid.uuid4()}", create_goal_row=True)
    s1, e1, l1, g1 = _row_counts(test_db)
    assert s1 == s0 + 1, "sessions count should increase by 1"
    assert e1 == e0 + 1, "episodes count should increase by 1"
    assert l1 == l0 + 1, "episode_links count should increase by 1"
    assert g1 == g0 + 1, "goals count should increase by 1"
    assert isinstance(episode_id, int) and episode_id > 0


def test_successful_episode_post_without_goal_increments_session_episode_only(test_db):
    """Positive control: episode POST without goal creates session+episode but no goal/link."""
    s0, e0, l0, g0 = _row_counts(test_db)
    _atomic_episode_post(test_db, f"sid-{uuid.uuid4()}", create_goal_row=False)
    s1, e1, l1, g1 = _row_counts(test_db)
    assert s1 == s0 + 1, "sessions count should increase by 1"
    assert e1 == e0 + 1, "episodes count should increase by 1"
    assert l1 == l0, "no link row should be created without a goal"
    assert g1 == g0, "no goal row should be created"
