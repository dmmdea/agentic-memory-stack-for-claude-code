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

1.37.0 (audit NC-07, operator decision 2026-10-18): the promoter was the only source of new goals and
its output was context-free noise in half of all goal blocks (27 of 27 descriptions empty). The bar is
now >= MIN_SESSIONS (3) distinct sessions spread over >= MIN_SPAN_DAYS (5) days, every new goal gets a
one-line description built from its contributing episodes, and a promoter goal with no new episode
link for ABANDON_DAYS (14) is abandoned (ledgered, reversible through the goal tools).

Dry-run by default; --apply to write. Exit 0 on success/no-op.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
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
RECEIPT = Path.home() / ".mem0" / "goal-recurrence-promote.jsonl"
MIN_SESSIONS = 3
MIN_SPAN_DAYS = 5
ABANDON_DAYS = 14


def _append_receipt(outcome: str, **fields) -> None:
    """Jobs-queue receipt contract (W6, same as semantic-dedup): a ts-bearing,
    outcome-coded line on EVERY exit path, carrying jobs_key from
    JOBS_IDEMPOTENCY_KEY so the queue can attribute THIS run — without it the
    wrapper marks the run failed:receipt-missing (which is exactly how this
    gap was caught, on the pre-timer wire test). Advisory: never crashes."""
    try:
        rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
               "outcome": outcome, **fields}
        key = os.environ.get("JOBS_IDEMPOTENCY_KEY")
        if key:
            rec["jobs_key"] = key
        RECEIPT.parent.mkdir(parents=True, exist_ok=True)
        with RECEIPT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        print(f"goal-recurrence-promote: receipt append failed (non-fatal): {e}", flush=True)


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
    # 'test-' sessions are live-suite fixtures (v0.19 A.1 prefix convention;
    # the purge tool reaps them) — paired fixture sessions satisfy the >=2
    # recurrence rule by construction, so they must never feed the miner.
    rows = conn.execute(
        """SELECT e.id, e.session_id, e.created_at, e.advanced_goals, e.blocked_goals, s.brand
           FROM episodes e LEFT JOIN sessions s ON s.session_id = e.session_id
           WHERE e.created_at >= ? AND e.session_id NOT LIKE 'test-%'""",
        (cutoff,)).fetchall()
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


def _parse_db_ts(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def span_days(g: dict) -> float:
    """Days between the first and the last contributing session."""
    stamps = [d for d in (_parse_db_ts(t) for t in g["sessions"].values()) if d]
    if len(stamps) < 2:
        return 0.0
    return (max(stamps) - min(stamps)).total_seconds() / 86400


def build_description(conn: sqlite3.Connection, g: dict) -> str:
    """One line from the contributing episodes: how often and how long it recurred, and what the
    newest episode was about (its goal_text, else its summary)."""
    stamps = sorted(d for d in (_parse_db_ts(t) for t in g["sessions"].values()) if d)
    ep_ids = [e[0] for e in sorted(g["episodes"], key=lambda e: e[2] or "", reverse=True)]
    latest = ""
    for ep_id in ep_ids[:5]:
        row = conn.execute("SELECT goal_text, summary_text FROM episodes WHERE id = ?", (ep_id,)).fetchone()
        if row:
            latest = (row[0] or row[1] or "").strip()
            if latest:
                break
    latest = re.sub(r"\s+", " ", latest)
    if len(latest) > 160:
        latest = latest[:157].rstrip() + "..."
    when = f"{stamps[0]:%Y-%m-%d} to {stamps[-1]:%Y-%m-%d}" if stamps else "unknown dates"
    desc = f"Recurred in {len(g['sessions'])} sessions, {when}."
    return f"{desc} Latest: {latest}" if latest else desc


def abandon_idle_promoter_goals(conn: sqlite3.Connection, apply: bool) -> int:
    """Promoter goals (open/blocked) whose newest episode link (or, with none, their creation) is
    older than ABANDON_DAYS: abandoned, one ledger event each."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ABANDON_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """SELECT g.id, g.title, COALESCE(MAX(l.created_at), g.created_at) AS last_touch
           FROM goals g LEFT JOIN episode_links l
             ON l.target_kind = 'goal' AND l.target_id = CAST(g.id AS TEXT)
           WHERE g.created_by = 'recurrence-promoter' AND g.status IN ('open', 'blocked')
           GROUP BY g.id HAVING last_touch < ?""", (cutoff,)).fetchall()
    n = 0
    for gid, title, last_touch in rows:
        print(f"  ABANDON: {str(title)[:70]!r} last_touch={str(last_touch)[:10]}")
        if not apply:
            continue
        cur = conn.execute("UPDATE goals SET status = 'abandoned', updated_at = ? WHERE id = ? "
                           "AND status IN ('open', 'blocked')", (_iso_now(), gid))
        if cur.rowcount:
            _ledger({"event": "goal-abandoned", "goal_id": gid, "actor": "recurrence-promoter",
                     "reason": f"no episode link in {ABANDON_DAYS}d (promoter auto-abandon, 1.37.0)"})
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--days", type=int, default=14, help="episode window (default 14)")
    args = ap.parse_args()

    if not DB.exists():
        print("goal-recurrence-promote: no episodic.db — no-op")
        _append_receipt("no-op:no-episodic-db", dry_run=not args.apply)
        return 0
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return _run(conn, args)
    except Exception as e:  # noqa: BLE001 — every exit path must leave a receipt
        _append_receipt(f"degraded:aborted:{type(e).__name__}: {str(e)[:120]}",
                        dry_run=not args.apply)
        raise
    finally:
        conn.close()


def _run(conn: sqlite3.Connection, args) -> int:
    groups = mine_unmatched(conn, args.days)
    recurring = {k: g for k, g in groups.items()
                 if len(g["sessions"]) >= MIN_SESSIONS and span_days(g) >= MIN_SPAN_DAYS}
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
            gid = create_goal(conn, title=title, description=build_description(conn, g), brand=brand, priority=3,
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
    abandoned = abandon_idle_promoter_goals(conn, args.apply)
    if args.apply:
        conn.commit()
    print(f"goal-recurrence-promote: window={args.days}d unmatched-groups={len(groups)} "
          f"recurring={len(recurring)} created={created} linked={linked} abandoned={abandoned} "
          f"{'APPLIED' if args.apply else 'DRY-RUN'}")
    _append_receipt("ok", dry_run=not args.apply, window_days=args.days,
                    unmatched_groups=len(groups), recurring=len(recurring),
                    created=created, linked=linked, abandoned=abandoned)
    return 0


if __name__ == "__main__":
    sys.exit(main())
