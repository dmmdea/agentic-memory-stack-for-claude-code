"""test_open_questions.py — pytest coverage for v0.17 Phase D open_questions.

5 tests:
  1. test_episode_post_promotes_open_questions — POST episode with open_questions JSON → rows in global table.
  2. test_open_question_fuzzy_dedup — pre-create question; POST episode with similar text → no duplicate.
  3. test_resolve_open_question — PATCH /resolve → status='resolved' + ledger event.
  4. test_search_open_questions_fts5 — FTS5 search returns ranked results.
  5. test_list_open_questions_brand_filter — brand filter works.

Mix of unit tests (temp DB) and live-HTTP endpoint tests (requires MEM0_KEY + server running).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from episodic import (
    _connect_to,
    init_schema,
    create_session,
    create_open_question,
    list_open_questions,
    search_open_questions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ.get("MEM0_KEY", "")
H = {"X-API-Key": KEY, "Content-Type": "application/json"}


@pytest.fixture()
def db(tmp_path):
    """Return an open connection to a fresh episodic DB in tmp_path."""
    db_path = tmp_path / "test_oq.db"
    conn = _connect_to(db_path)
    init_schema(conn)
    yield conn
    conn.close()


def _sess(db) -> str:
    sid = str(uuid.uuid4())
    create_session(db, sid, brand="ai-ecosystem")
    return sid


def _live():
    """Skip live tests if MEM0_KEY is not configured."""
    if not KEY:
        pytest.skip("MEM0_KEY not set; skipping live endpoint test")


# ---------------------------------------------------------------------------
# 1. test_episode_post_promotes_open_questions
# ---------------------------------------------------------------------------

def test_episode_post_promotes_open_questions():
    """POST episode with open_questions list → rows appear in open_questions global table.

    v0.17 F.2.6: Uses a unique brand string per test run so the FTS5 dedup does not
    collide across test runs, and the search query uses the unique brand tag so the
    result is unambiguous even when the FTS5 limit=5 would otherwise return only old
    entries sharing the same rank.
    """
    _live()
    # v0.19 A.1: 'test-' session prefix so the conftest session cleanup can scope it
    unique_tag = uuid.uuid4().hex[:8]
    session_id = f"test-oq-sess-{unique_tag}-{uuid.uuid4()}"
    # v0.17 F.2.6: include unique brand tag in BOTH question text AND search query
    # so the FTS5 search returns exactly this run's entry regardless of accumulated state.
    unique_brand = f"test-oq-{unique_tag}"
    q_text = f"Should we use DPAPI for canonical-key isolation? [{unique_brand}]"

    def _count_tagged() -> int:
        # v0.22: count ONLY this run's tagged rows via FTS5 search. The global
        # /v1/open_questions list page saturates once the registry exceeds the
        # limit, so a raw page count is not a reliable before/after delta.
        rs = httpx.post(
            f"{URL}/v1/open_questions/search",
            json={"query": unique_brand, "status": "open", "limit": 50},
            headers=H, timeout=10,
        )
        rs.raise_for_status()
        return len([q for q in rs.json() if unique_brand in q.get("question_text", "")])

    # Count this run's tagged open questions before (should be 0)
    count_before = _count_tagged()

    # POST episode with one open question
    payload = {
        "session_id": session_id,
        "started_at": "2026-06-11T01:00:00+00:00",
        "ended_at": "2026-06-11T01:30:00+00:00",
        "goal": "Test Phase D open_questions promotion",
        "summary": "Verifying that episode POST sync promotes open_questions to global registry.",
        "brand": "ai-ecosystem",
        "open_questions": [q_text],
    }
    r = httpx.post(f"{URL}/v1/episodes", json=payload, headers=H, timeout=15)
    r.raise_for_status()
    ep_id = r.json()["episode_id"]

    # Count this run's tagged open questions after — must have increased by 1
    count_after = _count_tagged()
    assert count_after == count_before + 1, (
        f"Expected {count_before + 1} tagged open questions after episode POST, got {count_after}"
    )

    # v0.17 F.2.6: search using the unique brand tag so FTS5 returns only this run's entry.
    # Previous approach searched "DPAPI canonical-key isolation" which matched many prior
    # test entries at equal rank; with limit=5, this run's new entry was never returned.
    r_search = httpx.post(
        f"{URL}/v1/open_questions/search",
        json={"query": unique_brand, "status": "open", "limit": 10},
        headers=H, timeout=10,
    )
    r_search.raise_for_status()
    results = r_search.json()
    matching = [q for q in results if unique_brand in q.get("question_text", "")]
    assert matching, f"Posted question not found via FTS5 search for {unique_brand!r}; results={results}"
    assert matching[0]["first_seen_episode_id"] == ep_id


# ---------------------------------------------------------------------------
# 2. test_open_question_fuzzy_dedup
# ---------------------------------------------------------------------------

def test_open_question_fuzzy_dedup():
    """Pre-create a question; POST episode with similar text → no duplicate row created.

    v0.17 F.2.6: unique brand tag per test run isolates from accumulated state.
    """
    _live()
    session_id = f"test-oq-sess-{uuid.uuid4()}"  # v0.19 A.1: test- prefix for cleanup scoping
    # v0.17 F.2.6: full UUID hex ensures uniqueness even across parallel runs
    tag = uuid.uuid4().hex[:12]
    dedup_tag = f"dedup-{tag}"
    q_text = f"How should we handle backup ordering during restore? [{dedup_tag}]"
    similar_text = f"How should we handle backup ordering during restore? [{dedup_tag}]"  # exact

    def _count_tagged() -> int:
        # v0.22: count ONLY this run's tagged rows via FTS5 search; the global list
        # page saturates once the registry exceeds the limit, so a raw page count is
        # not a reliable dedup signal.
        rs = httpx.post(
            f"{URL}/v1/open_questions/search",
            json={"query": dedup_tag, "status": "open", "limit": 50},
            headers=H, timeout=10,
        )
        rs.raise_for_status()
        return len([q for q in rs.json() if dedup_tag in q.get("question_text", "")])

    # Count this run's tagged rows before (should be 0)
    count_before = _count_tagged()

    # Create via direct POST first
    r_create = httpx.post(f"{URL}/v1/open_questions",
                          json={"question_text": q_text, "brand": "ai-ecosystem"},
                          headers=H, timeout=10)
    r_create.raise_for_status()
    oq_id = r_create.json()["open_question_id"]

    # Now POST an episode with the same (or very similar) question text
    payload = {
        "session_id": session_id,
        "started_at": "2026-06-11T02:00:00+00:00",
        "ended_at": "2026-06-11T02:30:00+00:00",
        "goal": "Test dedup in Phase D",
        "summary": "Testing that identical question text does not create a duplicate global entry.",
        "brand": "ai-ecosystem",
        "open_questions": [similar_text],
    }
    r = httpx.post(f"{URL}/v1/episodes", json=payload, headers=H, timeout=15)
    r.raise_for_status()

    # Count after — must be count_before + 1 (the manual create), NOT count_before + 2.
    # The episode POST's identical question must dedup against the existing row.
    count_after = _count_tagged()
    assert count_after == count_before + 1, (
        f"Dedup failed: expected {count_before + 1} (no duplicate), got {count_after}"
    )

    # Cleanup: mark as abandoned so it doesn't pollute other tests
    httpx.patch(f"{URL}/v1/open_questions/{oq_id}/status",
                json={"status": "abandoned", "actor": "test-cleanup"},
                headers=H, timeout=5)


# ---------------------------------------------------------------------------
# 3. test_resolve_open_question
# ---------------------------------------------------------------------------

def test_resolve_open_question():
    """PATCH /resolve → status='resolved' + ledger event appended."""
    _live()
    tag = str(uuid.uuid4())[:8]
    session_id = f"test-oq-sess-{uuid.uuid4()}"  # v0.19 A.1: test- prefix for cleanup scoping
    q_text = f"Is SQLite WAL mode safe for concurrent mem0-server access? [resolve-{tag}]"

    # First create a real session by posting an episode (resolved_in_session_id FK requires this)
    ep_payload = {
        "session_id": session_id,
        "started_at": "2026-06-11T11:00:00+00:00",
        "ended_at": "2026-06-11T11:01:00+00:00",
        "goal": f"Resolve test setup [{tag}]",
        "summary": "Session created for resolve smoke test.",
        "brand": "ai-ecosystem",
    }
    r_ep = httpx.post(f"{URL}/v1/episodes", json=ep_payload, headers=H, timeout=15)
    r_ep.raise_for_status()

    # Create question
    r_create = httpx.post(f"{URL}/v1/open_questions",
                          json={"question_text": q_text, "brand": "ai-ecosystem"},
                          headers=H, timeout=10)
    r_create.raise_for_status()
    oq_id = r_create.json()["open_question_id"]

    # Resolve it
    r_resolve = httpx.patch(
        f"{URL}/v1/open_questions/{oq_id}/resolve",
        json={
            "resolved_in_session_id": session_id,
            "resolution_text": "Yes — WAL mode with timeout=30 + check_same_thread=False is safe for our single-writer pattern.",
            "actor": "test-resolver",
        },
        headers=H, timeout=10,
    )
    r_resolve.raise_for_status()
    result = r_resolve.json()
    assert result["ok"] is True
    assert result["status"] == "resolved"
    assert result["open_question_id"] == oq_id

    # Verify via GET
    r_get = httpx.get(f"{URL}/v1/open_questions/{oq_id}", headers=H, timeout=10)
    r_get.raise_for_status()
    oq = r_get.json()
    assert oq["status"] == "resolved"
    assert oq["resolved_in_session_id"] == session_id
    assert "WAL mode" in oq["resolution_text"]
    assert oq["resolved_at"] is not None

    # Verify ledger has the event (MEM-16: across legacy + monthly segments)
    from _ledger_paths import ledger_lines as _ledger_lines
    lines = _ledger_lines()
    if lines:
        import json
        ledger_events = [json.loads(l) for l in lines]
        resolve_events = [e for e in ledger_events
                         if e.get("event") == "open-question-resolved"
                         and e.get("open_question_id") == oq_id]
        assert resolve_events, f"No open-question-resolved ledger entry found for oq_id={oq_id}"


# ---------------------------------------------------------------------------
# 4. test_search_open_questions_fts5
# ---------------------------------------------------------------------------

def test_search_open_questions_fts5(db):
    """FTS5 keyword search returns ranked results matching query terms."""
    _sess(db)

    # Create a few distinct questions
    id1 = create_open_question(db, "Should canonical keys be stored in DPAPI vault?", brand="ai-ecosystem")
    id2 = create_open_question(db, "Is the Qdrant collection backed up to WSL snapshots?", brand="ai-ecosystem")
    id3 = create_open_question(db, "What FTS5 tokenizer works best for short questions?", brand="ai-ecosystem")

    # Search for DPAPI — should only return id1
    results = search_open_questions(db, "DPAPI vault", status="open", limit=10)
    assert len(results) >= 1
    ids_returned = [r["id"] for r in results]
    assert id1 in ids_returned
    assert id2 not in ids_returned

    # Search for Qdrant — should only return id2
    results2 = search_open_questions(db, "Qdrant snapshots", status="open", limit=10)
    ids2 = [r["id"] for r in results2]
    assert id2 in ids2
    assert id1 not in ids2

    # Search for FTS5 tokenizer — should return id3
    results3 = search_open_questions(db, "FTS5 tokenizer", status="open", limit=10)
    ids3 = [r["id"] for r in results3]
    assert id3 in ids3


# ---------------------------------------------------------------------------
# 5. test_list_open_questions_brand_filter
# ---------------------------------------------------------------------------

def test_list_open_questions_brand_filter(db):
    """list_open_questions brand filter isolates brand-specific questions."""
    sid = _sess(db)
    create_session(db, sid + "-r", brand="brand-a")

    id_eco = create_open_question(db, "Should we migrate episodic.db to Postgres?", brand="ai-ecosystem")
    id_rp = create_open_question(db, "When does Brand-A cart sync run nightly?", brand="brand-a")
    id_none = create_open_question(db, "Cross-brand architectural question about agents.")  # brand=None

    # Filter to ai-ecosystem
    eco_results = list_open_questions(db, status="open", brand="ai-ecosystem", limit=50)
    eco_ids = [r["id"] for r in eco_results]
    assert id_eco in eco_ids
    assert id_rp not in eco_ids
    assert id_none not in eco_ids

    # Filter to brand-a
    rp_results = list_open_questions(db, status="open", brand="brand-a", limit=50)
    rp_ids = [r["id"] for r in rp_results]
    assert id_rp in rp_ids
    assert id_eco not in rp_ids

    # No brand filter — should see all
    all_results = list_open_questions(db, status="open", brand=None, limit=200)
    all_ids = [r["id"] for r in all_results]
    assert id_eco in all_ids
    assert id_rp in all_ids
    assert id_none in all_ids
