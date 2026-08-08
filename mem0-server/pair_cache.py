"""W5 ADOPT-4: per-pair judge-verdict cache for the contradiction sweeps.

Why: the weekly sweeps re-pay 20-30s of Codex latency per pair for pairs whose
texts have not changed — idempotency was a per-CANDIDATE stamp, not per-pair,
and it was not keyed by judge or prompt version. This cache makes the verdict
layer content-addressed.

Store: SQLite at ``~/.mem0/pair-verdict-cache.db`` beside the existing
sidecars (chosen over JSONL so TTL pruning and keyed upsert are transactional
— an append-only verdict log would be audit defect class 7, unbounded
accumulation). WAL, busy_timeout, 0600.

Contract (review R2, all binding):
- ONLY boolean verdicts are cached. ``None``/error verdicts (codex-error,
  llm-error, unparseable) are NEVER written — a transient Codex outage must
  not suppress judging for the TTL window.
- The key covers question_type + judge_mode + model + prompt_version + the
  pair texts. Contradiction is an unordered predicate (sorted hashes);
  supersession is DIRECTION-DEPENDENT (older-vs-newer) so its key is ordered.
- Every cache I/O is fail-soft: a locked/corrupt/missing db degrades to a
  cache miss on read and a no-op on write — the sweep must never fail (or
  block) because of its cache. Three different mkdir-locked runners can
  overlap on this file.
- TTL 30 days, enforced at READ time (an expired row is a miss) and pruned
  opportunistically on write. The authoritative ``--rejudge-stamped``
  self-heal path BYPASSES the cache entirely at the call site (pinned) — the
  cache accelerates DISCOVERY, never re-judgment.
- Rows hold text HASHES and the verdict only — never memory content and never
  the judge's free-text detail (this file is a new disk artifact; it must not
  become a fourth place memory text leaks to).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

TTL_SECONDS = 30 * 86400
_DB_NAME = "pair-verdict-cache.db"


def _db_path() -> Path:
    return Path.home() / ".mem0" / _DB_NAME


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=3)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS verdicts ("
        " key TEXT PRIMARY KEY,"
        " verdict INTEGER NOT NULL,"
        " question_type TEXT NOT NULL,"
        " judge_mode TEXT NOT NULL,"
        " created_ts REAL NOT NULL)"
    )
    try:
        os.chmod(p, 0o600)
    except (OSError, NotImplementedError):
        pass
    return conn


def _h(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def make_key(question_type: str, judge_mode: str, model: str,
             prompt_version: str, text_a: str, text_b: str) -> str:
    """Content-addressed verdict key. ``contradiction`` is symmetric →
    unordered (sorted hashes); ``supersession`` asks 'does B supersede A' →
    ORDERED. Any other question_type defaults to ordered (the safe side)."""
    ha, hb = _h(text_a), _h(text_b)
    if question_type == "contradiction":
        pair = "|".join(sorted((ha, hb)))
    else:
        pair = f"{ha}|{hb}"
    return f"{question_type}|{judge_mode}|{model}|{prompt_version}|{pair}"


def get(key: str, now: Optional[float] = None) -> Optional[bool]:
    """Cached verdict, or None on miss/expiry/ANY cache failure."""
    hit = get_with_ts(key, now=now)
    return None if hit is None else hit[0]


def get_with_ts(key: str, now: Optional[float] = None):
    """(verdict, created_ts) or None — review fix 4: an APPLIED stamp whose
    justification came from the cache must say WHEN the verdict was judged,
    not just 'cache-hit'."""
    try:
        now = time.time() if now is None else now
        with _connect() as conn:
            row = conn.execute(
                "SELECT verdict, created_ts FROM verdicts WHERE key = ?",
                (key,)).fetchone()
        if row is None:
            return None
        verdict, created = int(row[0]), float(row[1])
        if now - created > TTL_SECONDS:
            return None
        return bool(verdict), created
    except Exception:
        return None


def put(key: str, verdict, question_type: str, judge_mode: str,
        now: Optional[float] = None) -> bool:
    """Write-through — booleans ONLY (R2: an error/None verdict must never
    suppress future judging). Returns False on skip or any cache failure."""
    if not isinstance(verdict, bool):
        return False
    try:
        now = time.time() if now is None else now
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO verdicts"
                " (key, verdict, question_type, judge_mode, created_ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, int(verdict), question_type, judge_mode, now))
            # Opportunistic TTL prune keeps the file bounded.
            conn.execute("DELETE FROM verdicts WHERE created_ts < ?",
                         (now - TTL_SECONDS,))
        return True
    except Exception:
        return False


def stats() -> dict:
    """Row count + oldest ts for receipts. Fail-soft."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(created_ts) FROM verdicts").fetchone()
        return {"rows": int(row[0] or 0), "oldest_ts": row[1]}
    except Exception:
        return {"rows": None, "oldest_ts": None}
