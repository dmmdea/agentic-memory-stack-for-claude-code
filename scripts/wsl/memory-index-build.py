#!/usr/bin/env python3
"""Build ~/.mem0/MEMORY.md - a lean index of canonical + insight + stable + temporal
tiers, grouped by SOURCE (lens A2: metadata.category / kind / topic don't exist
in this store, so categorizing by those keys collapses everything to 'other';
metadata.source IS standardized - every write tags it).

Hard cap 200 lines. Lead section: top 7 highest-signal items (lens N1: cognitive
working-memory ~ 7, so the first ~7 lines should give a useful snapshot even
if the reader stops there). Full structured list follows.

Verbose content stays in mem0; this is just a pointer index for SessionStart hydration."""
from __future__ import annotations
import datetime as dt, json, sys
from collections import defaultdict
from pathlib import Path
import httpx

def get_open_questions(n=5):
    """Read top N open questions from episodic.db for the MEMORY.md Open frontier section."""
    EPISODIC_DB = Path.home() / ".mem0" / "episodic.db"
    if not EPISODIC_DB.exists():
        return []
    import sqlite3
    try:
        with sqlite3.connect(str(EPISODIC_DB)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("""
                SELECT id, question_text, brand, priority, updated_at
                FROM open_questions WHERE status = 'open'
                ORDER BY priority, updated_at DESC LIMIT ?
            """, (n,))
            return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def get_active_goals(n=5):
    """Read top N open+blocked goals from episodic.db for the MEMORY.md Active goals section."""
    EPISODIC_DB = Path.home() / ".mem0" / "episodic.db"
    if not EPISODIC_DB.exists():
        return []
    import sqlite3
    try:
        with sqlite3.connect(str(EPISODIC_DB)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("""
                SELECT id, title, brand, status, priority, updated_at
                FROM goals
                WHERE status IN ('open', 'blocked')
                ORDER BY priority, updated_at DESC LIMIT ?
            """, (n,))
            return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def get_recent_episodes(n=7):
    """Read last N episodes from episodic.db for the MEMORY.md index. Returns list or empty list."""
    EPISODIC_DB = Path.home() / ".mem0" / "episodic.db"
    if not EPISODIC_DB.exists():
        return []
    import sqlite3
    try:
        with sqlite3.connect(str(EPISODIC_DB)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("""
                SELECT e.id, e.goal_text, e.summary_text, e.ended_at, s.brand
                FROM episodes e LEFT JOIN sessions s ON e.session_id = s.session_id
                ORDER BY e.ended_at DESC LIMIT ?
            """, (n,))
            return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []

QDRANT = "http://127.0.0.1:6333"
COLLECTION = "memories"
KEY = (Path.home() / ".mem0" / "api-key").read_text().strip()
OUT = Path.home() / ".mem0" / "MEMORY.md"
MAX_LINES = 200
LEAD_N = 7  # cognitive working-memory anchor (Miller 7+/-2)
# Tier priority for both index ordering AND lead-7 selection
PRIORITY = ["canonical", "insight", "stable", "temporal", "evidence"]
TIER_RANK = {t: i for i, t in enumerate(PRIORITY)}

def scroll_all():
    points, off = [], None
    with httpx.Client(timeout=15.0) as c:
        while True:
            body = {"limit": 256, "with_payload": True, "with_vector": False}
            if off is not None: body["offset"] = off
            r = c.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll", json=body)
            r.raise_for_status()
            res = r.json()["result"]
            points.extend(res.get("points", []))
            off = res.get("next_page_offset")
            if not off: break
    return points

def group_key(payload: dict) -> str:
    """Group by source (the field that IS standardized). Falls back to 'unsourced'."""
    src = (payload.get("source") or "").strip().lower()
    return src or "unsourced"

def main():
    pts = scroll_all()
    indexed = []   # entries for full index
    for p in pts:
        pl = p.get("payload") or {}
        if pl.get("retrievable") is False: continue
        tier = pl.get("tier", "evidence")
        if tier == "evidence": continue   # plain evidence stays out of the index
        text = pl.get("data") or pl.get("memory") or ""
        if not text: continue
        indexed.append({
            "id": str(p["id"]),
            "tier": tier,
            "source": group_key(pl),
            "snippet": " ".join(text.split())[:150],
            "tier_actor": pl.get("tier_actor", ""),
            "updated_at": pl.get("updated_at") or pl.get("created_at") or "",
        })

    # Lead 7: highest-priority items by (tier_rank, recency)
    def lead_sort(e):
        return (TIER_RANK.get(e["tier"], 99), e["updated_at"])
    lead = sorted(indexed, key=lead_sort)[:LEAD_N]

    # Full grouping: tier -> source -> [entries]
    by_tier_src = defaultdict(lambda: defaultdict(list))
    for e in indexed:
        by_tier_src[e["tier"]][e["source"]].append(e)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        f"# MEMORY.md - agentic-memory-stack lean index",
        f"Generated {now} by memory-index-build.py * Hard cap {MAX_LINES} lines * Lead {LEAD_N} = working-memory anchor.",
        f"",
        f"## Top {LEAD_N} (skim-only)",
    ]
    for e in lead:
        out.append(f"- `{e['id'][:8]}` [{e['tier']}] {e['snippet']}")
    out.extend([
        f"",
        f"## Full index (grouped by tier -> source)",
        f"Plain evidence lives in mem0 (search by query). Full text via `memory_search` / `memory_list`.",
        f"",
    ])
    for tier in PRIORITY:
        if tier == "evidence": continue
        srcs = by_tier_src.get(tier)
        if not srcs: continue
        out.append(f"### tier={tier}")
        for src, items in sorted(srcs.items()):
            out.append(f"**source={src}** ({len(items)})")
            for e in items[:12]:
                out.append(f"- `{e['id'][:8]}` {e['snippet']}")
            if len(items) > 12:
                out.append(f"- ... +{len(items)-12} more")
        out.append("")
        if len(out) >= MAX_LINES:
            out = out[:MAX_LINES - 1]
            out.append("... (truncated at hard cap; see mem0 for full set)")
            break
    # Recent episodes section — appended after tier index, before hard-cap truncation
    episodes = get_recent_episodes(7)
    if episodes and len(out) < MAX_LINES - 12:
        out.append(f"## Recent episodes (last {len(episodes)})")
        out.append("")
        for e in episodes:
            ended = (e.get("ended_at") or "")[:10]
            brand = (e.get("brand") or "ai-ecosystem")[:14]
            goal = (e.get("goal_text") or "")
            if len(goal) > 120:
                goal = goal[:120] + '...'
            out.append(f"- [{ended}] **{brand}** — {goal}")
        out.append("")
    # Active goals section — v0.16: open + blocked goals from episodic.db
    goals = get_active_goals(5)
    if goals and len(out) < MAX_LINES - 12:
        out.append(f"## Active goals (top {len(goals)} by priority)")
        out.append("")
        for g in goals:
            brand = (g.get("brand") or "—")[:14]
            title = (g.get("title") or "")
            if len(title) > 100:
                title = title[:100] + '...'
            st = g.get("status") or "open"
            out.append(f"- **{brand}** [P{g.get('priority',3)} {st.upper()}] {title}")
        out.append("")
    # Open frontier section — v0.17 Phase D: Epistemic Reachability signal
    questions = get_open_questions(5)
    if questions and len(out) < MAX_LINES - 12:
        out.append(f"## Open frontier ({len(questions)} questions, Epistemic Reachability)")
        out.append("")
        for q in questions:
            brand = (q.get("brand") or "cross-brand")[:14]
            text = (q.get("question_text") or "")
            if len(text) > 100:
                text = text[:100] + '...'
            out.append(f"- **{brand}** [P{q.get('priority',3)}] {text}")
        out.append("")
    # Enforce hard cap
    if len(out) > MAX_LINES:
        out = out[:MAX_LINES - 1]
        out.append("... (truncated at hard cap; see mem0 for full set)")
    OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(out)} lines)")

if __name__ == "__main__":
    sys.exit(main())
