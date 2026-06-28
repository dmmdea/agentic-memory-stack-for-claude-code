#!/usr/bin/env python3
"""mem0 MCP shim — exposes our local mem0 REST server (:18791) as a proper MCP server.
Stdio transport (what Claude Code expects for stdio-type MCP entries).
"""
from __future__ import annotations
import os
from pathlib import Path
import httpx
from fastmcp import FastMCP

MEM0_URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY_FILE = Path.home() / ".mem0" / "api-key"
if not KEY_FILE.exists():
    raise SystemExit(f"FAIL: mem0 API key not found at {KEY_FILE}")
MEM0_KEY = KEY_FILE.read_text(encoding="utf-8").strip()

mcp = FastMCP("mem0")

def _headers() -> dict:
    return {"X-API-Key": MEM0_KEY, "Content-Type": "application/json"}

@mcp.tool
def memory_add(text: str, user_id: str = "__WSL_USER__", infer: bool = False, metadata: dict | None = None) -> dict:
    """Add a memory to mem0. Set infer=False to store as-is; True to LLM-extract facts.
    metadata dict can include {source, tier, kind, workspace, project, ...}.

    v0.16.1 client-side UX guard: tier='canonical' in metadata is auto-downgraded to 'evidence'
    with a note explaining how to promote via mem0-canonize.sh CLI. tier='insight' from
    non-consolidator sources is similarly downgraded with a note. The server-side enforcement
    is unchanged — this is a friendlier client-side default.
    """
    md = dict(metadata or {})
    note = None
    if md.get("tier") == "canonical":
        md["tier"] = "evidence"
        md["_canonical_intent"] = True
        note = (
            "tier auto-downgraded canonical→evidence; canonical promotions require user-direct "
            "HMAC gate (v0.14+). To promote: bash mem0-canonize.sh <returned id> \"<reason>\" "
            "from a terminal."
        )
    elif md.get("tier") == "insight":
        INSIGHT_ALLOWED = {"c1-consolidator", "dream-consolidator", "c1-dream-consolidator"}
        src = (md.get("source") or "").lower()
        if src not in INSIGHT_ALLOWED:
            md["tier"] = "evidence"
            md["_insight_intent"] = True
            note = (
                "tier auto-downgraded insight→evidence; insight is reserved for c1/dream "
                "consolidator. Dream will pick this up on its next nightly cycle if it crosses "
                "the bar."
            )
    payload = {"messages": text, "user_id": user_id, "infer": infer, "metadata": md}
    r = httpx.post(f"{MEM0_URL}/v1/memories", json=payload, headers=_headers(), timeout=30.0)
    r.raise_for_status()
    result = r.json()
    if note:
        result["note"] = note
    return result

@mcp.tool
def memory_search(query: str, user_id: str = "__WSL_USER__", limit: int = 5, threshold: float = 0.1, rerank: bool | None = None, query_class: str = "durable", brand: str | None = None, allow_cross_brand: bool = False) -> dict:
    """Semantic search over mem0. Returns up to `limit` memories above `threshold`.
    Auto-reranks via the bge cross-encoder when limit>=5 (measured 2026-06-22 to IMPROVE
    relevance — blind Codex A/B 7/11; adds ~2s on this CPU box, fine for a DELIBERATE search,
    and NOT on the <500ms per-prompt bundle). Pass rerank=True/False to override the default.
    query_class: 'durable' (default) | 'operational' (recency-decayed) |
    'canonical' (REQUIRED to retrieve tier=canonical ground-truth records —
    the default class excludes them).
    brand: scope the search to one brand (e.g. 'myapp', 'ai-ecosystem') —
    pass it when working in a brand context. v0.19 fail-closed default: a
    search WITHOUT brand returns only brand-neutral (null-brand) records.
    allow_cross_brand: explicit opt-in for a brandless search to also return
    brand-scoped records from every brand (audited; use deliberately)."""
    filters: dict = {"user_id": user_id}
    if brand:
        filters["brand"] = brand
    if allow_cross_brand:
        filters["allow_cross_brand"] = True
    eff_rerank = (limit >= 5) if rerank is None else rerank   # auto-on for deliberate (limit>=5) searches
    payload = {"query": query, "filters": filters, "limit": limit, "threshold": threshold, "rerank": eff_rerank, "query_class": query_class}
    r = httpx.post(f"{MEM0_URL}/v1/memories/search", json=payload, headers=_headers(), timeout=30.0)
    r.raise_for_status()
    return r.json()

@mcp.tool
def memory_recall(query: str, brand: str | None = None, initiative: str | None = None, project: str | None = None, user_id: str = "__WSL_USER__") -> dict:
    """PROACTIVELY pull the memory the task you are about to start depends on — the SAME
    admission-gated, brand-scoped, ranked context the per-prompt UserPromptSubmit hook USED
    to inject before that hook went dead in this runtime (VS Code extension / Agent SDK).
    Call it at the START of any substantive task that could turn on a prior decision, brand or
    project state, a port/path/config value, or an open goal — NOT on every trivial turn
    (over-recall is the named anti-pattern; an EMPTY result is a valid "nothing relevant is
    stored", never a failure).

    Returns {ok, canonical, memories, goals, open_questions}:
      - canonical: query-relevant tier=canonical GROUND TRUTH (locked facts — reserved ports,
        locked decisions, brand directives). The default search class EXCLUDES these, so this
        verb fetches them explicitly; trust them as ground truth.
      - memories: the per-prompt bundle's top gated durable/evidence facts for `query` (K<=2,
        threshold-gated — identical to what the dead hook would have injected).
      - goals / open_questions: open goals + questions, optionally scoped to brand/initiative.

    No side effects — the episode checkpoint is suppressed (checkpoint=False), so a recall never
    pollutes the SessionStart resume banner. brand: pass it when working in a brand context
    (e.g. 'brand-a', 'ai-ecosystem'). v1.0 Phase B: a branded recall now returns that brand's
    facts PLUS the brand-NEUTRAL (general) facts that apply to every brand, excluding only OTHER
    brands — so passing brand no longer starves recall; it ADDS brand-specific facts on top of the
    neutral set. Brandless returns the brand-neutral set only. Either is safe (no cross-brand leak).
    initiative/project scope the goals/questions to a repo leaf. For a DELIBERATE free-text search
    instead of this curated bundle, use memory_search."""
    H = _headers()
    out: dict = {"ok": True}
    # 1) the per-prompt bundle (durable memories + open goals + open questions), checkpoint
    #    suppressed so a manual pull writes no episode. Reuses the EXACT _search_core gate path.
    bpayload: dict = {"session_id": "mcp-memory-recall", "prompt": query, "checkpoint": False}
    if brand:
        bpayload["brand"] = brand
    if initiative:
        bpayload["initiative"] = initiative
    if project:
        bpayload["project"] = project
    try:
        rb = httpx.post(f"{MEM0_URL}/v1/context/bundle", json=bpayload, headers=H, timeout=30.0)
        rb.raise_for_status()
        b = rb.json()
        out["memories"] = b.get("memories", [])
        out["goals"] = b.get("goals", [])
        out["open_questions"] = b.get("open_questions", [])
    except Exception as e:
        out["memories"], out["goals"], out["open_questions"] = [], [], []
        out["bundle_error"] = str(e)
    # 2) query-relevant canonical ground-truth — the default class excludes tier=canonical,
    #    so a recall would otherwise miss the locked facts (ports/decisions) most worth pulling.
    cfilters: dict = {"user_id": user_id}
    if brand:
        cfilters["brand"] = brand
    try:
        rc = httpx.post(f"{MEM0_URL}/v1/memories/search", json={"query": query, "filters": cfilters, "limit": 5, "threshold": 0.0, "rerank": False, "query_class": "canonical"}, headers=H, timeout=30.0)
        rc.raise_for_status()
        out["canonical"] = rc.json().get("results", [])
    except Exception as e:
        out["canonical"] = []
        out["canonical_error"] = str(e)
    return out

@mcp.tool
def memory_list(user_id: str = "__WSL_USER__", limit: int = 50) -> dict:
    """List recent memories for user_id. Hard-capped server-side at 500.
    Prefer memory_search for content discovery; this is for inventory."""
    if limit > 500:
        limit = 500
    r = httpx.get(f"{MEM0_URL}/v1/memories", params={"user_id": user_id, "limit": limit}, headers=_headers(), timeout=30.0)
    r.raise_for_status()
    return r.json()

@mcp.tool
def memory_delete(memory_id: str) -> dict:
    """Delete a memory by its ID."""
    r = httpx.delete(f"{MEM0_URL}/v1/memories/{memory_id}", headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()

@mcp.tool
def memory_update(memory_id: str, text: str) -> dict:
    """Update a memory's text content."""
    r = httpx.put(f"{MEM0_URL}/v1/memories/{memory_id}", json={"text": text}, headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()

@mcp.tool
def memory_promote(memory_id: str, tier: str = "stable", reason: str | None = None) -> dict:
    """Promote a memory's trust tier. tier in {evidence, stable, insight, temporal}.

    NOTE: 'canonical' is NOT allowed via MCP — it requires user-direct CLI authentication.
    To canonize a memory, ask the operator to run:
      bash mem0-canonize.sh <memory_id> "<reason>"

    actor is always 'claude-autonomous' when called via MCP (server-enforced).
    'insight' is reserved for c1-consolidator/dream-consolidator (server-enforced; will 403).

    reason is recommended for audit clarity and REQUIRED for canonical (n/a here).

    Writes a tier-ledger entry at ~/.mem0/tier-ledger.jsonl on success."""
    if tier == "canonical":
        raise ValueError(
            "canonical promotion requires user-direct CLI authentication. "
            "Ask the operator to run: bash mem0-canonize.sh " + memory_id + " \"<reason>\""
        )
    payload = {"tier": tier, "actor": "claude-autonomous", "reason": reason}
    r = httpx.patch(f"{MEM0_URL}/v1/memories/{memory_id}/tier", json=payload, headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()

@mcp.tool
def memory_demote(memory_id: str, tier: str = "evidence", reason: str | None = None) -> dict:
    """Demote a memory's trust tier (e.g., stable -> evidence when wrong).
    For full removal use memory_delete. Writes a tier-ledger entry.
    actor is always 'claude-autonomous' when called via MCP.
    reason is recommended for audit clarity."""
    payload = {"tier": tier, "actor": "claude-autonomous", "reason": reason}
    r = httpx.patch(f"{MEM0_URL}/v1/memories/{memory_id}/tier", json=payload, headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()

@mcp.tool
def memory_health() -> dict:
    """Check mem0 server health."""
    r = httpx.get(f"{MEM0_URL}/health", timeout=5.0)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# v0.15: Episodic memory tools
# These query the SQLite + FTS5 episodic sidecar (~/.mem0/episodic.db).
# Use these for temporal / session-level questions that mem0 vector search
# can't answer: "what was I working on Tuesday?", "every session that
# touched MyApp", "when did the canonical-key idea first come up?".
# ---------------------------------------------------------------------------

@mcp.tool
def episodic_search(
    query: str,
    since: str | None = None,
    until: str | None = None,
    brand: str | None = None,
    limit: int = 10,
) -> dict:
    """Search past episodic memory (sessions) by keyword + optional date range + brand filter.

    Returns episodes ranked by FTS5 relevance. Each result has: id, session_id,
    started_at, ended_at, goal_text, summary_text, brand, workspace, project, rank.

    Use cases:
    - "what was I working on Tuesday?" → episodic_search("memory stack", since="2026-06-10")
    - "every session that touched MyApp" → episodic_search("checkout", brand="myapp")
    - "when did we first discuss the canonical-key idea?" → episodic_search("canonical key")
    - "sessions about MyApp landing page" → episodic_search("landing page myapp")

    since/until: ISO 8601 date strings, e.g. "2026-06-10" or "2026-06-10T14:00:00Z".
    brand: your project/brand label as configured in brands.json (default "ai-ecosystem").
    limit: max results (default 10, server cap 20).
    """
    payload: dict = {"query": query, "limit": limit}
    if since:
        payload["since"] = since
    if until:
        payload["until"] = until
    if brand:
        payload["brand"] = brand
    r = httpx.post(f"{MEM0_URL}/v1/episodes/search", json=payload, headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def episodic_recent(limit: int = 7, brand: str | None = None) -> list:
    """Return the last N episodes ordered by ended_at descending.

    Default limit=7 — a cognitive working-memory anchor (Miller's Law).
    Each result has: id, session_id, started_at, ended_at, goal_text, summary_text,
    brand, workspace, project.

    Use cases:
    - "what have I been working on lately?" → episodic_recent()
    - "show me recent MyApp sessions" → episodic_recent(limit=10, brand="myapp")
    - "what happened in the last 3 sessions?" → episodic_recent(limit=3)

    brand: filter to a specific brand (optional).
    """
    params: dict = {"recent": limit}
    if brand:
        params["brand"] = brand
    r = httpx.get(f"{MEM0_URL}/v1/episodes", params=params, headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def episodic_get(episode_id: int) -> dict:
    """Fetch full detail for one episode by its integer id.

    Returns all episode fields plus linked_memories: a list of mem0 memory IDs
    (cross-references) that were produced or cited in that session.

    Use this after episodic_search or episodic_recent to drill into a specific
    episode and see which mem0 facts were created/cited during that session.

    episode_id: integer id from episodic_search or episodic_recent results.
    """
    r = httpx.get(f"{MEM0_URL}/v1/episodes/{episode_id}", headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# v0.16: Goal tools
# These query the goals table in the episodic sidecar via mem0 REST.
# Use for planning questions: "what am I working on?", "what's blocked?",
# "show me the goal hierarchy", or to manually create a new goal.
# ---------------------------------------------------------------------------

@mcp.tool
def goals_list(status: str | None = None, brand: str | None = None, limit: int = 20) -> list[dict]:
    """List goals with optional status/brand filters. status ∈ {open, blocked, advanced, completed, abandoned}.
    Use for 'show me all open goals for MyApp' / 'what's the goal landscape?'."""
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    if brand:
        params["brand"] = brand
    r = httpx.get(f"{MEM0_URL}/v1/goals", params=params, headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def goals_tree(root_id: int | None = None) -> list[dict]:
    """Recursive goal tree (adjacency-list hierarchy). If root_id is None, returns all top-level goals + descendants with depth field.
    Use for 'show me the goal hierarchy' / 'what's the structure of my plans?'."""
    params: dict = {}
    if root_id is not None:
        params["root_id"] = root_id
    r = httpx.get(f"{MEM0_URL}/v1/goals/tree", params=params, headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def goals_open(brand: str | None = None, limit: int = 10) -> list[dict]:
    """Convenience: list open goals. Use for 'what am I currently working on?'."""
    params: dict = {"status": "open", "limit": limit}
    if brand:
        params["brand"] = brand
    r = httpx.get(f"{MEM0_URL}/v1/goals", params=params, headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def goals_blocked(brand: str | None = None, limit: int = 10) -> list[dict]:
    """Convenience: list blocked goals. Use for 'what's stuck right now?' / 'show me blockers'."""
    params: dict = {"status": "blocked", "limit": limit}
    if brand:
        params["brand"] = brand
    r = httpx.get(f"{MEM0_URL}/v1/goals", params=params, headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def goal_details(goal_id: int) -> dict:
    """Full goal detail including linked_episode_count and parent reference.
    Use to investigate a specific goal."""
    r = httpx.get(f"{MEM0_URL}/v1/goals/{goal_id}", headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def goal_create_manual(title: str, description: str | None = None, brand: str | None = None,
                       priority: int = 3, parent_goal_id: int | None = None) -> dict:
    """Create a top-level or child goal directly (operator-direct, not auto-extracted from sessions).
    Use when the operator says 'add a goal: ship X by Y'."""
    body: dict = {"title": title, "priority": priority}
    if description:
        body["description"] = description
    if brand:
        body["brand"] = brand
    if parent_goal_id is not None:
        body["parent_goal_id"] = parent_goal_id
    r = httpx.post(f"{MEM0_URL}/v1/goals", json=body, headers=_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# v0.17 Phase D: Open Questions tools (Epistemic Reachability signal)
# These expose the global open_questions registry — cross-session frontier tracking.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# v0.17 Phase E: Goal lifecycle tools
# ---------------------------------------------------------------------------

@mcp.tool
def goal_abandon(goal_id: int, reason: str, actor: str = "claude-autonomous") -> dict:
    """Abandon a goal as no-longer-relevant. Distinct from 'completed' (which means goal was achieved).
    Use when scope changed, priority dropped, or goal was redundant with another.
    Requires a non-empty reason — this is a deliberate trash-can move; document why."""
    body = {"actor": actor, "reason": reason}
    r = httpx.patch(f"{MEM0_URL}/v1/goals/{goal_id}/abandon", json=body, headers=_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def goal_complete(goal_id: int, reason: str, actor: str = "claude-autonomous") -> dict:
    """Mark a goal as completed — the work shipped / the goal was achieved.
    Distinct from 'abandon' (which means scope dropped or the goal became infeasible).
    Use this when a goal's deliverable is actually done so it stops bleeding into every
    session's surfaced context. Sets completed_at and stamps a goal-completed ledger entry.
    Requires a non-empty reason — document what shipped. (v0.22 Phase A, resolves OQ#636.)"""
    body = {"actor": actor, "reason": reason}
    r = httpx.patch(f"{MEM0_URL}/v1/goals/{goal_id}/complete", json=body, headers=_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# v0.17 Phase F.3.1: memory_get_by_id — exact read before update/delete/promote
# ---------------------------------------------------------------------------

@mcp.tool
def memory_get_by_id(memory_id: str) -> dict:
    """Exact read by memory_id. Returns text, metadata, tier, retrievable, source, timestamps.

    Use BEFORE memory_update, memory_delete, memory_promote — search/list are not substitutes
    for exact reads. Avoids wrong-ID edits caused by search rediscovering similar records.

    Pattern: call memory_get_by_id first to confirm the correct record, THEN act on it."""
    r = httpx.get(f"{MEM0_URL}/v1/memories/{memory_id}", headers=_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# v0.17 Phase F.3.2: goal management — priority, link_episode, merge
# ---------------------------------------------------------------------------

@mcp.tool
def goal_set_priority(goal_id: int, priority: int, reason: str | None = None,
                      actor: str = "claude-autonomous") -> dict:
    """Update a goal's priority (1=highest, 5=lowest). v0.17 F.3.2.
    Use when a goal's urgency changes relative to other open goals."""
    body = {"priority": priority, "actor": actor, "reason": reason}
    r = httpx.patch(f"{MEM0_URL}/v1/goals/{goal_id}/priority", json=body, headers=_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def goal_link_episode(goal_id: int, episode_id: int, link_type: str = "advanced_goal",
                      delta_text: str | None = None, actor: str = "claude-autonomous") -> dict:
    """Explicitly link an episode to a goal.
    link_type ∈ {advanced_goal, blocked_goal, completed_goal, cited_goal}.
    Use when a session advanced a goal but auto-extraction from the Stop hook missed it.
    v0.17 F.3.2."""
    body = {"episode_id": episode_id, "link_type": link_type, "delta_text": delta_text, "actor": actor}
    r = httpx.post(f"{MEM0_URL}/v1/goals/{goal_id}/link_episode", json=body, headers=_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def goal_merge(source_goal_id: int, target_goal_id: int, reason: str,
               actor: str = "claude-autonomous") -> dict:
    """Merge source goal into target: move all episode links + mark source as 'duplicate'.
    Use when two goals turn out to be the same thing (duplicate created by auto-extraction).
    Source stays in DB for audit but is excluded from default listings. v0.17 F.3.2."""
    body = {"target_goal_id": target_goal_id, "actor": actor, "reason": reason}
    r = httpx.post(f"{MEM0_URL}/v1/goals/{source_goal_id}/merge", json=body, headers=_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# v0.17 Phase D: Open Questions tools (Epistemic Reachability signal)
# These expose the global open_questions registry — cross-session frontier tracking.
# ---------------------------------------------------------------------------

@mcp.tool
def open_questions_open(brand: str | None = None, limit: int = 7) -> list[dict]:
    """List current open frontier questions (Epistemic Reachability signal).
    Filter by brand. Default limit 7 = Miller's working-memory anchor.

    Use this when starting work on a brand to see what's unresolved across past sessions."""
    params: dict = {"status": "open", "limit": limit}
    if brand:
        params["brand"] = brand
    r = httpx.get(f"{MEM0_URL}/v1/open_questions", params=params, headers=_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def open_question_search(query: str, brand: str | None = None, status: str = "open", limit: int = 10) -> list[dict]:
    """FTS5 keyword search across open questions. status='all' to include resolved.

    Use for 'what did we ask about X across all sessions?'."""
    body: dict = {"query": query, "limit": limit}
    if brand:
        body["brand"] = brand
    if status:
        body["status"] = status
    r = httpx.post(f"{MEM0_URL}/v1/open_questions/search", json=body, headers=_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def open_question_resolve(open_question_id: int, resolution_text: str,
                          resolved_in_session_id: str, actor: str = "claude-autonomous") -> dict:
    """Mark a frontier question as resolved with a resolution summary.

    Use when the answer to a previously-open question becomes clear in the current session."""
    body = {
        "resolved_in_session_id": resolved_in_session_id,
        "resolution_text": resolution_text,
        "actor": actor,
    }
    r = httpx.patch(f"{MEM0_URL}/v1/open_questions/{open_question_id}/resolve",
                    json=body, headers=_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


@mcp.tool
def open_question_details(open_question_id: int) -> dict:
    """Full detail on a frontier question including related goal title."""
    r = httpx.get(f"{MEM0_URL}/v1/open_questions/{open_question_id}", headers=_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    # show_banner=False is CRITICAL — fastmcp 3.x prints an ANSI banner to STDOUT by default,
    # which corrupts the MCP JSON-RPC stdio protocol and causes client timeouts.
    mcp.run(show_banner=False)
