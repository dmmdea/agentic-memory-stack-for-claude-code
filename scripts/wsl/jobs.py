#!/usr/bin/env python3
"""W6 durable job queue (QUEUE-1..3) — library + CLI, NO daemon.

MASTER-PLAN §5 locked shape: SQLite beside the sidecars (``~/.mem0/jobs.db``),
two-phase claim (``queued → running(claimed_at, owner) → done|failed``), work
recorded complete only after the JOB'S OWN receipt is observed, stale-claim
reap by the next invocation, idempotency keys per the outbox-replay
convention. Existing timers/tasks call ``jobs.py run <name> ...`` — the queue
is invoked, never scheduled (operator rule: no unattended schedulers).

Roast bindings (2026-08-08 council, ALL binding):
- F1  receipt observation parses per-LINE UTC ``ts`` (never file mtime), and
      the line must carry the run's idempotency key (adopters stamp
      ``jobs_key`` from the JOBS_IDEMPOTENCY_KEY env into their summary
      lines). Reap receipts live in a QUEUE-OWNED file
      (``~/.mem0/jobs-reap.jsonl``) and can never satisfy observation.
      Per-adopter receipt files are NAMED at the call site (``--receipt``) —
      never the shared monthly ledger.
- F2  same-host reap requires the recorded pid DEAD **and** claim age >
      stale_after; foreign-host rows reap on age alone (a restored jobs.db
      must not let this box kill another box's live work — owner is
      host-keyed). Reap is PER-NAME at each run entry (R5): nothing re-runs
      a reaped row until that name's own next invocation anyway.
- F7  role file MISSING ⇒ brain (replay-ops precedent — a naive
      missing⇒not-brain would silently no-op the queue on any unmarked box);
      ``run`` PROPAGATES the child's exit code (the only 0-overrides are the
      quiet claim-lost and role-gate exits); ``no-op:*`` outcomes are
      preserved distinct from ``ok`` on the row (the AMS-07 blind spot);
      every invocation refreshes the file-read heartbeat mirror
      (``~/.mem0/jobs-heartbeat.json``) the SessionStart banner may read —
      the banner must never open the db.
- Epochs are written by THIS Python process only, UTC — the PS 5.1 epoch
  skew corrupted dream-catchup timing once already; no shell computes time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".mem0" / "jobs.db"
REAP_LOG = Path.home() / ".mem0" / "jobs-reap.jsonl"
HEARTBEAT = Path.home() / ".mem0" / "jobs-heartbeat.json"
ROLE_FILE = Path.home() / ".mem0" / "role"
DEFAULT_STALE_AFTER_S = 6 * 3600


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return dt.datetime.fromtimestamp(ts if ts is not None else _now(),
                                     dt.timezone.utc).isoformat()


def _host() -> str:
    return socket.gethostname().lower()


def _role_is_brain() -> bool:
    """F7a: MISSING role file ⇒ brain (fail toward doing the work on the one
    box that is unmarked; a replica is always explicitly marked)."""
    try:
        if not ROLE_FILE.exists():
            return True
        return ROLE_FILE.read_text(encoding="utf-8").strip().lower() == "brain"
    except OSError:
        return True


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    # Every `with conn:` transaction opens BEGIN IMMEDIATE — claimants
    # serialize on the reserved lock, closing the queued-check/insert race
    # without manual BEGIN statements (which conflict with the context
    # manager's own transaction handling).
    conn.isolation_level = "IMMEDIATE"
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jobs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL,"
        " state TEXT NOT NULL,"          # queued | running | done | failed
        " idempotency_key TEXT NOT NULL,"
        " owner_host TEXT,"
        " owner_pid INTEGER,"
        " claimed_at REAL,"
        " finished_at REAL,"
        " outcome TEXT,"
        " reap_count INTEGER NOT NULL DEFAULT 0,"
        " stale_after REAL NOT NULL,"
        " created_at REAL NOT NULL)"
    )
    try:
        os.chmod(DB_PATH, 0o600)
    except (OSError, NotImplementedError):
        pass
    return conn


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _append_reap_receipt(rec: dict) -> None:
    try:
        REAP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with REAP_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        os.chmod(REAP_LOG, 0o600)
    except OSError:
        pass  # advisory


def _refresh_heartbeat(conn: sqlite3.Connection) -> None:
    """R4/F7d: the banner-readable mirror — file read only, age-gateable via
    its own ts. Atomic write; failure is advisory."""
    try:
        now = _now()
        row = conn.execute(
            "SELECT"
            " SUM(CASE WHEN state='queued' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN state='running' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN state='failed' AND finished_at > ? THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN reap_count > 0 AND claimed_at > ? THEN 1 ELSE 0 END),"
            " MIN(CASE WHEN state='running' THEN claimed_at END),"
            " MIN(CASE WHEN state='queued' THEN created_at END)"
            " FROM jobs", (now - 86400, now - 86400)).fetchone()
        hb = {
            "ts": _iso(now),
            "queued": int(row[0] or 0),
            "running": int(row[1] or 0),
            "failed_24h": int(row[2] or 0),
            "reaped_24h": int(row[3] or 0),
            "oldest_running_age_s": (round(now - row[4], 1)
                                     if row[4] is not None else None),
            # F7c: a stuck-queued row is the 'deployed, never triggered'
            # class — its age must be visible, not just its existence.
            "oldest_queued_age_s": (round(now - row[5], 1)
                                    if row[5] is not None else None),
        }
        tmp = HEARTBEAT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(hb), encoding="utf-8")
        os.replace(tmp, HEARTBEAT)
    except Exception:
        pass  # advisory


def reap(conn: sqlite3.Connection, name: str) -> int:
    """F2/R5: per-name stale-claim reap. Same-host requires pid-dead AND
    age>stale_after; foreign-host reaps on age alone. Returns rows reaped."""
    now = _now()
    reaped = 0
    rows = conn.execute(
        "SELECT id, owner_host, owner_pid, claimed_at, stale_after,"
        " idempotency_key FROM jobs WHERE name = ? AND state = 'running'",
        (name,)).fetchall()
    for rid, host, pid, claimed, stale_after, key in rows:
        age = now - float(claimed or 0)
        if age <= float(stale_after or DEFAULT_STALE_AFTER_S):
            continue
        if host == _host() and pid and _pid_alive(int(pid)):
            continue  # F2: NEVER requeue a live same-host destructive job
        with conn:
            cur = conn.execute(
                "UPDATE jobs SET state='queued', owner_host=NULL,"
                " owner_pid=NULL, claimed_at=NULL,"
                " reap_count = reap_count + 1"
                " WHERE id = ? AND state = 'running'", (rid,))
        if cur.rowcount:
            reaped += 1
            _append_reap_receipt({
                "ts": _iso(now), "event": "reap", "name": name, "row_id": rid,
                "idempotency_key": key, "prev_owner": f"{host}:{pid}",
                "claim_age_s": round(age, 1),
            })
    return reaped


def _claim(conn: sqlite3.Connection, name: str, stale_after: float):
    """Enqueue-or-claim (F7c): claim the oldest queued row for this name, or
    create one directly in running. BEGIN IMMEDIATE serializes claimants —
    the loser sees zero queued rows AND a fresh running row, and exits
    quietly. Returns (row_id, idempotency_key) or None when claim lost."""
    with conn:
        row = conn.execute(
            "SELECT id, idempotency_key FROM jobs"
            " WHERE name = ? AND state = 'queued'"
            " ORDER BY created_at LIMIT 1", (name,)).fetchone()
        now = _now()
        if row is not None:
            rid, key = int(row[0]), str(row[1])
            cur = conn.execute(
                "UPDATE jobs SET state='running', owner_host=?, owner_pid=?,"
                " claimed_at=?, stale_after=? WHERE id=? AND state='queued'",
                (_host(), os.getpid(), now, stale_after, rid))
            if cur.rowcount:
                return rid, key
            return None
        # Atomic conditional insert — sqlite3 only BEGINs before DML, so a
        # separate COUNT would race a concurrent claimant; the NOT EXISTS
        # guard rides the same IMMEDIATE transaction as the insert itself.
        key = str(uuid.uuid4())
        cur = conn.execute(
            "INSERT INTO jobs (name, state, idempotency_key, owner_host,"
            " owner_pid, claimed_at, stale_after, created_at)"
            " SELECT ?, 'running', ?, ?, ?, ?, ?, ?"
            " WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE name = ?"
            "                   AND state IN ('queued', 'running'))",
            (name, key, _host(), os.getpid(), now, stale_after, now, name))
        if not cur.rowcount:
            return None  # fresh claim held elsewhere — quiet loss
        return int(cur.lastrowid), key


def observe_receipt(receipt_path: Path, key: str,
                    claimed_at: float) -> Optional[str]:
    """F1: the job's own receipt line — per-LINE UTC ts NEWER than the claim,
    an ``outcome`` field, and THIS run's idempotency key stamped in the line
    (``jobs_key``). File mtime is never consulted; reap receipts live in a
    different, queue-owned file and cannot match."""
    try:
        if not receipt_path.exists():
            return None
        for line in reversed(receipt_path.read_text(
                encoding="utf-8", errors="replace").splitlines()[-200:]):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("jobs_key") != key or "outcome" not in rec:
                continue
            try:
                ts = dt.datetime.fromisoformat(
                    str(rec.get("ts")).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                continue
            if ts >= claimed_at - 1.0:   # 1s grace for coarse receipt clocks
                return str(rec.get("outcome"))
        return None
    except OSError:
        return None


def cmd_run(args) -> int:
    if not _role_is_brain():
        print("jobs: role is not brain — no-op (role gate)")
        return 0
    conn = _connect()
    try:
        reap(conn, args.name)
        claim = _claim(conn, args.name, args.stale_after)
        if claim is None:
            print(f"jobs: claim lost for {args.name} (fresh run in flight) — quiet exit")
            _refresh_heartbeat(conn)
            return 0
        rid, key = claim
        claimed_at = _now()
        env = dict(os.environ)
        env["JOBS_IDEMPOTENCY_KEY"] = key
        try:
            proc = subprocess.run(args.argv, env=env)
            child_rc = proc.returncode
        except OSError as e:
            with conn:
                conn.execute(
                    "UPDATE jobs SET state='failed', finished_at=?,"
                    " outcome=? WHERE id=?",
                    (_now(), f"failed:exec-error:{e}"[:200], rid))
            print(f"jobs: exec failed for {args.name}: {e}")
            _refresh_heartbeat(conn)
            return 1
        outcome = observe_receipt(Path(args.receipt), key, claimed_at)
        if outcome is None:
            with conn:
                conn.execute(
                    "UPDATE jobs SET state='failed', finished_at=?, outcome=?"
                    " WHERE id=?",
                    (_now(), "failed:receipt-missing", rid))
            print(f"jobs: {args.name} exited {child_rc} but NO keyed receipt line "
                  f"observed in {args.receipt} — marked failed:receipt-missing "
                  "(the exact silent-completion class this queue exists to kill)")
            _refresh_heartbeat(conn)
            return child_rc if child_rc != 0 else 3
        state = "done" if child_rc == 0 else "failed"
        with conn:
            conn.execute(
                "UPDATE jobs SET state=?, finished_at=?, outcome=? WHERE id=?",
                (state, _now(), str(outcome)[:200], rid))
        print(f"jobs: {args.name} -> {state} outcome={outcome} (exit {child_rc})")
        _refresh_heartbeat(conn)
        return child_rc   # F7b: propagate — degraded:* exits stay loud
    finally:
        conn.close()


def cmd_status(args) -> int:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, state, outcome, claimed_at, finished_at, reap_count,"
            " owner_host, owner_pid FROM jobs WHERE name=?"
            " ORDER BY id DESC LIMIT ?", (args.name, args.limit)).fetchall()
        for r in rows:
            print(json.dumps({
                "id": r[0], "state": r[1], "outcome": r[2],
                "claimed_at": _iso(r[3]) if r[3] else None,
                "finished_at": _iso(r[4]) if r[4] else None,
                "reap_count": r[5],
                "owner": f"{r[6]}:{r[7]}" if r[6] else None,
            }))
        _refresh_heartbeat(conn)
        return 0
    finally:
        conn.close()


def cmd_reap(args) -> int:
    if not _role_is_brain():
        print("jobs: role is not brain — no-op (role gate)")
        return 0
    conn = _connect()
    try:
        n = reap(conn, args.name)
        print(f"jobs: reaped {n} stale claim(s) for {args.name}")
        _refresh_heartbeat(conn)
        return 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="W6 durable job queue (no daemon)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="claim-run-observe-complete one job")
    r.add_argument("name")
    r.add_argument("--receipt", required=True,
                   help="the JOB'S OWN outcome-coded JSONL (per-adopter file; "
                        "lines must stamp jobs_key from JOBS_IDEMPOTENCY_KEY)")
    r.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER_S,
                   help="seconds before an abandoned claim is reapable "
                        "(set per adopter vs its own time limit)")
    r.add_argument("argv", nargs=argparse.REMAINDER,
                   help="-- <command...> to execute")
    s = sub.add_parser("status", help="recent rows for a job name")
    s.add_argument("name")
    s.add_argument("--limit", type=int, default=5)
    rp = sub.add_parser("reap", help="per-name stale-claim reap")
    rp.add_argument("name")
    args = p.parse_args()
    if args.cmd == "run":
        if args.argv and args.argv[0] == "--":
            args.argv = args.argv[1:]
        if not args.argv:
            print("jobs: run requires -- <command...>")
            return 2
        return cmd_run(args)
    if args.cmd == "status":
        return cmd_status(args)
    return cmd_reap(args)


if __name__ == "__main__":
    sys.exit(main())
