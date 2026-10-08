"""Goal redesign (operator-approved 2026-08-09) — "goals are earned, not minted".

Measured basis for the redesign: ~99 goals+OQs/day minted by per-session
extraction, 98.4% never touched again, 2.8% ever seen by a second session,
priority/initiative/related_goal_id 100% unused. The approved changes:
1. ingest no longer creates goal rows (unmatched intents ride the episode JSON)
2. the nightly recurrence promoter creates a goal only when the same intent
   recurred across >=2 distinct sessions (brand inherited from the sessions)
3. auto-abandon is standing but SCOPED: never manual goals, only past 90 days

Headless: sqlite fixtures + importlib loads; no live stack.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_pspec = importlib.util.spec_from_file_location(
    "goal_promote", REPO_ROOT / "scripts" / "wsl" / "goal-recurrence-promote.py")
promote = importlib.util.module_from_spec(_pspec)
_pspec.loader.exec_module(promote)

_sspec = importlib.util.spec_from_file_location(
    "goal_sweep", REPO_ROOT / "scripts" / "wsl" / "goals-stale-sweep.py")
sweep = importlib.util.module_from_spec(_sspec)
_sspec.loader.exec_module(sweep)

_espec = importlib.util.spec_from_file_location(
    "episodic_redesign", REPO_ROOT / "mem0-server" / "episodic.py")
episodic = importlib.util.module_from_spec(_espec)
_espec.loader.exec_module(episodic)


from _home_isolation import home_env  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = episodic._connect_to(tmp_path / "e.db")
    episodic.init_schema(c)
    yield c
    c.close()


def _ep(c, sid, days_ago, adv=None, blk=None, brand=None):
    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT OR IGNORE INTO sessions (session_id, brand, started_at) VALUES (?,?,?)",
              (sid, brand, ts))
    import json as _j
    cur = c.execute(
        "INSERT INTO episodes (session_id, goal_text, summary_text, state, started_at, ended_at, created_at, advanced_goals, blocked_goals)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, "g", "s", "complete", ts, ts, ts,
         _j.dumps(adv) if adv else None, _j.dumps(blk) if blk else None))
    c.commit()
    return cur.lastrowid


# --- schema + creator stamps ------------------------------------------------

def test_created_by_column_migrates_idempotently(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(goals)")}
    assert "created_by" in cols
    episodic.init_schema(conn)  # second run must not raise
    gid = episodic.create_goal(conn, title="t", created_by="manual")
    row = conn.execute("SELECT created_by FROM goals WHERE id=?", (gid,)).fetchone()
    assert row[0] == "manual"


# --- the promoter: earning rule ---------------------------------------------

def test_intent_in_one_session_is_not_promoted(conn):
    _ep(conn, "s1", 3, adv=[{"goal_title": "Ship the widget", "unmatched": True}])
    _ep(conn, "s1", 2, adv=[{"goal_title": "Ship the widget", "unmatched": True}])
    groups = promote.mine_unmatched(conn, days=14)
    rec = {k: g for k, g in groups.items() if len(g["sessions"]) >= promote.MIN_SESSIONS}
    assert rec == {}, "two mentions in ONE session must not earn a goal"


def test_intent_across_two_sessions_is_promoted_with_session_brand(conn):
    _ep(conn, "s1", 5, adv=[{"goal_title": "Ship The Widget", "unmatched": True}], brand="acme")
    _ep(conn, "s2", 2, blk=[{"goal_title": "ship the  widget", "unmatched": True}], brand="acme")
    groups = promote.mine_unmatched(conn, days=14)
    key = promote.normalize_title("Ship The Widget")
    assert key in groups and len(groups[key]["sessions"]) == 2, \
        "case/whitespace variants of one intent across two sessions must group"
    assert promote.majority_brand(groups[key]["brands"]) == "acme"
    # earliest occurrence's original casing wins
    assert groups[key]["original"] == "Ship The Widget"


def test_matched_intents_are_ignored_by_the_miner(conn):
    """Entries without unmatched:true are already-linked goals — mining them
    would recreate what the ingest matched."""
    _ep(conn, "s1", 3, adv=[{"goal_id": 7, "delta_text": "d"}])
    _ep(conn, "s2", 2, adv=[{"goal_id": 7, "delta_text": "d"}])
    assert promote.mine_unmatched(conn, days=14) == {}


def test_test_session_intents_are_ignored_by_the_miner(conn):
    """The live suite writes 'test-' sessions into the live DB (v0.19 A.1
    convention; the purge tool reaps them later) — their fixture intents recur
    across paired test sessions and must never earn a real goal. First
    unattended run minted 4 'med3dup<md5>' goals exactly this way."""
    _ep(conn, "test-aaaa", 3, adv=[{"goal_title": "med3dupcafe", "unmatched": True}],
        brand="ai-ecosystem")
    _ep(conn, "test-bbbb", 2, adv=[{"goal_title": "med3dupcafe", "unmatched": True}],
        brand="ai-ecosystem")
    assert promote.mine_unmatched(conn, days=14) == {}, \
        "test-session intents must be excluded from recurrence mining"
    # a real session mentioning the same title alongside test debris still
    # counts only its own (single) session — no promotion from mixed pairs
    _ep(conn, "real-1", 1, adv=[{"goal_title": "med3dupcafe", "unmatched": True}])
    groups = promote.mine_unmatched(conn, days=14)
    key = promote.normalize_title("med3dupcafe")
    assert len(groups.get(key, {"sessions": {}})["sessions"]) <= 1


def test_majority_brand_null_when_no_session_carries_one():
    assert promote.majority_brand([None, None]) is None
    assert promote.majority_brand(["a", "b", "b"]) == "b"


# --- the scoped auto-abandon -------------------------------------------------

def test_abandon_exemption_matrix():
    assert sweep.abandon_exempt({"created_by": "manual", "first_seen_session_id": "s"}) is True
    assert sweep.abandon_exempt({"created_by": None, "first_seen_session_id": None}) is True
    assert sweep.abandon_exempt({"created_by": None, "first_seen_session_id": "s"}) is False
    assert sweep.abandon_exempt({"created_by": "recurrence-promoter", "first_seen_session_id": "s"}) is False


# --- ingest no longer mints --------------------------------------------------

def test_ingest_no_longer_creates_goals():
    """Source-shape pin: the advanced/blocked ingest blocks contain NO
    _episodic_create_goal call — creation lives solely in the manual endpoint
    and the recurrence promoter."""
    src = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    i = src.find("# v0.16: process advanced_goals / blocked_goals")
    j = src.find("oq_filtered", i)
    block = src[i:j]
    assert i != -1 and j != -1
    assert "_episodic_create_goal" not in block, \
        "ingest minting is back — the redesign removed it (operator-approved 2026-08-09)"
    assert '"unmatched": True' in block, "unmatched intents must be serialized for the promoter"


def test_manual_endpoint_stamps_created_by():
    src = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    i = src.find("def create_goal_endpoint")
    body = src[i:src.find("\n@app.", i)]
    assert 'created_by="manual"' in body


def test_weekly_unit_passes_scoped_auto_abandon():
    unit = (REPO_ROOT / "systemd" / "goals-stale-sweep.service").read_text(encoding="utf-8")
    assert "--auto-abandon" in unit
    promoter_unit = (REPO_ROOT / "systemd" / "goal-recurrence-promote.service").read_text(encoding="utf-8")
    assert "goal-recurrence-promote.py --apply" in promoter_unit


def test_promoter_writes_the_jobs_receipt_contract(tmp_path):
    """W6 queue contract: the child must append a ts-bearing, outcome-coded
    line carrying jobs_key from JOBS_IDEMPOTENCY_KEY into its receipt file on
    EVERY exit path — without it jobs.py marks the run failed:receipt-missing.
    This gap was caught live by the pre-timer wire test; pinned so it cannot
    return. Exercised via subprocess under a throwaway HOME (no-op path)."""
    import json as _j
    import subprocess
    import sys as _sys
    env = home_env(tmp_path)   # HOME alone leaves Windows resolving ~ from the REAL USERPROFILE
    env["JOBS_IDEMPOTENCY_KEY"] = "pin-key-123"
    r = subprocess.run(
        [_sys.executable, str(REPO_ROOT / "scripts" / "wsl" / "goal-recurrence-promote.py"), "--apply"],
        capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr[-400:]
    receipt = tmp_path / ".mem0" / "goal-recurrence-promote.jsonl"
    assert receipt.exists(), "no receipt written on the no-op exit path"
    rec = _j.loads(receipt.read_text().strip().splitlines()[-1])
    assert rec.get("jobs_key") == "pin-key-123"
    assert "ts" in rec and "outcome" in rec


# --- DC-03: the nightly promoter must not re-insert the links it already made -----------------------------------------
import argparse  # noqa: E402
import sqlite3  # noqa: E402


def _promoter_night(c, monkeypatch, tmp_path):
    """One `goal-recurrence-promote.py --apply` pass against the fixture DB, its ledger and receipt under tmp_path."""
    monkeypatch.setattr(promote, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(promote, "RECEIPT", tmp_path / "goal-recurrence-promote.jsonl")
    assert promote._run(c, argparse.Namespace(apply=True, days=14)) == 0


def _goal_links(c):
    return sorted(tuple(r) for r in c.execute("SELECT episode_id, link_type, target_id FROM episode_links WHERE target_kind='goal'"))


def test_a_second_promoter_night_does_not_relink_the_same_episodes(conn, monkeypatch, tmp_path):
    """The 14-day window re-presents the same two episodes every night; each LINK-EXISTING run inserted their links
    again (live 2026-10-08: 4219 goal-link rows, 3815 distinct, one episode linked 20 times to one goal)."""
    _ep(conn, "s1", 5, adv=[{"goal_title": "Ship The Widget", "unmatched": True}], brand="acme")
    _ep(conn, "s2", 2, adv=[{"goal_title": "ship the  widget", "unmatched": True}], brand="acme")
    _promoter_night(conn, monkeypatch, tmp_path)                    # night 1: CREATE, two links
    first = _goal_links(conn)
    assert len(first) == 2 and conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 1
    _promoter_night(conn, monkeypatch, tmp_path)                    # nights 2 and 3: LINK-EXISTING
    _promoter_night(conn, monkeypatch, tmp_path)
    assert _goal_links(conn) == first, "re-running over the same window must add no link"
    assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 1


def test_link_episode_to_goal_is_idempotent_and_returns_the_existing_id(conn):
    gid = episodic.create_goal(conn, title="g", created_by="manual")
    eid = _ep(conn, "s1", 1)
    a = episodic.link_episode_to_goal(conn, eid, gid, link_type="advanced_goal")
    b = episodic.link_episode_to_goal(conn, eid, gid, link_type="advanced_goal")
    assert a == b and a > 0, "POST /v1/goals/{id}/link_episode returns this id; a repeat must name the same link"
    assert len(_goal_links(conn)) == 1
    other = episodic.link_episode_to_goal(conn, eid, gid, link_type="blocked_goal")
    assert other != a and len(_goal_links(conn)) == 2, "a different link_type is a different link"


def test_init_schema_dedupes_existing_goal_links_then_enforces_uniqueness(conn, caplog):
    """A table written by the old promoter already holds duplicates: the index must not fail on them (keep the
    earliest row, so created_at is the first link's), must leave memory links alone, and must then refuse a new one."""
    gid = episodic.create_goal(conn, title="g", created_by="manual")
    eid = _ep(conn, "s1", 1)
    conn.execute("DROP INDEX IF EXISTS uq_episode_links_goal")      # the pre-fix schema
    for day in ("2026-10-01 08:00:00", "2026-10-02 08:00:00", "2026-10-03 08:00:00"):
        conn.execute("INSERT INTO episode_links (episode_id, link_type, target_kind, target_id, created_at) VALUES (?, 'advanced_goal', 'goal', ?, ?)", (eid, str(gid), day))
    for _ in range(2):                                              # memory links are not goal links
        conn.execute("INSERT INTO episode_links (episode_id, link_type, target_kind, target_id) VALUES (?, 'cited', 'mem0', 'm1')", (eid,))
    conn.commit()
    with caplog.at_level("WARNING", logger="episodic"):
        episodic.init_schema(conn)
    assert "removed 2 duplicate goal link(s)" in caplog.text           # a one-time live deletion is logged
    rows = conn.execute("SELECT created_at FROM episode_links WHERE target_kind='goal'").fetchall()
    assert [r[0] for r in rows] == ["2026-10-01 08:00:00"]
    assert conn.execute("SELECT COUNT(*) FROM episode_links WHERE target_kind='mem0'").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episode_links (episode_id, link_type, target_kind, target_id) VALUES (?, 'advanced_goal', 'goal', ?)", (eid, str(gid)))
    episodic.init_schema(conn)                                      # and a second boot is a no-op
    assert len(_goal_links(conn)) == 1


def test_goal_merge_retargets_collision_safely_under_the_unique_index(conn):
    """A merge moves every source link to the target, drops (and counts) the ones the target already has, and
    leaves nothing on the source. A plain UPDATE would raise IntegrityError on uq_episode_links_goal (the merge
    endpoint would 500); a DELETE before the UPDATE would wipe the source's links."""
    src = episodic.create_goal(conn, title="source", created_by="manual")
    tgt = episodic.create_goal(conn, title="target", created_by="manual")
    e1, e2, e3 = _ep(conn, "s1", 1), _ep(conn, "s2", 1), _ep(conn, "s3", 1)
    for e in (e1, e2, e3):
        episodic.link_episode_to_goal(conn, e, src, link_type="advanced_goal")
    episodic.link_episode_to_goal(conn, e2, tgt, link_type="advanced_goal")      # the collision
    episodic.link_episode_to_goal(conn, e3, tgt, link_type="blocked_goal")       # same episode, other type: no collision
    moved, dropped = episodic.retarget_goal_links(conn, src, tgt)
    conn.commit()
    assert (moved, dropped) == (2, 1)
    assert conn.execute("SELECT COUNT(*) FROM episode_links WHERE target_kind='goal' AND target_id=?",
                        (str(src),)).fetchone()[0] == 0
    on_target = sorted(tuple(r) for r in conn.execute("SELECT episode_id, link_type FROM episode_links "
                                                      "WHERE target_kind='goal' AND target_id=?", (str(tgt),)))
    assert on_target == sorted([(e1, "advanced_goal"), (e2, "advanced_goal"), (e3, "advanced_goal"),
                                (e3, "blocked_goal")])


def test_the_merge_endpoint_uses_the_tested_retarget_and_reports_the_drops():
    """Source-static (app.py builds the live Memory client on import)."""
    src = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    assert ("            relinked, dropped = _episodic_retarget_goal_links(conn, source_goal_id, b.target_goal_id)\n"
            "            # Mark source as duplicate") in src                  # nothing rewrites the counts in between
    assert src.count('"dropped_duplicates": dropped,') == 2                       # the ledger line and the response
    assert "UPDATE OR IGNORE episode_links" not in src                           # one implementation, in episodic.py


def test_add_link_routes_a_goal_link_through_the_ensure_exists_path(conn):
    gid = episodic.create_goal(conn, title="g", created_by="manual")
    eid = _ep(conn, "s1", 1)
    a = episodic.add_link(conn, eid, "advanced_goal", str(gid), target_kind="goal")
    assert episodic.add_link(conn, eid, "advanced_goal", str(gid), target_kind="goal") == a     # no IntegrityError
    assert len(_goal_links(conn)) == 1
