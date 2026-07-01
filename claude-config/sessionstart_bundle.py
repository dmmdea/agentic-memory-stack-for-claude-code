#!/usr/bin/env python3
"""SessionStart durable/evidence bundle enrichment (B1).

The SessionStart banner already surfaces canonical facts + open goals + recent episodes, but NOT
the ranked durable/evidence facts the per-prompt UserPromptSubmit hook used to inject before that
hook went dead in the VS Code / Agent-SDK runtime. This helper closes that gap: it pulls the SAME
admission-gated /v1/context/bundle and emits a thin, distilled, advisory precis of the top
durable/evidence fact(s) under the banner.

Design (frontier-grounded; see docs/research and the B1 plan item):
  - SCOPE-FIRST, RANK-SECOND: at SessionStart there is NO live user query. We build a RECENCY
    PSEUDO-QUERY from the most-recent episode goal ("what was I last doing"), brand-scoped. The
    durable-fact search is BRAND-scoped server-side (fail-closed); `initiative` is forwarded
    (it scopes the bundle's goals) and seeds the pseudo-query fallback. The query text only seeds
    RANKING, so an off-topic recency goal degrades to silence (safe abstention), never a leak.
  - PRECISION OVER RECALL at boot (worst pollution regime: no query to disambiguate, brand-scoped
    facts are mutually-similar distractors, length alone taxes accuracy). We pass tier="small" so
    the server returns K<=1 at its calibrated 0.30 semantic gate — the "tighter K" lever using the
    server's OWN calibrated machinery, not a guessed client-side floor on the wrong (combined) score
    scale. We DISTILL (truncate), never inject raw bundle text.
  - checkpoint=False so this read never writes a synthetic episode into the resume banner.

Operator-agnostic + dependency-free (urllib/sqlite3/json stdlib only) + FAIL-SILENT: any error
prints nothing and exits 0, exactly like the rest of the SessionStart banner.

Usage (from storage-cap-check.sh):
  python3 sessionstart_bundle.py --brand "$BRAND" --initiative "$INITIATIVE"
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request

HEADER = "Recently-relevant memory (verify before acting):"
DEFAULT_LIMIT = 120  # per-fact char cap (matches the canonical/episode banner lines)
DEFAULT_K = 1        # boot precision: at most the single highest-ranked durable/evidence fact
MARKER_NAME = "precompact-query.json"  # written by precompact_capture.py (B1 Phase 2)
MARKER_MAX_AGE = 300  # s — a marker older than this is stale (the post-compact boot fires seconds later)


# --- pure logic (unit-tested) -------------------------------------------------

def build_boot_query(recent_goal, brand, initiative) -> str:
    """The boot pseudo-query: the recency goal if present, else the scope tokens, else ''.

    Returns '' when there is no signal at all — the caller then injects nothing (abstention).
    """
    rg = (recent_goal or "").strip()
    if rg:
        return rg
    scope = " ".join(t for t in [(brand or "").strip(), (initiative or "").strip()] if t)
    return scope


def distill(text, limit: int = DEFAULT_LIMIT) -> str:
    """A thin precis, never a dump: strip + truncate to `limit` chars (length taxes accuracy)."""
    return (text or "").strip()[:limit]


def select_facts(memories, k: int = DEFAULT_K) -> list:
    """Top-k non-blank durable/evidence facts, distilled. Preserves the bundle's ranking order."""
    out: list = []
    for m in memories or []:
        t = (m.get("memory") or "").strip()
        if not t:
            continue
        out.append(distill(t))
        if len(out) >= k:
            break
    return out


def format_block(facts) -> str:
    """The advisory banner block, or '' when there is nothing to show (silent)."""
    if not facts:
        return ""
    return HEADER + "\n" + "\n".join(f"  - [recall] {f}" for f in facts)


def choose_query_and_params(marker_query, recency_query):
    """Pick the retrieval query + bundle params. A fresh PreCompact marker (real conversation query)
    wins → tier=frontier, K=2 (a real query justifies the second slot + ranks it). Otherwise the
    cold-boot recency pseudo-query → precision-first tier=small, K=1. Returns (query, tier, k)."""
    mq = (marker_query or "").strip()
    if mq:
        return mq, "frontier", 2
    return (recency_query or "").strip(), "small", 1


# --- I/O (exercised by the live e2e, not unit-tested) -------------------------

def recent_goal_for_brand(db_path: str, brand) -> "str | None":
    """Most-recent episode goal_text. When a brand is given this is BRAND-SCOPED and ABSTAINS
    (returns None) if that brand has no episode yet — it does NOT fall back to another brand's goal
    (a foreign-brand pseudo-query weakens precision). Global only in the brandless case."""
    if not os.path.isfile(db_path):
        return None
    try:
        con = sqlite3.connect(db_path, timeout=2.0)  # bound the worst-case lock wait
        con.row_factory = sqlite3.Row
        base = (
            "SELECT e.goal_text AS g FROM episodes e "
            "LEFT JOIN sessions s ON e.session_id = s.session_id "
            "WHERE e.goal_text IS NOT NULL AND TRIM(e.goal_text) <> '' "
        )
        if brand:
            row = con.execute(base + "AND s.brand = ? ORDER BY e.ended_at DESC LIMIT 1", (brand,)).fetchone()
            return row["g"] if (row and (row["g"] or "").strip()) else None
        row = con.execute(base + "ORDER BY e.ended_at DESC LIMIT 1").fetchone()
        return row["g"] if row else None
    except Exception:
        return None


def load_and_consume_marker(path: str, now, max_age: int = MARKER_MAX_AGE) -> "str | None":
    """Return the PreCompact marker's query iff fresh (< max_age s old), else None. ALWAYS consumes
    (deletes) the marker — fresh, stale, or corrupt — so it can never linger into a later session."""
    if not os.path.isfile(path):
        return None
    m = None
    try:
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh)
    except Exception:
        m = None
    try:
        os.remove(path)  # consume-once, regardless of validity
    except Exception:
        pass
    if not isinstance(m, dict):
        return None
    q = (m.get("query") or "").strip()
    ts = m.get("ts")
    if not q or not isinstance(ts, (int, float)) or abs(now - ts) > max_age:
        return None
    return q


def fetch_bundle(url: str, key: str, query: str, brand, initiative, tier: str = "small", timeout: float = 6.0) -> list:
    """POST /v1/context/bundle (checkpoint:false). tier scales K at the calibrated 0.30 gate
    (small=>K<=1, frontier=>K<=2). Returns memories[] or [] on any error."""
    payload = {"session_id": "sessionstart-enrich", "prompt": query, "checkpoint": False, "tier": tier}
    if brand:
        payload["brand"] = brand
    if initiative:
        payload["initiative"] = initiative
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/v1/context/bundle",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-API-Key": key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.load(r) or {}).get("memories", []) or []
    except Exception:
        return []


def main(argv=None) -> int:
    """Emit the advisory durable/evidence precis, or nothing. Never raises; always exits 0."""
    try:
        ap = argparse.ArgumentParser()
        ap.add_argument("--brand", default="")
        ap.add_argument("--initiative", default="")
        args, _ = ap.parse_known_args(argv)  # never SystemExit on an unexpected arg
        brand = args.brand.strip() or None
        initiative = args.initiative.strip() or None

        home = os.path.expanduser("~")
        key = ""
        keyfile = os.path.join(home, ".mem0", "api-key")
        if os.path.isfile(keyfile):
            with open(keyfile, encoding="utf-8") as fh:
                key = fh.read().strip()
        if not key:
            return 0  # no key -> the server would reject; stay silent

        # Phase 2: a FRESH PreCompact marker (post-compaction) supplies a real conversation query
        # -> frontier K=2; otherwise the cold-boot recency pseudo-query -> precision-first small K=1.
        marker_query = load_and_consume_marker(os.path.join(home, ".mem0", MARKER_NAME), now=int(time.time()))
        recent_goal = recent_goal_for_brand(os.path.join(home, ".mem0", "episodic.db"), brand)
        recency_query = build_boot_query(recent_goal, brand, initiative)
        query, tier, k = choose_query_and_params(marker_query, recency_query)
        if not query:
            return 0  # no signal -> inject nothing

        url = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
        memories = fetch_bundle(url, key, query, brand, initiative, tier=tier)
        block = format_block(select_facts(memories, k=k))
        if block:
            print(block)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
