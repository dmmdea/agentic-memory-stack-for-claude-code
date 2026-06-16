"""episodic.py — SQLite + FTS5 episodic memory sidecar (schema v17.0)

One file, no new dependencies — uses Python stdlib sqlite3 only.
Phase A foundation: schema init, write path, FTS5 search.
Phase B (REST endpoints) and Phase C (dream integration) are separate.

v0.16 hook columns (open_questions, advanced_goals, blocked_goals) are
reserved in the schema now to avoid a future migration when Value Improvement
+ Epistemic Reachability principles are layered on top.

v0.17 Phase 0: episodes.state column ('in_progress' | 'complete' | 'abandoned')
for within-session checkpoint via UserPromptSubmit hook. ADD COLUMN is applied
idempotently via _add_column_if_missing() helper after main schema script.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EPISODIC_DB_PATH = Path.home() / ".mem0" / "episodic.db"
SCHEMA_VERSION = "17.0"

VALID_GOAL_STATUSES = {"open", "blocked", "advanced", "completed", "abandoned", "duplicate"}

VALID_OPEN_QUESTION_STATUSES = {"open", "resolved", "abandoned", "duplicate"}

SCHEMA_SQL = """
-- sessions table: one row per Claude Code conversation
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    transcript_path TEXT,
    message_count INTEGER DEFAULT 0,
    brand TEXT,
    workspace TEXT,
    project TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_brand_started ON sessions(brand, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_ended ON sessions(ended_at DESC);

-- episodes table: 1+ rows per session
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    goal_text TEXT,
    summary_text TEXT,
    source_msg_start INTEGER,
    source_msg_end INTEGER,
    -- v0.16 hook columns (write null in v0.15; v0.16 fills them):
    open_questions TEXT,
    advanced_goals TEXT,
    blocked_goals TEXT,
    -- bookkeeping:
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id);
CREATE INDEX IF NOT EXISTS idx_episodes_ended ON episodes(ended_at DESC);

-- FTS5 virtual table for keyword search over goal + summary
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    goal_text,
    summary_text,
    content='episodes',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS5 in sync with episodes table
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, goal_text, summary_text)
    VALUES (new.id, new.goal_text, new.summary_text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, goal_text, summary_text)
    VALUES ('delete', old.id, old.goal_text, old.summary_text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, goal_text, summary_text)
    VALUES ('delete', old.id, old.goal_text, old.summary_text);
    INSERT INTO episodes_fts(rowid, goal_text, summary_text)
    VALUES (new.id, new.goal_text, new.summary_text);
END;

-- episode_links table: cross-references to mem0 memory IDs
CREATE TABLE IF NOT EXISTS episode_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL,
    link_type TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (episode_id) REFERENCES episodes(id)
);
CREATE INDEX IF NOT EXISTS idx_links_episode ON episode_links(episode_id);
CREATE INDEX IF NOT EXISTS idx_links_target ON episode_links(target_kind, target_id);

-- schema_meta for version tracking and future migrations
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '16.0');
INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('created_by_release', 'v0.15');

-- v0.16: goals table — adjacency-list hierarchy (parent_goal_id self-FK)
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_goal_id INTEGER,
    title TEXT NOT NULL,
    description TEXT,
    brand TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    priority INTEGER DEFAULT 3,
    first_seen_session_id TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (parent_goal_id) REFERENCES goals(id),
    FOREIGN KEY (first_seen_session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_goal_id);
CREATE INDEX IF NOT EXISTS idx_goals_brand_status ON goals(brand, status);
CREATE INDEX IF NOT EXISTS idx_goals_status_priority ON goals(status, priority, updated_at DESC);

-- FTS5 for goals (title + description) so fuzzy-title-matching from Codex extraction works
CREATE VIRTUAL TABLE IF NOT EXISTS goals_fts USING fts5(
    title,
    description,
    content='goals',
    content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS goals_ai AFTER INSERT ON goals BEGIN
    INSERT INTO goals_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;
CREATE TRIGGER IF NOT EXISTS goals_ad AFTER DELETE ON goals BEGIN
    INSERT INTO goals_fts(goals_fts, rowid, title, description) VALUES ('delete', old.id, old.title, old.description);
END;
CREATE TRIGGER IF NOT EXISTS goals_au AFTER UPDATE ON goals BEGIN
    INSERT INTO goals_fts(goals_fts, rowid, title, description) VALUES ('delete', old.id, old.title, old.description);
    INSERT INTO goals_fts(rowid, title, description) VALUES (new.id, new.title, new.description);
END;

-- Bump schema version to 17.0 (v0.17 Phase 0 — episodes.state column added via _add_column_if_missing below)
INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '17.0');
INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('upgraded_by_release', 'v0.17');

-- v0.17 Phase D: open_questions table (Q2(b) global registry — promotes per-session JSON to first-class records)
CREATE TABLE IF NOT EXISTS open_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_text TEXT NOT NULL,
    topic TEXT,
    brand TEXT,
    first_seen_session_id TEXT,
    first_seen_episode_id INTEGER,
    resolved_in_session_id TEXT,
    resolved_at TEXT,
    resolution_text TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'resolved' | 'abandoned' | 'duplicate'
    priority INTEGER DEFAULT 3,
    related_goal_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (first_seen_session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (resolved_in_session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (related_goal_id) REFERENCES goals(id)
);
CREATE INDEX IF NOT EXISTS idx_oq_status_priority ON open_questions(status, priority, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_oq_brand_status ON open_questions(brand, status);

CREATE VIRTUAL TABLE IF NOT EXISTS open_questions_fts USING fts5(
    question_text, topic,
    content='open_questions', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS oq_ai AFTER INSERT ON open_questions BEGIN
    INSERT INTO open_questions_fts(rowid, question_text, topic) VALUES (new.id, new.question_text, new.topic);
END;
CREATE TRIGGER IF NOT EXISTS oq_ad AFTER DELETE ON open_questions BEGIN
    INSERT INTO open_questions_fts(open_questions_fts, rowid, question_text, topic) VALUES ('delete', old.id, old.question_text, old.topic);
END;
CREATE TRIGGER IF NOT EXISTS oq_au AFTER UPDATE ON open_questions BEGIN
    INSERT INTO open_questions_fts(open_questions_fts, rowid, question_text, topic) VALUES ('delete', old.id, old.question_text, old.topic);
    INSERT INTO open_questions_fts(rowid, question_text, topic) VALUES (new.id, new.question_text, new.topic);
END;
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _connect_to(db_path: Path) -> sqlite3.Connection:
    """Open connection to *db_path* with WAL mode + sqlite3.Row factory.

    Context-manager friendly: use ``with _connect_to(path) as conn:``.
    The caller is responsible for commits (autocommit is OFF for DML; DDL
    inside executescript auto-commits per SQLite spec).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _connect() -> sqlite3.Connection:
    """Open connection to the canonical EPISODIC_DB_PATH."""
    return _connect_to(EPISODIC_DB_PATH)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column_def: str,
    column_name: str,
) -> None:
    """Idempotent ADD COLUMN. Catches duplicate-column errors (SQLite has no IF NOT EXISTS for ALTER TABLE ADD COLUMN)."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return  # already added — idempotent
        raise


def init_schema(conn: sqlite3.Connection) -> None:
    """Run SCHEMA_SQL against *conn*.  Idempotent — all DDL uses IF NOT EXISTS.

    v0.17: also applies ADD COLUMN migrations that can't be expressed as IF NOT EXISTS.
    """
    conn.executescript(SCHEMA_SQL)
    # v0.17 Phase 0.A: add episodes.state for within-session checkpoint
    _add_column_if_missing(
        conn,
        "episodes",
        "state TEXT NOT NULL DEFAULT 'complete'",
        "state",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_state ON episodes(state, session_id)"
    )
    # v0.22 Pillar 1: add the initiative axis to goals + open_questions so the
    # context bundle can scope injection to the active repo/initiative
    # (agentic-memory-stack vs local-offload) when both share the same brand.
    # initiative IS NULL == cross-cutting (surfaces in every session). Added via
    # _add_column_if_missing (same idempotent ADD COLUMN style as episodes.state
    # above) because IF NOT EXISTS does not exist for ALTER TABLE ADD COLUMN.
    # NOT backfilled — existing rows stay NULL (Phase H is data hygiene).
    _add_column_if_missing(conn, "goals", "initiative TEXT", "initiative")
    _add_column_if_missing(conn, "open_questions", "initiative TEXT", "initiative")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_goals_initiative_status ON goals(initiative, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_oq_initiative_status ON open_questions(initiative, status)"
    )
    # v0.19 M7/L1/L7 (MED-1 migration guard): a pre-v0.18 DB — e.g. restored via
    # stack-restore.sh from an old snapshot — may already hold >1 in_progress row
    # per session (residue of the very race the index below closes). CREATE UNIQUE
    # INDEX raises IntegrityError on existing violations (IF NOT EXISTS only
    # suppresses the index-already-exists error), the startup caller swallows it,
    # and the race fix silently never deploys — repeating every boot and blocking
    # any future migration appended below. Dedup first: keep the newest (MAX(id))
    # row per session, demote the rest to 'abandoned' (a documented valid state,
    # excluded by the partial index — non-destructive, no row deletion). No-op on
    # clean DBs, so it is safe on every startup.
    conn.execute(
        """UPDATE episodes SET state = 'abandoned'
           WHERE state = 'in_progress'
             AND id NOT IN (
                 SELECT MAX(id) FROM episodes
                 WHERE state = 'in_progress'
                 GROUP BY session_id
             )"""
    )
    # v0.18 MED-1: partial unique index — at most ONE in_progress episode per session.
    # Closes the read-then-write race in upsert_in_progress_episode (two concurrent
    # UserPromptSubmit hooks could both see no in_progress row and both INSERT).
    # Lives here rather than in SCHEMA_SQL because the state column is added by the
    # _add_column_if_missing migration above — SCHEMA_SQL executes before the column
    # exists on fresh databases. IF NOT EXISTS makes re-creation idempotent; the
    # dedup UPDATE above makes it succeed on dirty pre-v0.18 DBs too.
    try:
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_episodes_one_in_progress
               ON episodes(session_id) WHERE state = 'in_progress'"""
        )
    except sqlite3.IntegrityError:
        # Should be unreachable after the dedup above. If it fires, the MED-1
        # race protection is NOT deployed — log loudly and re-raise so the
        # failure surfaces instead of vanishing into the generic startup catch.
        log.error(
            "uq_episodes_one_in_progress creation failed: duplicate in_progress "
            "rows survived the pre-index dedup — MED-1 race protection NOT "
            "deployed. Inspect: SELECT session_id, COUNT(*) FROM episodes "
            "WHERE state='in_progress' GROUP BY session_id HAVING COUNT(*) > 1"
        )
        raise
    conn.commit()


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def create_session(
    conn: sqlite3.Connection,
    session_id: str,
    transcript_path: str | None = None,
    brand: str | None = None,
    workspace: str | None = None,
    project: str | None = None,
    started_at: str | None = None,
    commit: bool = True,
) -> str:
    """UPSERT a session row.  Re-creating the same session_id is safe.

    Pass ``commit=False`` when the caller manages the transaction (e.g.
    ``create_episode`` in app.py which wraps the full episode POST in one
    atomic transaction to prevent partial state on connection drop).
    """
    if started_at is None:
        started_at = datetime.now(timezone.utc).isoformat()
    # H4 fix: do NOT update started_at on conflict — first-write value sticks.
    # Updating started_at on every UserPromptSubmit upsert destroys the real session start time.
    conn.execute(
        """
        INSERT INTO sessions (session_id, started_at, transcript_path, brand, workspace, project)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path),
            brand           = COALESCE(excluded.brand, sessions.brand),
            workspace       = COALESCE(excluded.workspace, sessions.workspace),
            project         = COALESCE(excluded.project, sessions.project)
            -- started_at NOT updated: first-write value sticks (H4 fix)
        """,
        (session_id, started_at, transcript_path, brand, workspace, project),
    )
    if commit:
        conn.commit()
    return session_id


def end_session(
    conn: sqlite3.Connection,
    session_id: str,
    ended_at_iso: str,
    message_count: int,
    commit: bool = True,
) -> None:
    """Set ended_at and message_count on an existing session."""
    conn.execute(
        "UPDATE sessions SET ended_at = ?, message_count = ? WHERE session_id = ?",
        (ended_at_iso, message_count, session_id),
    )
    if commit:
        conn.commit()


def add_episode(
    conn: sqlite3.Connection,
    session_id: str,
    started_at: str,
    ended_at: str,
    goal_text: str | None = None,
    summary_text: str | None = None,
    source_msg_start: int | None = None,
    source_msg_end: int | None = None,
    open_questions: str | None = None,   # v0.16 hook — write None in v0.15
    advanced_goals: str | None = None,   # v0.16 hook — write None in v0.15
    blocked_goals: str | None = None,    # v0.16 hook — write None in v0.15
    state: str = "complete",             # v0.17 Phase 0: 'in_progress' | 'complete' | 'abandoned'
    commit: bool = True,
) -> int:
    """Insert an episode row; returns the new episode id (lastrowid).

    v0.17: state defaults to 'complete' for backward compatibility (Stop hook path).
    UserPromptSubmit hook uses upsert_in_progress_episode instead.
    """
    cur = conn.execute(
        """
        INSERT INTO episodes (
            session_id, started_at, ended_at,
            goal_text, summary_text,
            source_msg_start, source_msg_end,
            open_questions, advanced_goals, blocked_goals,
            state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id, started_at, ended_at,
            goal_text, summary_text,
            source_msg_start, source_msg_end,
            open_questions, advanced_goals, blocked_goals,
            state,
        ),
    )
    if commit:
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def add_link(
    conn: sqlite3.Connection,
    episode_id: int,
    link_type: str,
    target_id: str,
    target_kind: str = "mem0",
    commit: bool = True,
) -> int:
    """Insert an episode_link row; returns the new link id."""
    cur = conn.execute(
        """
        INSERT INTO episode_links (episode_id, link_type, target_kind, target_id)
        VALUES (?, ?, ?, ?)
        """,
        (episode_id, link_type, target_kind, target_id),
    )
    if commit:
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# v0.17 Phase 0.A — Within-session checkpoint functions
# ---------------------------------------------------------------------------

def upsert_in_progress_episode(
    conn: sqlite3.Connection,
    session_id: str,
    transcript_path: str | None = None,
    brand: str | None = None,
    workspace: str | None = None,
    project: str | None = None,
    prompt_text: str | None = None,
    commit: bool = True,
) -> tuple[int, str]:
    """Upsert a within-session in_progress episode checkpoint.

    Called by the UserPromptSubmit hook on every user message so partial state
    survives VS Code restarts (the Stop hook may never fire on interruption).

    Returns (episode_id, action) where action is 'created' or 'updated'.
    """
    now = _iso_now()
    # Ensure session exists (upsert — safe to call on existing session)
    create_session(
        conn, session_id, transcript_path=transcript_path,
        brand=brand, workspace=workspace, project=project,
        started_at=now, commit=False,
    )

    # Find the latest in_progress episode for this session
    row = conn.execute(
        """
        SELECT id, summary_text
        FROM episodes
        WHERE session_id = ? AND state = 'in_progress'
        ORDER BY id DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()

    if row:
        # UPDATE existing in_progress episode
        episode_id = row["id"]
        # Append prompt preview to running summary (first 200 chars, separated by " | ")
        existing_summary = row["summary_text"] or ""
        if prompt_text:
            snippet = prompt_text[:200]
            new_summary = (existing_summary + " | " + snippet).strip(" | ") if existing_summary else snippet
        else:
            new_summary = existing_summary
        # Cap summary at 800 chars (running log, not final summary)
        new_summary = new_summary[:800]
        conn.execute(
            """
            UPDATE episodes
            SET ended_at = ?, summary_text = ?
            WHERE id = ?
            """,
            (now, new_summary, episode_id),
        )
        # Track prompt count on the session (sessions table has message_count)
        conn.execute(
            "UPDATE sessions SET message_count = COALESCE(message_count, 0) + 1 WHERE session_id = ?",
            (session_id,),
        )
        action = "updated"
    else:
        # INSERT new in_progress episode (episodes table has no message_count column)
        summary = (prompt_text[:300] if prompt_text else "") or ""
        cur = conn.execute(
            """
            INSERT INTO episodes (
                session_id, started_at, ended_at,
                goal_text, summary_text, state
            ) VALUES (?, ?, ?, '', ?, 'in_progress')
            """,
            (session_id, now, now, summary),
        )
        episode_id = cur.lastrowid
        # Track first prompt on session
        conn.execute(
            "UPDATE sessions SET message_count = COALESCE(message_count, 0) + 1 WHERE session_id = ?",
            (session_id,),
        )
        action = "created"

    if commit:
        conn.commit()
    return episode_id, action  # type: ignore[return-value]


def finalize_episode(
    conn: sqlite3.Connection,
    session_id: str,
    goal_text: str,
    summary_text: str,
    ended_at: str,
    message_count: int,
    commit: bool = True,
) -> int:
    """Finalize the latest in_progress episode for session_id → state='complete'.

    Called by the Stop hook (via POST /v1/episodes) to transition the episode
    from 'in_progress' to 'complete' with the final extracted goal/summary.

    If no in_progress episode exists for the session, inserts a new complete row
    (backward compat path for direct API callers).

    Returns the episode_id.
    """
    row = conn.execute(
        """
        SELECT id FROM episodes
        WHERE session_id = ? AND state = 'in_progress'
        ORDER BY id DESC LIMIT 1
        """,
        (session_id,),
    ).fetchone()

    if row:
        episode_id = row["id"]
        conn.execute(
            """
            UPDATE episodes
            SET state = 'complete', goal_text = ?, summary_text = ?,
                ended_at = ?
            WHERE id = ?
            """,
            (goal_text, summary_text, ended_at, episode_id),
        )
        # Store final message_count on the session row (that's where it lives)
        if message_count:
            conn.execute(
                "UPDATE sessions SET message_count = ? WHERE session_id = ?",
                (message_count, session_id),
            )
    else:
        # No in_progress row — check for a recent duplicate complete row (H3 fix).
        # The Stop hook can fire twice for the same session (VS Code restart + new Stop).
        # If a complete row was written within the last 10s, UPDATE it instead of inserting.
        recent_complete = conn.execute(
            """
            SELECT id, ended_at FROM episodes
            WHERE session_id = ? AND state = 'complete'
            ORDER BY id DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()

        _within_duplicate_window = False
        if recent_complete and recent_complete["ended_at"]:
            try:
                from datetime import datetime, timezone, timedelta as _td
                recent_dt = datetime.fromisoformat(
                    str(recent_complete["ended_at"]).replace("Z", "+00:00")
                )
                ended_dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                _within_duplicate_window = abs((ended_dt - recent_dt).total_seconds()) <= 10
            except Exception:
                pass  # parse failure → fall through to insert

        if _within_duplicate_window:
            # UPDATE existing complete row rather than inserting a duplicate
            episode_id = recent_complete["id"]
            conn.execute(
                """
                UPDATE episodes
                SET goal_text = ?, summary_text = ?, ended_at = ?
                WHERE id = ?
                """,
                (goal_text, summary_text, ended_at, episode_id),
            )
            if message_count:
                conn.execute(
                    "UPDATE sessions SET message_count = ? WHERE session_id = ?",
                    (message_count, session_id),
                )
        else:
            # Insert a new complete episode (v0.16 compat path)
            cur = conn.execute(
                """
                INSERT INTO episodes (
                    session_id, started_at, ended_at,
                    goal_text, summary_text, state
                ) VALUES (?, ?, ?, ?, ?, 'complete')
                """,
                (session_id, ended_at, ended_at, goal_text, summary_text),
            )
            episode_id = cur.lastrowid
            if message_count:
                conn.execute(
                    "UPDATE sessions SET message_count = ? WHERE session_id = ?",
                    (message_count, session_id),
                )

    if commit:
        conn.commit()
    return episode_id  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# FTS5 safety helper
# ---------------------------------------------------------------------------

def _sanitize_fts(query: str) -> str | None:
    """Strip FTS5 special characters from *query* and phrase-quote each token.

    FTS5 special chars include ``" * ( ) : ^ ~ -`` — hyphens are dangerous
    because FTS5 treats ``word-word`` as negation in some tokenizer modes.
    Reserved operator words (AND, OR, NOT, NEAR) are also interpreted as FTS5
    syntax when bare.  We keep only word chars (\\w) and whitespace, replacing
    everything else with a space, then wrap each token in double-quotes so FTS5
    treats every token as a phrase literal and ignores operator semantics.
    Returns ``None`` if the result is empty.
    """
    cleaned = re.sub(r'[^\w\s]', ' ', query).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    if not cleaned:
        return None
    # Wrap each token in FTS5 phrase quotes to neutralise AND/OR/NOT/NEAR
    cleaned = ' '.join(f'"{w}"' for w in cleaned.split())
    return cleaned


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# v0.16 — Goals CRUD
# ---------------------------------------------------------------------------

def create_goal(
    conn: sqlite3.Connection,
    title: str,
    description: str | None = None,
    brand: str | None = None,
    parent_goal_id: int | None = None,
    priority: int = 3,
    first_seen_session_id: str | None = None,
    initiative: str | None = None,
    commit: bool = True,
) -> int:
    """Create a goal row. Returns new goal id (lastrowid).

    v0.22 Pillar 1: initiative stamps the cwd-derived repo/initiative
    (agentic-memory-stack vs local-offload). None == cross-cutting (surfaces
    in every session).
    """
    cur = conn.execute(
        """INSERT INTO goals (title, description, brand, parent_goal_id, priority, first_seen_session_id, initiative)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (title, description, brand, parent_goal_id, priority, first_seen_session_id, initiative),
    )
    if commit:
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_goal(
    conn: sqlite3.Connection,
    goal_id: int,
) -> dict[str, Any] | None:
    """Return goal dict with linked_episode_count, or None."""
    cur = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    cur2 = conn.execute(
        "SELECT COUNT(*) FROM episode_links WHERE link_type IN ('advanced_goal', 'blocked_goal', 'completed_goal') AND target_kind = 'goal' AND target_id = ?",
        (str(goal_id),),
    )
    d["linked_episode_count"] = cur2.fetchone()[0]
    return d


def update_goal_status(
    conn: sqlite3.Connection,
    goal_id: int,
    status: str,
    completed_at: str | None = None,
    commit: bool = True,
) -> bool:
    """UPDATE status + updated_at. Validates status. Returns True if updated."""
    if status not in VALID_GOAL_STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {sorted(VALID_GOAL_STATUSES)}")
    now = _iso_now()
    if status == "completed" and completed_at is None:
        completed_at = now
    cur = conn.execute(
        "UPDATE goals SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
        (status, completed_at, now, goal_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def list_goals(
    conn: sqlite3.Connection,
    status: str | None = None,
    brand: str | None = None,
    parent_goal_id: int | str | None = None,
    limit: int = 50,
    only_brand_neutral: bool = False,
    initiative: str | None = None,
) -> list[dict[str, Any]]:
    """List goals with optional filters. Returns list of dicts.

    v0.21 Phase A (M2): only_brand_neutral=True restricts to brand-neutral
    (NULL-brand) rows — used by context_bundle when the session brand is unknown
    so cross-brand goals never leak into an unrecognized session (mirrors the
    memory Layer-2 fail-closed semantics). Ignored when brand is a non-empty
    string; an empty/whitespace brand normalizes to None (review L4) and takes
    the brand-neutral path.

    v0.22 Pillar 1: when initiative is provided, restrict to that initiative
    PLUS cross-cutting (NULL-initiative) rows — WHERE (initiative = ? OR
    initiative IS NULL) — so an open goal from another initiative under the same
    brand never bleeds in. When initiative is None (admin/global listing, e.g.
    the MCP goals_list tool which has no initiative), the result is unfiltered on
    initiative — preserving the existing behavior.
    """
    sql = "SELECT * FROM goals WHERE 1=1"
    args: list[Any] = []
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    # M2 review L4: normalize empty/whitespace brand to None so it falls through
    # to the brand-neutral path — mirrors admission_gate's str(... or '').strip()
    # so this gate is byte-for-byte the memory Layer-2 brand normalization.
    _b = brand.strip() if isinstance(brand, str) else brand
    if _b:
        sql += " AND brand = ?"
        args.append(_b)
    elif only_brand_neutral:
        sql += " AND brand IS NULL"
    if initiative is not None:
        sql += " AND (initiative = ? OR initiative IS NULL)"
        args.append(initiative)
    if parent_goal_id is not None:
        if parent_goal_id == 0 or parent_goal_id == "root":
            sql += " AND parent_goal_id IS NULL"
        else:
            sql += " AND parent_goal_id = ?"
            args.append(parent_goal_id)
    sql += " ORDER BY priority, updated_at DESC LIMIT ?"
    args.append(limit)
    cur = conn.execute(sql, args)
    return [dict(r) for r in cur.fetchall()]


def get_goal_tree(
    conn: sqlite3.Connection,
    root_goal_id: int | None = None,
    max_depth: int = 10,
) -> list[dict[str, Any]]:
    """Recursive CTE returning tree rows with depth.

    If root_goal_id=None, returns all top-level (parent_goal_id IS NULL) trees.
    """
    if root_goal_id is None:
        sql = """
        WITH RECURSIVE goal_tree(id, parent_goal_id, title, status, priority, brand, depth) AS (
            SELECT id, parent_goal_id, title, status, priority, brand, 0
            FROM goals WHERE parent_goal_id IS NULL
            UNION ALL
            SELECT g.id, g.parent_goal_id, g.title, g.status, g.priority, g.brand, gt.depth + 1
            FROM goals g INNER JOIN goal_tree gt ON g.parent_goal_id = gt.id
            WHERE gt.depth < ?
        )
        SELECT * FROM goal_tree ORDER BY depth, priority, id
        """
        cur = conn.execute(sql, (max_depth,))
    else:
        sql = """
        WITH RECURSIVE goal_tree(id, parent_goal_id, title, status, priority, brand, depth) AS (
            SELECT id, parent_goal_id, title, status, priority, brand, 0
            FROM goals WHERE id = ?
            UNION ALL
            SELECT g.id, g.parent_goal_id, g.title, g.status, g.priority, g.brand, gt.depth + 1
            FROM goals g INNER JOIN goal_tree gt ON g.parent_goal_id = gt.id
            WHERE gt.depth < ?
        )
        SELECT * FROM goal_tree ORDER BY depth, priority, id
        """
        cur = conn.execute(sql, (root_goal_id, max_depth))
    return [dict(r) for r in cur.fetchall()]


def find_goal_by_title_fuzzy(
    conn: sqlite3.Connection,
    title: str,
    brand: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """FTS5 fuzzy match on title. Optionally restrict to brand. Returns ranked candidates."""
    sanitized = _sanitize_fts(title)
    if not sanitized:
        return []
    # v0.18 MED-3: stricter signal for short queries. A single-token query is too
    # weak a match signal — every goal containing that one word would collapse into
    # the same candidate (over-dedup of distinct goals). Queries with <3 tokens must
    # have at least 2 matching tokens: _sanitize_fts phrase-quotes each token and
    # FTS5 treats space-separated phrases as implicit AND (all must match), so a
    # 2-token query already requires both tokens — but a 1-token query can never
    # satisfy the 2-match minimum. ≥3-token queries keep the existing
    # all-tokens-must-match behavior.
    if len(sanitized.split()) < 2:
        # v0.19 M3: 1-token queries can't meet the 2-token fuzzy minimum, but an
        # exact (case-insensitive) title match is still an unambiguous signal —
        # short-circuit on equality BEFORE rejecting, otherwise the Stop-hook
        # pipeline treats [] as goal-not-found and auto-creates a duplicate goal
        # per session for recurring one-word titles (e.g. a project name).
        # Brand filter uses the NULL-safe IS operator (HIGH-4 pattern below).
        # v0.20 Phase F (L2): terminal statuses are excluded from the exact-match
        # short-circuit — matching a completed/abandoned/duplicate goal re-linked
        # new episodes to a dead goal instead of letting the pipeline auto-create
        # a fresh open one (restores pre-v0.19 semantics for closed goals while
        # keeping the M3 dedup for live ones).
        cur = conn.execute(
            "SELECT g.*, 0 AS rank FROM goals g "
            "WHERE LOWER(g.title) = LOWER(?) AND g.brand IS ? "
            "AND g.status NOT IN ('completed', 'abandoned', 'duplicate') "
            "ORDER BY g.id LIMIT ?",
            (title.strip(), brand, limit),
        )
        return [dict(r) for r in cur.fetchall()]
    # v0.21 Phase A (M1): the v0.20 L2 terminal-status guard was added only to
    # the 1-token exact-match short-circuit above; the multi-token FTS path still
    # resurrected/re-linked completed/abandoned/duplicate goals. Mirror the guard
    # here so >=2-token titles get live-goals-only semantics too (both call sites,
    # app.py:1614/1636, want only live goals).
    sql = """
    SELECT g.*, fts.rank
    FROM goals g
    INNER JOIN goals_fts fts ON g.id = fts.rowid
    WHERE goals_fts MATCH ?
    AND g.brand IS ?
    AND g.status NOT IN ('completed', 'abandoned', 'duplicate')
    """
    # SQL IS operator is NULL-safe equality: NULL IS NULL → true, 'foo' IS 'foo' → true.
    # Always filtering by brand (even when None) prevents cross-brand contamination
    # when a brand=None episode would otherwise match goals from ANY brand.
    args: list[Any] = [sanitized, brand]
    sql += " ORDER BY fts.rank LIMIT ?"
    args.append(limit)
    cur = conn.execute(sql, args)
    return [dict(r) for r in cur.fetchall()]


def link_episode_to_goal(
    conn: sqlite3.Connection,
    episode_id: int,
    goal_id: int,
    link_type: str = "advanced_goal",
    delta_text: str | None = None,  # noqa: ARG001 — reserved for Phase B
    commit: bool = True,
) -> int:
    """Insert into episode_links with target_kind='goal'.

    link_type must be one of: advanced_goal, blocked_goal, completed_goal, cited_goal.
    delta_text is accepted for API symmetry but stored externally (Phase B populates
    episodes.advanced_goals / episodes.blocked_goals JSON columns).
    """
    if link_type not in {"advanced_goal", "blocked_goal", "completed_goal", "cited_goal"}:
        raise ValueError(f"invalid link_type {link_type!r}")
    cur = conn.execute(
        "INSERT INTO episode_links (episode_id, link_type, target_kind, target_id) VALUES (?, ?, 'goal', ?)",
        (episode_id, link_type, str(goal_id)),
    )
    if commit:
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def search_fts(
    conn: sqlite3.Connection,
    query: str,
    since: str | None = None,
    until: str | None = None,
    brand: str | None = None,
    limit: int = 20,
    only_brand_neutral: bool = False,
) -> list[dict[str, Any]]:
    """Keyword search over episode goal_text + summary_text via FTS5 MATCH.

    Returns a list of episode dicts enriched with ``sessions.brand`` and
    the FTS5 ``rank`` score (lower is better).

    v0.29 R4: ``only_brand_neutral=True`` restricts to brand-neutral (NULL-brand)
    episodes — used by the context_bundle raw-trace fallback when the session
    brand is unknown, so a branded episode never leaks into an unrecognized
    session (mirrors ``_episodic_list_goals`` / the memory Layer-2 fail-closed
    gate). Ignored when ``brand`` is a non-empty string; an empty/whitespace
    brand normalizes to None (review L4) and takes the brand-neutral path.
    Default ``False`` preserves the prior cross-brand listing behaviour.
    """
    safe_query = _sanitize_fts(query)
    if safe_query is None:
        return []

    # Build WHERE clauses incrementally (parameterized — no f-string SQL)
    where_parts = ["episodes_fts MATCH ?"]
    params: list[Any] = [safe_query]

    if since:
        where_parts.append("e.ended_at >= ?")
        params.append(since)
    if until:
        where_parts.append("e.ended_at <= ?")
        params.append(until)
    # v0.29 R4 brand gate — mirrors _episodic_list_goals exactly: normalize an
    # empty/whitespace brand to None (review L4) so it never becomes AND brand='',
    # then fail closed to NULL-brand episodes when only_brand_neutral is set.
    _b = brand.strip() if isinstance(brand, str) else brand
    if _b:
        where_parts.append("s.brand = ?")
        params.append(_b)
    elif only_brand_neutral:
        where_parts.append("s.brand IS NULL")

    where_sql = " AND ".join(where_parts)
    params.append(limit)

    sql = f"""
        SELECT e.id, e.session_id, e.started_at, e.ended_at,
               e.goal_text, e.summary_text,
               e.source_msg_start, e.source_msg_end,
               e.open_questions, e.advanced_goals, e.blocked_goals,
               e.created_at, s.brand, s.workspace, s.project,
               episodes_fts.rank AS rank
        FROM episodes_fts
        JOIN episodes e ON episodes_fts.rowid = e.id
        LEFT JOIN sessions s ON e.session_id = s.session_id
        WHERE {where_sql}
        ORDER BY rank
        LIMIT ?
    """  # nosec — where_sql contains only hard-coded AND/column refs + ? placeholders
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def recent(
    conn: sqlite3.Connection,
    limit: int = 10,
    brand: str | None = None,
) -> list[dict[str, Any]]:
    """Return last *limit* episodes ordered by ended_at DESC."""
    if brand:
        rows = conn.execute(
            """
            SELECT e.*, s.brand, s.workspace, s.project
            FROM episodes e
            LEFT JOIN sessions s ON e.session_id = s.session_id
            WHERE s.brand = ?
            ORDER BY e.ended_at DESC
            LIMIT ?
            """,
            (brand, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT e.*, s.brand, s.workspace, s.project
            FROM episodes e
            LEFT JOIN sessions s ON e.session_id = s.session_id
            ORDER BY e.ended_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_episode(
    conn: sqlite3.Connection,
    episode_id: int,
) -> dict[str, Any] | None:
    """Return a single episode dict + linked memory ids, or None if not found."""
    row = conn.execute(
        """
        SELECT e.*, s.brand, s.workspace, s.project, s.transcript_path AS session_transcript
        FROM episodes e
        LEFT JOIN sessions s ON e.session_id = s.session_id
        WHERE e.id = ?
        """,
        (episode_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    link_rows = conn.execute(
        "SELECT link_type, target_kind, target_id FROM episode_links WHERE episode_id = ?",
        (episode_id,),
    ).fetchall()
    result["linked_memories"] = [dict(lr) for lr in link_rows]
    return result


def count_episodes(
    conn: sqlite3.Connection,
    since: str | None = None,
    brand: str | None = None,
) -> dict[str, Any]:
    """Return ``{count, last_ended_at}`` for health checks / Test-MemoryStack."""
    where_parts = []
    params: list[Any] = []

    if since:
        where_parts.append("e.ended_at >= ?")
        params.append(since)
    if brand:
        where_parts.append("s.brand = ?")
        params.append(brand)

    join_sql = "LEFT JOIN sessions s ON e.session_id = s.session_id" if brand else ""
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = f"""
        SELECT COUNT(*) AS cnt, MAX(e.ended_at) AS last_ended_at
        FROM episodes e {join_sql} {where_sql}
    """  # nosec — no user data in sql template; params are parameterized
    row = conn.execute(sql, params).fetchone()
    return {
        "count": row["cnt"] if row else 0,
        "last_ended_at": row["last_ended_at"] if row else None,
    }


# ---------------------------------------------------------------------------
# v0.17 Phase D — Open Questions CRUD
# ---------------------------------------------------------------------------

def create_open_question(
    conn: sqlite3.Connection,
    question_text: str,
    brand: str | None = None,
    topic: str | None = None,
    first_seen_session_id: str | None = None,
    first_seen_episode_id: int | None = None,
    related_goal_id: int | None = None,
    priority: int = 3,
    initiative: str | None = None,
    commit: bool = True,
) -> int:
    """Create an open question. Returns new id.

    v0.22 Pillar 1: initiative stamps the cwd-derived repo/initiative. None ==
    cross-cutting (surfaces in every session).
    """
    cur = conn.execute(
        """INSERT INTO open_questions
           (question_text, brand, topic, first_seen_session_id, first_seen_episode_id, related_goal_id, priority, initiative)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (question_text.strip(), brand, topic, first_seen_session_id, first_seen_episode_id, related_goal_id, priority, initiative),
    )
    if commit:
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_open_question(
    conn: sqlite3.Connection,
    oq_id: int,
) -> dict[str, Any] | None:
    """Return open_question dict + related goal info, or None."""
    cur = conn.execute("""
        SELECT oq.*, g.title AS related_goal_title
        FROM open_questions oq
        LEFT JOIN goals g ON oq.related_goal_id = g.id
        WHERE oq.id = ?
    """, (oq_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def resolve_open_question(
    conn: sqlite3.Connection,
    oq_id: int,
    resolved_in_session_id: str,
    resolution_text: str,
    commit: bool = True,
) -> bool:
    """Flip status='resolved' + populate resolution fields + bump updated_at."""
    now = _iso_now()
    cur = conn.execute(
        """UPDATE open_questions
           SET status = 'resolved', resolved_in_session_id = ?, resolved_at = ?,
               resolution_text = ?, updated_at = ?
           WHERE id = ? AND status = 'open'""",
        (resolved_in_session_id, now, resolution_text, now, oq_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def update_open_question_status(
    conn: sqlite3.Connection,
    oq_id: int,
    status: str,
    commit: bool = True,
) -> bool:
    """Transition to abandoned/duplicate. resolved goes through resolve_open_question()."""
    if status not in VALID_OPEN_QUESTION_STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {sorted(VALID_OPEN_QUESTION_STATUSES)}")
    now = _iso_now()
    cur = conn.execute(
        "UPDATE open_questions SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, oq_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def list_open_questions(
    conn: sqlite3.Connection,
    status: str | None = "open",
    brand: str | None = None,
    limit: int = 20,
    only_brand_neutral: bool = False,
    initiative: str | None = None,
) -> list[dict[str, Any]]:
    """List with status + brand filters.

    v0.21 Phase A (M2): only_brand_neutral=True restricts to brand-neutral
    (NULL-brand) rows — used by context_bundle for unknown-brand sessions so
    cross-brand open questions never leak (mirrors memory Layer-2 fail-closed).
    Ignored when brand is a non-empty string; an empty/whitespace brand
    normalizes to None (review L4) and takes the brand-neutral path.

    v0.22 Pillar 1: when initiative is provided, restrict to that initiative
    PLUS cross-cutting (NULL-initiative) rows. When None (admin/global listing),
    unfiltered on initiative (preserves the existing behavior).
    """
    sql = "SELECT * FROM open_questions WHERE 1=1"
    args: list[Any] = []
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    # M2 review L4: normalize empty/whitespace brand to None (mirrors the memory
    # Layer-2 brand normalization) so it takes the brand-neutral path, not AND brand=''.
    _b = brand.strip() if isinstance(brand, str) else brand
    if _b:
        sql += " AND brand = ?"
        args.append(_b)
    elif only_brand_neutral:
        sql += " AND brand IS NULL"
    if initiative is not None:
        sql += " AND (initiative = ? OR initiative IS NULL)"
        args.append(initiative)
    sql += " ORDER BY priority, updated_at DESC LIMIT ?"
    args.append(limit)
    cur = conn.execute(sql, args)
    return [dict(r) for r in cur.fetchall()]


def search_open_questions(
    conn: sqlite3.Connection,
    query: str,
    brand: str | None = None,
    status: str | None = "open",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """FTS5 keyword search. status='all' to ignore status filter."""
    sanitized = _sanitize_fts(query)
    if not sanitized:
        return []
    sql = """
    SELECT oq.*, fts.rank
    FROM open_questions oq
    INNER JOIN open_questions_fts fts ON oq.id = fts.rowid
    WHERE open_questions_fts MATCH ?
    """
    args: list[Any] = [sanitized]
    if status and status != "all":
        sql += " AND oq.status = ?"
        args.append(status)
    if brand is not None:
        sql += " AND oq.brand = ?"
        args.append(brand)
    sql += " ORDER BY fts.rank LIMIT ?"
    args.append(limit)
    cur = conn.execute(sql, args)
    return [dict(r) for r in cur.fetchall()]


def find_open_question_by_text_fuzzy(
    conn: sqlite3.Connection,
    text: str,
    brand: str | None = None,
    status: str | None = "open",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """For episode POST sync: dedupe questions across sessions."""
    return search_open_questions(conn, text, brand=brand, status=status, limit=limit)
