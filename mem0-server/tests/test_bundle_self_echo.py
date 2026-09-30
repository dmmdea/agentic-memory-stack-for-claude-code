"""The context bundle must not echo a session's own goals and open questions back at it.

Measured on the live registry: 41 % of prompts received an open question raised in their OWN session
(the extractor mints open questions from the session you are in and the very next prompt served them
back), and the served set was `ORDER BY priority, updated_at`, the same freshest few for every prompt
whatever it was about. The bundle now (1) leaves out rows whose first_seen_session_id is the requesting
session and (2) ranks what remains by how recently an episode touched it, then priority.

The rules live in episodic.list_goals / list_open_questions (headless, temp SQLite); the bundle
handler's wiring is pinned in source because app.py cannot be imported without the live memory client.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from episodic import (  # noqa: E402
    _connect_to,
    add_episode,
    create_goal,
    create_open_question,
    create_session,
    init_schema,
    link_episode_to_goal,
    list_goals,
    list_open_questions,
)


@pytest.fixture()
def db(tmp_path):
    conn = _connect_to(tmp_path / "bundle.db")
    init_schema(conn)
    for sid in ("sess-me", "sess-a", "sess-b"):
        create_session(conn, sid)
    yield conn
    conn.close()


def _episode(db, session, ended):
    return add_episode(db, session, ended, ended, goal_text="g", summary_text="s")


def test_bundle_excludes_open_questions_from_the_requesting_session(db):
    """Three open questions, one raised in the requesting session: that one is not served."""
    own = create_open_question(db, "asked in this very session", first_seen_session_id="sess-me")
    other_a = create_open_question(db, "asked in another session", first_seen_session_id="sess-a")
    unknown = create_open_question(db, "no session recorded", first_seen_session_id=None)
    served = {q["id"] for q in list_open_questions(db, status="open", exclude_session_id="sess-me")}
    assert served == {other_a, unknown}, "the requesting session's own question is left out; the rest stay"
    assert own not in served
    # no requesting session (the MCP goals/questions tools, admin listings): unchanged
    assert {q["id"] for q in list_open_questions(db, status="open")} == {own, other_a, unknown}


def test_bundle_excludes_goals_from_the_requesting_session(db):
    own = create_goal(db, "raised here", first_seen_session_id="sess-me")
    other = create_goal(db, "raised elsewhere", first_seen_session_id="sess-a")
    unknown = create_goal(db, "no session recorded")
    served = {g["id"] for g in list_goals(db, status="open", exclude_session_id="sess-me")}
    assert served == {other, unknown}
    assert own not in served
    assert {g["id"] for g in list_goals(db, status="open")} == {own, other, unknown}


def test_goals_rank_by_recency_of_episode_link_then_priority(db):
    """The P1 goal nobody has touched for weeks no longer beats the goal an episode advanced yesterday."""
    stale_p1 = create_goal(db, "stale but P1", priority=1, first_seen_session_id="sess-a")
    fresh_p3 = create_goal(db, "advanced yesterday", priority=3, first_seen_session_id="sess-a")
    older_p2 = create_goal(db, "advanced last month", priority=2, first_seen_session_id="sess-a")
    never_p1 = create_goal(db, "never linked, P1", priority=1, first_seen_session_id="sess-a")
    never_p4 = create_goal(db, "never linked, P4", priority=4, first_seen_session_id="sess-a")
    link_episode_to_goal(db, _episode(db, "sess-a", "2026-09-28T10:00:00+00:00"), fresh_p3)
    link_episode_to_goal(db, _episode(db, "sess-b", "2026-08-20T10:00:00+00:00"), older_p2)
    # episode_links.created_at is the link's own clock: pin it so the order is deterministic
    db.execute("UPDATE episode_links SET created_at = '2026-09-28 10:00:00' WHERE target_id = ?", (str(fresh_p3),))
    db.execute("UPDATE episode_links SET created_at = '2026-08-20 10:00:00' WHERE target_id = ?", (str(older_p2),))
    db.commit()
    order = [g["id"] for g in list_goals(db, status="open", rank_by_recency=True)]
    assert order[:2] == [fresh_p3, older_p2], "goals an episode touched recently come first, newest link first"
    # never-linked goals follow, by priority: the two P1s (either order), then the P4
    assert {order[2], order[3]} == {stale_p1, never_p1}
    assert order[4] == never_p4
    # default (admin / MCP) ordering is untouched: priority first
    plain = [g["id"] for g in list_goals(db, status="open")]
    assert plain[0] in {stale_p1, never_p1} and plain[-1] == never_p4


def test_open_questions_rank_by_the_recency_of_their_episode_then_priority(db):
    ep_new = _episode(db, "sess-a", "2026-09-28T10:00:00+00:00")
    ep_old = _episode(db, "sess-b", "2026-07-01T10:00:00+00:00")
    old_p1 = create_open_question(db, "old but P1", priority=1, first_seen_session_id="sess-b",
                                  first_seen_episode_id=ep_old)
    new_p3 = create_open_question(db, "recent P3", priority=3, first_seen_session_id="sess-a",
                                  first_seen_episode_id=ep_new)
    new_p2 = create_open_question(db, "recent P2", priority=2, first_seen_session_id="sess-a",
                                  first_seen_episode_id=ep_new)
    order = [q["id"] for q in list_open_questions(db, status="open", rank_by_recency=True)]
    assert order == [new_p2, new_p3, old_p1], "recent episodes first, priority breaks the tie inside one"
    assert [q["id"] for q in list_open_questions(db, status="open")][0] == old_p1   # default: priority


def test_exclusion_composes_with_the_brand_gate_and_limit(db):
    """The new filter is additive: brand/initiative scoping and the cap still apply."""
    create_open_question(db, "own", first_seen_session_id="sess-me", brand="x")
    keep = create_open_question(db, "kept", first_seen_session_id="sess-a", brand="x")
    create_open_question(db, "other brand", first_seen_session_id="sess-a", brand="y")
    got = list_open_questions(db, status="open", brand="x", exclude_session_id="sess-me", limit=5,
                              rank_by_recency=True)
    assert [q["id"] for q in got] == [keep]


def test_bundle_handler_passes_the_session_and_asks_for_recency_ranking():
    """app.py cannot be imported headless: pin the wiring in its source. The bundle must hand the
    requesting session to both list calls, or the exclusion is a dead letter."""
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    start = src.index("def context_bundle(")
    end = src.index('@app.post("/v1/episodes")', start)
    body = src[start:end]
    for call in ("_episodic_list_goals(", "_episodic_list_open_questions("):
        seg = body[body.index(call):]
        seg = seg[:seg.index(")\n")]
        assert "exclude_session_id=b.session_id" in seg, call
        assert "rank_by_recency=True" in seg, call
