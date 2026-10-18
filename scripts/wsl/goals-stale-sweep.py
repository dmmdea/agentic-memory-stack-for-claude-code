#!/usr/bin/env python3
"""v0.17 Phase E: weekly stale-goal sweeper.

Logic:
1. Query goals with status='open' AND no episode_links activity in last STALE_DAYS days.
2. Default: print summary to stdout + log to ~/.mem0/goals-stale-sweep.jsonl.
3. With --auto-abandon: flip qualifying goals to status='abandoned' + write ledger event.

Run weekly via systemd-user timer Sun 04:00 (after stack-backup 03:30).

The goal lifecycle pressure Codex's audit flagged in MED 3.6: 'goals have no
lifecycle pressure — abandoned exists but nothing in the system triggers
abandonment or review'.

1.37.0 (audit GQ-03, operator decision 2026-10-18): the open-question registry gets the same
pressure. It was write-only (4,954 open, median age 53 d, about 2 % ever resolved). With
--auto-abandon, an open question untouched for --oq-abandon-days (default 30) is set to
'abandoned', one open-question-status-change ledger event each. To resolve one later, reopen it
first (PATCH /v1/open_questions/{id}/status), since open_question_resolve acts on open questions only."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_STALE_DAYS = 30
DEFAULT_OQ_ABANDON_DAYS = 30
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
            g.created_by,
            g.first_seen_session_id,
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


def abandon_exempt(row: dict) -> bool:
    """Goal redesign (operator-approved 2026-08-09): auto-abandon NEVER touches
    a manual goal. Exempt = created_by 'manual', or a legacy row with no
    first_seen_session_id (the pre-created_by manual path left it NULL —
    everything extraction-minted always carried a session id)."""
    if (row.get("created_by") or "") == "manual":
        return True
    if row.get("first_seen_session_id") is None and row.get("created_by") is None:
        return True
    return False


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


def abandon_stale_open_questions(conn, days: int) -> int:
    """Open questions whose updated_at is older than `days`: abandoned, one ledger event each."""
    rows = conn.execute(
        "SELECT id FROM open_questions WHERE status = 'open' "
        "AND datetime(updated_at) < datetime('now', ?)", (f"-{int(days)} days",)).fetchall()
    now = _iso_now()
    n = 0
    for (oq_id,) in rows:
        cur = conn.execute("UPDATE open_questions SET status = 'abandoned', updated_at = ? "
                           "WHERE id = ? AND status = 'open'", (now, oq_id))
        if cur.rowcount:
            n += 1
            _ledger({"event": "open-question-status-change", "open_question_id": oq_id,
                     "new_status": "abandoned", "actor": "goals-stale-sweep",
                     "reason": f"open question untouched for {days}d (oq sweep, 1.37.0)"})
    conn.commit()
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description="v0.17 Phase E: stale-goal sweeper")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                        help=f"goals open with no link activity in this many days are flagged (default {DEFAULT_STALE_DAYS})")
    parser.add_argument("--auto-abandon", action="store_true",
                        help="actually flip status to 'abandoned' for eligible flagged goals (default: report only)")
    parser.add_argument("--abandon-days", type=int, default=90,
                        help="auto-abandon acts only on goals inactive at least this long "
                             "(operator-set 2026-08-09: 90; reporting still uses --stale-days)")
    parser.add_argument("--oq-abandon-days", type=int, default=DEFAULT_OQ_ABANDON_DAYS,
                        help="with --auto-abandon, abandon open questions untouched this long "
                             f"(default {DEFAULT_OQ_ABANDON_DAYS}; 0 turns the open-question pass off)")
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
                # Goal redesign (2026-08-09): abandonment is SCOPED — only
                # auto-created goals (never manual, see abandon_exempt) and
                # only past --abandon-days (90 by operator ruling), which is
                # deliberately longer than the --stale-days reporting window:
                # a goal gets 90 days to earn a second touch before it closes.
                abandon_cutoff = (dt.datetime.now(dt.timezone.utc)
                                  - dt.timedelta(days=args.abandon_days)).isoformat()
                ok = skipped_exempt = skipped_young = 0
                for g in stale:
                    if abandon_exempt(g):
                        skipped_exempt += 1
                        continue
                    if g["last_activity"] >= abandon_cutoff:
                        skipped_young += 1
                        continue
                    if abandon_goal(conn, g["id"],
                                    reason=f"auto-created, no activity in {args.abandon_days}d "
                                           "(scoped auto-abandon, operator-set 2026-08-09)",
                                    commit=False):
                        ok += 1
                conn.commit()
                sweep_record["abandoned_count"] = ok
                sweep_record["abandon_days"] = args.abandon_days
                sweep_record["skipped_exempt"] = skipped_exempt
                sweep_record["skipped_young"] = skipped_young
                print(f"[{ts}] auto-abandoned {ok}/{len(stale)} "
                      f"(exempt: {skipped_exempt}, under {args.abandon_days}d: {skipped_young})",
                      flush=True)
            else:
                print(f"[{ts}] (report-only; pass --auto-abandon to flip status)", flush=True)

        if args.auto_abandon and args.oq_abandon_days > 0:
            oq_n = abandon_stale_open_questions(conn, args.oq_abandon_days)
            sweep_record["oq_abandoned_count"] = oq_n
            sweep_record["oq_abandon_days"] = args.oq_abandon_days
            print(f"[{ts}] open questions: abandoned {oq_n} untouched for {args.oq_abandon_days}d", flush=True)

        # Always log the sweep run
        with SWEEP_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sweep_record) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
