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
    _ep(conn, "s1", 5, adv=[{"goal_title": "Ship The Widget", "unmatched": True}], brand="readypep")
    _ep(conn, "s2", 2, blk=[{"goal_title": "ship the  widget", "unmatched": True}], brand="readypep")
    groups = promote.mine_unmatched(conn, days=14)
    key = promote.normalize_title("Ship The Widget")
    assert key in groups and len(groups[key]["sessions"]) == 2, \
        "case/whitespace variants of one intent across two sessions must group"
    assert promote.majority_brand(groups[key]["brands"]) == "readypep"
    # earliest occurrence's original casing wins
    assert groups[key]["original"] == "Ship The Widget"


def test_matched_intents_are_ignored_by_the_miner(conn):
    """Entries without unmatched:true are already-linked goals — mining them
    would recreate what the ingest matched."""
    _ep(conn, "s1", 3, adv=[{"goal_id": 7, "delta_text": "d"}])
    _ep(conn, "s2", 2, adv=[{"goal_id": 7, "delta_text": "d"}])
    assert promote.mine_unmatched(conn, days=14) == {}


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
