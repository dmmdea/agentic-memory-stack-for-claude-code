#!/usr/bin/env python3
"""mem0 MCP shim — exposes our local mem0 REST server (:18791) as a proper MCP server.
Stdio transport (what Claude Code expects for stdio-type MCP entries).
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
import httpx
from fastmcp import FastMCP

KEY_FILE = Path.home() / ".mem0" / "api-key"
# Per-host authority address, same machine-local file pattern as the API key above.
# WHY A FILE: this shim is launched as `wsl.exe -d <distro> -e <python> <shim>`, which execs the
# binary directly — no login shell (no profile sourced) and no WSLENV pass-through. Any MEM0_URL
# set on the Windows side is therefore NOT visible here, so a replica box silently fell back to
# loopback, found no local server, and every op returned OfflineError/QUEUED_OFFLINE. Reading the
# authority from disk decouples it from the launch command and from env pass-through entirely.
AUTHORITY_FILE = Path.home() / ".mem0" / "authority-url"

def _resolve_authority() -> str:
    """MEM0_URL env (ad-hoc override) > ~/.mem0/authority-url (durable, per-host) > loopback."""
    env = (os.environ.get("MEM0_URL") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        for line in AUTHORITY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line.rstrip("/")
    except OSError:
        pass
    return "http://127.0.0.1:18791"

MEM0_URL = _resolve_authority()
if not KEY_FILE.exists():
    raise SystemExit(f"FAIL: mem0 API key not found at {KEY_FILE}")
MEM0_KEY = KEY_FILE.read_text(encoding="utf-8").strip()

import json as _json
import uuid as _uuid
import datetime as _dt

AUTHORITY_URL = MEM0_URL                       # resolved above: env > authority-url file > loopback
LOCAL_URL = "http://127.0.0.1:18791"           # dormant local replica, up only when offline
OUTBOX = Path.home() / ".mem0" / "outbox.jsonl"
_CONNECT_TIMEOUT = 1.5
_READ_TIMEOUT = 30.0
# Connect-level failures = fail over. A read timeout or HTTP status is NOT a connect failure.
_FAILOVER_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# 2026-07-26: the ONE HTTP status that is not a real answer. app.py returns 503 +
# Retry-After when the upstream embedder is rate-limited (llama-swap 429 outliving
# the embedder's bounded retry) — the server explicitly saying "ask again later".
# Before this, that arrived as a flat 500, which this shim correctly treats as a
# real answer, so a queued-offline write was instead DROPPED; a memory add was lost
# that way. Treating 503 like a connect failure routes reads to the replica and
# writes to the outbox. Every OTHER status stays a real answer and is never masked
# with a stale read — the invariant this rule exists to protect.
_RETRYABLE_STATUS = {503}

class OfflineError(Exception):
    """The memory authority is unusable: connect-unreachable, or answering 503."""

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()

def _timeout() -> "httpx.Timeout":
    return httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=_READ_TIMEOUT, pool=_CONNECT_TIMEOUT)

def _request(method: str, path: str, *, json: dict | None = None, params: dict | None = None):
    """Reads: try authority (short connect), else fail over to the local replica.
    Returns (payload, source). Raises OfflineError if BOTH are unusable (connect-
    unreachable, or answering a _RETRYABLE_STATUS).
    Propagates httpx.HTTPStatusError (a real answer is never masked with a stale read)."""
    last_exc = None
    for url, source in ((AUTHORITY_URL, "authority"), (LOCAL_URL, "local-replica")):
        try:
            r = httpx.request(method, f"{url}{path}", json=json, params=params,
                              headers=_headers(), timeout=_timeout())
        except _FAILOVER_EXC as e:
            last_exc = e
            continue
        if r.status_code in _RETRYABLE_STATUS:
            # Not a real answer — the server told us to ask again. Fall through to
            # the replica rather than raising it at the caller as a hard failure.
            last_exc = httpx.HTTPStatusError(
                f"{source} returned {r.status_code}", request=r.request, response=r)
            continue
        r.raise_for_status()
        return r.json(), source
    raise OfflineError(f"authority and local replica both unreachable: {last_exc}")

def _authority_only(method: str, path: str, *, json: dict | None = None) -> dict:
    """Writes/mutations: authority only. Raise OfflineError on connect failure, or on a
    _RETRYABLE_STATUS answer, so the caller queues to the outbox instead of losing the
    write (a 503 means the authority cannot serve right now, not that the op is bad)."""
    try:
        r = httpx.request(method, f"{AUTHORITY_URL}{path}", json=json,
                          headers=_headers(), timeout=_timeout())
    except _FAILOVER_EXC:
        raise OfflineError()
    if r.status_code in _RETRYABLE_STATUS:
        raise OfflineError(f"authority answered {r.status_code} (retryable); queueing")
    r.raise_for_status()
    return r.json()

def _queue_op(op: str, args: dict) -> dict:
    key = str(_uuid.uuid4())
    rec = {"op": op, "args": args, "queued_ts": _now_iso(), "key": key}
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(rec) + "\n")
    return {"queued": True, "op": op, "event": "QUEUED_OFFLINE", "key": key}

def _pending_adds(query: str) -> list[dict]:
    """Outbox adds whose text matches `query` (case-insensitive substring),
    shaped for merging into offline read results with a pending_sync marker."""
    if not OUTBOX.exists():
        return []
    q = (query or "").lower()
    out = []
    for line in OUTBOX.read_text(encoding="utf-8").splitlines():
        try:
            rec = _json.loads(line)
        except Exception:
            continue
        if rec.get("op") != "add":
            continue
        text = (rec.get("args") or {}).get("text", "")
        if q in text.lower():
            out.append({"memory": text, "pending_sync": True, "queued_ts": rec.get("queued_ts")})
    return out

def _pending_op_count() -> int:
    """Number of queued (non-empty) outbox lines awaiting sync."""
    if not OUTBOX.exists():
        return 0
    return sum(1 for line in OUTBOX.read_text(encoding="utf-8").splitlines() if line.strip())

# MEM-19 (2026-07-03): stamp the hook contract version on the shim's POSTs so
# /health/deep checks.hook_contract.missing stays 0 for MCP traffic (the shim
# was the last field-less high-traffic caller). Values MUST stay inside
# hook_contract.py's KNOWN_HOOK_CONTRACT_VERSIONS and are extended in the SAME
# commit that bumps a wire contract (v0.19 M15 rule — no pre-whitelisting):
#   '17.0' = the search wire contract on /v1/memories/search — the shim's
#            search POSTs speak this wire (pre-tool-check.ps1 was its other
#            speaker until AMS-16 retired that hook, 2026-08-09),
#   '20.0' = the batched /v1/context/bundle contract (user-prompt-extract.ps1).
# Parity-pinned against hook_contract.py by tests/test_mcp_shim_contract.py.
SEARCH_HOOK_CONTRACT_VERSION = "17.0"
BUNDLE_HOOK_CONTRACT_VERSION = "20.0"

# 2026-08-24 oversize ADVISORY line (= l10-audit OVERSIZE_CHARS). Direct session
# saves over this length are the l10 audit's oversize class - measured as 100% of
# the flag backlog's steady inflow (~10/day). The server DELIBERATELY accepts up
# to 4000 chars (the v0.22 cap raise exists because 1500 rejected legitimate
# milestone records), and an auto-split here would silently corrupt deliberate
# authored records and break the add->promote first-id contract - so this is
# advisory ONLY: the record is stored intact and the WRITER (who knows the
# semantics) is told to split when the record bundles several claims.
OVERSIZE_ADVISORY_CHARS = 1200

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
    # MEM-19: stamped on the add POST too. AddIn doesn't validate the field yet
    # (pydantic ignores extras), so this is forward-stamping: the day the add
    # contract is versioned server-side, the shim is already compliant.
    payload = {"messages": text, "user_id": user_id, "infer": infer, "metadata": md,
               "hook_contract_version": SEARCH_HOOK_CONTRACT_VERSION}
    try:
        result = _authority_only("POST", "/v1/memories", json=payload)
    except OfflineError:
        return _queue_op("add", {"text": text, "user_id": user_id, "infer": infer, "metadata": md})
    if (not infer) and isinstance(text, str) and len(text) > OVERSIZE_ADVISORY_CHARS:
        adv = (f"stored INTACT ({len(text)} chars) - above the l10-audit oversize advisory "
               f"line ({OVERSIZE_ADVISORY_CHARS}). If this bundles several claims, split it "
               "into atomic facts (<=~700 chars each) and re-save; one embedding per topic "
               "retrieves better. A single coherent record (a milestone, a causal finding) "
               "is fine as-is - the flag is advisory, never a failure.")
        note = f"{note} | {adv}" if note else adv
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
    # MEM-19: version-stamp the search POST (was the last field-less caller
    # inflating /health/deep hook_contract.missing).
    payload = {"query": query, "filters": filters, "limit": limit, "threshold": threshold, "rerank": eff_rerank, "query_class": query_class,
               "hook_contract_version": SEARCH_HOOK_CONTRACT_VERSION}
    data, source = _request("POST", "/v1/memories/search", json=payload)
    if source == "local-replica":
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
        # Offline reads are stale by definition — surface queued-but-unsynced adds
        # so a fact written minutes ago offline is still findable.
        data.setdefault("results", []).extend(_pending_adds(query))
    # MEM-8 (2026-07-03): a brandless search fail-closes on brand-scoped records
    # (v0.19 M4) — correct isolation, but the caller only saw a thin/empty result
    # and no reason (retrieval starvation, invisible). The server now counts the
    # brand_scope_required hides per call (rejected_brand_scoped); surface a hint
    # so the caller knows the fix is scoping, not absence of memory.
    n_hidden = data.get("rejected_brand_scoped") or 0
    if n_hidden:
        data["hint"] = (f"{n_hidden} brand-scoped records were hidden — "
                        "pass brand= or use memory_recall")
    # W5 T1.2 (ADOPT-2): reranker degradation was invisible to callers — a
    # dense-only fallback silently drops the lexical (exact-token) rescue.
    # New key; the brand hint string above is byte-pinned, never reworded.
    if data.get("rerank_status") == "failed_fallback_dense":
        data["rerank_note"] = ("reranker unavailable — dense-only order; "
                               "lexical exact-token rescue dropped fail-closed")
    # W5 T2.2 (ADOPT-3): withheld-result families — a search that silently
    # drops a superseded/contradicted record is failure-shaped-as-nothing.
    _n_sup = data.get("rejected_superseded") or 0
    _n_con = data.get("rejected_contradicted") or 0
    if _n_sup or _n_con:
        data["withheld_note"] = (
            f"{_n_sup} superseded and {_n_con} contradicting-canonical result(s) "
            "withheld — use query_class='history' for forensics")
    return data

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
    out: dict = {"ok": True}
    # 1) the per-prompt bundle (durable memories + open goals + open questions), checkpoint
    #    suppressed so a manual pull writes no episode. Reuses the EXACT _search_core gate path.
    #    MEM-19: stamped with the bundle contract version ('20.0' — the batched
    #    /v1/context/bundle wire user-prompt-extract.ps1 speaks).
    bpayload: dict = {"session_id": "mcp-memory-recall", "prompt": query, "checkpoint": False,
                      "hook_contract_version": BUNDLE_HOOK_CONTRACT_VERSION}
    if brand:
        bpayload["brand"] = brand
    if initiative:
        bpayload["initiative"] = initiative
    if project:
        bpayload["project"] = project
    try:
        b, _bsource = _request("POST", "/v1/context/bundle", json=bpayload)
        out["memories"] = b.get("memories", [])
        out["goals"] = b.get("goals", [])
        out["open_questions"] = b.get("open_questions", [])
        # W5 T2.2 (ADOPT-3): withheld-family note from the bundle's forwarded
        # counters (older servers don't forward them — .get() fails open).
        _n_sup = int(b.get("rejected_superseded") or 0)
        _n_con = int(b.get("rejected_contradicted") or 0)
        if _n_sup or _n_con:
            out["withheld_note"] = (
                f"{_n_sup} superseded and {_n_con} contradicting-canonical "
                "result(s) withheld — use memory_search query_class='history' "
                "for forensics")
        # W5 T2.2: oldest/newest age summary — BUNDLE memories only (the
        # canonical leg ages well by design and would make every recall read
        # 'old'; offline _pending_adds carry no created_at and are skipped).
        _ages = []
        _now = _dt.datetime.now(_dt.timezone.utc)
        for _m in out["memories"]:
            _c = (_m.get("metadata") or {}).get("created_at") or _m.get("created_at")
            if not _c:
                continue
            try:
                _cd = _dt.datetime.fromisoformat(str(_c).replace("Z", "+00:00"))
                _ages.append(max(0.0, (_now - _cd).total_seconds() / 86400.0))
            except (ValueError, TypeError):
                pass
        if _ages:
            out["age_summary"] = {"newest_days": round(min(_ages), 1),
                                  "oldest_days": round(max(_ages), 1)}
        if _bsource == "local-replica":
            # Stale replica answered (authority unreachable) — mark the bundle as replica-served
            # (same key/value convention as memory_search) and merge queued-but-unsynced adds.
            out["source"] = "local-replica"
            out["memories"].extend(_pending_adds(query))
            out["pending_ops"] = _pending_op_count()
    except OfflineError:
        out["memories"], out["goals"], out["open_questions"] = [], [], []
        out["offline"] = True
        out["memories"].extend(_pending_adds(query))
        out["pending_ops"] = _pending_op_count()
    except Exception as e:
        out["memories"], out["goals"], out["open_questions"] = [], [], []
        out["bundle_error"] = str(e)
    # 2) query-relevant canonical ground-truth — the default class excludes tier=canonical,
    #    so a recall would otherwise miss the locked facts (ports/decisions) most worth pulling.
    cfilters: dict = {"user_id": user_id}
    if brand:
        cfilters["brand"] = brand
    try:
        # MEM-19: the canonical pull is a plain search POST — same '17.0' stamp.
        c, _csource = _request("POST", "/v1/memories/search", json={"query": query, "filters": cfilters, "limit": 5, "threshold": 0.0, "rerank": False, "query_class": "canonical", "hook_contract_version": SEARCH_HOOK_CONTRACT_VERSION})
        out["canonical"] = c.get("results", [])
    except OfflineError:
        out["canonical"] = []
        out["offline"] = True
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
    data, source = _request("GET", "/v1/memories", params={"user_id": user_id, "limit": limit})
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data

@mcp.tool
def memory_delete(memory_id: str) -> dict:
    """Delete a memory by its ID."""
    try:
        return _authority_only("DELETE", f"/v1/memories/{memory_id}")
    except OfflineError:
        return _queue_op("delete", {"memory_id": memory_id})

@mcp.tool
def memory_update(memory_id: str, text: str) -> dict:
    """Update a memory's text content."""
    try:
        return _authority_only("PUT", f"/v1/memories/{memory_id}", json={"text": text})
    except OfflineError:
        return _queue_op("update", {"memory_id": memory_id, "text": text})

@mcp.tool
def memory_promote(memory_id: str, tier: str = "stable", reason: str | None = None) -> dict:
    """Promote a memory's trust tier. tier in {evidence, stable, insight, temporal}.

    NOTE: 'canonical' is NOT allowed via MCP — it requires user-direct CLI authentication.
    To canonize a memory, ask the operator to run:
      bash mem0-canonize.sh <memory_id> "<reason>"

    actor is always 'claude-autonomous' when called via MCP (server-enforced).
    'insight' is reserved for c1-consolidator/dream-consolidator (server-enforced; will 403).

    reason is recommended for audit clarity and REQUIRED for canonical (n/a here).

    Writes a tier-ledger entry (~/.mem0/tier-ledger-YYYY-MM.jsonl current-month
    segment; MEM-16 monthly rotation) on success."""
    if tier == "canonical":
        raise ValueError(
            "canonical promotion requires user-direct CLI authentication. "
            "Ask the operator to run: bash mem0-canonize.sh " + memory_id + " \"<reason>\""
        )
    payload = {"tier": tier, "actor": "claude-autonomous", "reason": reason}
    try:
        return _authority_only("PATCH", f"/v1/memories/{memory_id}/tier", json=payload)
    except OfflineError:
        return _queue_op("promote", {"memory_id": memory_id, "tier": tier, "reason": reason})

@mcp.tool
def memory_demote(memory_id: str, tier: str = "evidence", reason: str | None = None) -> dict:
    """Demote a memory's trust tier (e.g., stable -> evidence when wrong).
    For full removal use memory_delete. Writes a tier-ledger entry.
    actor is always 'claude-autonomous' when called via MCP.
    reason is recommended for audit clarity."""
    payload = {"tier": tier, "actor": "claude-autonomous", "reason": reason}
    try:
        return _authority_only("PATCH", f"/v1/memories/{memory_id}/tier", json=payload)
    except OfflineError:
        return _queue_op("demote", {"memory_id": memory_id, "tier": tier, "reason": reason})

@mcp.tool
def memory_health() -> dict:
    """Check mem0 server health: store, embedder, and canonical-key end to end."""
    # 2026-07-26: was GET /health, which returns a STATIC dict — it never touches
    # Qdrant or the embedder, so it answered ok=True while the write path was
    # broken. A live 429 burst on the embedder had memory_add returning 500 for an
    # extended period and this tool reported healthy throughout, which is exactly
    # why the outage stayed invisible. /health/deep actually embeds (asserts the
    # 768-dim vector comes back), checks Qdrant, and reports the canonical key.
    #
    # This is a diagnostic tool an agent calls deliberately, not a liveness probe,
    # so /health/deep's extra cost is the right trade. The genuinely hot/liveness
    # callers stay on shallow /health on purpose: storage-cap-check.sh (prompt hot
    # path, 1s timeout, fail-open) and install/1-wsl-services.sh (port-up check).
    data, source = _request("GET", "/health/deep")
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


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
    data, source = _request("POST", "/v1/episodes/search", json=payload)
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


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
    data, source = _request("GET", "/v1/episodes", params=params)
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


@mcp.tool
def episodic_get(episode_id: int) -> dict:
    """Fetch full detail for one episode by its integer id.

    Returns all episode fields plus linked_memories: a list of mem0 memory IDs
    (cross-references) that were produced or cited in that session.

    Use this after episodic_search or episodic_recent to drill into a specific
    episode and see which mem0 facts were created/cited during that session.

    episode_id: integer id from episodic_search or episodic_recent results.
    """
    data, source = _request("GET", f"/v1/episodes/{episode_id}")
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


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
    data, source = _request("GET", "/v1/goals", params=params)
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


@mcp.tool
def goals_tree(root_id: int | None = None) -> list[dict]:
    """Recursive goal tree (adjacency-list hierarchy). If root_id is None, returns all top-level goals + descendants with depth field.
    Use for 'show me the goal hierarchy' / 'what's the structure of my plans?'."""
    params: dict = {}
    if root_id is not None:
        params["root_id"] = root_id
    data, source = _request("GET", "/v1/goals/tree", params=params)
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


@mcp.tool
def goals_open(brand: str | None = None, limit: int = 10) -> list[dict]:
    """Convenience: list open goals. Use for 'what am I currently working on?'."""
    params: dict = {"status": "open", "limit": limit}
    if brand:
        params["brand"] = brand
    data, source = _request("GET", "/v1/goals", params=params)
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


@mcp.tool
def goals_blocked(brand: str | None = None, limit: int = 10) -> list[dict]:
    """Convenience: list blocked goals. Use for 'what's stuck right now?' / 'show me blockers'."""
    params: dict = {"status": "blocked", "limit": limit}
    if brand:
        params["brand"] = brand
    data, source = _request("GET", "/v1/goals", params=params)
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


@mcp.tool
def goal_details(goal_id: int) -> dict:
    """Full goal detail including linked_episode_count and parent reference.
    Use to investigate a specific goal."""
    data, source = _request("GET", f"/v1/goals/{goal_id}")
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


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
    try:
        return _authority_only("POST", "/v1/goals", json=body)
    except OfflineError:
        return _queue_op("goal_create_manual", body)


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
    try:
        return _authority_only("PATCH", f"/v1/goals/{goal_id}/abandon", json=body)
    except OfflineError:
        return _queue_op("goal_abandon", {"goal_id": goal_id, "reason": reason, "actor": actor})


@mcp.tool
def goal_complete(goal_id: int, reason: str, actor: str = "claude-autonomous") -> dict:
    """Mark a goal as completed — the work shipped / the goal was achieved.
    Distinct from 'abandon' (which means scope dropped or the goal became infeasible).
    Use this when a goal's deliverable is actually done so it stops bleeding into every
    session's surfaced context. Sets completed_at and stamps a goal-completed ledger entry.
    Requires a non-empty reason — document what shipped. (v0.22 Phase A, resolves OQ#636.)"""
    body = {"actor": actor, "reason": reason}
    try:
        return _authority_only("PATCH", f"/v1/goals/{goal_id}/complete", json=body)
    except OfflineError:
        return _queue_op("goal_complete", {"goal_id": goal_id, "reason": reason, "actor": actor})


# ---------------------------------------------------------------------------
# v0.17 Phase F.3.1: memory_get_by_id — exact read before update/delete/promote
# ---------------------------------------------------------------------------

@mcp.tool
def memory_get_by_id(memory_id: str) -> dict:
    """Exact read by memory_id. Returns text, metadata, tier, retrievable, source, timestamps.

    Use BEFORE memory_update, memory_delete, memory_promote — search/list are not substitutes
    for exact reads. Avoids wrong-ID edits caused by search rediscovering similar records.

    Pattern: call memory_get_by_id first to confirm the correct record, THEN act on it."""
    data, source = _request("GET", f"/v1/memories/{memory_id}")
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


@mcp.tool
def memory_diagnose(query: str, target_id: str, user_id: str = "__WSL_USER__",
                    brand: str | None = None, query_class: str = "durable",
                    threshold: float = 0.1, limit: int = 20,
                    rerank: bool = False, allow_cross_brand: bool = False) -> dict:
    """W5 (ADOPT-2): why does memory `target_id` not surface for `query`?

    Replays each retrieval layer INDEPENDENTLY server-side — dense rank (at
    the overfetch pool and at 500), threshold, retired/canonical-intent flags,
    the PURE admission verdict (no counters mutated), rerank delta, trim —
    and returns a verdict naming the FIRST stage that eats the target.

    Set threshold/limit/rerank/query_class to the FAILING search's values —
    the defaults mirror the server search defaults, not your caller's (a
    threshold-0.55 hook query diagnosed at 0.1 names the wrong stage).
    Read-only; safe to run against production."""
    body: dict = {"query": query, "target_id": target_id, "user_id": user_id,
                  "query_class": query_class, "threshold": threshold,
                  "limit": limit, "rerank": rerank}
    if brand:
        body["brand"] = brand
    if allow_cross_brand:
        body["allow_cross_brand"] = True
    data, source = _request("POST", "/v1/memories/diagnose", json=body)
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


# ---------------------------------------------------------------------------
# v0.17 Phase F.3.2: goal management — priority, link_episode, merge
# ---------------------------------------------------------------------------

@mcp.tool
def goal_set_priority(goal_id: int, priority: int, reason: str | None = None,
                      actor: str = "claude-autonomous") -> dict:
    """Update a goal's priority (1=highest, 5=lowest). v0.17 F.3.2.
    Use when a goal's urgency changes relative to other open goals."""
    body = {"priority": priority, "actor": actor, "reason": reason}
    try:
        return _authority_only("PATCH", f"/v1/goals/{goal_id}/priority", json=body)
    except OfflineError:
        return _queue_op("goal_set_priority", {"goal_id": goal_id, "priority": priority,
                                               "reason": reason, "actor": actor})


@mcp.tool
def goal_link_episode(goal_id: int, episode_id: int, link_type: str = "advanced_goal",
                      delta_text: str | None = None, actor: str = "claude-autonomous") -> dict:
    """Explicitly link an episode to a goal.
    link_type ∈ {advanced_goal, blocked_goal, completed_goal, cited_goal}.
    Use when a session advanced a goal but auto-extraction from the Stop hook missed it.
    v0.17 F.3.2."""
    body = {"episode_id": episode_id, "link_type": link_type, "delta_text": delta_text, "actor": actor}
    try:
        return _authority_only("POST", f"/v1/goals/{goal_id}/link_episode", json=body)
    except OfflineError:
        return _queue_op("goal_link_episode", {"goal_id": goal_id, "episode_id": episode_id,
                                               "link_type": link_type, "delta_text": delta_text,
                                               "actor": actor})


@mcp.tool
def goal_merge(source_goal_id: int, target_goal_id: int, reason: str,
               actor: str = "claude-autonomous") -> dict:
    """Merge source goal into target: move all episode links + mark source as 'duplicate'.
    Use when two goals turn out to be the same thing (duplicate created by auto-extraction).
    Source stays in DB for audit but is excluded from default listings. v0.17 F.3.2."""
    body = {"target_goal_id": target_goal_id, "actor": actor, "reason": reason}
    try:
        return _authority_only("POST", f"/v1/goals/{source_goal_id}/merge", json=body)
    except OfflineError:
        return _queue_op("goal_merge", {"source_goal_id": source_goal_id,
                                        "target_goal_id": target_goal_id,
                                        "reason": reason, "actor": actor})


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
    data, source = _request("GET", "/v1/open_questions", params=params)
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


@mcp.tool
def open_question_search(query: str, brand: str | None = None, status: str = "open", limit: int = 10) -> list[dict]:
    """FTS5 keyword search across open questions. status='all' to include resolved.

    Use for 'what did we ask about X across all sessions?'."""
    body: dict = {"query": query, "limit": limit}
    if brand:
        body["brand"] = brand
    if status:
        body["status"] = status
    data, source = _request("POST", "/v1/open_questions/search", json=body)
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


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
    try:
        return _authority_only("PATCH", f"/v1/open_questions/{open_question_id}/resolve", json=body)
    except OfflineError:
        return _queue_op("open_question_resolve", {"open_question_id": open_question_id,
                                                   "resolution_text": resolution_text,
                                                   "resolved_in_session_id": resolved_in_session_id,
                                                   "actor": actor})


@mcp.tool
def open_question_details(open_question_id: int) -> dict:
    """Full detail on a frontier question including related goal title."""
    data, source = _request("GET", f"/v1/open_questions/{open_question_id}")
    if source == "local-replica" and isinstance(data, dict):
        data["source"] = "local-replica"
        data["stale_note"] = "served from local replica; authority unreachable"
    return data


def _drain_outbox_async() -> None:
    """Fire-and-forget outbox drain at startup. Fail-open and stdio-safe.

    WHY: a QUEUED_OFFLINE write used to sit in the outbox indefinitely — the only drain trigger
    was the offline-watcher's offline->online transition, which never fires when the authority was
    reachable the whole time and only this shim was pointed at the wrong address. Session start is
    the natural unconditional retry point. Delegates to replay-ops.py (adds-first, idempotent via
    its key ledger) rather than reimplementing replay here.

    STDIO SAFETY: the child's stdout/stderr go to a log file, NEVER to our stdout — that is the
    MCP JSON-RPC channel and any stray byte on it breaks the protocol.
    """
    try:
        if not OUTBOX.exists() or OUTBOX.stat().st_size == 0:
            return
        drainer = Path(__file__).resolve().parent / "replay-ops.py"
        if not drainer.exists():
            return
        log = Path.home() / ".mem0" / "outbox-drain.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            subprocess.Popen(
                [sys.executable, str(drainer), "--authority", AUTHORITY_URL],
                stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, start_new_session=True,
            )
    except Exception:
        pass  # a drain attempt must never delay or break the MCP server


# W4 (review F13): the shim's own release stamp. This is a PINNED LITERAL, not a
# lookup, and that is the point: the deployed copies live in ~/apps/mem0-scripts and
# the Windows %USERPROFILE%\.claude\scripts, neither of which has a VERSION file, and
# resolving the SERVER's VERSION would always agree with the server and detect nothing.
# The launch path (the Windows copy) is refreshed only by install/2-windows-config.ps1,
# never by deploy.sh — that gap is how two shipped fixes stayed dead for 12 days
# (AMS-02). A stale copy carries a stale literal here, the server compares it to its own
# STACK_VERSION, and the mcp-shim manifest row goes 'degraded'.
# BUMPED WITH THE REPO VERSION — mem0-server/tests/test_capabilities.py pins the two.
SHIM_STACK_VERSION = "1.20.6"


def _write_start_receipt() -> None:
    """Host-keyed start receipt for the mcp-shim capability row. Fail-open.

    The shim is spawned per session over stdio, so "this file was written recently"
    is a genuine per-session liveness signal — the only one this component has.

    HOST-KEYED because ~/.mem0 travels in the stack backups: a receipt restored from
    another box must not read as local liveness, so the reader (job_liveness.py)
    compares hostnames and treats a foreign receipt as absent. The manifest row is
    scoped to 'brain' for the same family of reasons: a replica's shim writes to the
    REPLICA's ~/.mem0, whose server may not even be running to read it.

    STDIO SAFETY: writes to a FILE and nothing else. stdout is the MCP JSON-RPC
    channel — a single stray byte there breaks the protocol (the same reason
    mcp.run is called with show_banner=False below), so this function must never
    print, and any failure is swallowed rather than reported."""
    try:
        import socket
        rcpt = Path.home() / ".mem0" / "mcp-shim-receipt.json"
        rcpt.parent.mkdir(parents=True, exist_ok=True)
        rcpt.write_text(_json.dumps({
            "ts": int(_dt.datetime.now(_dt.timezone.utc).timestamp()),
            "host": socket.gethostname(),
            "stack_version": SHIM_STACK_VERSION,
            "authority": AUTHORITY_URL,
            "pid": os.getpid(),
        }), encoding="utf-8")
    except Exception:
        pass  # a liveness receipt must never delay or break the MCP server


if __name__ == "__main__":
    _write_start_receipt()
    _drain_outbox_async()
    # show_banner=False is CRITICAL — fastmcp 3.x prints an ANSI banner to STDOUT by default,
    # which corrupts the MCP JSON-RPC stdio protocol and causes client timeouts.
    mcp.run(show_banner=False)
