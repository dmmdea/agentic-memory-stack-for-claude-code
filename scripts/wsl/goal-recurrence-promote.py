#!/usr/bin/env python3
"""goal-recurrence-promote.py — nightly cross-session goal promotion.

Goal redesign (operator-approved 2026-08-09, "goals are earned, not minted"):
the per-session extractor no longer creates goal rows — a fuzzy-missed intent
is serialized onto the episode's advanced_goals/blocked_goals JSON with
{"goal_title": ..., "unmatched": true}. This job is the ONLY automatic goal
producer left: it mines those unmatched intents over a sliding window and
creates a goal exactly when the same intent has recurred across >= MIN_SESSIONS
distinct sessions — cross-session recurrence is the evidence someone will come
back to it. Everything else stays session detail on the episode.

Mechanics:
- Titles are normalized (casefold + whitespace collapse) for grouping; the
  goal is created with the EARLIEST occurrence's original title.
- brand is inherited from the contributing sessions (majority; ties -> the
  earliest session's brand) — the NULL-brand leak the diagnosis measured came
  from creation paths that never consulted the session.
- created_by='recurrence-promoter'; first_seen_session_id = earliest session.
- Contributing episodes are linked (advanced_goal), so the new goal is born
  with its cross-session evidence attached.
- An existing same-brand fuzzy match is LINKED, not duplicated (and unblocked
  if the intent was an advance — mirrors the ingest MED-B rule).
- One ledger event per creation (event=goal-created, the shape ledger-audit
  knows).
- Idempotent: a created/linked goal matches the fuzzy probe on the next run,
  so the same intent group never creates twice.

Dry-run by default; --apply to write. Exit 0 on success/no-op.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path

# LAYOUT-AWARE (same rule as the sibling sweeps): repo layout puts episodic.py
# at <repo>/mem0-server; deployed, the server modules live at ~/apps/mem0-server.
_HERE = Path(__file__).resolve().parent
for _cand in (_HERE.parents[1] / "mem0-server", Path.home() / "apps" / "mem0-server"):
    if (_cand / "episodic.py").is_file():
        sys.path.insert(0, str(_cand))
        break
from episodic import find_goal_by_title_fuzzy, create_goal, link_episode_to_goal, update_goal_status  # noqa: E402

DB = Path.home() / ".mem0" / "episodic.db"
LEDGER_DIR = Path.home() / ".mem0"
MIN_SESSIONS = 2


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ledger(rec: dict) -> None:
    rec.setdefault("ts", _iso_now())
    rec.setdefault("schema_version", "v18")
    p = LEDGER_DIR / f"tier-ledger-{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m')}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def normalize_title(t: str) -> str:
    """Grouping key: casefold + collapse internal whitespace + strip."""
    return re.sub(r"\s+", " ", (t or "").strip()).casefold()


def mine_unmatched(conn: sqlite3.Connection, days: int) -> dict:
    """{normalized_title: {"sessions": {sid: earliest_ts}, "episodes": [(ep_id, sid, ts)],
    "original": title_of_earliest, "brands": [session brands]}} from the
    unmatched intents serialized on episodes in the window."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    groups: dict = {}
    rows = conn.execute(
        """SELECT e.id, e.session_id, e.created_at, e.advanced_goals, e.blocked_goals, s.brand
           FROM episodes e LEFT JOIN sessions s ON s.session_id = e.session_id
           WHERE e.created_at >= ?""", (cutoff,)).fetchall()
    for ep_id, sid, ts, adv, blk, brand in rows:
        for col in (adv, blk):
            if not col:
                continue
            try:
                items = json.loads(col)
            except (ValueError, TypeError):
                continue
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict) or not it.get("unmatched"):
                    continue
                title = (it.get("goal_title") or "").strip()
                if not title:
                    continue
                key = normalize_title(title)
                g = groups.setdefault(key, {"sessions": {}, "episodes": [],
                                            "original": None, "original_ts": None,
                                            "brands": []})
                if sid and (sid not in g["sessions"] or ts < g["sessions"][sid]):
                    g["sessions"][sid] = ts
                g["episodes"].append((ep_id, sid, ts))
                g["brands"].append(brand)
                if g["original_ts"] is None or ts < g["original_ts"]:
                    g["original"], g["original_ts"] = title, ts
    return groups


def majority_brand(brands: list) -> str | None:
    """Majority non-null brand; ties broken by first occurrence order."""
    counts: dict = {}
    for b in brands:
        if b:
            counts[b] = counts.get(b, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    for b in brands:
        if b and counts[b] == best:
            return b
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--days", type=int, default=14, help="episode window (default 14)")
    args = ap.parse_args()

    if not DB.exists():
        print("goal-recurrence-promote: no episodic.db — no-op")
        return 0
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    groups = mine_unmatched(conn, args.days)
    recurring = {k: g for k, g in groups.items() if len(g["sessions"]) >= MIN_SESSIONS}
    created = linked = 0
    for key, g in sorted(recurring.items(), key=lambda kv: kv[1]["original_ts"] or ""):
        title = g["original"]
        brand = majority_brand(g["brands"])
        existing = find_goal_by_title_fuzzy(conn, title, brand=brand, limit=1)
        if existing:
            gid = existing[0]["id"]
            action = "LINK-EXISTING"
        else:
            gid = None
            action = "CREATE"
        print(f"  {action}: {title[:70]!r} sessions={len(g['sessions'])} brand={brand}")
        if not args.apply:
            continue
        if gid is None:
            earliest_sid = min(g["sessions"], key=g["sessions"].get)
            gid = create_goal(conn, title=title, brand=brand, priority=3,
                              first_seen_session_id=earliest_sid,
                              created_by="recurrence-promoter", commit=False)
            _ledger({"event": "goal-created", "goal_id": gid, "actor": "recurrence-promoter",
                     "reason": f"intent recurred across {len(g['sessions'])} sessions in {args.days}d",
                     "title": title[:120], "brand": brand})
            created += 1
        else:
            if existing and existing[0]["status"] == "blocked":
                update_goal_status(conn, gid, "open", commit=False)
            linked += 1
        for ep_id, _sid, _ts in g["episodes"]:
            link_episode_to_goal(conn, ep_id, gid, link_type="advanced_goal",
                                 delta_text="recurrence-promoter backlink", commit=False)
    if args.apply:
        conn.commit()
    print(f"goal-recurrence-promote: window={args.days}d unmatched-groups={len(groups)} "
          f"recurring={len(recurring)} created={created} linked={linked} "
          f"{'APPLIED' if args.apply else 'DRY-RUN'}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
