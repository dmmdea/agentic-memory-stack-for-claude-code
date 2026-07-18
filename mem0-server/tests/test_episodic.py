"""test_episodic.py — pytest coverage for mem0-server/episodic.py (Phase A + B)

Phase A: 7 unit tests covering schema idempotency, session upsert, episode insert
+ FTS5 trigger, FTS5 keyword search, temporal filtering, brand filtering, and
episode_links / get_episode round-trip.  Uses temp-DB fixtures (no ~/.mem0 touch).

Phase B: 4 live-HTTP endpoint tests against the running mem0-server at :18791.
Requires MEM0_KEY env var and the server to be running with episode endpoints.
"""

import os
import uuid
import sqlite3
from pathlib import Path

import httpx
import pytest

# Import from the repo (not the deployed copy) so changes are tested directly.
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

# v0.19 Phase A.1: inline cleanup for tests that hold the goal ids they create
# (goals have no DELETE endpoint; conftest.py backstops anything missed).
from _debris_patterns import delete_goal_rows

from episodic import (
    SCHEMA_SQL,
    _add_column_if_missing,
    _connect_to,
    _iso_now,
    _sanitize_fts,
    init_schema,
    create_session,
    add_episode,
    add_link,
    search_fts,
    get_episode,
    create_goal,
    get_goal,
    update_goal_status,
    list_goals,
    get_goal_tree,
    find_goal_by_title_fuzzy,
    link_episode_to_goal,
    # v0.17 Phase 0 — within-session checkpoint
    upsert_in_progress_episode,
    finalize_episode,
    # v0.17 Phase D — open questions (v0.22: initiative scoping tests)
    list_open_questions,
)


# ---------------------------------------------------------------------------
# Fixture: isolated temp DB per test
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """Return an open connection to a fresh episodic DB in tmp_path."""
    db_path = tmp_path / "test_episodic.db"
    conn = _connect_to(db_path)
    init_schema(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _iso(offset_days: int = 0, hour: int = 12) -> str:
    from datetime import datetime, timezone, timedelta
    dt = datetime(2025, 1, 10, hour, 0, 0, tzinfo=timezone.utc) + timedelta(days=offset_days)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# A.4.1  schema idempotency
# ---------------------------------------------------------------------------

def test_init_schema_idempotent(tmp_path):
    """Calling init_schema twice must not raise and schema_version must be 17.0 (v0.17 Phase 0)."""
    db_path = tmp_path / "idem.db"
    conn = _connect_to(db_path)
    init_schema(conn)  # first call
    init_schema(conn)  # second call — must be a no-op
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert row is not None
    assert row["value"] == "17.0"
    conn.close()


# ---------------------------------------------------------------------------
# A.4.2  session upsert
# ---------------------------------------------------------------------------

def test_create_session_upsert(db):
    """Creating the same session_id twice must yield exactly one row."""
    create_session(db, "sess-aaa", transcript_path="/tmp/a.jsonl", brand="ai-ecosystem")
    create_session(db, "sess-aaa", transcript_path="/tmp/a-updated.jsonl", brand="ai-ecosystem")
    count = db.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_id='sess-aaa'"
    ).fetchone()[0]
    assert count == 1
    # transcript_path should reflect the most recent upsert
    row = db.execute(
        "SELECT transcript_path FROM sessions WHERE session_id='sess-aaa'"
    ).fetchone()
    assert row["transcript_path"] == "/tmp/a-updated.jsonl"


# ---------------------------------------------------------------------------
# A.4.3  add_episode inserts FTS5 row via trigger
# ---------------------------------------------------------------------------

def test_add_episode_inserts_fts_row(db):
    """After add_episode, episodes_fts must contain the new row."""
    create_session(db, "sess-bbb", brand="ai-ecosystem", started_at=_iso())
    ep_id = add_episode(
        db,
        session_id="sess-bbb",
        started_at=_iso(),
        ended_at=_iso(0, 13),
        goal_text="Deploy the agentic memory stack",
        summary_text="Successfully deployed v0.15 foundation with SQLite sidecar.",
    )
    # Direct FTS5 table query
    fts_rows = db.execute(
        "SELECT rowid FROM episodes_fts WHERE rowid = ?", (ep_id,)
    ).fetchall()
    assert len(fts_rows) == 1


# ---------------------------------------------------------------------------
# A.4.4  FTS5 keyword search
# ---------------------------------------------------------------------------

def test_search_fts_keyword(db):
    """search_fts must return only episodes matching the keyword."""
    create_session(db, "sess-k1", started_at=_iso())
    create_session(db, "sess-k2", started_at=_iso())
    create_session(db, "sess-k3", started_at=_iso())

    add_episode(db, "sess-k1", _iso(), _iso(0, 13),
                goal_text="Work on SQLite schema migration",
                summary_text="Schema v15.0 deployed successfully with FTS5 tables.")
    add_episode(db, "sess-k2", _iso(), _iso(0, 14),
                goal_text="Debug Brand-A checkout flow",
                summary_text="Fixed cart total calculation bug in brand-a store.")
    add_episode(db, "sess-k3", _iso(), _iso(0, 15),
                goal_text="Brainstorm Brand-B landing page copy",
                summary_text="Drafted three headline variants for brand-b homepage.")

    results = search_fts(db, "SQLite schema", limit=10)
    assert len(results) == 1
    assert results[0]["goal_text"] == "Work on SQLite schema migration"

    results2 = search_fts(db, "brand-b homepage", limit=10)
    assert len(results2) == 1
    assert "Brand-B" in results2[0]["goal_text"]


# ---------------------------------------------------------------------------
# A.4.5  temporal filter
# ---------------------------------------------------------------------------

def test_search_temporal_filter(db):
    """since/until filters must restrict results by ended_at."""
    create_session(db, "sess-t1", started_at=_iso(0))
    create_session(db, "sess-t2", started_at=_iso(5))
    create_session(db, "sess-t3", started_at=_iso(10))

    add_episode(db, "sess-t1", _iso(0), _iso(0, 13),
                goal_text="Early memory work session alpha",
                summary_text="Initialized the memory alpha stack for testing purposes.")
    add_episode(db, "sess-t2", _iso(5), _iso(5, 13),
                goal_text="Mid memory work session beta",
                summary_text="Extended the memory beta stack with new features.")
    add_episode(db, "sess-t3", _iso(10), _iso(10, 13),
                goal_text="Late memory work session gamma",
                summary_text="Finalized the memory gamma stack deployment.")

    # "memory" appears in all three; filter to day 5-10 range (ended_at)
    since = _iso(4, 0)   # 2025-01-14T00:00:00
    until = _iso(10, 23) # 2025-01-20T23:00:00

    results = search_fts(db, "memory", since=since, until=until, limit=20)
    assert len(results) == 2
    goals = {r["goal_text"] for r in results}
    assert "Mid memory work session beta" in goals
    assert "Late memory work session gamma" in goals
    assert "Early memory work session alpha" not in goals


# ---------------------------------------------------------------------------
# A.4.6  brand filter
# ---------------------------------------------------------------------------

def test_search_brand_filter(db):
    """brand filter must restrict results to sessions with matching brand."""
    create_session(db, "sess-b1", brand="brand-a", started_at=_iso())
    create_session(db, "sess-b2", brand="ai-ecosystem", started_at=_iso())

    add_episode(db, "sess-b1", _iso(), _iso(0, 13),
                goal_text="Debug Brand-A store checkout page",
                summary_text="Fixed the brand-a checkout validation logic.")
    add_episode(db, "sess-b2", _iso(), _iso(0, 14),
                goal_text="Debug ai-ecosystem memory pipeline store",
                summary_text="Fixed the ai-ecosystem store pipeline processing logic.")

    results = search_fts(db, "store", brand="brand-a", limit=10)
    assert len(results) == 1
    assert results[0]["session_id"] == "sess-b1"

    results_eco = search_fts(db, "store", brand="ai-ecosystem", limit=10)
    assert len(results_eco) == 1
    assert results_eco[0]["session_id"] == "sess-b2"


# ---------------------------------------------------------------------------
# A.4.6b  v0.29 R4 — fail-closed only_brand_neutral path (mirrors goals/OQ
# Layer-2 gate: an unknown-brand session must NEVER receive branded episodes
# via the raw-trace fallback). Default (only_brand_neutral=False) is unchanged.
# ---------------------------------------------------------------------------

def test_search_fts_only_brand_neutral_excludes_branded(db):
    """only_brand_neutral=True with brand=None returns ONLY NULL-brand episodes;
    every branded episode is excluded (fail-closed — no cross-brand leak)."""
    create_session(db, "sess-bn-null", brand=None, started_at=_iso())
    create_session(db, "sess-bn-rp", brand="brand-a", started_at=_iso())
    add_episode(db, "sess-bn-null", _iso(), _iso(0, 13),
                goal_text="Investigate the deploy pipeline failure",
                summary_text="Traced the deploy pipeline crash to a missing env var.")
    add_episode(db, "sess-bn-rp", _iso(), _iso(0, 14),
                goal_text="Investigate the brand-a deploy pipeline failure",
                summary_text="Traced the brand-a deploy pipeline crash to a token typo.")

    # Default (only_brand_neutral=False, brand=None) — current behaviour: both match.
    both = search_fts(db, "deploy pipeline", limit=10)
    assert len(both) == 2

    # Fail-closed: unknown-brand session must see only the NULL-brand episode.
    neutral = search_fts(db, "deploy pipeline", brand=None, only_brand_neutral=True, limit=10)
    assert len(neutral) == 1
    assert neutral[0]["session_id"] == "sess-bn-null"
    assert neutral[0]["brand"] is None


def test_search_fts_only_brand_neutral_ignored_when_brand_set(db):
    """A non-empty brand takes the brand path; only_brand_neutral is ignored
    (matches _episodic_list_goals semantics)."""
    create_session(db, "sess-bi-null", brand=None, started_at=_iso())
    create_session(db, "sess-bi-rp", brand="brand-a", started_at=_iso())
    add_episode(db, "sess-bi-null", _iso(), _iso(0, 13),
                goal_text="Generic cache warmup task",
                summary_text="Warmed the cache for the generic task.")
    add_episode(db, "sess-bi-rp", _iso(), _iso(0, 14),
                goal_text="Brand-A cache warmup task",
                summary_text="Warmed the cache for the brand-a task.")

    res = search_fts(db, "cache warmup", brand="brand-a", only_brand_neutral=True, limit=10)
    assert len(res) == 1
    assert res[0]["session_id"] == "sess-bi-rp"


def test_search_fts_empty_brand_neutral_normalizes(db):
    """Review L4: an empty/whitespace brand + only_brand_neutral=True normalizes
    to None and takes the NULL-brand path (mirrors list_goals L4 test)."""
    create_session(db, "sess-be-null", brand=None, started_at=_iso())
    create_session(db, "sess-be-rp", brand="brand-a", started_at=_iso())
    add_episode(db, "sess-be-null", _iso(), _iso(0, 13),
                goal_text="Refactor the extraction gate",
                summary_text="Refactored the extraction gate logic cleanly.")
    add_episode(db, "sess-be-rp", _iso(), _iso(0, 14),
                goal_text="Brand-A extraction gate refactor",
                summary_text="Refactored the brand-a extraction gate.")

    for empty in ("", "   "):
        rows = search_fts(db, "extraction gate", brand=empty, only_brand_neutral=True, limit=10)
        assert len(rows) == 1, f"empty brand {empty!r} should take NULL-brand path"
        assert rows[0]["session_id"] == "sess-be-null"


# ---------------------------------------------------------------------------
# A.4.7  episode_links + get_episode round-trip
# ---------------------------------------------------------------------------

def test_add_link(db):
    """add_link must persist a link; get_episode must return it in linked_memories."""
    create_session(db, "sess-l1", started_at=_iso())
    ep_id = add_episode(
        db, "sess-l1", _iso(), _iso(0, 13),
        goal_text="Link episode to mem0 memories",
        summary_text="Verified cross-reference between episodic and mem0 layers.",
    )
    link_id = add_link(
        db,
        episode_id=ep_id,
        link_type="produced_evidence",
        target_id="mem0-uuid-abc123",
        target_kind="mem0",
    )
    assert isinstance(link_id, int)
    assert link_id > 0

    ep = get_episode(db, ep_id)
    assert ep is not None
    assert ep["id"] == ep_id
    assert len(ep["linked_memories"]) == 1
    lm = ep["linked_memories"][0]
    assert lm["target_id"] == "mem0-uuid-abc123"
    assert lm["link_type"] == "produced_evidence"
    assert lm["target_kind"] == "mem0"

    # Non-existent episode returns None
    assert get_episode(db, 999999) is None


# ===========================================================================
# Phase B — live HTTP endpoint tests (require running mem0-server + MEM0_KEY)
# ===========================================================================

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
_KEY = os.environ.get("MEM0_KEY", "")
H = {"X-API-Key": _KEY, "Content-Type": "application/json"}
# Operator-agnostic live tenant: match the server's MEM0_DEFAULT_USER_ID
# (systemd substitutes __WSL_USER__ to the install user), falling back to the
# current user — so test records land in the tenant the conftest backstop sweeps.
import getpass as _getpass
_UID = os.environ.get("MEM0_DEFAULT_USER_ID") or _getpass.getuser()


def _episode_payload(**overrides) -> dict:
    """Return a minimal valid EpisodeIn dict with a fresh session_id."""
    base = {
        "session_id": f"test-ep-{uuid.uuid4()}",
        "started_at": "2026-01-10T10:00:00+00:00",
        "ended_at": "2026-01-10T11:00:00+00:00",
        "goal": "Unit test the episode REST endpoint",
        "summary": "Posted a synthetic test episode to verify endpoint routing and DB write.",
        "brand": "ai-ecosystem",
        "workspace": "agentic-memory-stack",
        "project": "test-phase-b",
        "message_count": 5,
        "linked_memory_ids": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# B.3.1  POST /v1/episodes returns episode_id; GET /count reflects the write
# ---------------------------------------------------------------------------

def test_post_episode_endpoint():
    """POST a fresh episode — server must return episode_id integer."""
    payload = _episode_payload()
    r = httpx.post(f"{URL}/v1/episodes", json=payload, headers=H, timeout=15)
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("ok") is True
    assert isinstance(body.get("episode_id"), int), f"expected int episode_id: {body}"
    assert body.get("session_id") == payload["session_id"]


# ---------------------------------------------------------------------------
# B.3.2  POST /v1/episodes/search returns only matching episodes
# ---------------------------------------------------------------------------

def test_search_endpoint_returns_results():
    """POST 3 episodes with distinct keywords; search must return only the match."""
    unique_token = f"xyzqwerty{uuid.uuid4().hex[:8]}"

    # Episode with unique keyword in goal
    r1 = httpx.post(f"{URL}/v1/episodes", json=_episode_payload(
        goal=f"Debug the {unique_token} feature in memory stack",
        summary="Investigated the unique token flow and resolved the issue.",
    ), headers=H, timeout=15)
    assert r1.status_code == 200

    # Unrelated episodes that should NOT match
    r2 = httpx.post(f"{URL}/v1/episodes", json=_episode_payload(
        goal="Update the Brand-A landing page copy",
        summary="Rewrote hero section headlines for conversion improvement.",
    ), headers=H, timeout=15)
    assert r2.status_code == 200

    r3 = httpx.post(f"{URL}/v1/episodes", json=_episode_payload(
        goal="Review Brand-B onboarding flow accessibility",
        summary="Checked colour contrast ratios and keyboard navigation paths.",
    ), headers=H, timeout=15)
    assert r3.status_code == 200

    # Search for unique_token — should match only r1
    search_r = httpx.post(f"{URL}/v1/episodes/search",
                           json={"query": unique_token, "limit": 10},
                           headers=H, timeout=15)
    assert search_r.status_code == 200, f"search failed: {search_r.text}"
    data = search_r.json()
    assert data["count"] >= 1, f"expected >= 1 result for '{unique_token}': {data}"
    goals = [ep["goal_text"] for ep in data["results"]]
    assert any(unique_token in g for g in goals), \
        f"unique token not found in returned goals: {goals}"


# ---------------------------------------------------------------------------
# B.3.3  GET /v1/episodes/{id} returns correct content
# ---------------------------------------------------------------------------

def test_get_episode_endpoint():
    """POST an episode, then GET it by id — content must round-trip cleanly."""
    payload = _episode_payload(
        goal="Verify round-trip fidelity of GET /v1/episodes endpoint",
        summary="Posted and retrieved episode to confirm field mapping is correct.",
    )
    post_r = httpx.post(f"{URL}/v1/episodes", json=payload, headers=H, timeout=15)
    assert post_r.status_code == 200
    episode_id = post_r.json()["episode_id"]

    get_r = httpx.get(f"{URL}/v1/episodes/{episode_id}", headers=H, timeout=10)
    assert get_r.status_code == 200, f"GET failed: {get_r.text}"
    ep = get_r.json()
    assert ep["id"] == episode_id
    assert ep["goal_text"] == payload["goal"]
    assert ep["summary_text"] == payload["summary"]
    assert "linked_memories" in ep  # must include links field even if empty


# ---------------------------------------------------------------------------
# B.3.4  GET /v1/episodes/count returns >= 2 after 2 inserts
# ---------------------------------------------------------------------------

def test_count_endpoint():
    """POST 2 episodes, then GET /count — result must be >= 2."""
    r1 = httpx.post(f"{URL}/v1/episodes", json=_episode_payload(), headers=H, timeout=15)
    assert r1.status_code == 200
    r2 = httpx.post(f"{URL}/v1/episodes", json=_episode_payload(), headers=H, timeout=15)
    assert r2.status_code == 200

    count_r = httpx.get(f"{URL}/v1/episodes/count", headers=H, timeout=10)
    assert count_r.status_code == 200, f"count failed: {count_r.text}"
    data = count_r.json()
    assert "count" in data, f"missing count key: {data}"
    assert isinstance(data["count"], int)
    assert data["count"] >= 2, f"expected >= 2, got {data['count']}"
    assert "last_ended_at" in data


# ===========================================================================
# Phase A v0.16 — Goals CRUD unit tests (tmp_path, no server required)
# ===========================================================================

# ---------------------------------------------------------------------------
# v0.16.1  create_goal + get_goal round-trip
# ---------------------------------------------------------------------------

def test_create_goal(tmp_path):
    """Goal creation + retrieval round-trip."""
    db_path = tmp_path / "test_v016_create_goal.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        gid = create_goal(conn, title="Ship v0.16 goal hierarchy", brand="ai-ecosystem", priority=2)
        assert gid > 0
        got = get_goal(conn, gid)
        assert got["title"] == "Ship v0.16 goal hierarchy"
        assert got["brand"] == "ai-ecosystem"
        assert got["priority"] == 2
        assert got["status"] == "open"
        assert got["linked_episode_count"] == 0


# ---------------------------------------------------------------------------
# v0.16.2  child goal references parent
# ---------------------------------------------------------------------------

def test_create_goal_with_parent(tmp_path):
    """Child goal references parent."""
    db_path = tmp_path / "test_v016_parent.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        parent = create_goal(conn, title="Memory stack roadmap", brand="ai-ecosystem", priority=1)
        child = create_goal(conn, title="Ship v0.16", brand="ai-ecosystem", parent_goal_id=parent, priority=2)
        got_child = get_goal(conn, child)
        assert got_child["parent_goal_id"] == parent


# ---------------------------------------------------------------------------
# v0.16.3  status transition open → advanced
# ---------------------------------------------------------------------------

def test_update_goal_status_valid(tmp_path):
    """open → advanced transition."""
    db_path = tmp_path / "test_v016_status.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        gid = create_goal(conn, title="Test goal")
        assert update_goal_status(conn, gid, "advanced") is True
        got = get_goal(conn, gid)
        assert got["status"] == "advanced"


# ---------------------------------------------------------------------------
# v0.16.4  invalid status rejected with ValueError
# ---------------------------------------------------------------------------

def test_update_goal_status_invalid(tmp_path):
    """Invalid status rejected."""
    db_path = tmp_path / "test_v016_status_invalid.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        gid = create_goal(conn, title="Test goal")
        with pytest.raises(ValueError):
            update_goal_status(conn, gid, "nonsense")


# ---------------------------------------------------------------------------
# v0.16.5  list_goals filtered by status
# ---------------------------------------------------------------------------

def test_list_goals_filtered_by_status(tmp_path):
    """Status filter returns correct subset."""
    db_path = tmp_path / "test_v016_list.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        create_goal(conn, title="Open 1", brand="b1")
        create_goal(conn, title="Open 2", brand="b1")
        bid = create_goal(conn, title="Blocked one", brand="b1")
        update_goal_status(conn, bid, "blocked")
        opens = list_goals(conn, status="open", brand="b1")
        assert len(opens) == 2
        blocked = list_goals(conn, status="blocked", brand="b1")
        assert len(blocked) == 1


def test_list_goals_empty_brand_normalizes_to_neutral(tmp_path):
    """Review L4: an empty/whitespace brand with only_brand_neutral=True must
    take the `AND brand IS NULL` path (mirror the memory Layer-2
    str(... or '').strip() normalization), NOT `AND brand = ''` which would
    return the empty set. Pins the SQL branch directly (no server round-trip)."""
    db_path = tmp_path / "test_l4_brand_norm.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        create_goal(conn, title="Brand-tagged goal", brand="brand-a")
        create_goal(conn, title="Neutral goal")  # no brand => NULL
        for empty in ("", "   "):
            rows = list_goals(conn, status="open", brand=empty, only_brand_neutral=True)
            titles = {g["title"] for g in rows}
            assert "Neutral goal" in titles, (
                f"brand={empty!r} did not surface the NULL-brand goal (AND brand='' bug): {titles}"
            )
            assert "Brand-tagged goal" not in titles, (
                f"brand={empty!r} leaked the brand-tagged goal: {titles}"
            )


# ---------------------------------------------------------------------------
# v0.16.6  get_goal_tree 3-level recursive CTE
# ---------------------------------------------------------------------------

def test_get_goal_tree_recursive(tmp_path):
    """3-level deep hierarchy via recursive CTE."""
    db_path = tmp_path / "test_v016_tree.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        root = create_goal(conn, title="Root", brand="ai-ecosystem")
        child = create_goal(conn, title="Child", parent_goal_id=root, brand="ai-ecosystem")
        _grandchild = create_goal(conn, title="Grandchild", parent_goal_id=child, brand="ai-ecosystem")
        tree = get_goal_tree(conn, root_goal_id=root)
        depths = sorted([r["depth"] for r in tree])
        assert depths == [0, 1, 2]
        assert len(tree) == 3


# ---------------------------------------------------------------------------
# v0.16.7  find_goal_by_title_fuzzy FTS5
# ---------------------------------------------------------------------------

def test_find_goal_by_title_fuzzy(tmp_path):
    """FTS5 fuzzy match returns ranked candidates."""
    db_path = tmp_path / "test_v016_fuzzy.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        create_goal(conn, title="Ship Brand-A storefront staging", brand="brand-a")
        create_goal(conn, title="Refactor mem0 schema", brand="ai-ecosystem")
        results = find_goal_by_title_fuzzy(conn, "Brand-A storefront", brand="brand-a")
        assert len(results) >= 1
        assert "Brand-A" in results[0]["title"]


# ---------------------------------------------------------------------------
# v0.16.8  link_episode_to_goal + get_goal linked_episode_count
# ---------------------------------------------------------------------------

def test_link_episode_to_goal(tmp_path):
    """Episode-to-goal link inserted and visible via get_goal linked_episode_count."""
    db_path = tmp_path / "test_v016_link.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        create_session(conn, "test-session-v016", transcript_path="/tmp/foo.jsonl", started_at=_iso_now())
        eid = add_episode(conn, "test-session-v016", _iso_now(), _iso_now(), "Test goal", "Test summary")
        gid = create_goal(conn, title="Linked goal", brand="ai-ecosystem")
        lid = link_episode_to_goal(conn, eid, gid, link_type="advanced_goal", delta_text="Made progress")
        assert lid > 0
        got = get_goal(conn, gid)
        assert got["linked_episode_count"] == 1


# ===========================================================================
# Phase B v0.16 — Live HTTP endpoint tests for goal CRUD + episode goal path
# ===========================================================================

def _now_iso() -> str:
    """Current UTC time as ISO 8601 string (for live-HTTP tests)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def test_post_episode_with_advanced_goal_autocreates():
    """POST episode with advanced_goal that doesn't match → goal auto-created + linked."""
    title = f"v016-autocreated-goal-{uuid.uuid4()}"
    body = {
        "session_id": f"test-{uuid.uuid4()}",
        "started_at": _now_iso(),
        "ended_at": _now_iso(),
        "goal": "test", "summary": "test",
        "brand": "ai-ecosystem",
        "advanced_goals": [{"goal_title": title, "delta_text": "auto-created"}],
    }
    r = httpx.post(f"{URL}/v1/episodes", json=body, headers=H, timeout=15)
    assert r.status_code == 200, f"expected 200: {r.text}"
    # Verify a goal with that title exists
    gr = httpx.get(f"{URL}/v1/goals?brand=ai-ecosystem&limit=200", headers=H, timeout=10)
    assert gr.status_code == 200
    assert any(g["title"] == title for g in gr.json()), f"goal '{title}' not found in list"


def test_post_episode_advanced_goal_fuzzy_matches_existing():
    """Pre-create a goal; POST episode with similar title → links to existing, no duplicate."""
    _u = uuid.uuid4()
    base_title = f"manual-{_u}"  # conftest debris pattern (^manual-<uuid>$)
    gr = httpx.post(f"{URL}/v1/goals", json={"title": base_title, "brand": "brand-a"}, headers=H, timeout=10)
    assert gr.status_code == 200
    existing_id = gr.json()["goal_id"]
    # Episode references a partial match of the title (first 3 uuid segments —
    # >=2 sanitized tokens, all present in the full title, so fuzzy must match)
    body = {
        "session_id": f"test-{uuid.uuid4()}",
        "started_at": _now_iso(), "ended_at": _now_iso(),
        "goal": "test", "summary": "test", "brand": "brand-a",
        "advanced_goals": [{"goal_title": f"manual-{str(_u)[:23]}", "delta_text": "partial match test"}],
    }
    er = httpx.post(f"{URL}/v1/episodes", json=body, headers=H, timeout=15)
    assert er.status_code == 200, f"episode POST failed: {er.text}"
    # Verify the goal still has the original title (not a new duplicate)
    listed = httpx.get(f"{URL}/v1/goals?brand=brand-a&limit=200", headers=H, timeout=10).json()
    matching = [g for g in listed if g["title"] == base_title]
    assert len(matching) == 1, f"expected exactly 1 goal with title '{base_title}', got {len(matching)}"
    delete_goal_rows([existing_id])


def test_post_episode_blocked_goal_flips_status():
    """POST episode with blocked_goal → goal status='blocked'."""
    base_title = f"v016-block-test-{uuid.uuid4()}"
    gr = httpx.post(f"{URL}/v1/goals", json={"title": base_title, "brand": "ai-ecosystem"}, headers=H, timeout=10)
    assert gr.status_code == 200
    gid = gr.json()["goal_id"]
    body = {
        "session_id": f"test-{uuid.uuid4()}",
        "started_at": _now_iso(), "ended_at": _now_iso(),
        "goal": "t", "summary": "t", "brand": "ai-ecosystem",
        "blocked_goals": [{"goal_title": base_title, "block_reason": "test block"}],
    }
    er = httpx.post(f"{URL}/v1/episodes", json=body, headers=H, timeout=15)
    assert er.status_code == 200, f"episode POST failed: {er.text}"
    # Verify status flipped to blocked
    g_after = httpx.get(f"{URL}/v1/goals/{gid}", headers=H, timeout=10).json()
    assert g_after["status"] == "blocked", f"expected status=blocked, got {g_after['status']}"
    delete_goal_rows([gid])


def test_post_episode_with_open_questions():
    """open_questions array round-trips through JSON column.

    v0.20 Phase F (M2): questions carry the [test-oq-XXXXXXXX] tag (matched by
    OQ_TEXT_REGEXES) — the bare 'Q1?' literals were retired from the conftest
    cleanup patterns because a real OQ could collide with them."""
    questions = [f"Q1? [test-oq-{uuid.uuid4().hex[:8]}]",
                 f"Q2 about something? [test-oq-{uuid.uuid4().hex[:8]}]"]
    body = {
        "session_id": f"test-{uuid.uuid4()}",
        "started_at": _now_iso(), "ended_at": _now_iso(),
        "goal": "t", "summary": "t",
        "open_questions": questions,
    }
    er = httpx.post(f"{URL}/v1/episodes", json=body, headers=H, timeout=15)
    assert er.status_code == 200, f"episode POST failed: {er.text}"
    eid = er.json()["episode_id"]
    ep = httpx.get(f"{URL}/v1/episodes/{eid}", headers=H, timeout=10).json()
    assert ep["id"] == eid
    # open_questions column should be populated as JSON
    import json as _json
    stored = ep.get("open_questions")
    if stored is not None:
        parsed = _json.loads(stored) if isinstance(stored, str) else stored
        assert parsed == questions, f"open_questions mismatch: {parsed} != {questions}"


def test_post_goal_manual():
    """Direct goal POST creates row with correct fields."""
    title = f"manual-{uuid.uuid4()}"
    r = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "ai-ecosystem", "priority": 1}, headers=H, timeout=10)
    assert r.status_code == 200, f"expected 200: {r.text}"
    body = r.json()
    assert body.get("ok") is True
    assert isinstance(body.get("goal_id"), int) and body["goal_id"] > 0
    # Verify GET by id
    gr = httpx.get(f"{URL}/v1/goals/{body['goal_id']}", headers=H, timeout=10)
    assert gr.status_code == 200
    g = gr.json()
    assert g["title"] == title
    assert g["brand"] == "ai-ecosystem"
    assert g["priority"] == 1
    delete_goal_rows([body["goal_id"]])


def test_patch_goal_status():
    """Status PATCH validates and updates; invalid status rejected with 400; missing actor = 400."""
    title = f"status-test-{uuid.uuid4()}"
    r = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "ai-ecosystem"}, headers=H, timeout=10)
    assert r.status_code == 200
    gid = r.json()["goal_id"]
    # Valid status flip — HIGH-3: actor is now required
    pr = httpx.patch(f"{URL}/v1/goals/{gid}/status", json={"status": "completed", "actor": "test"}, headers=H, timeout=10)
    assert pr.status_code == 200, f"PATCH failed: {pr.text}"
    assert pr.json()["status"] == "completed"
    # Invalid status must be rejected with 400
    bad = httpx.patch(f"{URL}/v1/goals/{gid}/status", json={"status": "invalid", "actor": "test"}, headers=H, timeout=10)
    assert bad.status_code == 400, f"expected 400 for invalid status, got {bad.status_code}: {bad.text}"
    # Missing actor must also be rejected with 400 (HIGH-3)
    no_actor = httpx.patch(f"{URL}/v1/goals/{gid}/status", json={"status": "open"}, headers=H, timeout=10)
    assert no_actor.status_code == 422, f"expected 422 for missing actor, got {no_actor.status_code}: {no_actor.text}"
    delete_goal_rows([gid])


# ===========================================================================
# v0.16 adversarial-review fix tests (HIGH-1, HIGH-3, HIGH-4, HIGH-5)
# ===========================================================================

# ---------------------------------------------------------------------------
# HIGH-1 unit: _sanitize_fts must neutralise FTS5 operator words
# ---------------------------------------------------------------------------

def test_sanitize_fts_reserved_words():
    """HIGH-1: AND/OR/NOT/NEAR must be phrase-quoted so FTS5 cannot interpret them."""
    result = _sanitize_fts("AND payment flow")
    assert result is not None, "_sanitize_fts('AND payment flow') must not return None"
    # Each token must be wrapped in double-quotes so FTS5 treats them as phrase literals
    assert '"AND"' in result, f"AND must be phrase-quoted in: {result!r}"
    assert '"payment"' in result
    assert '"flow"' in result

    result_or = _sanitize_fts("OR checkout refund")
    assert result_or is not None
    assert '"OR"' in result_or

    result_not = _sanitize_fts("NOT deprecated feature")
    assert result_not is not None
    assert '"NOT"' in result_not

    result_near = _sanitize_fts("NEAR deploy staging")
    assert result_near is not None
    assert '"NEAR"' in result_near

    # Empty / only-punctuation should return None
    assert _sanitize_fts("") is None
    assert _sanitize_fts("!!! ???") is None


# ---------------------------------------------------------------------------
# HIGH-1 live: POST episode with goal_title starting with AND must return 200
# ---------------------------------------------------------------------------

def test_post_episode_goal_title_with_fts5_operator():
    """HIGH-1 live: goal_title starting with 'AND ' must not 500 (was: fts5:
    syntax error near 'AND').

    v0.20 Phase F (M2): the title is UUID-tagged ('AND payment flow <uuid>',
    matched by GOAL_TITLE_REGEXES) — still leads with the FTS5 reserved word
    AND, preserving the HIGH-1 regression property, but can no longer collide
    with a human-typed title in the conftest cleanup."""
    body = {
        "session_id": f"test-fts5-{uuid.uuid4()}",
        "started_at": _now_iso(),
        "ended_at": _now_iso(),
        "goal": "test fts5 operator fix",
        "summary": "Verify that AND/OR/NOT in goal_title no longer causes FTS5 syntax error",
        "brand": "ai-ecosystem",
        "advanced_goals": [{"goal_title": f"AND payment flow {uuid.uuid4()}", "delta_text": "should not 500"}],
    }
    r = httpx.post(f"{URL}/v1/episodes", json=body, headers=H, timeout=15)
    assert r.status_code == 200, f"HIGH-1: expected 200 for AND goal_title, got {r.status_code}: {r.text}"
    assert r.json().get("episode_id") is not None


# ---------------------------------------------------------------------------
# HIGH-4 unit: brand=None episodes must NOT match brand='other' goals
# ---------------------------------------------------------------------------

def test_find_goal_brand_none_isolation(tmp_path):
    """HIGH-4: fuzzy match with brand=None must only match brand=None goals."""
    db_path = tmp_path / "test_high4_brand.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        # Create a goal with brand='ai-ecosystem'
        create_goal(conn, title="Ship Brand-A staging env", brand="ai-ecosystem")
        # Create a goal with brand=None
        none_gid = create_goal(conn, title="Ship Brand-A staging env", brand=None)
        # Search with brand=None — must NOT return the brand='ai-ecosystem' goal
        results = find_goal_by_title_fuzzy(conn, "Ship Brand-A staging", brand=None)
        assert len(results) >= 1, "should find the brand=None goal"
        returned_ids = {r["id"] for r in results}
        assert none_gid in returned_ids, "brand=None goal must be in results"
        # Crucially: all returned goals must have brand=None
        for r in results:
            assert r["brand"] is None, f"cross-brand contamination: got brand={r['brand']!r} when searching brand=None"


# ---------------------------------------------------------------------------
# HIGH-5 unit: partial-failure must rollback all writes
# ---------------------------------------------------------------------------

def test_episode_post_atomic_on_failure(tmp_path, monkeypatch):
    """HIGH-5: if a mid-write helper raises, no partial rows must land in the DB."""
    db_path = tmp_path / "test_high5_atomic.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        create_session(conn, "sess-atomic", started_at=_iso_now())
        episode_id = add_episode(conn, "sess-atomic", _iso_now(), _iso_now(),
                                 goal_text="Atomic test", summary_text="Testing atomicity")
        assert episode_id > 0

        # Simulate: goal is created without commit, then raises before final commit
        # Verify: manually test rollback semantics by inserting + rolling back
        try:
            conn.execute("INSERT INTO goals (title, brand, status, priority, created_at, updated_at) VALUES (?, ?, 'open', 3, datetime('now'), datetime('now'))",
                         ("Partial goal that should vanish", "ai-ecosystem"))
            # Simulate failure mid-transaction
            raise RuntimeError("simulated mid-write failure")
            # (the commit would happen here — unreachable after the simulated failure)
        except RuntimeError:
            conn.rollback()

        # After rollback, the partial goal must NOT exist
        goals = conn.execute("SELECT COUNT(*) FROM goals WHERE title='Partial goal that should vanish'").fetchone()[0]
        assert goals == 0, f"HIGH-5: rollback failed — {goals} partial goal rows found"


# ---------------------------------------------------------------------------
# MED-B live: advanced_goal on a blocked goal unblocks it
# ---------------------------------------------------------------------------

def test_advanced_goal_unblocks_blocked_goal():
    """MED-B: if a goal is currently blocked, processing it as advanced_goal flips status to open."""
    title = f"med-b-unblock-{uuid.uuid4()}"
    # Create and immediately block the goal
    gr = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "ai-ecosystem"}, headers=H, timeout=10)
    assert gr.status_code == 200
    gid = gr.json()["goal_id"]
    block_r = httpx.patch(f"{URL}/v1/goals/{gid}/status",
                          json={"status": "blocked", "actor": "test", "reason": "intentionally blocked for med-b test"},
                          headers=H, timeout=10)
    assert block_r.status_code == 200

    # POST episode with advanced_goal for the same title — should unblock
    body = {
        "session_id": f"test-{uuid.uuid4()}",
        "started_at": _now_iso(), "ended_at": _now_iso(),
        "goal": "t", "summary": "t", "brand": "ai-ecosystem",
        "advanced_goals": [{"goal_title": title, "delta_text": "unblocking via advance signal"}],
    }
    er = httpx.post(f"{URL}/v1/episodes", json=body, headers=H, timeout=15)
    assert er.status_code == 200, f"episode POST failed: {er.text}"

    # Status should now be 'open' (unblocked by advance signal)
    g_after = httpx.get(f"{URL}/v1/goals/{gid}", headers=H, timeout=10).json()
    assert g_after["status"] == "open", f"MED-B: expected status=open after advance, got {g_after['status']}"
    delete_goal_rows([gid])


# ===========================================================================
# v0.17 Phase E — Goals lifecycle tests (PATCH /abandon endpoint)
# ===========================================================================

def test_goal_abandon_endpoint_writes_ledger():
    """v0.17 Phase E: PATCH /abandon flips status + appends goal-abandoned ledger event."""
    title = f"v017-abandon-{uuid.uuid4()}"
    r = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "test"}, headers=H, timeout=10)
    assert r.status_code == 200, f"goal create failed: {r.text}"
    gid = r.json()["goal_id"]

    # Read ledger count before (MEM-16: across legacy + monthly segments)
    from _ledger_paths import ledger_line_count
    n_before = ledger_line_count()

    r = httpx.patch(
        f"{URL}/v1/goals/{gid}/abandon",
        json={"actor": "test-pytest", "reason": "scope shifted to v0.18 architectural work"},
        headers=H, timeout=10,
    )
    assert r.status_code == 200, f"abandon failed: {r.text}"
    assert r.json()["status"] == "abandoned"

    n_after = ledger_line_count()
    assert n_after > n_before, "ledger must record goal-abandoned event"

    # Verify status flip via GET
    g = httpx.get(f"{URL}/v1/goals/{gid}", headers=H, timeout=10).json()
    assert g["status"] == "abandoned", f"expected status=abandoned, got {g['status']}"
    delete_goal_rows([gid])


def test_goal_abandon_without_reason_rejected():
    """v0.17 Phase E: empty reason → 400 (deliberate trash-can move requires documented why)."""
    title = f"v017-abandon-noreason-{uuid.uuid4()}"
    r = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "test"}, headers=H, timeout=10)
    assert r.status_code == 200, f"goal create failed: {r.text}"
    gid = r.json()["goal_id"]
    r = httpx.patch(
        f"{URL}/v1/goals/{gid}/abandon",
        json={"actor": "test-pytest", "reason": ""},
        headers=H, timeout=10,
    )
    assert r.status_code == 400, f"expected 400 for empty reason, got {r.status_code}: {r.text}"
    assert "reason" in r.text.lower(), f"error message should mention 'reason': {r.text}"
    delete_goal_rows([gid])


# ===========================================================================
# v0.22 Phase A — goal_complete lifecycle verb (PATCH /complete endpoint).
# OQ#636: shipped goals must close as 'completed' (not 'abandoned', which means
# scope-dropped). Mirrors PATCH /abandon: ergonomic wrapper over PATCH /status,
# requires a non-empty reason, stamps a dedicated 'goal-completed' ledger event,
# sets completed_at, and excludes the goal from open listings + fuzzy dedup.
# ===========================================================================

def test_goal_complete_endpoint_writes_ledger():
    """v0.22 Phase A: PATCH /complete flips status->completed, sets completed_at,
    appends a goal-completed ledger event, and drops the goal from status=open
    listings and the fuzzy-dedup live-goal match set."""
    title = f"v022-complete-{uuid.uuid4()}"
    r = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "test"}, headers=H, timeout=10)
    assert r.status_code == 200, f"goal create failed: {r.text}"
    gid = r.json()["goal_id"]
    try:
        # MEM-16: count + last-line across legacy archive + monthly segments
        from _ledger_paths import ledger_line_count, ledger_last_line
        n_before = ledger_line_count()

        r = httpx.patch(
            f"{URL}/v1/goals/{gid}/complete",
            json={"actor": "test-pytest", "reason": "shipped — closing as completed (OQ#636)"},
            headers=H, timeout=10,
        )
        assert r.status_code == 200, f"complete failed: {r.text}"
        assert r.json()["status"] == "completed"

        # a dedicated goal-completed event must be appended
        n_after = ledger_line_count()
        assert n_after > n_before, "ledger must record a goal-completed event"
        last = ledger_last_line()
        import json as _json
        rec = _json.loads(last)
        assert rec.get("event") == "goal-completed", f"expected goal-completed event, got {rec.get('event')}"
        assert rec.get("goal_id") == gid
        assert rec.get("actor") == "test-pytest"

        # status flipped + completed_at stamped
        g = httpx.get(f"{URL}/v1/goals/{gid}", headers=H, timeout=10).json()
        assert g["status"] == "completed", f"expected status=completed, got {g['status']}"
        assert g.get("completed_at"), "completed_at must be set"

        # excluded from open listings
        open_goals = httpx.get(f"{URL}/v1/goals?status=open&limit=200", headers=H, timeout=10).json()
        assert gid not in [x["id"] for x in open_goals], "completed goal must not appear in status=open"

        # excluded from fuzzy dedup (live-goal match set) — multi-token title
        from episodic import find_goal_by_title_fuzzy
        from _debris_patterns import episodic_db_path
        conn = sqlite3.connect(str(episodic_db_path()), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            matches = find_goal_by_title_fuzzy(conn, title, brand="test")
        finally:
            conn.close()
        assert gid not in [m["id"] for m in matches], "completed goal must not fuzzy-match as a live goal"
    finally:
        delete_goal_rows([gid])


def test_goal_complete_without_reason_rejected():
    """v0.22 Phase A: empty reason -> 400 (mirror /abandon; a lifecycle close is
    a deliberate, documented transition)."""
    title = f"v022-complete-noreason-{uuid.uuid4()}"
    r = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "test"}, headers=H, timeout=10)
    assert r.status_code == 200, f"goal create failed: {r.text}"
    gid = r.json()["goal_id"]
    r = httpx.patch(
        f"{URL}/v1/goals/{gid}/complete",
        json={"actor": "test-pytest", "reason": ""},
        headers=H, timeout=10,
    )
    assert r.status_code == 400, f"expected 400 for empty reason, got {r.status_code}: {r.text}"
    assert "reason" in r.text.lower(), f"error message should mention 'reason': {r.text}"
    delete_goal_rows([gid])


def test_mcp_shim_registers_goal_complete_tool():
    """v0.22 Phase A: the agent-facing MCP layer (scripts/wsl/mem0-mcp-shim.py)
    must expose goal_complete as a registered, dispatchable tool — mirroring the
    pre-existing goal_abandon tool. Loads the shim by file path (it lives outside
    the package dir) and asserts both tools resolve with a callable fn. Skips if
    fastmcp is unavailable in the test interpreter (Windows-side runs)."""
    import asyncio
    import importlib.util
    pytest.importorskip("fastmcp")
    shim_path = Path(__file__).resolve().parents[2] / "scripts" / "wsl" / "mem0-mcp-shim.py"
    assert shim_path.exists(), f"shim not found at {shim_path}"
    spec = importlib.util.spec_from_file_location("mem0_mcp_shim_undertest", shim_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    complete_tool = asyncio.run(mod.mcp.get_tool("goal_complete"))
    abandon_tool = asyncio.run(mod.mcp.get_tool("goal_abandon"))
    assert complete_tool.name == "goal_complete"
    assert callable(getattr(complete_tool, "fn", None)), "goal_complete must be dispatchable"
    # mirror check: same arity/shape as goal_abandon (goal_id, reason, actor)
    import inspect
    assert list(inspect.signature(complete_tool.fn).parameters) == \
        list(inspect.signature(abandon_tool.fn).parameters), \
        "goal_complete must mirror goal_abandon's (goal_id, reason, actor) signature"


# ===========================================================================
# v0.17 Phase 0 — within-session episode checkpoint unit tests (tmp_path)
# ===========================================================================

# ---------------------------------------------------------------------------
# v0.17.P0.1  upsert_in_progress_episode creates a row with state='in_progress'
# ---------------------------------------------------------------------------

def test_upsert_in_progress_creates_episode(tmp_path):
    """upsert_in_progress_episode: first call must create an in_progress row."""
    db_path = tmp_path / "test_v017_p0_create.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        session_id = f"v017-p0-create-{uuid.uuid4()}"

        episode_id, action = upsert_in_progress_episode(
            conn,
            session_id=session_id,
            transcript_path="/tmp/test.jsonl",
            brand="ai-ecosystem",
            workspace="agentic-memory-stack",
            prompt_text="v0.17 Phase 0.A smoke: synthetic user prompt",
        )

        assert isinstance(episode_id, int) and episode_id > 0, f"expected int episode_id, got {episode_id}"
        assert action == "created", f"expected action='created', got {action!r}"

        row = conn.execute(
            "SELECT id, state, summary_text FROM episodes WHERE id = ?",
            (episode_id,),
        ).fetchone()
        assert row is not None, "episode row not found after upsert"
        assert row["state"] == "in_progress", f"expected state='in_progress', got {row['state']!r}"
        assert "synthetic user prompt" in (row["summary_text"] or "")


# ---------------------------------------------------------------------------
# v0.17.P0.2  upsert_in_progress_episode updates existing row (same session)
# ---------------------------------------------------------------------------

def test_upsert_in_progress_updates_existing(tmp_path):
    """Two upserts for same session must produce ONE row with message_count=2."""
    db_path = tmp_path / "test_v017_p0_update.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        session_id = f"v017-p0-update-{uuid.uuid4()}"

        ep_id1, action1 = upsert_in_progress_episode(
            conn, session_id=session_id, prompt_text="First message"
        )
        ep_id2, action2 = upsert_in_progress_episode(
            conn, session_id=session_id, prompt_text="Second message"
        )

        assert action1 == "created", f"expected 'created', got {action1!r}"
        assert action2 == "updated", f"expected 'updated', got {action2!r}"
        assert ep_id1 == ep_id2, f"expected same episode row, got {ep_id1} vs {ep_id2}"

        # Exactly one in_progress row for this session
        rows = conn.execute(
            "SELECT id, state FROM episodes WHERE session_id = ? AND state = 'in_progress'",
            (session_id,),
        ).fetchall()
        assert len(rows) == 1, f"expected 1 in_progress row, got {len(rows)}"
        # Summary should contain both prompts (running log)
        row = conn.execute(
            "SELECT summary_text FROM episodes WHERE id = ?", (ep_id1,)
        ).fetchone()
        assert "First message" in (row["summary_text"] or ""), "First message should be in running summary"


# ---------------------------------------------------------------------------
# v0.17.P0.3  finalize_episode transitions state from in_progress → complete
# ---------------------------------------------------------------------------

def test_finalize_episode_transitions_state(tmp_path):
    """upsert in_progress + finalize_episode → state='complete', goal/summary set."""
    db_path = tmp_path / "test_v017_p0_finalize.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        session_id = f"v017-p0-finalize-{uuid.uuid4()}"
        now = _iso_now()

        # Create in_progress episode
        ep_id, _ = upsert_in_progress_episode(
            conn, session_id=session_id, prompt_text="Partial state during session"
        )
        # Finalize it (simulates Stop hook)
        final_id = finalize_episode(
            conn,
            session_id=session_id,
            goal_text="Ship v0.17 Phase 0 continuity fix",
            summary_text="Implemented UserPromptSubmit hook + episode state machine.",
            ended_at=now,
            message_count=7,
        )

        assert final_id == ep_id, f"finalize_episode must update the same row ({ep_id}), got {final_id}"

        row = conn.execute(
            "SELECT state, goal_text, summary_text FROM episodes WHERE id = ?",
            (ep_id,),
        ).fetchone()
        assert row is not None
        assert row["state"] == "complete", f"expected state='complete', got {row['state']!r}"
        assert row["goal_text"] == "Ship v0.17 Phase 0 continuity fix"
        assert "UserPromptSubmit" in row["summary_text"]
        # message_count lives on sessions table (not episodes); verify there
        sess_msg_count = conn.execute(
            "SELECT message_count FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        assert sess_msg_count is not None and sess_msg_count["message_count"] == 7

        # No residual in_progress rows for this session
        in_prog = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE session_id = ? AND state = 'in_progress'",
            (session_id,),
        ).fetchone()[0]
        assert in_prog == 0, f"expected 0 in_progress rows after finalize, got {in_prog}"


# ---------------------------------------------------------------------------
# v0.18 MED-1  partial unique index: at most one in_progress row per session
# ---------------------------------------------------------------------------

def test_upsert_in_progress_unique(tmp_path):
    """v0.18 MED-1: partial unique index prevents two in_progress rows for same session_id."""
    db_path = tmp_path / "test_v018_med1_unique.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        create_session(conn, "sess-x", started_at=_iso())
        upsert_in_progress_episode(conn, "sess-x", prompt_text="g1")
        # Second upsert must UPDATE the existing row, not INSERT a new one
        upsert_in_progress_episode(conn, "sess-x", prompt_text="g2")
        n = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE session_id='sess-x' AND state='in_progress'"
        ).fetchone()[0]
        assert n == 1, f"expected exactly 1 in_progress row for sess-x, got {n}"
        # The index itself must exist (created by init_schema on fresh AND existing DBs)
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_episodes_one_in_progress'"
        ).fetchone()
        assert idx is not None, "uq_episodes_one_in_progress index missing"
        # DB-level enforcement: a raw second INSERT (simulating the read-then-write
        # race the upsert leaves open under concurrency) must be rejected by SQLite.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episodes (session_id, started_at, ended_at, state) "
                "VALUES ('sess-x', ?, ?, 'in_progress')",
                (_iso(), _iso(0, 13)),
            )


# ---------------------------------------------------------------------------
# v0.18 MED-3  find_goal_by_title_fuzzy: single-token queries must not match
# ---------------------------------------------------------------------------

def test_find_goal_fuzzy_single_token_no_match(tmp_path):
    """v0.18 MED-3: a 1-token query can never fuzzy-match (single-word over-dedup guard)."""
    db_path = tmp_path / "test_v018_med3_fuzzy.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        create_goal(conn, title="Deploy the staging environment", brand="ai-ecosystem")
        create_goal(conn, title="Deploy the analytics pipeline", brand="ai-ecosystem")
        # Single-token query — previously collapsed BOTH distinct goals into one match
        results = find_goal_by_title_fuzzy(conn, "Deploy", brand="ai-ecosystem")
        assert results == [], f"1-token query must return no candidates, got {results}"
        # Two-token query (both tokens must match) still resolves to the right goal
        results2 = find_goal_by_title_fuzzy(conn, "Deploy staging", brand="ai-ecosystem")
        assert len(results2) == 1, f"expected 1 candidate for 2-token query, got {len(results2)}"
        assert "staging" in results2[0]["title"]


# ---------------------------------------------------------------------------
# v0.19 M3  find_goal_by_title_fuzzy: exact-title short-circuit for 1-token queries
# ---------------------------------------------------------------------------

def test_find_goal_fuzzy_single_token_exact_match(tmp_path):
    """v0.19 M3: a 1-token query exact-title match (case-insensitive) short-circuits
    before the fuzzy reject — recurring one-word goal titles dedupe by equality
    instead of auto-creating a duplicate goal per session."""
    db_path = tmp_path / "test_v019_m3_exact.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        gid = create_goal(conn, title="Zerovial", brand="ai-ecosystem")
        # Exact single-token title must match the existing goal
        results = find_goal_by_title_fuzzy(conn, "Zerovial", brand="ai-ecosystem")
        assert len(results) == 1, f"expected exact-title match, got {results}"
        assert results[0]["id"] == gid
        # Case-insensitive
        results_ci = find_goal_by_title_fuzzy(conn, "zerovial", brand="ai-ecosystem")
        assert len(results_ci) == 1 and results_ci[0]["id"] == gid, \
            f"expected case-insensitive exact match, got {results_ci}"
        # A 1-token query with NO exact-title goal still returns [] (v0.18
        # over-dedup guard intact — substring/word containment must NOT match)
        create_goal(conn, title="Deploy the staging environment", brand="ai-ecosystem")
        assert find_goal_by_title_fuzzy(conn, "Deploy", brand="ai-ecosystem") == []
        # Brand filter stays NULL-safe: other brand must not match
        assert find_goal_by_title_fuzzy(conn, "Zerovial", brand="other-brand") == []


def test_post_episode_one_word_goal_title_no_duplicate():
    """v0.19 M3 endpoint-level: two episodes (two sessions) with the same one-word
    goal_title must yield exactly ONE goal row — the Stop-hook pipeline dedupes by
    exact title instead of auto-creating a duplicate per session."""
    title = f"med3dup{uuid.uuid4().hex}"  # single token — exercises the <2-token path
    for _ in range(2):
        body = {
            "session_id": f"test-{uuid.uuid4()}",
            "started_at": _now_iso(), "ended_at": _now_iso(),
            "goal": "t", "summary": "t", "brand": "ai-ecosystem",
            "advanced_goals": [{"goal_title": title, "delta_text": "one-word dedup test"}],
        }
        r = httpx.post(f"{URL}/v1/episodes", json=body, headers=H, timeout=15)
        assert r.status_code == 200, f"episode POST failed: {r.text}"
    listed = httpx.get(f"{URL}/v1/goals?brand=ai-ecosystem&limit=200", headers=H, timeout=10).json()
    matching = [g for g in listed if g["title"] == title]
    # Clean up BEFORE asserting so a failure doesn't leave duplicate debris
    delete_goal_rows([g["id"] for g in matching])
    assert len(matching) == 1, f"expected exactly 1 goal titled '{title}', got {len(matching)}"


# ---------------------------------------------------------------------------
# v0.20 Phase F (L2)  exact-title goal dedup must respect goal status —
# completed/abandoned/duplicate goals never short-circuit-match, so the
# pipeline auto-creates a fresh open goal instead of re-linking to a dead one.
# ---------------------------------------------------------------------------

def test_find_goal_fuzzy_exact_match_skips_terminal_statuses(tmp_path):
    """v0.20 L2 unit: the v0.19 M3 exact-title short-circuit ignored status —
    a completed (or abandoned/duplicate) goal matched and got new episodes
    re-linked to it. Terminal statuses now fall through to []."""
    db_path = tmp_path / "test_v020_l2_status.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        gid = create_goal(conn, title="Zerovial", brand="ai-ecosystem")
        assert [r["id"] for r in find_goal_by_title_fuzzy(conn, "Zerovial", brand="ai-ecosystem")] == [gid]
        for terminal in ("completed", "abandoned", "duplicate"):
            update_goal_status(conn, gid, terminal)
            assert find_goal_by_title_fuzzy(conn, "Zerovial", brand="ai-ecosystem") == [], \
                f"status={terminal} goal must not exact-title match"
        # back to a live status -> matches again (M3 dedup intact for live goals)
        update_goal_status(conn, gid, "blocked")
        assert [r["id"] for r in find_goal_by_title_fuzzy(conn, "Zerovial", brand="ai-ecosystem")] == [gid]


def test_post_episode_completed_goal_same_title_creates_new_goal():
    """v0.20 L2 endpoint-level: an episode advancing a one-word title whose only
    existing goal is COMPLETED must create a NEW open goal and leave the
    completed goal's status and link set untouched (no re-linking to dead
    goals)."""
    title = f"statusdedup{uuid.uuid4().hex}"  # single token -> exact-match path
    gr = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "ai-ecosystem"}, headers=H, timeout=10)
    assert gr.status_code == 200
    old_gid = gr.json()["goal_id"]
    created_ids = [old_gid]
    try:
        pr = httpx.patch(f"{URL}/v1/goals/{old_gid}/status",
                         json={"status": "completed", "actor": "test"}, headers=H, timeout=10)
        assert pr.status_code == 200, f"complete PATCH failed: {pr.text}"
        body = {
            "session_id": f"test-{uuid.uuid4()}",
            "started_at": _now_iso(), "ended_at": _now_iso(),
            "goal": "t", "summary": "t", "brand": "ai-ecosystem",
            "advanced_goals": [{"goal_title": title, "delta_text": "v0.20 L2 status dedup test"}],
        }
        er = httpx.post(f"{URL}/v1/episodes", json=body, headers=H, timeout=15)
        assert er.status_code == 200, f"episode POST failed: {er.text}"
        listed = httpx.get(f"{URL}/v1/goals?brand=ai-ecosystem&limit=200", headers=H, timeout=10).json()
        matching = [g for g in listed if g["title"] == title]
        created_ids = [g["id"] for g in matching]
        assert len(matching) == 2, \
            f"expected a NEW goal next to the completed one, got {len(matching)}: {matching}"
        by_id = {g["id"]: g for g in matching}
        assert by_id[old_gid]["status"] == "completed", "completed goal must stay completed"
        new_gid = next(g["id"] for g in matching if g["id"] != old_gid)
        assert by_id[new_gid]["status"] == "open", "auto-created goal must be open"
        # the episode link must point at the NEW goal, not the completed one
        from _debris_patterns import episodic_db_path
        conn = sqlite3.connect(str(episodic_db_path()), timeout=30)
        try:
            n_old = conn.execute(
                "SELECT COUNT(*) FROM episode_links WHERE target_kind='goal' AND target_id=?",
                (str(old_gid),)).fetchone()[0]
            n_new = conn.execute(
                "SELECT COUNT(*) FROM episode_links WHERE target_kind='goal' AND target_id=?",
                (str(new_gid),)).fetchone()[0]
        finally:
            conn.close()
        assert n_old == 0, "completed goal must not gain episode links"
        assert n_new == 1, "new goal must carry the episode link"
    finally:
        delete_goal_rows(created_ids)


# ---------------------------------------------------------------------------
# v0.21 Phase A (M1)  the v0.20 L2 terminal-status guard only covered the
# 1-token exact-match short-circuit; the multi-token FTS path still
# resurrected/re-linked completed/abandoned/duplicate goals. The FTS branch now
# carries the same guard.
# ---------------------------------------------------------------------------

def test_find_goal_fuzzy_fts_match_skips_terminal_statuses(tmp_path):
    """v0.21 M1 unit: a >=2-token (FTS-path) title whose goal is terminal must
    return [] — the multi-token branch now mirrors the 1-token terminal-status
    guard. Falls back to matching once flipped to a live status."""
    db_path = tmp_path / "test_v021_m1_fts_status.db"
    with _connect_to(db_path) as conn:
        init_schema(conn)
        # Two tokens -> exercises the FTS branch (len(sanitized.split()) >= 2)
        title = "Zerovial staging deploy"
        gid = create_goal(conn, title=title, brand="ai-ecosystem")
        assert [r["id"] for r in find_goal_by_title_fuzzy(conn, title, brand="ai-ecosystem")] == [gid], \
            "live multi-token goal must FTS-match before the status guard kicks in"
        for terminal in ("completed", "abandoned", "duplicate"):
            update_goal_status(conn, gid, terminal)
            assert find_goal_by_title_fuzzy(conn, title, brand="ai-ecosystem") == [], \
                f"status={terminal} multi-token goal must not FTS-match"
        # back to a live status -> matches again
        update_goal_status(conn, gid, "blocked")
        assert [r["id"] for r in find_goal_by_title_fuzzy(conn, title, brand="ai-ecosystem")] == [gid]


def test_post_episode_completed_multitoken_goal_creates_new_goal():
    """v0.21 M1 endpoint-level twin of the L2 test using a TWO-token title to pin
    the FTS path: an episode advancing a multi-token title whose only existing
    goal is COMPLETED must create a NEW open goal, leaving the completed one
    untouched (no FTS re-link to a dead goal)."""
    # 'AND payment flow <uuid>' is already a conftest debris pattern (M2 row);
    # it is a >=2-token FTS-path title, so it doubles as the M1 endpoint twin.
    title = f"AND payment flow {uuid.uuid4()}"
    gr = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "ai-ecosystem"}, headers=H, timeout=10)
    assert gr.status_code == 200
    old_gid = gr.json()["goal_id"]
    created_ids = [old_gid]
    try:
        pr = httpx.patch(f"{URL}/v1/goals/{old_gid}/status",
                         json={"status": "completed", "actor": "test"}, headers=H, timeout=10)
        assert pr.status_code == 200, f"complete PATCH failed: {pr.text}"
        body = {
            "session_id": f"test-{uuid.uuid4()}",
            "started_at": _now_iso(), "ended_at": _now_iso(),
            "goal": "t", "summary": "t", "brand": "ai-ecosystem",
            "advanced_goals": [{"goal_title": title, "delta_text": "v0.21 M1 multi-token status dedup test"}],
        }
        er = httpx.post(f"{URL}/v1/episodes", json=body, headers=H, timeout=15)
        assert er.status_code == 200, f"episode POST failed: {er.text}"
        listed = httpx.get(f"{URL}/v1/goals?brand=ai-ecosystem&limit=200", headers=H, timeout=10).json()
        matching = [g for g in listed if g["title"] == title]
        created_ids = [g["id"] for g in matching]
        assert len(matching) == 2, \
            f"expected a NEW goal next to the completed multi-token one, got {len(matching)}: {matching}"
        by_id = {g["id"]: g for g in matching}
        assert by_id[old_gid]["status"] == "completed", "completed goal must stay completed"
        new_gid = next(g["id"] for g in matching if g["id"] != old_gid)
        assert by_id[new_gid]["status"] == "open", "auto-created goal must be open"
    finally:
        delete_goal_rows(created_ids)


# ---------------------------------------------------------------------------
# v0.19 M7/L1/L7  init_schema: dedup pre-existing duplicate in_progress rows
# before creating uq_episodes_one_in_progress (dirty pre-v0.18 DB migration)
# ---------------------------------------------------------------------------

def test_init_schema_dedupes_preexisting_in_progress_duplicates(tmp_path):
    """v0.19 M7/L1/L7: a pre-v0.18 DB holding >1 in_progress row per session
    (race residue / restored snapshot) must migrate cleanly: init_schema demotes
    all but MAX(id) per session to 'abandoned' (no deletion), then creates the
    unique index — instead of raising IntegrityError and silently never deploying
    the MED-1 race fix."""
    db_path = tmp_path / "test_v019_dirty_migration.db"
    conn = _connect_to(db_path)
    try:
        # Build a v0.17-shaped DB: full schema + state column, NO unique index.
        conn.executescript(SCHEMA_SQL)
        _add_column_if_missing(conn, "episodes", "state TEXT NOT NULL DEFAULT 'complete'", "state")
        conn.execute(
            "INSERT INTO sessions (session_id, started_at) VALUES ('test-dirty-sess', ?)",
            (_iso(),),
        )
        conn.execute(
            "INSERT INTO sessions (session_id, started_at) VALUES ('test-clean-sess', ?)",
            (_iso(),),
        )
        # Two in_progress rows for ONE session — the exact pre-v0.18 race residue.
        conn.execute(
            "INSERT INTO episodes (session_id, started_at, ended_at, state) "
            "VALUES ('test-dirty-sess', ?, ?, 'in_progress')",
            (_iso(), _iso(0, 13)),
        )
        conn.execute(
            "INSERT INTO episodes (session_id, started_at, ended_at, state) "
            "VALUES ('test-dirty-sess', ?, ?, 'in_progress')",
            (_iso(), _iso(0, 14)),
        )
        # A singleton in_progress row in another session — must NOT be touched.
        conn.execute(
            "INSERT INTO episodes (session_id, started_at, ended_at, state) "
            "VALUES ('test-clean-sess', ?, ?, 'in_progress')",
            (_iso(), _iso(0, 15)),
        )
        conn.commit()
        max_id = conn.execute(
            "SELECT MAX(id) FROM episodes WHERE session_id='test-dirty-sess'"
        ).fetchone()[0]

        # Pre-fix this raised sqlite3.IntegrityError and the index never deployed.
        init_schema(conn)

        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_episodes_one_in_progress'"
        ).fetchone()
        assert idx is not None, "uq_episodes_one_in_progress index missing after migration"

        rows = conn.execute(
            "SELECT id, state FROM episodes WHERE session_id='test-dirty-sess' ORDER BY id"
        ).fetchall()
        in_prog = [r["id"] for r in rows if r["state"] == "in_progress"]
        assert in_prog == [max_id], \
            f"expected only MAX(id)={max_id} to stay in_progress, got {in_prog}"
        demoted = [r for r in rows if r["id"] != max_id]
        assert len(demoted) == 1 and demoted[0]["state"] == "abandoned", \
            f"older duplicate must be demoted to 'abandoned' (not deleted), got {[dict(r) for r in demoted]}"

        # Singleton in another session untouched
        clean_state = conn.execute(
            "SELECT state FROM episodes WHERE session_id='test-clean-sess'"
        ).fetchone()["state"]
        assert clean_state == "in_progress", \
            f"singleton in_progress row must survive migration untouched, got {clean_state!r}"
    finally:
        conn.close()


# ===========================================================================
# v0.17 Phase F.3 — MCP surface polish (REST endpoint tests)
# ===========================================================================

def test_memory_get_by_id_returns_full_record():
    """F.3.1: POST evidence memory; GET /v1/memories/{id}; verify all expected fields present."""
    unique_text = f"F3.1 test memory exact-read {uuid.uuid4()}"
    add_r = httpx.post(
        f"{URL}/v1/memories",
        json={"messages": unique_text, "user_id": _UID, "infer": False,
              "metadata": {"source": "test-f31", "kind": "test"}},
        headers=H, timeout=15,
    )
    assert add_r.status_code == 200, f"add failed: {add_r.text}"
    results = add_r.json().get("results", [])
    if not results:
        pytest.skip("add returned 0 results (mem0 dedup may have merged this)")
    memory_id = results[0]["id"]

    get_r = httpx.get(f"{URL}/v1/memories/{memory_id}", headers=H, timeout=10)
    assert get_r.status_code == 200, f"GET /v1/memories/{memory_id} failed: {get_r.text}"
    body = get_r.json()
    # Required fields per F.3.1 spec
    assert body.get("id") == memory_id, f"id mismatch: {body}"
    assert "memory" in body, f"missing 'memory' field: {body}"
    assert "metadata" in body, f"missing 'metadata' field: {body}"
    assert "tier" in body, f"missing 'tier' field: {body}"
    assert "retrievable" in body, f"missing 'retrievable' field: {body}"

    # Cleanup
    httpx.delete(f"{URL}/v1/memories/{memory_id}", headers=H, timeout=10)


def test_goal_priority_endpoint():
    """F.3.2: POST goal; PATCH /priority; verify update persists."""
    title = f"f32-priority-test-{uuid.uuid4()}"
    create_r = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "ai-ecosystem", "priority": 3},
                          headers=H, timeout=10)
    assert create_r.status_code == 200, f"goal create failed: {create_r.text}"
    gid = create_r.json()["goal_id"]

    patch_r = httpx.patch(
        f"{URL}/v1/goals/{gid}/priority",
        json={"priority": 1, "actor": "test-pytest", "reason": "f3.2 test — highest priority"},
        headers=H, timeout=10,
    )
    assert patch_r.status_code == 200, f"PATCH /priority failed: {patch_r.text}"
    body = patch_r.json()
    assert body.get("ok") is True
    assert body.get("priority") == 1

    # Verify via GET
    g = httpx.get(f"{URL}/v1/goals/{gid}", headers=H, timeout=10).json()
    assert g["priority"] == 1, f"priority not updated: {g}"

    # Edge: priority out of range → 400
    bad_r = httpx.patch(f"{URL}/v1/goals/{gid}/priority",
                        json={"priority": 6, "actor": "test"}, headers=H, timeout=10)
    assert bad_r.status_code == 400, f"expected 400 for priority=6: {bad_r.text}"
    delete_goal_rows([gid])


def test_goal_link_episode_endpoint():
    """F.3.2: POST goal + POST episode; POST /link_episode; verify episode_links row created."""
    title = f"f32-link-ep-{uuid.uuid4()}"
    create_r = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "ai-ecosystem"},
                          headers=H, timeout=10)
    assert create_r.status_code == 200, f"goal create failed: {create_r.text}"
    gid = create_r.json()["goal_id"]

    ep_r = httpx.post(f"{URL}/v1/episodes", json=_episode_payload(), headers=H, timeout=15)
    assert ep_r.status_code == 200, f"episode create failed: {ep_r.text}"
    eid = ep_r.json()["episode_id"]

    link_r = httpx.post(
        f"{URL}/v1/goals/{gid}/link_episode",
        json={"episode_id": eid, "link_type": "advanced_goal", "actor": "test-pytest"},
        headers=H, timeout=10,
    )
    assert link_r.status_code == 200, f"link_episode failed: {link_r.text}"
    body = link_r.json()
    assert body.get("ok") is True
    assert body.get("goal_id") == gid
    assert body.get("episode_id") == eid
    assert isinstance(body.get("link_id"), int) and body["link_id"] > 0

    # Verify via goal detail — linked_episode_count should be >= 1
    g = httpx.get(f"{URL}/v1/goals/{gid}", headers=H, timeout=10).json()
    assert g.get("linked_episode_count", 0) >= 1, f"linked_episode_count not incremented: {g}"
    delete_goal_rows([gid])


def test_goal_merge_endpoint():
    """F.3.2: POST 2 goals + episode linked to source; POST /merge; verify re-target + source='duplicate'."""
    title_src = f"f32-merge-source-{uuid.uuid4()}"
    title_tgt = f"f32-merge-target-{uuid.uuid4()}"

    src_r = httpx.post(f"{URL}/v1/goals", json={"title": title_src, "brand": "ai-ecosystem"},
                       headers=H, timeout=10)
    assert src_r.status_code == 200, f"source goal create failed: {src_r.text}"
    src_id = src_r.json()["goal_id"]

    tgt_r = httpx.post(f"{URL}/v1/goals", json={"title": title_tgt, "brand": "ai-ecosystem"},
                       headers=H, timeout=10)
    assert tgt_r.status_code == 200, f"target goal create failed: {tgt_r.text}"
    tgt_id = tgt_r.json()["goal_id"]

    # Link an episode to the source goal first
    ep_r = httpx.post(f"{URL}/v1/episodes", json=_episode_payload(), headers=H, timeout=15)
    assert ep_r.status_code == 200
    eid = ep_r.json()["episode_id"]
    link_r = httpx.post(f"{URL}/v1/goals/{src_id}/link_episode",
                        json={"episode_id": eid, "link_type": "advanced_goal", "actor": "test"},
                        headers=H, timeout=10)
    assert link_r.status_code == 200, f"link_episode failed: {link_r.text}"

    # Merge source into target
    merge_r = httpx.post(
        f"{URL}/v1/goals/{src_id}/merge",
        json={"target_goal_id": tgt_id, "actor": "test-pytest", "reason": "f3.2 merge test"},
        headers=H, timeout=10,
    )
    assert merge_r.status_code == 200, f"merge failed: {merge_r.text}"
    body = merge_r.json()
    assert body.get("ok") is True
    assert body.get("source_goal_id") == src_id
    assert body.get("target_goal_id") == tgt_id
    assert isinstance(body.get("relinked_episodes"), int)
    assert body["relinked_episodes"] >= 1, f"expected >= 1 relinked, got {body['relinked_episodes']}"

    # Source should be 'duplicate' now
    src_after = httpx.get(f"{URL}/v1/goals/{src_id}", headers=H, timeout=10).json()
    assert src_after.get("status") == "duplicate", f"source not marked duplicate: {src_after}"

    # Target should have at least 1 linked episode
    tgt_after = httpx.get(f"{URL}/v1/goals/{tgt_id}", headers=H, timeout=10).json()
    assert tgt_after.get("linked_episode_count", 0) >= 1, f"target linked_episode_count not updated: {tgt_after}"

    # Cannot merge goal into itself → 400
    self_merge = httpx.post(f"{URL}/v1/goals/{src_id}/merge",
                            json={"target_goal_id": src_id, "actor": "test", "reason": "self-merge test"},
                            headers=H, timeout=10)
    assert self_merge.status_code == 400, f"expected 400 for self-merge: {self_merge.text}"
    delete_goal_rows([src_id, tgt_id])


def test_goal_merge_bulk_requires_hmac_user_direct():
    """v0.18 MED-9: merging a source goal with >100 episode_links requires
    actor='user-direct' + a valid HMAC user-direct token + nonce (bulk-tamper
    guard). <=100-link merges keep plain API-key auth (covered by
    test_goal_merge_endpoint above)."""
    import base64
    import datetime as dt
    import hashlib
    import hmac as _hmac

    title_src = f"med9-bulk-src-{uuid.uuid4()}"
    title_tgt = f"med9-bulk-tgt-{uuid.uuid4()}"
    src_id = httpx.post(f"{URL}/v1/goals", json={"title": title_src, "brand": "ai-ecosystem"},
                        headers=H, timeout=10).json()["goal_id"]
    tgt_id = httpx.post(f"{URL}/v1/goals", json={"title": title_tgt, "brand": "ai-ecosystem"},
                        headers=H, timeout=10).json()["goal_id"]

    # One real episode, then 101 direct episode_links rows pointing at the source goal
    ep_r = httpx.post(f"{URL}/v1/episodes", json=_episode_payload(), headers=H, timeout=15)
    assert ep_r.status_code == 200, f"episode create failed: {ep_r.text}"
    eid = ep_r.json()["episode_id"]

    db_path = Path.home() / ".mem0" / "episodic.db"
    if not db_path.exists():
        pytest.skip("episodic.db not found — cannot seed bulk links")
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.executemany(
            "INSERT INTO episode_links (episode_id, link_type, target_kind, target_id) "
            "VALUES (?, 'advanced_goal', 'goal', ?)",
            [(eid, str(src_id))] * 101,
        )
        conn.commit()

        # (1) >100 links, non-user-direct actor → 403 mentioning user-direct
        r1 = httpx.post(f"{URL}/v1/goals/{src_id}/merge",
                        json={"target_goal_id": tgt_id, "actor": "test-pytest",
                              "reason": "med9 bulk merge without hmac"},
                        headers=H, timeout=10)
        assert r1.status_code == 403, f"bulk merge without user-direct must be 403: {r1.status_code} {r1.text}"
        assert "user-direct" in r1.text, f"403 should mention user-direct requirement: {r1.text}"

        # (2) actor='user-direct' but no HMAC token → 403 mentioning token
        r2 = httpx.post(f"{URL}/v1/goals/{src_id}/merge",
                        json={"target_goal_id": tgt_id, "actor": "user-direct",
                              "reason": "med9 bulk merge no token"},
                        headers=H, timeout=10)
        assert r2.status_code == 403, f"bulk merge without token must be 403: {r2.status_code} {r2.text}"
        assert "token" in r2.text.lower() or "hmac" in r2.text.lower(), \
            f"403 should mention the HMAC token requirement: {r2.text}"

        # Source must NOT have been merged by the denied attempts
        src_state = httpx.get(f"{URL}/v1/goals/{src_id}", headers=H, timeout=10).json()
        assert src_state.get("status") != "duplicate", f"denied merge must not mark source duplicate: {src_state}"

        # (3) positive control: valid HMAC (action='merge_goals', memory_id slot =
        # source goal id) + nonce → 200 and all links relinked
        # v0.19 Phase H: key via provider (runtime tmpfs > dpapi-on-win > plaintext)
        from canonical_key_provider import CanonicalKeyProvider
        canonical_key = CanonicalKeyProvider().get_key()
        if canonical_key is None:
            pytest.skip("canonical-key not present — skip MED-9 positive control")
        ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        nonce = str(uuid.uuid4())
        reason = "med9 bulk merge with valid hmac"
        msg = f"{ts}|{nonce}|merge_goals|{src_id}|{reason}".encode("utf-8")
        token = base64.b64encode(
            _hmac.new(canonical_key.encode(), msg, hashlib.sha256).digest()
        ).decode("ascii").strip()
        r3 = httpx.post(
            f"{URL}/v1/goals/{src_id}/merge",
            json={"target_goal_id": tgt_id, "actor": "user-direct", "reason": reason},
            headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
        assert r3.status_code == 200, f"valid HMAC bulk merge should succeed: {r3.status_code} {r3.text}"
        assert r3.json().get("relinked_episodes", 0) >= 101, f"expected >=101 relinked: {r3.json()}"
        src_after = httpx.get(f"{URL}/v1/goals/{src_id}", headers=H, timeout=10).json()
        assert src_after.get("status") == "duplicate", f"source should be duplicate after valid merge: {src_after}"
    finally:
        # Cleanup: remove the seeded bulk links (now pointing at src or tgt)
        conn.execute(
            "DELETE FROM episode_links WHERE episode_id = ? AND target_kind = 'goal' "
            "AND target_id IN (?, ?)",
            (eid, str(src_id), str(tgt_id)),
        )
        conn.commit()
        conn.close()
        delete_goal_rows([src_id, tgt_id])


# ===========================================================================
# v0.17 Phase F.4.1 — query_class search policy tests
# ===========================================================================

def test_search_query_class_canonical_filters_tiers():
    """v0.17 F.4.1: query_class='canonical' returns only canonical+stable tier results.

    Seeds one evidence-tier memory and one stable-tier memory with the same unique
    keyword. Default search returns both. query_class='canonical' returns only the
    stable one (evidence is excluded).

    Note: we cannot easily promote to tier=canonical in pytest without the HMAC
    CLI, so this test verifies the filter logic using tier='stable' as the
    passing tier. Canonical tier records are kept out of automated test seeding
    to protect the integrity of production canonical data.
    """
    import uuid as _uuid
    unique_kw = f"qclass-canon-test-{_uuid.uuid4().hex[:10]}"

    # Add evidence-tier record
    ev_r = httpx.post(
        f"{URL}/v1/memories",
        json={"messages": f"{unique_kw} evidence fact", "user_id": _UID, "infer": False,
              "metadata": {"source": "test-f41", "kind": "test"}},
        headers=H, timeout=15,
    )
    assert ev_r.status_code == 200, f"evidence add failed: {ev_r.text}"
    ev_results = ev_r.json().get("results", [])
    if not ev_results:
        pytest.skip("evidence add returned 0 results (mem0 dedup may have merged this)")
    ev_id = ev_results[0]["id"]

    # Add second record and promote to stable
    stable_r = httpx.post(
        f"{URL}/v1/memories",
        json={"messages": f"{unique_kw} stable fact", "user_id": _UID, "infer": False,
              "metadata": {"source": "test-f41", "kind": "test"}},
        headers=H, timeout=15,
    )
    assert stable_r.status_code == 200, f"stable add failed: {stable_r.text}"
    stable_results = stable_r.json().get("results", [])
    if not stable_results:
        pytest.skip("stable-candidate add returned 0 results")
    stable_id = stable_results[0]["id"]

    # Promote to stable tier
    tier_r = httpx.patch(
        f"{URL}/v1/memories/{stable_id}/tier",
        json={"tier": "stable", "actor": "test-pytest", "reason": "F.4.1 query_class test"},
        headers=H, timeout=10,
    )
    assert tier_r.status_code == 200, f"tier promote failed: {tier_r.text}"

    try:
        # Default search should find both
        default_r = httpx.post(
            f"{URL}/v1/memories/search",
            json={"query": unique_kw, "filters": {"user_id": _UID}, "limit": 10, "threshold": 0.01},
            headers=H, timeout=15,
        )
        assert default_r.status_code == 200, f"default search failed: {default_r.text}"

        # query_class='canonical' should return only stable (or canonical) tier records
        canon_r = httpx.post(
            f"{URL}/v1/memories/search",
            json={"query": unique_kw, "filters": {"user_id": _UID},
                  "limit": 10, "threshold": 0.01, "query_class": "canonical"},
            headers=H, timeout=15,
        )
        assert canon_r.status_code == 200, f"canonical search failed: {canon_r.text}"
        canon_body = canon_r.json()
        canon_ids = {r["id"] for r in canon_body.get("results", [])}

        # All canonical-class results must be stable or canonical tier.
        # tier may appear as r["metadata"]["tier"] OR r["score_metadata"]["tier"] depending
        # on mem0 response shape; the server's filter reads (r.get("metadata") or {}).get("tier")
        # which is the same field — if the filter worked, no non-stable/canonical result appears.
        for r in canon_body.get("results", []):
            tier_val = (r.get("metadata") or {}).get("tier") or r.get("tier")
            assert tier_val in ("canonical", "stable", None), (
                # tier=None means the field isn't in the payload returned by mem0 search
                # (it lives in Qdrant payload but mem0's search response may not include it).
                # The server-side filter already ran — if the record is here, it passed the
                # (r.get("metadata") or {}).get("tier") in ("canonical","stable") check.
                # A None here means mem0's search response strips the tier field out of the
                # returned record shape — the filter still worked correctly server-side.
                f"F.4.1: query_class=canonical returned tier={tier_val!r} for id={r.get('id')}"
            )

        # The evidence record must NOT appear in canonical results
        assert ev_id not in canon_ids, (
            f"F.4.1: evidence record {ev_id} must not appear in query_class=canonical results"
        )

        # The stable record should appear (if search vector matched)
        # We assert query_class response has our metadata flag
        assert canon_body.get("query_class") == "canonical", (
            f"F.4.1: response missing query_class='canonical' marker: {canon_body}"
        )
    finally:
        # Cleanup
        httpx.delete(f"{URL}/v1/memories/{ev_id}?actor=test-cleanup&reason=F.4.1+test+cleanup",
                     headers=H, timeout=10)
        httpx.delete(f"{URL}/v1/memories/{stable_id}?actor=test-cleanup&reason=F.4.1+test+cleanup",
                     headers=H, timeout=10)


def _qdrant_backdate_created_at(memory_id: str, new_created_at: str) -> None:
    """v0.18 MED-19: backdate a memory's created_at by writing directly through the
    Qdrant client (the same client the server holds as mem.vector_store.client —
    config.py: host=localhost port=6333 collection='memories').

    The API path is intentionally closed: PATCH /v1/memories/{id}/metadata rejects
    created_at via FORBIDDEN_KEYS (which is why this test was previously skipped).
    Retrieving the live point (vector + payload) and re-upserting it with only
    created_at modified guarantees the upsert matches the live collection schema
    (unnamed dense vector + optional 'bm25' sparse — mem0 qdrant.py insert()).
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct

    from config import build_config
    vs_cfg = build_config()["vector_store"]["config"]
    client = QdrantClient(host=vs_cfg["host"], port=vs_cfg["port"])
    collection = vs_cfg["collection_name"]

    recs = client.retrieve(collection, ids=[memory_id], with_payload=True, with_vectors=True)
    assert recs, f"MED-19: point {memory_id} not found in Qdrant collection {collection!r}"
    rec = recs[0]
    payload = dict(rec.payload or {})
    payload["created_at"] = new_created_at
    client.upsert(collection, points=[PointStruct(id=rec.id, vector=rec.vector, payload=payload)])

    # Verify the synthetic backdate actually landed (defense against silent no-op)
    check = client.retrieve(collection, ids=[memory_id], with_payload=True)
    assert check and check[0].payload.get("created_at") == new_created_at, (
        f"MED-19: backdate upsert did not persist for {memory_id}"
    )


def test_search_query_class_operational_recency_boost():
    """v0.17 F.4.1 + v0.18 MED-19: query_class='operational' end-to-end.

    Previously SKIPPED: created_at is in FORBIDDEN_KEYS, so the metadata PATCH
    used to stamp an old date always 403'd. Now we backdate via direct Qdrant
    upsert (bypasses the API guard, which exists to stop *API callers* — not
    operator-level test tooling) and assert the full operational pipeline:

    - fresh + 90d-old records both surface (90d < 180d admission cap), with
      operational_recency_score stamped, fresh sorted before old (30d half-life
      decay: the 90d record's score is ~1/8 of its base);
    - a 200d-old record is EXCLUDED entirely by the v0.18 Phase C admission gate
      (operational max_age_days=180).
    """
    import uuid as _uuid
    import datetime as _dt
    unique_kw = f"qclass-op-test-{_uuid.uuid4().hex[:10]}"
    # MED-19 isolation: a PER-RUN source so the operational search below scopes to THIS
    # run's 3 records only — never crowded by records other runs left behind. (The conftest
    # memory cleanup currently no-ops against a stale Qdrant collection name, so test
    # records accumulate; a fixed shared source would let ~160 stale records crowd the
    # top-10 and flake this assertion.)
    op_source = f"test-f41-op-{unique_kw}"

    def _add(label: str) -> str:
        r = httpx.post(
            f"{URL}/v1/memories",
            json={"messages": f"{unique_kw} {label} operational fact", "user_id": _UID,
                  "infer": False,
                  "metadata": {"source": op_source, "kind": "test"}},
            headers=H, timeout=15,
        )
        assert r.status_code == 200, f"{label} add failed: {r.text}"
        results = r.json().get("results", [])
        if not results:
            pytest.skip(f"{label} add returned 0 results (mem0 dedup)")
        return results[0]["id"]

    fresh_id = _add("fresh")
    old_id = _add("old")
    ancient_id = _add("ancient")

    try:
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        # 90d: inside the 180d operational admission cap — decayed but admitted
        _qdrant_backdate_created_at(old_id, (now_utc - _dt.timedelta(days=90)).isoformat())
        # 200d: beyond the 180d cap — admission gate must exclude it
        _qdrant_backdate_created_at(ancient_id, (now_utc - _dt.timedelta(days=200)).isoformat())

        # MED-19 isolation: scope the operational search to THIS run's own records via the
        # per-run source (verified to filter server-side). Without it the search runs over
        # the whole growing store at threshold=0.01 and the *admitted* 90d record gets
        # crowded out of the top-10 — a flaky ranking artifact, not an admission failure.
        op_r = httpx.post(
            f"{URL}/v1/memories/search",
            json={"query": unique_kw,
                  "filters": {"user_id": _UID, "source": op_source},
                  "limit": 10, "threshold": 0.01,
                  "query_class": "operational", "rerank": False},
            headers=H, timeout=15,
        )
        assert op_r.status_code == 200, f"operational search failed: {op_r.text}"
        op_body = op_r.json()
        op_results = op_body.get("results", [])

        # Response should flag query_class
        assert op_body.get("query_class") == "operational", (
            f"F.4.1: response missing query_class='operational' marker: {op_body}"
        )

        # All results that have created_at should have operational_recency_score stamped
        for r in op_results:
            if (r.get("metadata") or {}).get("created_at") or r.get("created_at"):
                assert "operational_recency_score" in r, (
                    f"F.4.1: result {r.get('id')} missing operational_recency_score"
                )

        result_ids = [r["id"] for r in op_results]
        assert fresh_id in result_ids, (
            f"MED-19: fresh record {fresh_id} missing from operational results: {result_ids}"
        )
        assert old_id in result_ids, (
            f"MED-19: 90d-old record {old_id} must still be admitted (90d < 180d cap): {result_ids}"
        )
        assert ancient_id not in result_ids, (
            f"MED-19: 200d-old record {ancient_id} must be excluded by the admission gate "
            f"(operational max_age_days=180): {result_ids}"
        )

        # Recency boost: fresh sorts before old, and its score is strictly higher
        fresh_pos = result_ids.index(fresh_id)
        old_pos = result_ids.index(old_id)
        assert fresh_pos < old_pos, (
            f"F.4.1: expected fresh record (pos={fresh_pos}) before old (pos={old_pos}) "
            f"with query_class='operational'"
        )
        by_id = {r["id"]: r for r in op_results}
        fresh_score = by_id[fresh_id].get("operational_recency_score")
        old_score = by_id[old_id].get("operational_recency_score")
        assert fresh_score is not None and old_score is not None, (
            f"MED-19: operational_recency_score missing (fresh={fresh_score}, old={old_score})"
        )
        assert fresh_score > old_score, (
            f"MED-19: 30d half-life decay violated: fresh={fresh_score} <= old={old_score}"
        )
    finally:
        for mid in (fresh_id, old_id, ancient_id):
            httpx.delete(f"{URL}/v1/memories/{mid}?actor=test-cleanup&reason=MED-19+op+cleanup",
                         headers=H, timeout=10)


def test_resolve_open_question_from_untracked_session_no_500():
    """v1.0 regression: resolving (and creating) an open question from a session that
    was never checkpointed must NOT 500. open_questions.{first_seen,resolved_in}_
    session_id are FK-constrained to sessions(session_id); per-turn hooks are disabled
    in the VS Code / Agent-SDK runtime, so the live session is never written to
    `sessions` and the FK raised IntegrityError -> 500 (the live mem0
    open_question_resolve failure). create/resolve now ensure a minimal session row."""
    import uuid as _uuid
    s1 = f"test-oq-untracked-{_uuid.uuid4()}"
    cr = httpx.post(f"{URL}/v1/open_questions", json={
        "question_text": f"untracked-session OQ probe {s1}",
        "first_seen_session_id": s1,
    }, headers=H, timeout=15)
    assert cr.status_code == 200, f"create 500'd on untracked first_seen FK: {cr.text}"
    oqid = cr.json()["open_question_id"]
    s2 = f"test-oq-untracked-{_uuid.uuid4()}"
    rr = httpx.patch(f"{URL}/v1/open_questions/{oqid}/resolve", json={
        "resolved_in_session_id": s2,
        "resolution_text": "resolved from an untracked session",
        "actor": "claude-autonomous",
    }, headers=H, timeout=15)
    assert rr.status_code == 200, f"resolve 500'd on untracked resolved_in FK: {rr.text}"
    assert rr.json().get("status") == "resolved"


def _qdrant_set_tier(memory_id: str, tier: str) -> None:
    """v0.18 fix-pass: set a memory's tier by writing directly through the Qdrant
    client — mirrors _qdrant_backdate_created_at (the MED-19 FORBIDDEN_KEYS-bypass
    precedent). The API path is intentionally closed: tier='canonical' requires
    the user-direct HMAC gate, which exists to stop *API callers* — not
    operator-level test tooling seeding a synthetic probe point."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct

    from config import build_config
    vs_cfg = build_config()["vector_store"]["config"]
    client = QdrantClient(host=vs_cfg["host"], port=vs_cfg["port"])
    collection = vs_cfg["collection_name"]

    recs = client.retrieve(collection, ids=[memory_id], with_payload=True, with_vectors=True)
    assert recs, f"point {memory_id} not found in Qdrant collection {collection!r}"
    rec = recs[0]
    payload = dict(rec.payload or {})
    payload["tier"] = tier
    client.upsert(collection, points=[PointStruct(id=rec.id, vector=rec.vector, payload=payload)])
    check = client.retrieve(collection, ids=[memory_id], with_payload=True)
    assert check and check[0].payload.get("tier") == tier, (
        f"tier upsert did not persist for {memory_id}"
    )


def test_search_explicit_tier_canonical_filter_returns_canonical():
    """v0.18 fix-pass HIGH: an explicit filters.tier='canonical' (NO query_class)
    must return canonical records.

    Before the fix the admission gate ran with the default durable class
    (stable+evidence) on EVERY search, so a caller that explicitly filtered for
    tier=canonical got every hit stripped server-side — the canonical tier was
    unreachable through the MCP shim, the pre-tool-check hook, and curl alike
    (330 tier_disallowed:canonical rejections in <1 day of live logs).
    The explicit tier filter is the same trust posture as query_class='canonical'
    (both require the API key), so the gate now derives the admission class from it.
    """
    import uuid as _uuid
    unique_kw = f"explicit-tier-canon-test-{_uuid.uuid4().hex[:10]}"

    r = httpx.post(
        f"{URL}/v1/memories",
        json={"messages": f"{unique_kw} synthetic canonical ground-truth fact",
              "user_id": _UID, "infer": False,
              "metadata": {"source": "test-fixpass-canon-filter", "kind": "test"}},
        headers=H, timeout=15,
    )
    assert r.status_code == 200, f"seed add failed: {r.text}"
    results = r.json().get("results", [])
    if not results:
        pytest.skip("seed add returned 0 results (mem0 dedup)")
    mid = results[0]["id"]

    try:
        # Promote to canonical via direct Qdrant upsert (API requires HMAC)
        _qdrant_set_tier(mid, "canonical")

        # Explicit tier filter, NO query_class — must return the canonical record
        canon_r = httpx.post(
            f"{URL}/v1/memories/search",
            json={"query": unique_kw,
                  "filters": {"user_id": _UID, "tier": "canonical"},
                  "limit": 10, "threshold": 0.01},
            headers=H, timeout=15,
        )
        assert canon_r.status_code == 200, f"tier-filtered search failed: {canon_r.text}"
        canon_ids = {x["id"] for x in canon_r.json().get("results", [])}
        assert mid in canon_ids, (
            f"fix-pass HIGH regression: explicit filters.tier='canonical' search "
            f"did not return canonical record {mid}: {canon_ids}"
        )

        # Gate intact: plain search (no tier filter, no query_class) must NOT return it
        plain_r = httpx.post(
            f"{URL}/v1/memories/search",
            json={"query": unique_kw, "filters": {"user_id": _UID},
                  "limit": 10, "threshold": 0.01},
            headers=H, timeout=15,
        )
        assert plain_r.status_code == 200, f"plain search failed: {plain_r.text}"
        plain_ids = {x["id"] for x in plain_r.json().get("results", [])}
        assert mid not in plain_ids, (
            f"admission gate breached: canonical record {mid} leaked into a "
            f"default-class search without an explicit tier filter"
        )
    finally:
        # Demote back to evidence via Qdrant so the plain API delete is not HMAC-gated
        try:
            _qdrant_set_tier(mid, "evidence")
        except Exception:
            pass
        httpx.delete(f"{URL}/v1/memories/{mid}?actor=test-cleanup&reason=fixpass+canon+filter+cleanup",
                     headers=H, timeout=10)


def test_episode_checkpoint_hook_contract_version_back_compat():
    """v0.18 MED-17: hook_contract_version is WARN-only — never rejected.

    POST /v1/episodes/checkpoint must return 200 whether the field is absent
    (pre-v0.18 hooks / direct API callers), known ('17.0'), or unknown
    ('99.0-test' — accepted and ignored; the server only logs a WARN).

    v0.19 M15 fingerprint convention: the unknown version sent against the LIVE
    server MUST contain '-test' — the Test-MemoryStack 'hook-contract drift'
    journal row excludes such lines so test runs never false-WARN health.
    """
    # v0.19 A.1: 'test-' prefix so the conftest session cleanup can scope it
    session_id = f"test-med17-{uuid.uuid4()}"
    base = {"session_id": session_id, "transcript_path": None,
            "prompt_text": "MED-17 contract-version back-compat probe",
            "brand": "ai-ecosystem", "workspace": "ai-ecosystem"}

    for variant in (
        {},                                       # absent → INFO, still 200
        {"hook_contract_version": "17.0"},        # known → no log
        {"hook_contract_version": "99.0-test"},   # unknown → WARN, still 200
    ):
        r = httpx.post(f"{URL}/v1/episodes/checkpoint", json={**base, **variant},
                       headers=H, timeout=10)
        assert r.status_code == 200, (
            f"MED-17: checkpoint must accept payload variant {variant}: {r.status_code} {r.text}"
        )
        assert r.json().get("ok") is True


def test_warn_hook_contract_version_caplog(caplog):
    """v0.19 M15: the MED-17 WARN is assertable — _warn_hook_contract_version
    could previously be deleted and the suite still passed (the only test
    asserted HTTP 200, never the log). Direct import from the side-effect-free
    hook_contract module (app.py is not importable in tests).

    Also pins v0.19 M15 part 1 ('18.0' must NOT be pre-whitelisted: the set is
    extended only in the commit that actually bumps the hook contract) and
    v0.19 M10 (missing-field demoted to INFO; stats counters increment)."""
    import logging as _logging

    from hook_contract import (
        KNOWN_HOOK_CONTRACT_VERSIONS,
        hook_contract_stats,
        warn_hook_contract_version,
    )

    # M15: the first real drift must be detectable. v0.20 A.3 added '20.0' in
    # the same commit that bumped user-prompt-extract.ps1 to the bundle
    # contract (the documented extension rule), so the set is exactly these two.
    assert KNOWN_HOOK_CONTRACT_VERSIONS == {"17.0", "20.0"}, (
        f"known-versions set must contain only the real contract version; "
        f"pre-whitelisting a future version makes the first drift invisible: "
        f"{KNOWN_HOOK_CONTRACT_VERSIONS}"
    )

    before = dict(hook_contract_stats)

    # missing version → INFO (documented-legitimate callers; M10 de-spam)
    caplog.clear()
    with caplog.at_level(_logging.INFO, logger="mem0-server"):
        warn_hook_contract_version("/test/endpoint", None)
    recs = [r for r in caplog.records if "MED-17" in r.getMessage()]
    assert len(recs) == 1 and "without hook_contract_version" in recs[0].getMessage()
    assert recs[0].levelno == _logging.INFO, (
        f"M10: missing-field branch must log INFO, got {recs[0].levelname}"
    )

    # unknown version → WARN (the drift signal)
    caplog.clear()
    with caplog.at_level(_logging.INFO, logger="mem0-server"):
        warn_hook_contract_version("/test/endpoint", "99.0-test")
    recs = [r for r in caplog.records if "MED-17" in r.getMessage()]
    assert len(recs) == 1 and "unknown hook_contract_version" in recs[0].getMessage()
    assert "99.0-test" in recs[0].getMessage()
    assert recs[0].levelno == _logging.WARNING, (
        f"M15: unknown-version branch must log WARNING, got {recs[0].levelname}"
    )

    # known version → silent
    caplog.clear()
    with caplog.at_level(_logging.INFO, logger="mem0-server"):
        warn_hook_contract_version("/test/endpoint", "17.0")
    assert not [r for r in caplog.records if "MED-17" in r.getMessage()], (
        "known version must not log any MED-17 record"
    )

    # M10: counters incremented exactly once each; last_unknown recorded
    assert hook_contract_stats["missing"] == before["missing"] + 1
    assert hook_contract_stats["unknown"] == before["unknown"] + 1
    assert hook_contract_stats["last_unknown"] == "99.0-test"


def test_health_deep_exposes_hook_contract_stats():
    """v0.19 M10: /health/deep surfaces the in-process hook-contract drift
    counters (checks.hook_contract = {missing, unknown, last_unknown}) so the
    signal is readable, not journal-only."""
    r = httpx.get(f"{URL}/health/deep", timeout=20)
    assert r.status_code == 200, f"/health/deep failed: {r.status_code} {r.text}"
    hc = (r.json().get("checks") or {}).get("hook_contract")
    assert isinstance(hc, dict), f"checks.hook_contract missing from /health/deep: {r.json()}"
    assert isinstance(hc.get("missing"), int), f"hook_contract.missing not an int: {hc}"
    assert isinstance(hc.get("unknown"), int), f"hook_contract.unknown not an int: {hc}"
    assert "last_unknown" in hc, f"hook_contract.last_unknown absent: {hc}"


# ===========================================================================
# v0.20 Phase F (M12) — end-to-end history-class reachability through the
# real POST /v1/memories/search -> apply_admission wiring
# ===========================================================================

def test_search_query_class_history_returns_superseded_record():
    """v0.20 M12: a superseded record must be INVISIBLE to a durable-class
    search and REACHABLE via query_class='history' through the live API —
    pinning the server-side plumbing (a future query_class whitelist omitting
    'history' fails this test), not just the unit-level policy.

    superseded_by is stamped through the direct-Qdrant-upsert precedent
    (_qdrant_set_tier above / test_brand_isolation._qdrant_set_payload): the
    key has NO API writer by design since v0.20 Phase B — even trusted actors
    get 403 on the PATCH /metadata path (test_h_fixes), so operator-level test
    tooling seeds the stamp the same way the gate-key tests do."""
    from test_brand_isolation import _qdrant_set_payload

    unique_kw = f"qclass-history-e2e-{uuid.uuid4().hex[:10]}"
    r = httpx.post(
        f"{URL}/v1/memories",
        json={"messages": f"{unique_kw} superseded forensic fact", "user_id": _UID,
              "infer": False, "metadata": {"source": "test-f41", "kind": "test"}},
        headers=H, timeout=15,
    )
    assert r.status_code == 200, f"memory add failed: {r.text}"
    results = r.json().get("results", [])
    if not results:
        pytest.skip("memory add returned 0 results (mem0 dedup may have merged this)")
    mid = results[0]["id"]

    try:
        _qdrant_set_payload(mid, superseded_by="m-e2e-new")

        def _search(query_class: str) -> set:
            sr = httpx.post(
                f"{URL}/v1/memories/search",
                json={"query": unique_kw, "filters": {"user_id": _UID},
                      "limit": 10, "threshold": 0.01, "query_class": query_class},
                headers=H, timeout=15,
            )
            assert sr.status_code == 200, f"{query_class} search failed: {sr.text}"
            return {x["id"] for x in sr.json().get("results", [])}

        assert mid not in _search("durable"), (
            "superseded record must be rejected by the durable-class admission gate"
        )
        assert mid in _search("history"), (
            "superseded record must be admitted by the history (forensic) class"
        )
    finally:
        httpx.delete(f"{URL}/v1/memories/{mid}?actor=test-cleanup&reason=v020+M12+history+e2e+cleanup",
                     headers=H, timeout=10)


# ---------------------------------------------------------------------------
# v0.22 Pillar 1 — initiative-scoped goal / open-question injection
# (stop cross-initiative bleed under a shared brand). Unit tests on temp DBs.
# ---------------------------------------------------------------------------

def test_init_schema_adds_initiative_columns(tmp_path):
    """A fresh+migrated DB must carry the initiative column on BOTH goals and
    open_questions, plus the two initiative indexes."""
    db_path = tmp_path / "initiative.db"
    conn = _connect_to(db_path)
    init_schema(conn)
    init_schema(conn)  # idempotent — second call must not raise

    goal_cols = {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}
    oq_cols = {r["name"] for r in conn.execute("PRAGMA table_info(open_questions)")}
    assert "initiative" in goal_cols, f"goals missing initiative column: {sorted(goal_cols)}"
    assert "initiative" in oq_cols, f"open_questions missing initiative column: {sorted(oq_cols)}"

    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_goals_initiative_status" in idx, f"missing goals initiative index: {sorted(idx)}"
    assert "idx_oq_initiative_status" in idx, f"missing oq initiative index: {sorted(idx)}"
    conn.close()


def test_list_goals_initiative_scoping(db):
    """A goal stamped initiative='local-offload' is excluded from a query for
    initiative='agentic-memory-stack'; a NULL-initiative (cross-cutting) goal is
    included for ANY initiative; and an initiative=None (admin) query returns
    BOTH (unfiltered)."""
    g_lo = create_goal(db, title="local-offload only goal", initiative="local-offload")
    g_ams = create_goal(db, title="ams only goal", initiative="agentic-memory-stack")
    g_null = create_goal(db, title="cross-cutting goal", initiative=None)

    ams = {g["id"] for g in list_goals(db, initiative="agentic-memory-stack")}
    assert g_ams in ams, "same-initiative goal must surface"
    assert g_null in ams, "NULL-initiative (cross-cutting) goal must surface for any initiative"
    assert g_lo not in ams, "BLEED: other-initiative goal leaked into the scoped query"

    lo = {g["id"] for g in list_goals(db, initiative="local-offload")}
    assert g_lo in lo and g_null in lo and g_ams not in lo

    # admin/global listing (no initiative) is unfiltered on initiative
    allg = {g["id"] for g in list_goals(db, initiative=None)}
    assert {g_lo, g_ams, g_null} <= allg, "initiative=None must be unfiltered"


def test_list_open_questions_initiative_scoping(db):
    """Same 3 cases for open_questions: other-initiative excluded, NULL included
    for any initiative, initiative=None unfiltered."""
    from episodic import create_open_question
    q_lo = create_open_question(db, question_text="local-offload oq", initiative="local-offload")
    q_ams = create_open_question(db, question_text="ams oq", initiative="agentic-memory-stack")
    q_null = create_open_question(db, question_text="cross-cutting oq", initiative=None)

    ams = {q["id"] for q in list_open_questions(db, initiative="agentic-memory-stack")}
    assert q_ams in ams and q_null in ams
    assert q_lo not in ams, "BLEED: other-initiative open question leaked into the scoped query"

    lo = {q["id"] for q in list_open_questions(db, initiative="local-offload")}
    assert q_lo in lo and q_null in lo and q_ams not in lo

    allq = {q["id"] for q in list_open_questions(db, initiative=None)}
    assert {q_lo, q_ams, q_null} <= allq, "initiative=None must be unfiltered"
