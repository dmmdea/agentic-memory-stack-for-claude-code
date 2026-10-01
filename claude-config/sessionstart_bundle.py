#!/usr/bin/env python3
"""SessionStart durable/evidence bundle enrichment (B1).

The SessionStart banner already surfaces canonical facts + open goals + recent episodes, but NOT
the ranked durable/evidence facts the per-prompt UserPromptSubmit hook injects once a prompt
exists. There is no prompt yet at session start, so this helper closes that gap: it pulls the SAME
admission-gated /v1/context/bundle and emits a thin, distilled, advisory precis of the top
durable/evidence fact(s) under the banner. (Written in June 2026, when the per-prompt hook was
silent in the VS Code / Agent-SDK runtime; that outage was the hook command form, fixed in 1.18.0,
and the hook fires today. The precis stays: it is the only injection before the first prompt.)

Design (frontier-grounded; see docs/research and the B1 plan item):
  - SCOPE-FIRST, RANK-SECOND: at SessionStart there is NO live user query. We build a RECENCY
    PSEUDO-QUERY from the most-recent episode goal ("what was I last doing"), brand-scoped. The
    durable-fact search is BRAND-scoped server-side (fail-closed); `initiative` is forwarded
    (it scopes the bundle's goals) and seeds the pseudo-query fallback. The query text only seeds
    RANKING, so an off-topic recency goal degrades to silence (safe abstention), never a leak.
  - THE SEED FOLLOWS THE ROLE (One-Brain Rule): the brain reads its own episodic.db; a replica's
    copy froze at the authority cutover, so a replica/client asks the authority (GET /v1/episodes)
    and, when that read fails, has NO seed — it never falls back to the frozen copy.
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
import urllib.parse
import urllib.request

HEADER = "Recently-relevant memory (verify before acting):"
# Same value the hook daemon stamps; must stay in KNOWN_HOOK_CONTRACT_VERSIONS (mem0-server/hook_contract.py).
HOOK_CONTRACT_VERSION = "20.0"
DEFAULT_LIMIT = 120  # per-fact char cap (matches the canonical/episode banner lines)
DEFAULT_K = 1        # boot precision: at most the single highest-ranked durable/evidence fact
MARKER_NAME = "precompact-query.json"  # written by precompact_capture.py (B1 Phase 2)
MARKER_MAX_AGE = 300  # s — a marker older than this is stale (the post-compact boot fires seconds later)
RECENT_EPISODES = 20  # rows asked of the authority for the recency seed (GET /v1/episodes?recent=N)
EPISODES_TIMEOUT = 1.5  # s — the bound the banner's shell half puts on this same endpoint


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


def format_block(facts, source: str = "") -> str:
    """The advisory banner block, or '' when there is nothing to show (silent). `source` (v1.23
    P2-3) names where the facts came from — 'authority:<host:port>' — so a session can see which
    box answered; '' keeps the legacy header byte-for-byte."""
    if not facts:
        return ""
    header = HEADER[:-2] + f"; source={source}):" if source else HEADER
    return header + "\n" + "\n".join(f"  - [recall] {f}" for f in facts)


def resolve_authority_url(home: str) -> str:
    """~/.mem0/authority-url (per-host file) > MEM0_URL > loopback (v1.23 P2-3, spec §7): the file
    is the truth on a replica; the env var is only a fallback for a box that has no file."""
    try:
        with open(os.path.join(home, ".mem0", "authority-url"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line.rstrip("/")
    except OSError:
        pass
    env = os.environ.get("MEM0_URL", "").strip()
    return (env or "http://127.0.0.1:18791").rstrip("/")


def resolve_role(home: str) -> str:
    """This box's One-Brain role from ~/.mem0/role, read the way the banner's shell half reads it
    (storage-cap-check.sh): absent, unreadable or blank is the brain, because a single-machine install
    is its own authority. Anything else ('replica', 'client') means the local mirrors are frozen.
    Case-folded, like the other Python readers of this file."""
    try:
        with open(os.path.join(home, ".mem0", "role"), encoding="utf-8", errors="replace") as fh:
            role = fh.read().strip().lower()
    except OSError:
        role = ""
    return role or "brain"


def choose_query_and_params(marker_query, recency_query):
    """Pick the retrieval query + bundle params. A fresh PreCompact marker (real conversation query)
    wins → tier=frontier, K=2 (a real query justifies the second slot + ranks it). Otherwise the
    cold-boot recency pseudo-query → precision-first tier=small, K=1. Returns (query, tier, k)."""
    mq = (marker_query or "").strip()
    if mq:
        return mq, "frontier", 2
    return (recency_query or "").strip(), "small", 1


def pick_authority_goal(rows, brand) -> "str | None":
    """The authority-side twin of recent_goal_for_brand, over the GET /v1/episodes rows. The endpoint
    returns them newest first (ended_at DESC, the order the local query sorts by), so the seed is the
    first row with a non-blank goal. When a brand is given only that brand's rows count and a brand with
    no episode ABSTAINS (None): a foreign-brand goal weakens precision, exactly as in the local rule.
    Junk rows and non-list bodies are skipped, never raised on."""
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        goal = row.get("goal_text")
        if not isinstance(goal, str) or not goal.strip():
            continue
        if brand and row.get("brand") != brand:
            continue
        return goal
    return None


# --- I/O (real-server behaviour: the live e2e; seed source per role: claude-config/tests) ------

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


def fetch_recent_goal_from_authority(url: str, key: str, brand, limit: int = RECENT_EPISODES,
                                     timeout: float = EPISODES_TIMEOUT) -> "str | None":
    """GET /v1/episodes?recent=<limit>&state=complete[&brand=<brand>] and pick the seed
    (pick_authority_goal). The brand is forwarded so the server's window is THAT brand's newest episodes
    (the local query's `s.brand = ?`, not the newest few across every brand), and pick_authority_goal
    re-checks it, so an authority that ignores the parameter still cannot lend another brand's goal.
    state=complete keeps unfinished sessions (no goal, and always the newest ended_at) from filling the
    window; an older server ignores it and the blank-goal skip still applies. None on any error,
    timeout or malformed body: no seed, never an exception, and a session start waits `timeout` at most."""
    params = {"recent": limit, "state": "complete"}
    if brand:
        params["brand"] = brand
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/v1/episodes?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote),
            headers={"X-API-Key": key},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return pick_authority_goal(json.load(r), brand)
    except Exception:
        return None


def recency_seed(home: str, url: str, key: str, brand) -> "str | None":
    """The recency seed, from the source the box's role makes true (One-Brain Rule). The brain reads its
    own episodic.db. A replica's or thin client's copy froze at the authority cutover, so seeding from it
    ranked today's facts against a weeks-old goal: it asks the authority instead, and when that fails it
    has NO seed. It never falls back to the frozen copy."""
    if resolve_role(home) == "brain":
        return recent_goal_for_brand(os.path.join(home, ".mem0", "episodic.db"), brand)
    return fetch_recent_goal_from_authority(url, key, brand)


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


def build_bundle_payload(query: str, brand, initiative, tier: str = "small") -> dict:
    """The /v1/context/bundle body. It carries hook_contract_version: the server counts every bundle
    body without it as hook_contract.missing, and this helper was the larger unstamped share of that
    counter (session-12 audit)."""
    payload = {
        "session_id": "sessionstart-enrich", "prompt": query, "checkpoint": False, "tier": tier,
        "hook_contract_version": HOOK_CONTRACT_VERSION,
    }
    if brand:
        payload["brand"] = brand
    if initiative:
        payload["initiative"] = initiative
    return payload


def fetch_bundle(url: str, key: str, query: str, brand, initiative, tier: str = "small", timeout: float = 6.0) -> list:
    """POST /v1/context/bundle (checkpoint:false). tier scales K at the calibrated 0.30 gate
    (small=>K<=1, frontier=>K<=2). Returns memories[] or [] on any error."""
    payload = build_bundle_payload(query, brand, initiative, tier)
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

        # v1.23 P2-3: the per-host file first (the shim's precedence), MEM0_URL only as fallback.
        # Env-only resolution left this silently injecting NOTHING on a replica — the whole function
        # is wrapped in `except: pass`, so a connection refusal to a dead loopback looks identical
        # to "no memories matched". Resolved before the seed: a replica's seed is read from it too.
        url = resolve_authority_url(home)

        # Phase 2: a FRESH PreCompact marker (post-compaction) supplies a real conversation query
        # -> frontier K=2; otherwise the cold-boot recency pseudo-query -> precision-first small K=1.
        # The recency seed's source follows the role (recency_seed); a marker outranks the seed, so
        # when one is present the seed is not fetched at all (no round-trip for a discarded value).
        marker_query = load_and_consume_marker(os.path.join(home, ".mem0", MARKER_NAME), now=int(time.time()))
        recent_goal = None if marker_query else recency_seed(home, url, key, brand)
        recency_query = build_boot_query(recent_goal, brand, initiative)
        query, tier, k = choose_query_and_params(marker_query, recency_query)
        if not query:
            return 0  # no signal -> inject nothing

        memories = fetch_bundle(url, key, query, brand, initiative, tier=tier)
        block = format_block(select_facts(memories, k=k), source="authority:" + url.split("://", 1)[-1])
        if block:
            print(block)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
