#!/usr/bin/env python3
"""v0.17 Phase E: weekly stale-goal sweeper.

Logic:
1. Query goals with status='open' AND no episode_links activity in last STALE_DAYS days.
2. Default: print summary to stdout + log to ~/.mem0/goals-stale-sweep.jsonl.
3. With --auto-abandon: flip qualifying goals to status='abandoned' + write ledger event.

Run weekly via systemd-user timer Sun 04:00 (after stack-backup 03:30).

The goal lifecycle pressure Codex's audit flagged in MED 3.6: 'goals have no
lifecycle pressure — abandoned exists but nothing in the system triggers
abandonment or review'."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_STALE_DAYS = 30
EPISODIC_DB = Path.home() / ".mem0" / "episodic.db"
LEDGER_DIR = Path.home() / ".mem0"
SWEEP_LOG = Path.home() / ".mem0" / "goals-stale-sweep.jsonl"


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _ledger_path() -> Path:
    # MEM-16 (2026-07-03): append to the CURRENT-MONTH segment
    # (tier-ledger-YYYY-MM.jsonl), same naming as app.py _append_ledger — the
    # legacy tier-ledger.jsonl is a frozen historical archive.
    return LEDGER_DIR / f"tier-ledger-{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m')}.jsonl"


def _ledger(rec: dict) -> None:
    rec.setdefault("ts", _iso_now())
    # v0.18 LOW-1: stamp schema_version on sweep-written ledger entries
    rec.setdefault("schema_version", "v18")
    ledger = _ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def find_stale_goals(conn, stale_days: int = DEFAULT_STALE_DAYS) -> list[dict]:
    """Goals with status='open' AND no episode_links activity in last stale_days.

    'Last activity' = most recent episode_link.created_at for that goal.
    If a goal has NO links ever, we use its goals.created_at instead.
    """
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=stale_days)).isoformat()
    cur = conn.execute("""
        SELECT
            g.id,
            g.title,
            g.brand,
            g.priority,
            g.created_at AS goal_created_at,
            (SELECT MAX(el.created_at) FROM episode_links el
             WHERE el.target_kind = 'goal' AND el.target_id = CAST(g.id AS TEXT)) AS last_link_at
        FROM goals g
        WHERE g.status = 'open'
    """)
    rows = [dict(r) for r in cur.fetchall()]
    stale = []
    for r in rows:
        last_activity = r["last_link_at"] or r["goal_created_at"]
        if last_activity and last_activity < cutoff:
            r["last_activity"] = last_activity
            stale.append(r)
    return stale


def abandon_goal(conn, goal_id: int, reason: str, commit: bool = True) -> bool:
    """Flip status to 'abandoned' + write ledger entry."""
    now = _iso_now()
    cur = conn.execute(
        "UPDATE goals SET status = 'abandoned', updated_at = ? WHERE id = ? AND status = 'open'",
        (now, goal_id),
    )
    if cur.rowcount == 0:
        return False
    if commit:
        conn.commit()
    _ledger({
        "event": "goal-abandoned",
        "goal_id": goal_id,
        "actor": "goals-stale-sweep",
        "reason": reason,
    })
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="v0.17 Phase E: stale-goal sweeper")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                        help=f"goals open with no link activity in this many days are flagged (default {DEFAULT_STALE_DAYS})")
    parser.add_argument("--auto-abandon", action="store_true",
                        help="actually flip status to 'abandoned' for flagged goals (default: report only)")
    args = parser.parse_args()

    if not EPISODIC_DB.exists():
        print(f"episodic.db not found at {EPISODIC_DB}; nothing to sweep", flush=True)
        return 0

    SWEEP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(EPISODIC_DB)) as conn:
        conn.row_factory = sqlite3.Row
        stale = find_stale_goals(conn, args.stale_days)

        ts = _iso_now()
        sweep_record = {
            "ts": ts,
            # v0.18 LOW-1: schema_version field on sweep-log entries
            "schema_version": "v18",
            "stale_days": args.stale_days,
            "found_count": len(stale),
            "auto_abandon": args.auto_abandon,
            "abandoned_count": 0,
            "flagged_ids": [r["id"] for r in stale],
        }

        if not stale:
            print(f"[{ts}] goals-stale-sweep: 0 stale goals (threshold: {args.stale_days}d inactivity)", flush=True)
        else:
            print(f"[{ts}] goals-stale-sweep: {len(stale)} stale goal(s) found:", flush=True)
            for g in stale:
                title = (g["title"] or "")[:80]
                print(f"  - id={g['id']} brand={g['brand'] or 'none'} prio=P{g['priority']} "
                      f"last_activity={g['last_activity'][:10]}: {title}", flush=True)

            if args.auto_abandon:
                ok = 0
                for g in stale:
                    if abandon_goal(conn, g["id"], reason=f"no episode_links activity in {args.stale_days}d", commit=False):
                        ok += 1
                conn.commit()
                sweep_record["abandoned_count"] = ok
                print(f"[{ts}] auto-abandoned {ok}/{len(stale)} goals", flush=True)
            else:
                print(f"[{ts}] (report-only; pass --auto-abandon to flip status)", flush=True)

        # Always log the sweep run
        with SWEEP_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sweep_record) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
