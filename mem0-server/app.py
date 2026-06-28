"""mem0 v2.0.4 WSL-native FastAPI wrapper — v0.12 stack
Loopback only on 127.0.0.1:18791. X-API-Key auth.
Exposes same REST surface as official mem0 server (POST/GET/PUT/DELETE /v1/memories, POST /v1/memories/search).
"""
import os
import hmac
import json
import logging
import datetime as _dt
from pathlib import Path
from typing import Optional, Any, List

from fastapi import FastAPI, Header, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field
from mem0 import Memory

from config import build_config
from reranker import rerank as bge_rerank
from admission_gate import apply_admission
from redact import redact_secrets  # server-side secret scrub for stored prompt_text
from freshness import freshness_weight as _freshness_weight  # v1.0 R5 Weibull read-gate
import codex_shim_client  # v0.27.1 R5 keystone: Codex judgment via the Windows HTTP shim
import nli_write_gate     # v0.27.2 R5: NLI write-gate decision (pure; deps injected below)
from episodic import (
    _connect as _episodic_connect,
    init_schema as _episodic_init_schema,
    create_session as _episodic_create_session,
    add_episode as _episodic_add_episode,
    add_link as _episodic_add_link,
    search_fts as _episodic_search_fts,
    recent as _episodic_recent,
    get_episode as _episodic_get,
    count_episodes as _episodic_count,
    # v0.16 goals
    create_goal as _episodic_create_goal,
    find_goal_by_title_fuzzy as _episodic_find_goal_by_title_fuzzy,
    link_episode_to_goal as _episodic_link_episode_to_goal,
    update_goal_status as _episodic_update_goal_status,
    get_goal as _episodic_get_goal,
    list_goals as _episodic_list_goals,
    get_goal_tree as _episodic_get_goal_tree,
    # v0.17 Phase 0 — within-session checkpoint
    upsert_in_progress_episode as _episodic_upsert_checkpoint,
    finalize_episode as _episodic_finalize_episode,
    # v0.17 Phase D — open questions
    create_open_question as _episodic_create_open_question,
    get_open_question as _episodic_get_open_question,
    resolve_open_question as _episodic_resolve_open_question,
    update_open_question_status as _episodic_update_open_question_status,
    list_open_questions as _episodic_list_open_questions,
    search_open_questions as _episodic_search_open_questions,
    find_open_question_by_text_fuzzy as _episodic_find_open_question_by_text_fuzzy,
)
# v0.29 R4 — semantic raw-trace gate (episode-summary embeddings in Qdrant)
from episode_embeddings import (
    EPISODE_COLLECTION,
    ensure_episode_collection,
    embed_episode_summary,
    upsert_episode_embedding,
    search_episodes_semantic,
    _indexable_summary as _episode_indexable_summary,
)

# Read API key (file mode 600)
API_KEY_PATH = Path.home() / ".mem0" / "api-key"
if not API_KEY_PATH.exists():
    raise SystemExit(f"FAIL: API key not found at {API_KEY_PATH}. Run: python -c \"import secrets; print(secrets.token_urlsafe(32))\" > {API_KEY_PATH} && chmod 600 {API_KEY_PATH}")
API_KEY = API_KEY_PATH.read_text(encoding="utf-8").strip()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mem0-server")

# v0.18 Phase A: canonical-key loaded via provider (DPAPI on Windows, plaintext fallback on WSL)
# v0.19 Phase H adds the runtime tmpfs source; v0.20 Phase D (M6) adds source tracking + health.
from canonical_key_provider import CanonicalKeyProvider as _CKProvider
from canonical_key_provider import canonical_key_health as _canonical_key_health
_APP_KEY_PROVIDER = _CKProvider()

def _get_app_canonical_key():
    return _APP_KEY_PROVIDER.get_key()

# Eager probe at startup so log message still appears
# (v0.20 Phase D L1 belt-and-suspenders: truthiness, not is-not-None — the
# provider never serves '' anymore, but '' must read as keyless here too)
_ck_probe = _get_app_canonical_key()
if _ck_probe:
    log.info("canonical-key loaded via provider (source=%s); user-direct HMAC enforcement ACTIVE",
             _APP_KEY_PROVIDER.key_source)
else:
    log.warning(
        "canonical-key not found (checked runtime tmpfs + DPAPI + plaintext); canonical "
        "promotions will be REJECTED. If ~/.mem0/canonical-key.dpapi exists this is the "
        "keyless-degraded state: restart mem0 to re-run dpapi-fetch-key.sh (ExecStartPre) "
        "or follow docs/modular/dpapi-canonical-key.md Recovery"
    )

# v1.0 R7 (Phase 7A, recon defect B2): operator-agnostic default tenant.
# The /v1/context/bundle proactive-search default user_id MUST NOT be a hardcoded
# developer handle — a third-party install would query the wrong tenant and the
# [MEMORY CONTEXT] injection would surface nothing. The systemd unit sets
# MEM0_DEFAULT_USER_ID to the install user (installer substitutes __WSL_USER__);
# the fallback is neutral so a dev/test run without the env still functions and
# NO personal handle ships in the source.
DEFAULT_USER_ID = os.environ.get("MEM0_DEFAULT_USER_ID") or "default"

# v0.20 Phase G: CANONICAL_TOKEN_MAX_SKEW_S moved out with the inline format-1
# gate — the skew tolerance now lives solely in security_invariants (the
# central validator handles every canonical/insight HMAC, promote included).

# v0.18 MED-10: normalize perms on pre-existing log files. New files are created
# 0600 via _secure_open / security_invariants; pre-v0.18 files (and
# recent-decisions.jsonl, which is written by the Windows-side UserPromptSubmit
# hook over UNC and has no Python write site) are normalized once at startup.
for _log_name in ("retrieval-log.jsonl", "recent-decisions.jsonl", "canonical-replay.jsonl"):
    _log_file = Path.home() / ".mem0" / _log_name
    try:
        if _log_file.exists():
            os.chmod(_log_file, 0o600)
    except OSError:
        log.warning("MED-10: could not chmod 600 on %s", _log_file)

# v0.15: episodic.db schema init (idempotent — safe to run every startup)
try:
    with _episodic_connect() as _conn:
        _episodic_init_schema(_conn)
    log.info("episodic.db schema initialized")
except Exception:
    log.exception("episodic.db init failed (write/search will degrade gracefully)")

mem = Memory.from_config(build_config())
# v0.22 EmbeddingGemma migration: install the asymmetric prefix-shim embedder.
# build_config() declares provider=openai only to pass mem0's schema validation;
# the real embedder must prepend EmbeddingGemma's query/document task prefixes, which
# mem0's stock OpenAI embedder won't do. Swapping the attribute is the cleanest wiring
# (mem0 2.0.4's EmbedderConfig pydantic allowlist rejects a custom provider name).
from config import build_embedder
mem.embedding_model = build_embedder()
log.info("mem0 initialized (embedder: EmbeddingGemma-300m prefix-shim, collection: mem0_egemma_768)")

# v0.29 R4: ensure the semantic episode collection exists (idempotent). Non-fatal
# — if it fails, the raw-trace fallback search simply no-ops (fail-soft).
try:
    _ep_created = ensure_episode_collection(mem.vector_store.client)
    log.info("%s collection %s", EPISODE_COLLECTION, "created" if _ep_created else "present")
except Exception:
    log.exception("ensure_episode_collection failed (non-fatal; raw-fallback search will no-op)")

app = FastAPI(title="mem0 WSL", version="2.0.4-v012")

def auth(x_api_key: Optional[str] = Header(None)):
    if not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "missing or invalid X-API-Key")

class AddIn(BaseModel):
    messages: Any   # str | list[dict] | dict
    user_id: str
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    metadata: Optional[dict] = None
    infer: bool = True

class SearchIn(BaseModel):
    query: str
    filters: dict
    limit: int = 20
    threshold: float = 0.1
    rerank: bool = False
    # v0.17 F.4.1: recency-class search policy
    # durable (default): no recency boost — tier-based ranking; preserves v0.13-v0.16 behaviour.
    # operational: post-rerank exponential-decay recency boost with 30-day half-life.
    # canonical: filter to tier ∈ {canonical, stable} only; ignore freshness.
    # history (v0.19 I.1): forensic class — same allowlist as durable but the
    #   admission gate's supersession/contradiction checks are disabled.
    query_class: Optional[str] = "durable"  # durable | operational | canonical | history
    # v0.19 M15: hook contract version stamped by the Windows hook search callers
    # (user-prompt-extract.ps1 0.D, pre-tool-check.ps1). The search contract was
    # unversioned in v0.18, so drift on the highest-traffic hook call was
    # undetectable. WARN-only, never rejected (see hook_contract.py).
    hook_contract_version: Optional[str] = None

class UpdateIn(BaseModel):
    text: str

class TierIn(BaseModel):
    tier: str  # "evidence" | "canonical" | "insight" | "temporal" | "stable"
    reason: Optional[str] = None
    actor: Optional[str] = None  # no default - audit finding 2026-06-08: hardcoded
                                  # "claude" hid autonomous vs user-direct intent

class MetadataIn(BaseModel):
    metadata: dict   # shallow merge with existing payload
    actor: Optional[str] = None   # who triggered this — logged to ledger
    reason: Optional[str] = None

# v0.16: goal sub-models (used in EpisodeIn and GoalIn)
class GoalAdvanceItem(BaseModel):
    goal_title: str
    delta_text: Optional[str] = None

class GoalBlockItem(BaseModel):
    goal_title: str
    block_reason: Optional[str] = None

# v0.15: episodic memory models
class EpisodeIn(BaseModel):
    session_id: str
    started_at: str
    ended_at: str
    transcript_path: Optional[str] = None
    goal: str
    summary: str
    message_count: Optional[int] = 0
    brand: Optional[str] = None
    workspace: Optional[str] = None
    project: Optional[str] = None
    linked_memory_ids: Optional[List[str]] = None
    # v0.16 additions
    advanced_goals: Optional[List[GoalAdvanceItem]] = None
    blocked_goals: Optional[List[GoalBlockItem]] = None
    open_questions: Optional[List[str]] = None
    # v0.22 Pillar 1: the session's cwd-derived initiative (repo leaf). Goals/OQ
    # auto-created from this episode's advanced/blocked/open_questions are stamped
    # with it so they only resurface in same-initiative (or cross-cutting) sessions.
    initiative: Optional[str] = None
    # v0.18 MED-17: hook contract version stamped by the Windows hook scripts.
    # Accepted and ignored beyond WARN-validation (see _warn_hook_contract_version).
    hook_contract_version: Optional[str] = None

# v0.16: manual goal management models
class GoalIn(BaseModel):
    title: str
    description: Optional[str] = None
    brand: Optional[str] = None
    parent_goal_id: Optional[int] = None
    priority: Optional[int] = Field(default=3, ge=1, le=5)  # MED-A: 1=highest, 5=lowest; 0 is invalid
    initiative: Optional[str] = None  # v0.22 Pillar 1: cwd-derived initiative; None == cross-cutting

class GoalStatusIn(BaseModel):
    status: str
    completed_at: Optional[str] = None
    actor: str                         # required — audit trail for goal status changes
    reason: Optional[str] = None       # optional free-text rationale

class EpisodeSearchIn(BaseModel):
    query: str
    since: Optional[str] = None
    until: Optional[str] = None
    brand: Optional[str] = None
    limit: int = 20

# v0.17 Phase 0.A: UserPromptSubmit hook checkpoint model
class EpisodeCheckpointIn(BaseModel):
    session_id: str
    transcript_path: Optional[str] = None
    prompt_text: Optional[str] = None
    brand: Optional[str] = None
    workspace: Optional[str] = None
    project: Optional[str] = None
    # v0.18 MED-17: hook contract version stamped by the Windows hook scripts.
    hook_contract_version: Optional[str] = None

# v0.18 MED-17 / v0.19 M15+M10: hook contract drift detection lives in
# hook_contract.py (side-effect-free — tests caplog-assert the WARN via direct
# import, which app.py forbids: Memory.from_config needs the live stack).
# v0.19 M15: '18.0' removed from the known set — only '17.0' is real; the set
# is extended in the same commit that bumps $HookContractVersion in the hooks.
from hook_contract import (
    KNOWN_HOOK_CONTRACT_VERSIONS as _KNOWN_HOOK_CONTRACT_VERSIONS,
    hook_contract_stats as _hook_contract_stats,
    warn_hook_contract_version as _warn_hook_contract_version,
)

# Tier policy constants (audit finding 2026-06-08: tier protocol was bypassable
# by direct memory_add with metadata.tier=canonical, and memory_promote hardcoded
# actor=claude. Server now enforces transitions; caller cannot self-elevate.)
ADD_ALLOWED_TIERS = {"evidence", "temporal"}  # POST /v1/memories
PROMOTE_ALLOWED_TIERS = {"evidence", "stable", "canonical", "insight", "temporal"}
CANONICAL_REQUIRES_USER_DIRECT = True   # actor must be "user-direct" OR in CANONICAL_AUTOPROMOTE_ALLOWED for canonical promotions
INSIGHT_REQUIRES_C1 = True              # actor must be in INSIGHT_ALLOWED_ACTORS for insight writes
# v0.14 C: exact allowlist replaces substring check ("c1" in actor) which was trivially bypassable
# (e.g. actor="not-c1" passed the old check). Only these known consolidator identities may write insight tier.
INSIGHT_ALLOWED_ACTORS = {"c1-consolidator", "dream-consolidator", "c1-dream-consolidator"}
# Phase 2 autonomous promotion: the nightly dream consolidator may autonomously promote
# to canonical under the STRICT bar (Codex-judged, cap<=3/night, canary unchanged).
# The actor label is distinct from "user-direct" so ledger entries are auditable by source.
# HMAC signing with the same canonical key is still required — the actor is a body label only.
CANONICAL_AUTOPROMOTE_ALLOWED = {"dream-autopromote"}
MAX_MEMORY_CHARS = int(os.environ.get("MEM0_MAX_MEMORY_CHARS", "4000"))  # storage cap (env-overridable)
# v0.22: raised 1500 -> 4000. The old 1500 was a policy guess ("realistic for atomic facts",
# audit 2026-06-08); its "prompt-budget honest" half is now MOOT — the v0.22 model-aware
# injection truncates each memory to ~200 chars in the rendered [MEMORY CONTEXT] block, so a
# longer STORED memory never bloats the prompt. 1500 was rejecting legitimate milestone/
# checkpoint summaries (~1.5-2.5K chars) with a 413 every session. 4000 fits them with headroom
# and stays well within EmbeddingGemma's 2048-token (~8K char) context. Atomic facts (<=25 words)
# remain the preferred default for per-record retrieval precision; this cap is just a sane backstop.

# v0.28 Phase 2a: promote-canary — reject imperative standing-order text from canonical tier.
# Pure helper extracted into imperative_canary.py for testability without the full app stack.
# Canary lexicon (v0.28): MUST | NEVER | ALWAYS | DO NOT | DON'T | SHALL | YOU MUST | RULE:
from imperative_canary import is_imperative_canonical


# v0.27.2 R5: NLI write-gate config (env). DEFAULT OFF — the write path is HOT (every L1a
# Stop-hook extraction writes), so this never engages unless explicitly enabled. When on, a
# fast canonical-tier pre-filter (a high SEMANTIC threshold) means Codex is invoked ONLY when
# a genuinely high-cosine canonical neighbor exists; any shim failure FAILS OPEN (admits).
def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")
def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return float(default)
def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return int(default)
NLI_GATE_ENABLED = _env_flag("MEM0_NLI_GATE_ENABLED")  # opt-in; default OFF
NLI_GATE_COSINE_FLOOR = _env_float("MEM0_NLI_GATE_COSINE_FLOOR", 0.5)  # SEMANTIC scale (raw cosine), NOT the hybrid score
NLI_GATE_TOPK = _env_int("MEM0_NLI_GATE_TOPK", 3)
NLI_GATE_TIMEOUT_S = _env_int("MEM0_NLI_GATE_TIMEOUT_S", 45)  # Codex low-effort NLI runs ~20-30s
# Over-fetch window for the canonical pre-filter: mem.search returns the top-N by COMBINED
# score above the SEMANTIC floor, and _search_core then post-filters that window to canonical/
# stable. A small top_k could truncate a canonical neighbor sitting below several evidence
# neighbors on the combined scale (audit MED), so fetch wide then let the tier filter narrow.
# Cheap — the gate runs ASYNC, off the hot path.
NLI_GATE_FETCH = _env_int("MEM0_NLI_GATE_FETCH", 25)
# Retrieval-gating metadata keys a caller must NOT be able to set via add() (mirrors the PATCH
# /metadata FORBIDDEN_KEYS). contradicts_canonical is the NLI gate's own security primitive;
# only the gate (server-side) or a trusted-actor PATCH may write these (audit HIGH).
_ADD_FORBIDDEN_META = {"retrievable", "expires_at", "created_at", "tier_actor",
                       "superseded_by", "contradicts_canonical", "contradiction_checked_at",
                       "nli_gate_checked_at", "contradicts_canonical_pending"}

# v0.29 R4 — raw-trace fallback (opt-in, default OFF). When the condensed
# (semantic) memory search admits nothing in context_bundle, optionally surface
# ONE compact snippet from the most SEMANTICALLY-relevant past EPISODE (NEMORI
# dual-store: atomic mem0 facts = condensed tier; episode summaries = the fuller
# raw-trace tier). The relevance gate is SEMANTIC, not lexical: a live check
# proved bm25 cannot separate off-domain-but-keyword-dense episodes from relevant
# ones. We embed the prompt + episode summaries with the SAME EmbeddingGemma
# embedder and gate on the raw cosine (RAW_FALLBACK_COSINE_FLOOR, calibrated on
# the semantic scale — see eval/injection-gating/episode_probes). It fires only
# when (a) the condensed search returned 0 (low-confidence) AND (b) an episode
# clears the cosine floor; off-domain prompts sit well below the floor, so R2
# abstention holds. Brand is fail-closed (unknown-brand session -> neutral only).
RAW_FALLBACK_ENABLED = _env_flag("MEM0_RAW_FALLBACK_ENABLED")
# Calibrated cosine floor on the SEMANTIC scale (NOT a hybrid/combined score —
# the dedicated episodes_egemma_768 Cosine collection returns the raw cosine).
# Calibrated 2026-06-15 (eval/injection-gating/calibrate_episode_floor.py against
# the live 283-episode store, measured on the production limit=10 population):
# off-domain top-1 cosine maxes at 0.142; relevant probes span 0.267-0.432.
# Balanced accuracy = 1.000 across the [0.15, 0.25] plateau. 0.20 sits in the gap
# (~0.058 above off-domain max, ~0.067 below relevant min) — rejects every
# off-domain probe (R2 abstention preserved) while admitting every relevant probe.
# (Script's ceiling-0.05 rec was 0.215; 0.20 is the round value just below it.)
RAW_FALLBACK_COSINE_FLOOR = _env_float("MEM0_RAW_FALLBACK_COSINE_FLOOR", 0.20)
RAW_FALLBACK_TOPK = _env_int("MEM0_RAW_FALLBACK_TOPK", 10)
RAW_FALLBACK_SNIPPET_CHARS = _env_int("MEM0_RAW_FALLBACK_SNIPPET_CHARS", 300)

def _episode_raw_fallback(prompt, brand):
    """v0.29 R4 — compute the low-confidence SEMANTIC raw-trace fallback, or None.

    Embeds the prompt (query prefix) + semantic-searches episodes_egemma_768,
    fail-closed on brand (unknown-brand session sees only neutral episodes —
    mirrors goals/OQ Layer-2) and gated on the cosine floor, so off-domain prompts
    (which sit well below the floor) still abstain. Snippet comes from the Qdrant
    payload (goal — summary). Never raises (fail-soft)."""
    q = (prompt or "").strip()
    if not q:
        return None
    # Normalize brand BEFORE deriving only_brand_neutral so a whitespace-only brand
    # collapses to unknown (fail-closed), mirroring _brand_admits + the rest of the
    # stack — otherwise `not "  "` is False and the gate falls through to admit-all
    # (audit MED: whitespace-brand cross-brand leak).
    _b = brand.strip() if isinstance(brand, str) else brand
    try:
        hits = search_episodes_semantic(
            mem.vector_store.client, mem.embedding_model, q,
            brand=_b, only_brand_neutral=(not _b),
            limit=RAW_FALLBACK_TOPK, floor=RAW_FALLBACK_COSINE_FLOOR,
        )
    except Exception:
        log.exception("bundle: raw-trace fallback search failed (non-fatal)")
        return None
    if not hits:
        return None
    ep_id, _score, payload = hits[0]
    goal = (payload.get("goal") or "").strip()
    summ = (payload.get("summary") or "").strip()
    snippet = (f"{goal} — {summ}" if goal and summ else (goal or summ)).strip()
    if not snippet:
        return None
    # brand is included for the hook's defense-in-depth $brandGate backstop (the
    # server already fail-closes via only_brand_neutral; the client re-checks).
    return {"episode_id": ep_id, "brand": payload.get("brand"),
            "snippet": snippet[:RAW_FALLBACK_SNIPPET_CHARS].rstrip()}

def _nli_search_fn(query, filters, threshold, topk):
    """Canonical-tier neighbor lookup for the write-gate. mem0 hybrid search gates each
    candidate on its raw SEMANTIC cosine via `threshold`; query_class='canonical' post-filters
    the fetched window to canonical/stable. Over-fetch (NLI_GATE_FETCH) so a canonical neighbor
    below several evidence neighbors on the combined scale is not silently truncated. Returns
    the canonical result list (possibly empty)."""
    res = _search_core(SearchIn(query=query, filters=filters, query_class="canonical",
                                threshold=threshold,
                                limit=max(int(NLI_GATE_FETCH), int(topk or 1)), rerank=False))
    return (res or {}).get("results") if isinstance(res, dict) else []

def _nli_judge_fn(statement_a_canonical, statement_b_new, timeout_s):
    return codex_shim_client.judge_contradiction(statement_a_canonical, statement_b_new,
                                                 timeout_s=timeout_s)

def _nli_gate_stamp(records, user_id, brand):
    """BACKGROUND (post-admit) NLI write-gate. Delegates to the unit-tested pure helper
    nli_write_gate.stamp_contradictions, wiring the real canonical search, the Codex judge, and
    the Qdrant set_payload + ledger as the stamp. Runs AFTER the HTTP response (FastAPI
    BackgroundTask) so add() never blocks on Codex. Fail-soft throughout (logs, never raises)."""
    def _stamp(mid, cid):
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        mem.vector_store.client.set_payload(
            collection_name=mem.vector_store.collection_name,
            payload={"contradicts_canonical": cid, "nli_gate_checked_at": now_iso, "updated_at": now_iso},
            points=[mid],
        )
        _append_ledger({"event": "nli-write-gate-flag", "memory_id": str(mid),
                        "contradicts_canonical": str(cid), "actor": "nli-write-gate"})
        log.warning("NLI write-gate flagged %s as contradicting canonical %s "
                    "(hidden from durable/operational search)", mid, cid)
    try:
        nli_write_gate.stamp_contradictions(
            records, user_id, brand,
            cosine_floor=NLI_GATE_COSINE_FLOOR, topk=NLI_GATE_TOPK, timeout_s=NLI_GATE_TIMEOUT_S,
            search_fn=_nli_search_fn, judge_fn=_nli_judge_fn, stamp_fn=_stamp,
        )
    except Exception:
        log.exception("NLI write-gate background pass failed")

# v0.22 Phase D / v0.23: per-tier context-bundle policy. The UserPromptSubmit hook
# resolves the consuming model's tier (frontier|small) hook-side and sends it in the
# bundle request; the server scales the bundle's memory/goal/OQ caps + the search
# relevance_threshold by tier. FRONTIER originally reproduced the post-migration
# default (5 memories / 5 goals / 3 OQ @ 0.30 — v1.0 R2 below changes memory_cap to
# 2 and KEEPS the threshold at 0.30) and v0.23 expands it to cover EVERY 1M-context
# flagship — Opus 4.6/4.7/4.8, Fable 5, AND Sonnet 4.6. (Sonnet is a 1M model, so the
# old "mid" tier that trimmed it to 4 goals had no context-budget justification and
# was removed — full portfolio by capability/window, see CHANGELOG v0.23.) SMALL
# (Haiku 4.5, 200K ctx) trims item COUNT (the primary lever — fewer tokens for the
# smaller-window model) and nudged the threshold up only to 0.33 (v0.22; v1.0 R2
# below unifies BOTH tiers at 0.30 — the abstain decision is corpus/entity-side and
# model-independent, and 0.33 over-abstains on the compressed semantic scale). The
# offload harness (non-Claude Gemma) gets NO injection at all — enforced hook-side
# (Test-OffloadNoBlockInvariant), not a tier here. Threshold/caps MUST stay in sync
# with claude-config/model-tiers.json (the client-side detection config); this is the
# server's authoritative copy, and test_tier_parity.py guards the two against drift
# (v0.23 L7). Default + any unknown/legacy tier (incl. a stale "mid" sidecar) ->
# frontier (fail-open, never under-serve).
# v1.0 Phase 3 / R2 (abstention-first, entity-side gated injection). The R2 levers
# are (a) memory_cap (K) capped at 1-2 (was 5 / 3; fewer higher-relevance memories,
# ReasoningBank k=1 > k=4) and (b) the hook's BLOCK-LEVEL abstention: when nothing
# clears the relevance gate the bundle returns zero memories and
# Format-MemoryContextBlock emits NO block at all (goals/OQ no longer static-prepend
# on off-domain / no-memory turns — the paper's #2 anti-pattern). The session-start
# goal surface is unaffected. goal_cap/oq_cap unchanged (block abstention, not a
# smaller cap, drops their injection FREQUENCY).
#
# relevance_threshold STAYS at 0.30 (NOT raised). The CALIBRATE-FIRST probe
# (eval/injection-gating/) found the research's "raise to 0.5-0.6" is IMPOSSIBLE on
# this stack: mem0 2.0.4 does HYBRID search — score_and_rank() gates each candidate
# on its SEMANTIC score (raw Qdrant cosine) but RETURNS the combined
# (semantic+bm25+entity)/max_possible, which is much higher. On the SEMANTIC scale
# the threshold actually gates, EmbeddingGemma's separation is compressed: clearly
# off-domain prompts sit <=0.12, genuinely-relevant prompts 0.25-0.57 (median ~0.33).
# 0.30 already cleanly rejects all clearly-irrelevant with margin AND abstains on the
# weakest matches; raising even to 0.35 craters relevant recall to ~0.47, and 0.50
# drops 100%. So the calibration CONFIRMS 0.30 and proves the raise would be wrong
# (exactly the plan's caution). The abstain decision is corpus/entity-side and
# model-independent, so both tiers share 0.30 (small was 0.33 in v0.22, a marginal
# nudge that over-abstains on this compressed scale; unified to 0.30 — per-tier
# scaling lives only in K). Threshold/caps MUST stay in sync with
# claude-config/model-tiers.json (test_tier_parity.py + test_r2_injection_gating.py).
TIER_BUNDLE_POLICY: dict[str, dict[str, Any]] = {
    "frontier": {"memory_cap": 2, "goal_cap": 5, "oq_cap": 3, "relevance_threshold": 0.30},
    "small":    {"memory_cap": 1, "goal_cap": 3, "oq_cap": 2, "relevance_threshold": 0.30},
}


def resolve_tier_policy(tier: Optional[str]) -> dict[str, Any]:
    """Return the bundle caps/threshold for a request tier, defaulting (and
    fail-opening on any unknown value) to frontier so a bad tier never
    under-serves the bundle."""
    return TIER_BUNDLE_POLICY.get((tier or "frontier"), TIER_BUNDLE_POLICY["frontier"])

# v0.18 MED-6: server-internal promotion-intent markers. These metadata keys mark
# records auto-downgraded with "promote me when the operator confirms" intent (v0.16.1
# client gate). Search already hides _canonical_intent records by default (F.1.2);
# GET /v1/memories/{id} must strip the keys too, or an agent holding only the API
# key could enumerate IDs and harvest the markers for batch self-promotion.
# v0.19 M12/L3: extended to the full set the stack ACTUALLY writes (underscore +
# non-underscore variants) and applied to BOTH the by-id and search paths.
# Writer enumeration (repo-wide grep, 2026-06-12):
#   _canonical_intent — scripts/wsl/mem0-mcp-shim.py:36 (canonical auto-downgrade)
#   _insight_intent   — scripts/wsl/mem0-mcp-shim.py:47 (insight auto-downgrade)
#   _stable_intent    — scripts/windows/user-prompt-extract.ps1 Phase 0.B decision
#                       hook (renamed from 'stable_intent' in v0.19 to match the
#                       underscore = server-internal convention)
#   stable_intent     — pre-v0.19 0.B hook records still in the store carry the
#                       non-underscore key (strip is presentation-only, so old
#                       data is covered without a migration)
#   canonical_intent / insight_intent — no writer today; stripped anyway so a
#                       convention slip in a future writer cannot re-open the
#                       enumeration oracle.
_INTENT_KEYS = {
    "_canonical_intent", "canonical_intent",
    "_insight_intent", "insight_intent",
    "_stable_intent", "stable_intent",
}

# v0.18 MED-9: goal merges that would relink more than this many episode_links
# require the HMAC user-direct token + nonce (bulk-tamper guard).
GOAL_MERGE_HMAC_THRESHOLD = 100

def _coerce_to_text(messages: Any) -> str:
    """Render the messages payload to a single string for length check."""
    if isinstance(messages, str):
        return messages
    if isinstance(messages, list):
        out = []
        for m in messages:
            if isinstance(m, dict):
                out.append(str(m.get("content", "")))
            else:
                out.append(str(m))
        return "\n".join(out)
    if isinstance(messages, dict):
        return str(messages.get("content", messages))
    return str(messages)

def _secure_open(path: Path, mode: str = "a", encoding: str = "utf-8"):
    """v0.18 MED-10: open an append-log with chmod 600 enforced at creation.
    These logs carry query text, decision text, and replay nonces — owner-only perms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600, exist_ok=True)
    return path.open(mode, encoding=encoding)


def _append_ledger(record: dict) -> None:
    """Append-only ledger writer for tier-change events. Single source of truth for promotion audit."""
    import json as _json
    ledger = Path.home() / ".mem0" / "tier-ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if "ts" not in record:
        record["ts"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    # v0.17 F.4.4: every entry stamps its schema version automatically.
    record.setdefault("schema_version", "v17")
    with ledger.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(record) + "\n")

@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": "2.0.4-v012", "store": "qdrant", "embedder": "embeddinggemma-300m"}

@app.get("/health/deep")
def health_deep() -> dict:
    """Deeper liveness probe (audit finding 2026-06-08: shallow /health was green-lighting
    broken write paths). Checks Qdrant + embedder + mem0 collection point count. Slow
    enough that callers should use /health for liveness; /health/deep is for diagnostics."""
    import httpx as _httpx
    out: dict[str, Any] = {"ok": True, "checks": {}}
    # v0.22 H2: report the collection mem0 is ACTUALLY bound to at runtime (NOT a
    # hardcoded literal). The egemma-rollback-prune gate reads this to confirm the
    # stack is still on the new EmbeddingGemma collection before it deletes the old
    # nomic `memories` rollback anchor. After a documented rollback (config.py
    # collection -> memories), this flips to "memories" and the prune gate SKIPS,
    # so the gate can no longer destroy the live store out from under a rolled-back
    # stack. Read from the live Memory instance — fail-soft (None) if unavailable.
    try:
        out["collection"] = mem.vector_store.collection_name
    except Exception:
        out["collection"] = None
    # Qdrant (v0.22: live collection is mem0_egemma_768 after the EmbeddingGemma re-embed)
    try:
        r = _httpx.get("http://127.0.0.1:6333/collections/mem0_egemma_768", timeout=3.0)
        r.raise_for_status()
        d = r.json().get("result", {})
        out["checks"]["qdrant"] = {"ok": True, "points": d.get("points_count"), "status": d.get("status")}
    except Exception as e:
        out["ok"] = False
        out["checks"]["qdrant"] = {"ok": False, "error": str(e)[:120]}
    # Embedder (v0.22: EmbeddingGemma-300m on llama.cpp/llama-swap :11436, OpenAI-compatible.
    # Replaced the nomic-via-Ollama :11435 probe when Ollama was decommissioned.)
    try:
        r = _httpx.post("http://127.0.0.1:11436/v1/embeddings",
                       json={"model": "embeddinggemma", "input": "title: none | text: health"},
                       timeout=10.0)
        r.raise_for_status()
        dim = len(r.json().get("data", [{}])[0].get("embedding", []))
        out["checks"]["embedder"] = {"ok": dim == 768, "dim": dim}
        if dim != 768:
            out["ok"] = False
    except Exception as e:
        out["ok"] = False
        out["checks"]["embedder"] = {"ok": False, "error": str(e)[:120]}
    # v0.19 M10: hook-contract drift counters (in-process, zero I/O). missing =
    # field-less callers (documented-legitimate, logged INFO); unknown = real
    # drift candidates (logged WARN). Informational — never flips ok=False.
    out["checks"]["hook_contract"] = dict(_hook_contract_stats)
    # v0.20 Phase D (M6): surface the keyless-degraded state (ExecStartPre=-
    # swallows a dpapi-fetch-key.sh failure; the server then 503s every
    # canonical/insight HMAC mutation while /health/deep stayed green — exactly
    # the 'shallow health green-lighting broken write paths' failure this
    # endpoint exists to prevent). Key is cached after first read — zero I/O
    # beyond one dpapi_path.exists(). ok=False ONLY when the blob exists but no
    # key loaded; a dev box with no key configured at all stays green.
    out["checks"]["canonical_key"] = _canonical_key_health(_APP_KEY_PROVIDER)
    if not out["checks"]["canonical_key"]["ok"]:
        out["ok"] = False
    return out

@app.post("/v1/memories")
def add(b: AddIn, background_tasks: BackgroundTasks, x_api_key: Optional[str] = Header(None)):
    auth(x_api_key)
    # Storage cap enforcement (audit finding 2026-06-08: 341/384 backfilled points
    # exceeded the previously-documented 600-char cap which was never enforced).
    text_for_check = _coerce_to_text(b.messages)
    if len(text_for_check) > MAX_MEMORY_CHARS:
        raise HTTPException(
            413,
            f"add: memory exceeds {MAX_MEMORY_CHARS}-char cap (got {len(text_for_check)}). "
            "Split into atomic facts (best for retrieval precision) or trim; raise MEM0_MAX_MEMORY_CHARS if a larger record is truly intended."
        )
    # Enforce: only evidence|temporal|insight* can be written via add.
    # - canonical NEVER via add — must use PATCH /v1/memories/{id}/tier with actor='user-direct'
    # - insight via add allowed ONLY when source contains 'c1-consolidator' (the nightly synthesizer)
    if b.metadata and "tier" in b.metadata:
        t = b.metadata["tier"]
        if t == "canonical":
            raise HTTPException(
                403,
                "POST /v1/memories with tier='canonical' is not allowed. "
                "To save a durable decision: (1) POST with tier='evidence' (or omit tier) to write the memory now, "
                "then (2) run 'bash scripts/wsl/mem0-canonize.sh <returned_id> \"<reason>\"' from your stack repo "
                "to promote to canonical (v0.14+ HMAC gate)."
            )
        if t == "insight":
            src = (b.metadata.get("source") or "").lower()
            actor = (b.metadata.get("actor") or "").lower()
            if src not in INSIGHT_ALLOWED_ACTORS:
                raise HTTPException(
                    403,
                    f"tier='insight' is reserved for the C1/dream consolidator. "
                    f"Got source={src!r} or actor={actor!r}; allowlist: {sorted(INSIGHT_ALLOWED_ACTORS)}. "
                    "If you're manually marking an insight, POST with tier='evidence' or 'stable' instead, "
                    "or wait for the next dream cycle to consolidate it."
                )
        elif t not in ADD_ALLOWED_TIERS:
            raise HTTPException(
                403,
                f"add: tier={t!r} not allowed; only {sorted(ADD_ALLOWED_TIERS)} or 'insight' (with source=c1-consolidator) can be set on add."
            )

    # v0.27.2 R5 (audit HIGH): the add path must NOT let a caller FORGE retrieval-gating
    # metadata keys. PATCH /metadata enforces these per-actor via FORBIDDEN_KEYS, but add()
    # validated only 'tier' — so any API-key holder could POST metadata.contradicts_canonical
    # =<id> (or superseded_by) and silently bury a record (the same preserved-but-hidden burial
    # the NLI gate produces) with NO Codex/neighbor. Strip them here so the gate's own
    # server-side stamp is the ONLY writer of contradicts_canonical on the add path.
    if b.metadata:
        _stripped = _ADD_FORBIDDEN_META & set(b.metadata.keys())
        if _stripped:
            b.metadata = {k: v for k, v in b.metadata.items() if k not in _ADD_FORBIDDEN_META}
            log.warning("add() stripped caller-supplied retrieval-gating metadata keys %s "
                        "(only writable via PATCH /metadata by a trusted actor, or by the NLI gate)",
                        sorted(_stripped))

    try:
        result = mem.add(
            messages=b.messages,
            user_id=b.user_id,
            agent_id=b.agent_id,
            run_id=b.run_id,
            metadata=b.metadata,
            infer=b.infer,
        )
        # If this was an insight write (only path that lands non-evidence via add), log to ledger
        # so canonical-add-coverage isn't silent.
        if b.metadata and b.metadata.get("tier") == "insight":
            try:
                results = result.get("results", []) if isinstance(result, dict) else []
                for entry in results:
                    mid = entry.get("id")
                    if mid:
                        _append_ledger({
                            "event": "add",
                            "memory_id": str(mid),
                            "tier": "insight",
                            "actor": b.metadata.get("source", "c1-consolidator"),
                            "reason": f"C1 add (window_evidence_count={b.metadata.get('window_evidence_count', '?')})",
                        })
            except Exception:
                log.exception("ledger append failed for insight add")
        # v0.27.2 R5: the NLI write-gate runs ASYNC (after the response) so add() NEVER blocks
        # on Codex. The synchronous version made the L1a writer's 15s POST time out on the ~21s
        # Codex call -> dead-letter + retry -> DUPLICATE writes (audit HIGH). Now the record is
        # admitted immediately; a background task judges it against a high-cosine canonical
        # neighbor (Codex via the shim) and, on a confident contradiction, stamps
        # contradicts_canonical so the admission gate hides it from durable/operational search.
        # Fail-soft: any shim/search failure leaves the record un-flagged (admitted), never blocks.
        # Tradeoff vs sync: a contradicting record is briefly visible (~one Codex call) before it
        # is hidden — acceptable for a background hygiene gate, and the plan's explicit design.
        if NLI_GATE_ENABLED and isinstance(result, dict):
            _recs = [
                {"id": r.get("id"), "memory": (r.get("memory") or r.get("data") or text_for_check)}
                for r in (result.get("results") or []) if r.get("id")
            ]
            if _recs:
                background_tasks.add_task(_nli_gate_stamp, _recs, b.user_id, (b.metadata or {}).get("brand"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        log.exception("add failed")
        raise HTTPException(500, str(e))

@app.get("/v1/memories")
def list_all(user_id: str = Query(...), limit: int = Query(100), x_api_key: Optional[str] = Header(None)):
    auth(x_api_key)
    # Hard-cap: silently clamp to 500 regardless of what the caller requests.
    limit = min(limit, 500)
    try:
        # mem0 v2.0.4 Memory.get_all signature is (*, filters=None, top_k=20, **kwargs);
        # the param is named `top_k`, not `limit`. Passing `limit=N` silently no-ops and the
        # default `top_k=20` wins, capping every list call at 20 regardless of caller intent.
        # This caused C1 consolidation and L10 audit to operate on 20/384 = 5% of data
        # (audit finding 2026-06-08). Pass `top_k` explicitly.
        return mem.get_all(filters={"user_id": user_id}, top_k=limit)
    except Exception as e:
        log.exception("list failed")
        raise HTTPException(500, str(e))

def _search_core(b: SearchIn):
    """v0.20 A.3: search internals shared by POST /v1/memories/search and
    POST /v1/context/bundle. Contains the FULL retrieval-policy pipeline -
    retired/intent filtering, rerank, query_class recency policy, the
    server-side admission gate (apply_admission) and retrieval-log
    observability - so the bundle endpoint can never become a parallel
    ungated path. Raises on failure; callers map exceptions to HTTP.
    hook_contract_version WARN-validation stays at the endpoint layer
    (each endpoint reports its own route name)."""
    capped_limit = min(b.limit, 500)
    # v0.30 over-fetch: post-fetch filters (retired/_canonical_intent/admission) can drop
    # records and leave a gap; over-fetch a buffer, filter, then trim to capped_limit (below).
    _buf = int(os.environ.get("MEM0_SEARCH_OVERFETCH_BUFFER", "50"))
    _buf = max(0, min(_buf, 500))                      # clamp env to [0,500] (Minor: no range validation)
    if b.rerank:
        _buf = min(_buf, 10)                            # Important: rerank is a CPU cross-encoder;
                                                        # bound the candidate pool so latency stays sane
    overfetch_limit = 0 if capped_limit == 0 else min(capped_limit + _buf, 500)  # Minor: don't fetch 50 for limit=0
    if capped_limit > 0 and overfetch_limit == capped_limit:
        # Important: at limit>=~450 the buffer collapses to 0 and the gap repair is inactive.
        # Not a regression (old code gapped too at these limits), but make it observable.
        log.warning("_search_core: over-fetch buffer collapsed at capped_limit=%d (gap repair inactive at this limit)", capped_limit)
    # v0.17 F.1.2: strip server-side opt-in flags before passing to mem.search / Qdrant.
    # These keys are our own post-filter directives; Qdrant doesn't know them and would 500.
    # v0.19 M4: allow_cross_brand is the explicit opt-in for brandless searches to
    # receive brand-scoped records (admission gate is fail-closed on brand otherwise).
    # v1.0 Phase B: `brand` is ALSO stripped from the Qdrant pre-filter and scoped ONLY by
    # the admission gate below. The Qdrant `brand==X` pre-filter dropped brand-NEUTRAL
    # (null-brand) candidates before the gate saw them, starving branded queries of the
    # general facts that apply to every brand (A2 measured branded recall at 37.5% over a
    # ~96%-neutral store). The admission gate is DESIGNED to admit null+matching and reject
    # only a *different* brand (admission_gate.py), so moving brand entirely to the gate
    # restores neutral facts to branded queries WITHOUT relaxing cross-brand isolation - the
    # gate still rejects other brands (test_brand_isolation.py is the leak guard).
    _SERVER_FILTER_KEYS = {"include_retired", "include_canonical_intent", "allow_cross_brand", "brand"}
    search_filters = {k: v for k, v in (b.filters or {}).items() if k not in _SERVER_FILTER_KEYS}
    results = mem.search(
        query=b.query,
        filters=search_filters,
        top_k=overfetch_limit,
        threshold=b.threshold,
    )
    # v0.13: exclude retired records (retrievable=false) from search results unless caller explicitly opts in
    if not (b.filters or {}).get("include_retired"):
        if isinstance(results, dict) and isinstance(results.get("results"), list):
            results["results"] = [r for r in results["results"] if (r.get("metadata") or {}).get("retrievable") is not False]
    # v0.17 Phase F.1.2: hide _canonical_intent evidence from default search results.
    # These are memories that were auto-downgraded from tier='canonical' to tier='evidence'
    # (client-side gate in v0.16.1) with metadata._canonical_intent=True as a "promote me
    # when the operator confirms" marker. Surfacing them in default search is a privilege-escalation
    # oracle: a bad agent that grabs canonical-key could read the marker and batch-promote all.
    # To retrieve these explicitly, pass filters.include_canonical_intent=True.
    if not (b.filters or {}).get("include_canonical_intent"):
        if isinstance(results, dict) and isinstance(results.get("results"), list):
            # v0.18 MED-5: truthy check (was `is True`) — a truthy non-bool
            # _canonical_intent value (e.g. "true", 1) must not slip past the filter.
            results["results"] = [
                r for r in results["results"]
                if not ((r.get("metadata") or {}).get("_canonical_intent"))
            ]
    # v0.19 M12: strip server-internal intent markers from every surviving
    # result's metadata. F.1.2 above excludes whole _canonical_intent records,
    # but _insight_intent / stable_intent markers (and _canonical_intent on
    # records surfaced via include_canonical_intent=True) rode out through
    # search metadata — the same enumeration oracle MED-6 closed on the
    # by-id path. Presentation-only: stored payloads are untouched.
    if isinstance(results, dict) and isinstance(results.get("results"), list):
        for _r in results["results"]:
            _md = _r.get("metadata")
            if isinstance(_md, dict):
                for _k in _INTENT_KEYS:
                    _md.pop(_k, None)
    items = results.get("results") if isinstance(results, dict) else None
    if b.rerank and isinstance(items, list) and items:
        reranked_items = bge_rerank(b.query, items, text_key="memory")
        results = dict(results)
        results["results"] = reranked_items
        # Only mark reranked=True if the reranker actually ran (presence of rerank_score)
        results["reranked"] = any("rerank_score" in r for r in reranked_items)
    # v0.17 F.4.1: query_class recency policy (applied AFTER rerank, BEFORE return)
    import math as _math
    qclass = ((b.query_class or "durable") if hasattr(b, "query_class") else "durable").lower()
    if qclass == "operational":
        if isinstance(results, dict) and isinstance(results.get("results"), list):
            # v0.18 LOW-6: half-life (eta) configurable via MEM0_OPERATIONAL_HALF_LIFE_DAYS
            # (default 30; non-int or <=0 values fall back to 30).
            try:
                _eta_days = int(os.environ.get("MEM0_OPERATIONAL_HALF_LIFE_DAYS", "30"))
                if _eta_days <= 0:
                    _eta_days = 30
            except (TypeError, ValueError):
                _eta_days = 30
            # v1.0 R5: Weibull shape kappa (MEM0_WEIBULL_KAPPA, default 1.0 = the v0.18
            # plain exponential half-life exactly; >1 = steeper anti-staleness cliff).
            try:
                _kappa = float(os.environ.get("MEM0_WEIBULL_KAPPA", "1.0"))
                if _kappa <= 0:
                    _kappa = 1.0
            except (TypeError, ValueError):
                _kappa = 1.0
            now_utc = _dt.datetime.now(_dt.timezone.utc)
            for r in results["results"]:
                created = (r.get("metadata") or {}).get("created_at") or r.get("created_at")
                if not created:
                    continue
                try:
                    c_dt = _dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    age_days = max(0.0, (now_utc - c_dt).total_seconds() / 86400.0)
                    # v1.0 R5: Weibull freshness w = exp(-ln2*(age/eta)^kappa) (kappa=1.0
                    # default == the v0.18 exp(-age/eta*ln2); no regression). Pure fn in
                    # freshness.py (unit-tested without a server).
                    decay = _freshness_weight(age_days, float(_eta_days), _kappa)
                    # v0.18 MED-4: explicit None checks (was falsy-or chain) —
                    # a legitimate rerank_score of 0.0 must be preserved, not
                    # fall through to the raw vector score.
                    base_score = r["rerank_score"] if r.get("rerank_score") is not None else (r.get("score") if r.get("score") is not None else 0.0)
                    r["operational_recency_score"] = base_score * decay
                    r["freshness_weight"] = round(decay, 6)  # R5 observability (retrieval log)
                except (ValueError, TypeError):
                    pass
            # Re-sort by operational_recency_score descending (records without it sort last)
            if any("operational_recency_score" in r for r in results["results"]):
                results["results"].sort(
                    key=lambda x: x.get("operational_recency_score", -1.0), reverse=True
                )
            results["query_class"] = "operational"
    elif qclass == "canonical":
        if isinstance(results, dict) and isinstance(results.get("results"), list):
            results["results"] = [
                r for r in results["results"]
                if (r.get("metadata") or {}).get("tier") in ("canonical", "stable")
            ]
            results["query_class"] = "canonical"
    elif qclass == "durable" and _env_flag("MEM0_DURABLE_FRESHNESS_ENABLED"):
        # v0.29.4 item 1 / R5 (SSGM 2603.11768): extend the Weibull freshness read-gate
        # to the time-sensitive tier on the durable read path (DURABLE_DECAY_TIERS =
        # {evidence}) — the plan + research prescribed tier-scoped decay, not only the
        # operational query_class. temporal is also time-sensitive but is NOT admitted on
        # the durable class (admission allows stable/evidence/insight), so it is excluded
        # here (audit 2026-06-16). canonical/stable/insight are ATEMPORAL knowledge -> NO decay
        # (research: only time-sensitive memory should age). GENTLE by default (durable
        # evidence ages far slower than operational notes): MEM0_DURABLE_FRESHNESS_HALF_LIFE_DAYS
        # default 365 -> a 30-day-old evidence record keeps ~0.945 of its score; 1yr -> 0.5.
        # Gated by MEM0_DURABLE_FRESHNESS_ENABLED (set in systemd/mem0.service); default OFF
        # in code so tests/non-systemd runs keep the legacy relevance-only ordering.
        if isinstance(results, dict) and isinstance(results.get("results"), list):
            try:
                _deta = int(os.environ.get("MEM0_DURABLE_FRESHNESS_HALF_LIFE_DAYS", "365"))
                if _deta <= 0:
                    _deta = 365
            except (TypeError, ValueError):
                _deta = 365
            try:
                _dkappa = float(os.environ.get("MEM0_WEIBULL_KAPPA", "1.0"))
                if _dkappa <= 0:
                    _dkappa = 1.0
            except (TypeError, ValueError):
                _dkappa = 1.0
            from freshness import apply_durable_freshness as _apply_durable_freshness
            _apply_durable_freshness(results["results"], _deta, _dkappa,
                                     _dt.datetime.now(_dt.timezone.utc))
            results["query_class"] = "durable"
    # v0.18 Phase C: admission gate Phase-1 (scope + tier + recency + rejected logging)
    if isinstance(results, dict) and isinstance(results.get("results"), list):
        scope = {
            "user_id": (b.filters or {}).get("user_id"),
            "brand": (b.filters or {}).get("brand"),
            # v0.19 M4: explicit opt-in for cross-brand results on brandless searches
            "allow_cross_brand": (b.filters or {}).get("allow_cross_brand"),
        }
        # v0.18 fix-pass HIGH: an explicit filters.tier='canonical' is the same trust
        # posture as query_class='canonical' (both require the API key; both are a
        # deliberate ask for ground-truth records) — derive the admission class from
        # it so the gate uses the (stable, canonical) allowlist instead of silently
        # stripping every canonical hit the caller explicitly filtered for.
        _adm_qc = "canonical" if (b.filters or {}).get("tier") == "canonical" else (b.query_class or "durable")
        results["results"] = apply_admission(
            results["results"],
            scope=scope,
            query_class=_adm_qc,
            layer="server-search",
        )
    # v0.30 over-fetch trim: now that retired/intent/admission filters have run on the
    # larger pool, return only the caller's requested capped_limit (the K slots are now
    # filled with non-filtered records, not gapped). Last mutation before logging so the
    # logged returned_count/returned_top_ids reflect what the caller actually receives.
    if isinstance(results, dict) and isinstance(results.get("results"), list):
        results["results"] = results["results"][:capped_limit]
    # v0.17 Phase F.2.3: retrieval observability — log every search to ~/.mem0/retrieval-log.jsonl
    try:
        import hashlib as _hl
        query_hash = _hl.sha256(b.query.encode("utf-8")).hexdigest()[:16]
        log_full = os.environ.get("MEM0_LOG_FULL_QUERY") == "1"
        # v0.18 LOW-3: opt-in filter-value hashing — with MEM0_LOG_HASH_FILTERS=1,
        # brand/user_id values in the logged filter dict are replaced by
        # sha256-hex truncated to 12 chars. Default (env unset): raw values, unchanged.
        log_filters = b.filters
        if os.environ.get("MEM0_LOG_HASH_FILTERS") == "1" and isinstance(b.filters, dict):
            log_filters = dict(b.filters)
            for _fk in ("brand", "user_id"):
                if log_filters.get(_fk) is not None:
                    log_filters[_fk] = _hl.sha256(
                        str(log_filters[_fk]).encode("utf-8")
                    ).hexdigest()[:12]
        retrieval_record = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "actor": "rest-api",
            "query_hash": query_hash,
            "query_text": b.query[:200] if log_full else None,
            "filters": log_filters,
            "limit": b.limit,
            "rerank": b.rerank,
            # v0.20 Phase E (M4): query_class recorded on EVERY entry — the
            # history class is an authz-by-design escape hatch (admission-gate.md)
            # but was invisible here, so forensic reads of superseded/contradicted
            # records left no trace. qclass is computed unconditionally above;
            # the bundle endpoint shares _search_core, so both paths log it.
            # (The effective admission class can diverge to 'canonical' via
            # filters.tier — filters are logged above, so an auditor can
            # reconstruct it; logging qclass makes every history request visible.)
            "query_class": qclass,
            "forensic": qclass == "history",
            "threshold": b.threshold,
            "returned_count": len(results.get("results", [])) if isinstance(results, dict) else 0,
            "returned_top_ids": [r.get("id") for r in (results.get("results") or [])[:3]] if isinstance(results, dict) else [],
            "reranked": (results.get("reranked") if isinstance(results, dict) else None),
        }
        log_path = Path.home() / ".mem0" / "retrieval-log.jsonl"
        # Rotate at 10MB — move to .1 through .5, drop .5 if it exists.
        # v0.20 Phase E (L11): unlink the DST (.5) before renaming .4 into it —
        # the old code unlinked SRC (.4) at i==5, so .4 vanished each cycle and
        # .5 never existed. Windows-rename-safe by induction: dst is always
        # vacated before each rename. (Identical fix in admission_gate.py.)
        if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
            for i in range(5, 0, -1):
                src = log_path.with_suffix(f".jsonl.{i - 1}") if i > 1 else log_path
                dst = log_path.with_suffix(f".jsonl.{i}")
                if i == 5:
                    dst.unlink(missing_ok=True)
                if src.exists():
                    src.rename(dst)
        # v0.18 MED-10: _secure_open enforces chmod 600 at creation (also covers
        # the fresh file created after the 10MB rotation above).
        with _secure_open(log_path) as _lf:
            _lf.write(json.dumps(retrieval_record) + "\n")
    except Exception:
        log.exception("retrieval logging failed (non-fatal)")
    return results


@app.post("/v1/memories/search")
def search(b: SearchIn, x_api_key: Optional[str] = Header(None)):
    auth(x_api_key)
    # v0.19 M15: close the search drift gap — checkpoint/episodes were versioned
    # in v0.18 but the hooks' search POSTs were not.
    _warn_hook_contract_version("/v1/memories/search", b.hook_contract_version)
    try:
        return _search_core(b)
    except Exception as e:
        log.exception("search failed")
        raise HTTPException(500, str(e))

@app.get("/v1/memories/{mid}")
def get_memory_by_id(mid: str, x_api_key: Optional[str] = Header(None)):
    """v0.17 F.3.1: exact-read by memory_id.
    Returns text, metadata, tier, retrievable, source, created_at, updated_at.

    Use BEFORE memory_update, memory_delete, memory_promote — search/list are not
    substitutes for exact reads.  Avoids wrong-ID edits caused by search returning
    similar (but wrong) records."""
    auth(x_api_key)
    try:
        records = mem.vector_store.client.retrieve(
            collection_name=mem.vector_store.collection_name,
            ids=[mid], with_payload=True, with_vectors=False,
        )
        if not records:
            raise HTTPException(404, f"memory {mid} not found")
        rec = records[0]
        payload = rec.payload if hasattr(rec, "payload") else rec.get("payload", {})
        # v0.18 MED-6: strip server-internal intent markers from the by-id read.
        # Search hides _canonical_intent records by default (F.1.2), but this
        # endpoint leaked the markers to anyone enumerating IDs with the API key.
        metadata = {k: v for k, v in payload.items() if k not in ("data", "memory")}
        metadata = {k: v for k, v in metadata.items() if k not in _INTENT_KEYS}
        return {
            "id": mid,
            "memory": payload.get("data") or payload.get("memory"),
            "metadata": metadata,
            "tier": payload.get("tier"),
            "retrievable": payload.get("retrievable", True),
            "source": payload.get("source"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_by_id failed")
        raise HTTPException(500, str(e))


@app.put("/v1/memories/{mid}")
def update(
    mid: str, b: UpdateIn,
    x_api_key: Optional[str] = Header(None),
    x_user_direct_token: Optional[str] = Header(None, alias="X-User-Direct-Token"),
    x_user_direct_ts: Optional[str] = Header(None, alias="X-User-Direct-Ts"),
    x_user_direct_nonce: Optional[str] = Header(None, alias="X-User-Direct-Nonce"),
    actor: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
):
    """Update memory text. v0.17 Phase A: canonical/insight tier gate applied BEFORE update.
    v0.17 Phase F.1: X-User-Direct-Nonce header accepted for replay protection."""
    auth(x_api_key)
    if len(b.text) > MAX_MEMORY_CHARS:
        raise HTTPException(413, f"update: text exceeds {MAX_MEMORY_CHARS}-char cap (got {len(b.text)})")
    # v0.17 Phase A: canonical/insight tier write-path gate
    # v0.17 Phase F.1: nonce forwarded for replay protection
    from security_invariants import assert_writable
    current_tier = assert_writable(
        mem.vector_store.client, mem.vector_store.collection_name, mid,
        "put", x_user_direct_token, x_user_direct_ts,
        actor=(actor or ""), reason=(reason or ""),
        x_user_direct_nonce=x_user_direct_nonce,
    )
    # v0.28 Phase 2a: promote-canary on PUT — after HMAC enforcement, before the write.
    # When the target record is canonical, reject imperative text (declarative facts only).
    if current_tier == "canonical" and is_imperative_canonical(b.text.strip()):
        raise HTTPException(
            422,
            "canonical is declarative facts only; rephrase as a fact, not a standing order. "
            f"(detected imperative phrasing in: {b.text.strip()[:80]!r})",
        )
    try:
        result = mem.update(memory_id=mid, data=b.text)
        # v0.17 F.2.5 / H1: PUT carry-over fix — mem.update() strips tier from Qdrant payload.
        # Restore so subsequent PUT/DELETE/PATCH-metadata calls are still gated by Phase A.
        # H1 fix: retry with bounded exponential backoff (3 attempts: 100ms, 300ms, 900ms).
        # If all retries fail for canonical/insight, raise 500 instead of silently succeeding.
        if current_tier:
            import time as _time
            _tier_restored = False
            _backoff_delays = [0.1, 0.3, 0.9]  # seconds
            for _attempt, _delay in enumerate(_backoff_delays):
                try:
                    mem.vector_store.client.set_payload(
                        collection_name=mem.vector_store.collection_name,
                        payload={"tier": current_tier},
                        points=[mid],
                    )
                    _tier_restored = True
                    break
                except Exception:
                    if _attempt < len(_backoff_delays) - 1:
                        log.warning(
                            "H1: tier restore attempt %d/%d failed for mid=%s tier=%s; retrying in %.1fs",
                            _attempt + 1, len(_backoff_delays), mid, current_tier, _backoff_delays[_attempt + 1],
                        )
                        _time.sleep(_delay)
                    else:
                        log.exception(
                            "H1: all %d tier restore attempts exhausted for mid=%s tier=%s",
                            len(_backoff_delays), mid, current_tier,
                        )
            if not _tier_restored and current_tier in {"canonical", "insight"}:
                raise HTTPException(
                    500,
                    f"F.2.5/H1: tier restore failed after {len(_backoff_delays)} attempts for "
                    f"memory_id={mid!r} (tier={current_tier!r}). The record may be in an inconsistent "
                    "state — manual verification required. The update itself succeeded in mem0 "
                    "but tier field was not restored in Qdrant.",
                )
        # Ledger entry on success — user-direct PUTs to canonical/insight are audit-covered
        try:
            _append_ledger({
                "event": "memory-update",
                "memory_id": mid,
                "prior_tier": current_tier,
                "actor": actor or "rest-api",
                "reason": reason or "PUT /v1/memories/{mid}",
                "transport": "cli-user-direct" if x_user_direct_token else "rest-api",
            })
        except Exception:
            log.exception("ledger append failed for memory-update")
        return result
    except HTTPException:
        raise
    except Exception as e:
        log.exception("update failed")
        raise HTTPException(500, str(e))

@app.patch("/v1/memories/{mid}/tier")
def update_tier(mid: str, b: TierIn, x_api_key: Optional[str] = Header(None),
                x_user_direct_token: Optional[str] = Header(None, alias="X-User-Direct-Token"),
                x_user_direct_ts: Optional[str] = Header(None, alias="X-User-Direct-Ts"),
                x_user_direct_nonce: Optional[str] = Header(None, alias="X-User-Direct-Nonce")):
    """Update a memory's tier. Server-enforced actor requirements per tier.
    Canonical promotions additionally require a valid HMAC X-User-Direct-Token header (v0.14 B).
    v0.19 Phase G: the token is validated as format-2
    (<ts>|<nonce>|promote|<mid>|<reason>) via security_invariants —
    nonce + replay protection, HMAC verified before the nonce is burned (MED-8).
    v0.20 Phase G: the nonce-less format-1 path (<ts>|<mid>|<reason>) is
    REMOVED — a promotion without X-User-Direct-Nonce is rejected outright.
    Writes ONE ledger line after the Qdrant payload update succeeds."""
    auth(x_api_key)
    if b.tier not in PROMOTE_ALLOWED_TIERS:
        raise HTTPException(400, f"invalid tier: {b.tier}; allowed: {sorted(PROMOTE_ALLOWED_TIERS)}")
    actor = (b.actor or "").strip()
    reason = (b.reason or "").strip()
    if not actor:
        raise HTTPException(400, "actor is required (e.g., 'user-direct', 'c1-consolidator', 'claude-autonomous')")
    if b.tier == "canonical":
        if CANONICAL_REQUIRES_USER_DIRECT and actor != "user-direct" and actor not in CANONICAL_AUTOPROMOTE_ALLOWED:
            raise HTTPException(403,
                f"canonical promotion requires actor='user-direct' or actor in {sorted(CANONICAL_AUTOPROMOTE_ALLOWED)} "
                f"(you sent actor={actor!r}). "
                "Autonomous Claude promotions can only set tier='insight' or 'stable'.")
        if not reason:
            raise HTTPException(400, "canonical promotion requires non-empty 'reason' (audit-trail policy)")
        # v0.20 Phase G: format-1 (<ts>|<mid>|<reason>, no nonce) REMOVED — the
        # deprecation committed in v0.19 lands. Nonce-less promotion → 403 here,
        # before any validation (there is no legacy payload left to validate).
        if not x_user_direct_nonce:
            raise HTTPException(403, (
                "X-User-Direct-Nonce required: format-1 tier promotion was "
                "removed in v0.20 — sign format-2 <ts>|<nonce>|promote|<mid>|<reason> "
                "(mem0-canonize.sh does this)"
            ))
        # v0.19 Phase G: format-2 promote — reuse the central validator
        # (key presence, token/ts presence, skew, HMAC-before-nonce ordering
        # per MED-8, replay store). Mirrors merge_goals (v0.18 E.2.4).
        from security_invariants import validate_hmac_user_direct
        validate_hmac_user_direct(
            mid, "promote", reason,
            x_user_direct_token, x_user_direct_ts,
            x_user_direct_nonce=x_user_direct_nonce,
        )
    if b.tier == "insight":
        if INSIGHT_REQUIRES_C1 and actor.lower() not in INSIGHT_ALLOWED_ACTORS:
            raise HTTPException(403,
                f"insight tier requires actor in {sorted(INSIGHT_ALLOWED_ACTORS)} "
                f"(you sent actor={actor!r}).")

    # v0.29 Phase 2a: promote-canary — AFTER auth/HMAC enforcement, reject imperative text.
    # Canonical tier is declarative facts only; standing-order phrasing is rejected with 422.
    # FAIL-SAFE: if Qdrant retrieve RAISES, we cannot verify the text — reject with 503
    # rather than silently skipping the canary (fail-open would be wrong for a write gate).
    if b.tier == "canonical":
        try:
            _canon_records = mem.vector_store.client.retrieve(
                collection_name=mem.vector_store.collection_name,
                ids=[mid], with_payload=True, with_vectors=False,
            )
            _canon_text = ""
            if _canon_records:
                _pl = _canon_records[0].payload if hasattr(_canon_records[0], "payload") else _canon_records[0].get("payload", {})
                _canon_text = (_pl.get("data") or _pl.get("memory") or "").strip()
        except Exception as _e:
            log.warning("imperative-canary: Qdrant retrieve failed for %s (%s); rejecting promotion", mid, _e)
            raise HTTPException(
                503,
                "could not verify canonical text for the imperative-canary; "
                "promotion rejected — retry when the store is reachable",
            )
        if is_imperative_canonical(_canon_text):
            raise HTTPException(
                422,
                "canonical is declarative facts only; rephrase as a fact, not a standing order. "
                f"(detected imperative phrasing in: {_canon_text[:80]!r})",
            )

    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        mem.vector_store.client.set_payload(
            collection_name=mem.vector_store.collection_name,
            payload={"tier": b.tier, "updated_at": now, "tier_actor": actor},
            points=[mid],
        )
    except Exception as e:
        log.exception("tier-update failed")
        raise HTTPException(500, str(e))
    # Single ledger append AFTER successful payload update
    # transport field: "autonomous" when actor is from CANONICAL_AUTOPROMOTE_ALLOWED (dream-autopromote),
    # "cli-user-direct" for HMAC-validated user-direct canonical, "rest-api" otherwise.
    if b.tier == "canonical" and actor in CANONICAL_AUTOPROMOTE_ALLOWED:
        transport = "autonomous"
    elif b.tier == "canonical" and x_user_direct_token:
        transport = "cli-user-direct"
    else:
        transport = "rest-api"
    try:
        _append_ledger({
            "ts": now, "event": "tier-change", "memory_id": mid,
            "tier": b.tier, "actor": actor, "reason": reason or None,
            "transport": transport,
        })
    except Exception:
        log.exception("ledger append failed for tier-change")
    return {"ok": True, "memory_id": mid, "tier": b.tier, "actor": actor, "ts": now}

@app.patch("/v1/memories/{mid}/metadata")
def update_metadata(
    mid: str, b: MetadataIn,
    x_api_key: Optional[str] = Header(None),
    x_user_direct_token: Optional[str] = Header(None, alias="X-User-Direct-Token"),
    x_user_direct_ts: Optional[str] = Header(None, alias="X-User-Direct-Ts"),
    x_user_direct_nonce: Optional[str] = Header(None, alias="X-User-Direct-Nonce"),
):
    """Shallow-merge new metadata fields into the existing Qdrant payload.
    Cannot change `tier` (use PATCH /tier for that). Used by re-extraction
    (marks originals retrievable=false), decay (sets temporal.expires_at),
    and dream-consolidator (stamps touched_by_dream).

    v0.17 Phase A: canonical/insight tier gate runs BEFORE the FORBIDDEN_KEYS check.
    v0.17 Phase F.1: X-User-Direct-Nonce header accepted for replay protection.

    EVERY successful merge is appended to the tier-ledger so all post-hoc
    mutations are audit-covered (lens S1: shallow-merge could otherwise be
    used to undo retirement silently)."""
    auth(x_api_key)
    if "tier" in b.metadata:
        raise HTTPException(400, "use PATCH /v1/memories/{id}/tier to change tier")
    if not b.metadata:
        raise HTTPException(400, "metadata must be non-empty")
    # v0.17 Phase A: canonical/insight tier gate — runs FIRST, before FORBIDDEN_KEYS check,
    # so a canonical record is protected even from trusted-actor metadata writes unless the
    # caller also holds a valid HMAC user-direct token.
    # v0.17 Phase F.1: nonce forwarded for replay protection
    from security_invariants import assert_writable
    current_tier = assert_writable(
        mem.vector_store.client, mem.vector_store.collection_name, mid,
        "patch_metadata", x_user_direct_token, x_user_direct_ts,
        actor=(b.actor or ""), reason=(b.reason or ""),
        x_user_direct_nonce=x_user_direct_nonce,
    )
    # v0.13 hardening: block lifecycle-critical keys that gate retrieval semantics.
    # These should only be written by trusted server-side flows (re-extract, decay, dream).
    # Callers can stamp arbitrary OTHER metadata (test_patch, custom_tags, etc.) but
    # cannot flip retrieval gates via the generic shallow-merge endpoint.
    # H8 fix: TRUSTED_PATCH_ACTORS (e.g. stamp-retired-v013) may also write retired_at
    # on canonical/insight records without requiring a full HMAC user-direct token.
    # v0.19 I.3: the allowlist is per-actor (dict actor -> frozenset of exact keys).
    from security_invariants import TRUSTED_PATCH_ACTORS
    # v0.20 Phase B (M1/M3/M11): the v0.19 retrieval-gating keys (superseded_by
    # rejected at admission_gate I.1, contradicts_canonical at I.3, plus the
    # sweep's idempotency marker contradiction_checked_at) joined FORBIDDEN_KEYS
    # so an arbitrary API-key holder cannot censor retrieval via the generic
    # shallow-merge endpoint. The per-actor TRUSTED_PATCH_ACTORS dict is the
    # ONLY write path (contradiction-sweep-v019 -> its two contradiction keys).
    # superseded_by has NO API writer today (the cascade-delete walk below only
    # READS it; contradiction-sweep skips on it; mem0-backfill skips superseded
    # rows) so it is blocked for ALL actors — a future supersession-writer must
    # register in TRUSTED_PATCH_ACTORS (security_invariants.py) with an exact
    # key allowlist rather than reopen this endpoint.
    FORBIDDEN_KEYS = {"retrievable", "expires_at", "created_at", "tier_actor",
                      "superseded_by", "contradicts_canonical", "contradiction_checked_at",
                      "contradicts_canonical_pending"}  # v0.29.4: only the sweep actor writes it
    forbidden_hit = FORBIDDEN_KEYS & set(b.metadata.keys())
    if forbidden_hit:
        # v0.20 Final (adversarial-review MED, mixed-key bypass): every forbidden
        # key the caller sends must be INDIVIDUALLY authorized for that actor.
        # The old logic set a single global `allowed = True` if ANY legacy
        # per-key/actor rule matched, so actor='system' could smuggle
        # superseded_by past the gate alongside tier_actor. Now the legacy
        # server-flow actors carry an exact key set (mirroring the old rules),
        # unioned with TRUSTED_PATCH_ACTORS, and the whole forbidden_hit must be
        # a subset — superseded_by stays blocked for ALL actors (no actor lists
        # it).
        actor = (b.actor or "").strip().lower()
        # Legacy server-flow actors, each scoped to the EXACT keys it may write
        # (mirrors the previous per-key rules but as a per-actor key set, so a
        # forbidden key cannot be smuggled in alongside an authorized one).
        _LEGACY_ACTOR_KEYS = {
            "backfill-apply-v013": {"retrievable"},
            "decay-scan": {"expires_at"},
            "system": {"expires_at", "tier_actor"},
        }
        allowed_keys = set(_LEGACY_ACTOR_KEYS.get(actor, set()))
        allowed_keys |= set(TRUSTED_PATCH_ACTORS.get(actor, frozenset()))
        if not (forbidden_hit <= allowed_keys):
            raise HTTPException(
                403,
                f"forbidden metadata keys {sorted(forbidden_hit - allowed_keys)} "
                f"require trusted actor; got actor={actor!r}",
            )
    # H8: allow TRUSTED_PATCH_ACTORS to write their per-actor allowed keys (e.g.
    # retired_at for stamp-retired-v013, contradicts_canonical +
    # contradiction_checked_at for contradiction-sweep-v019) without the
    # canonical HMAC gate — the HMAC gate above would have blocked them because
    # assert_writable requires a user-direct token for canonical records; trusted actors get
    # a bypass specifically for their allowed keys.
    # NOTE: this re-checks for the specific trusted-actor bypass AFTER assert_writable already
    # ran; since assert_writable returned without raising for these actors (trusted-actor
    # allowlist check below), we proceed. For canonical records NOT covered by the allowlist,
    # assert_writable already raised 403 above — we never reach here.
    # v0.19 I.3: per-actor exact key allowlist (TRUSTED_PATCH_ACTORS is now a
    # dict actor -> frozenset of keys) so each trusted actor can write ONLY its
    # own stamps.
    _actor_lower = (b.actor or "").strip().lower()
    if _actor_lower in TRUSTED_PATCH_ACTORS:
        _actor_allowed_keys = TRUSTED_PATCH_ACTORS[_actor_lower]
        _not_allowed_keys = set(b.metadata.keys()) - _actor_allowed_keys
        if _not_allowed_keys:
            raise HTTPException(
                403,
                f"trusted actor {_actor_lower!r} may only write keys {sorted(_actor_allowed_keys)}; "
                f"disallowed keys in request: {sorted(_not_allowed_keys)}",
            )
    # Bump updated_at so lead-7 sort by recency in memory-index-build.py is correct
    merged = dict(b.metadata)
    merged["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        mem.vector_store.client.set_payload(
            collection_name=mem.vector_store.collection_name,
            payload=merged,
            points=[mid],
        )
    except Exception as e:
        log.exception("metadata update failed")
        raise HTTPException(500, str(e))
    try:
        _append_ledger({
            "event": "metadata-merge",
            "memory_id": mid,
            "merged_keys": sorted(merged.keys()),
            "actor": (b.actor or "unspecified"),
            "reason": (b.reason or None),
            "prior_tier": current_tier,
            "transport": "cli-user-direct" if x_user_direct_token else "rest-api",
        })
    except Exception:
        log.exception("ledger append failed for metadata-merge")
    return {"ok": True, "memory_id": mid, "merged_keys": sorted(merged.keys())}

# ---------------------------------------------------------------------------
# v0.16: Goal endpoints
# IMPORTANT: /v1/goals/tree must come BEFORE /v1/goals/{goal_id} so FastAPI
# does not try to parse "tree" as an integer.
# ---------------------------------------------------------------------------

@app.post("/v1/goals")
def create_goal_endpoint(b: GoalIn, x_api_key: Optional[str] = Header(None)):
    """Create a goal manually. Returns {ok, goal_id}."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            gid = _episodic_create_goal(
                conn, title=b.title, description=b.description, brand=b.brand,
                parent_goal_id=b.parent_goal_id,
                priority=b.priority if b.priority is not None else 3,  # MED-A: 0 is falsy but valid... Field(ge=1) blocks it
                initiative=b.initiative,  # v0.22 Pillar 1
            )
        return {"ok": True, "goal_id": gid}
    except Exception as e:
        log.exception("goal create failed")
        raise HTTPException(500, str(e))


@app.get("/v1/goals/tree")
def goals_tree_endpoint(root_id: Optional[int] = None, x_api_key: Optional[str] = Header(None)):
    """Return goal hierarchy as a flat list with depth field.
    root_id=None returns all top-level trees."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_get_goal_tree(conn, root_goal_id=root_id)
    except Exception as e:
        log.exception("goals tree failed")
        raise HTTPException(500, str(e))


@app.get("/v1/goals")
def list_goals_endpoint(
    status: Optional[str] = None,
    brand: Optional[str] = None,
    parent_id: Optional[int] = None,
    limit: int = 50,
    initiative: Optional[str] = None,
    x_api_key: Optional[str] = Header(None),
):
    """List goals with optional filters: status, brand, parent_id, limit, initiative.

    v0.22 Pillar 1: initiative (when provided) scopes to that initiative +
    cross-cutting (NULL) rows — used by the SessionStart brand-context injection.
    Omitted == unfiltered on initiative (preserves the MCP goals_list path).
    """
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_list_goals(conn, status=status, brand=brand, parent_goal_id=parent_id, limit=limit, initiative=initiative)
    except Exception as e:
        log.exception("goals list failed")
        raise HTTPException(500, str(e))


@app.get("/v1/goals/{goal_id}")
def get_goal_endpoint(goal_id: int, x_api_key: Optional[str] = Header(None)):
    """Fetch a single goal by integer id."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            g = _episodic_get_goal(conn, goal_id)
        if not g:
            raise HTTPException(404, f"goal {goal_id} not found")
        return g
    except HTTPException:
        raise
    except Exception as e:
        log.exception("goal get failed")
        raise HTTPException(500, str(e))


@app.patch("/v1/goals/{goal_id}/status")
def patch_goal_status_endpoint(goal_id: int, b: GoalStatusIn, x_api_key: Optional[str] = Header(None)):
    """Update a goal's status. Valid values: open, blocked, advanced, completed, abandoned.
    Requires actor field; appends a ledger entry on every successful change."""
    auth(x_api_key)
    actor = (b.actor or "").strip()
    if not actor:
        raise HTTPException(400, "actor is required (e.g. 'user-direct', 'claude-autonomous', 'test')")
    try:
        with _episodic_connect() as conn:
            ok = _episodic_update_goal_status(conn, goal_id, b.status, completed_at=b.completed_at)
        if not ok:
            raise HTTPException(404, f"goal {goal_id} not found or status unchanged")
        try:
            _append_ledger({
                "event": "goal-status-change",
                "goal_id": goal_id,
                "new_status": b.status,
                "actor": actor,
                "reason": (b.reason or None),
            })
        except Exception:
            log.exception("ledger append failed for goal-status-change")
        return {"ok": True, "goal_id": goal_id, "status": b.status}
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except HTTPException:
        raise
    except Exception as e:
        log.exception("goal status patch failed")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# v0.17 Phase E: Goal abandon endpoint (ergonomic wrapper over PATCH /status)
# ---------------------------------------------------------------------------

class GoalAbandonIn(BaseModel):
    actor: str
    reason: str


@app.patch("/v1/goals/{goal_id}/abandon")
def abandon_goal_endpoint(goal_id: int, b: GoalAbandonIn, x_api_key: Optional[str] = Header(None)):
    """v0.17 Phase E: ergonomic abandon endpoint. Equivalent to PATCH /status with status='abandoned'
    but requires non-empty reason (this is a deliberate trash-can move; document why)."""
    auth(x_api_key)
    if not (b.actor or "").strip():
        raise HTTPException(400, "actor is required")
    if not (b.reason or "").strip():
        raise HTTPException(400, "reason is required for abandon (deliberate trash-can move; document why)")
    try:
        with _episodic_connect() as conn:
            ok = _episodic_update_goal_status(conn, goal_id, "abandoned")
        if not ok:
            raise HTTPException(404, f"goal {goal_id} not found")
        try:
            _append_ledger({
                "event": "goal-abandoned",
                "goal_id": goal_id,
                "actor": b.actor,
                "reason": b.reason,
            })
        except Exception:
            log.exception("ledger append failed for goal-abandoned")
        return {"ok": True, "goal_id": goal_id, "status": "abandoned"}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("goal abandon failed")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# v0.22 Phase A: Goal complete endpoint (ergonomic wrapper over PATCH /status)
# OQ#636: shipped goals must close as 'completed' (goal achieved) — distinct from
# 'abandoned' (scope-dropped/infeasible). Mirrors /abandon exactly: same auth
# (plain API key + required actor + required non-empty reason), but stamps a
# dedicated 'goal-completed' ledger event and sets completed_at via the episodic
# update (status=='completed' path). No trusted-field mutation, so no extra gate
# beyond the abandon path — fail-closed invariants are unchanged.
# ---------------------------------------------------------------------------

class GoalCompleteIn(BaseModel):
    actor: str
    reason: str


@app.patch("/v1/goals/{goal_id}/complete")
def complete_goal_endpoint(goal_id: int, b: GoalCompleteIn, x_api_key: Optional[str] = Header(None)):
    """v0.22 Phase A: ergonomic complete endpoint. Equivalent to PATCH /status with
    status='completed' but requires a non-empty reason (a deliberate lifecycle close;
    document what shipped). Sets completed_at and appends a goal-completed ledger event."""
    auth(x_api_key)
    if not (b.actor or "").strip():
        raise HTTPException(400, "actor is required")
    if not (b.reason or "").strip():
        raise HTTPException(400, "reason is required for complete (deliberate lifecycle close; document what shipped)")
    try:
        with _episodic_connect() as conn:
            ok = _episodic_update_goal_status(conn, goal_id, "completed")
        if not ok:
            raise HTTPException(404, f"goal {goal_id} not found")
        try:
            _append_ledger({
                "event": "goal-completed",
                "goal_id": goal_id,
                "actor": b.actor,
                "reason": b.reason,
            })
        except Exception:
            log.exception("ledger append failed for goal-completed")
        return {"ok": True, "goal_id": goal_id, "status": "completed"}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("goal complete failed")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# v0.17 Phase F.3.2: Goal management endpoints — priority, link_episode, merge
# ---------------------------------------------------------------------------

class GoalPriorityIn(BaseModel):
    priority: int  # 1-5; 1 = highest
    actor: str
    reason: Optional[str] = None


class GoalLinkEpisodeIn(BaseModel):
    episode_id: int
    link_type: str = "advanced_goal"  # advanced_goal | blocked_goal | completed_goal | cited_goal
    delta_text: Optional[str] = None
    actor: str


class GoalMergeIn(BaseModel):
    target_goal_id: int  # the goal to merge INTO
    actor: str
    reason: str


@app.patch("/v1/goals/{goal_id}/priority")
def patch_goal_priority_endpoint(goal_id: int, b: GoalPriorityIn, x_api_key: Optional[str] = Header(None)):
    """v0.17 F.3.2: update a goal's priority (1=highest, 5=lowest)."""
    auth(x_api_key)
    if not (b.actor or "").strip():
        raise HTTPException(400, "actor required")
    if b.priority < 1 or b.priority > 5:
        raise HTTPException(400, "priority must be 1-5 (1=highest)")
    try:
        with _episodic_connect() as conn:
            cur = conn.execute(
                "UPDATE goals SET priority = ?, updated_at = ? WHERE id = ?",
                (b.priority, _dt.datetime.now(_dt.timezone.utc).isoformat(), goal_id),
            )
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, f"goal {goal_id} not found")
        try:
            _append_ledger({
                "event": "goal-priority-change",
                "goal_id": goal_id,
                "new_priority": b.priority,
                "actor": b.actor,
                "reason": b.reason,
            })
        except Exception:
            log.exception("ledger append failed for goal-priority-change")
        return {"ok": True, "goal_id": goal_id, "priority": b.priority}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("goal priority patch failed")
        raise HTTPException(500, str(e))


@app.post("/v1/goals/{goal_id}/link_episode")
def link_goal_to_episode_endpoint(goal_id: int, b: GoalLinkEpisodeIn, x_api_key: Optional[str] = Header(None)):
    """v0.17 F.3.2: explicitly link an episode to a goal.
    link_type ∈ {advanced_goal, blocked_goal, completed_goal, cited_goal}.
    Use when a session advanced a goal but auto-extraction missed it."""
    auth(x_api_key)
    if not (b.actor or "").strip():
        raise HTTPException(400, "actor required")
    try:
        with _episodic_connect() as conn:
            g = _episodic_get_goal(conn, goal_id)
            if not g:
                raise HTTPException(404, f"goal {goal_id} not found")
            ep_check = conn.execute("SELECT id FROM episodes WHERE id = ?", (b.episode_id,)).fetchone()
            if not ep_check:
                raise HTTPException(404, f"episode {b.episode_id} not found")
            link_id = _episodic_link_episode_to_goal(
                conn, b.episode_id, goal_id, link_type=b.link_type, delta_text=b.delta_text
            )
        return {"ok": True, "link_id": link_id, "goal_id": goal_id, "episode_id": b.episode_id}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("goal link episode failed")
        raise HTTPException(500, str(e))


@app.post("/v1/goals/{source_goal_id}/merge")
def merge_goals_endpoint(
    source_goal_id: int, b: GoalMergeIn,
    x_api_key: Optional[str] = Header(None),
    x_user_direct_token: Optional[str] = Header(None, alias="X-User-Direct-Token"),
    x_user_direct_ts: Optional[str] = Header(None, alias="X-User-Direct-Ts"),
    x_user_direct_nonce: Optional[str] = Header(None, alias="X-User-Direct-Nonce"),
):
    """v0.17 F.3.2: merge source goal into target.
    Moves all episode_links from source to target; marks source as 'duplicate' status.
    The source goal stays in the DB for audit but won't appear in default listings.

    v0.18 MED-9: merges relinking more than GOAL_MERGE_HMAC_THRESHOLD (100)
    episode_links require actor='user-direct' plus a valid HMAC user-direct token
    + nonce (format 2, action='merge_goals', memory_id slot = source goal id) —
    bulk-tamper guard. Smaller merges keep plain API-key auth."""
    auth(x_api_key)
    if not (b.actor or "").strip() or not (b.reason or "").strip():
        raise HTTPException(400, "actor and reason required for merge")
    if source_goal_id == b.target_goal_id:
        raise HTTPException(400, "cannot merge a goal into itself")
    try:
        with _episodic_connect() as conn:
            source_g = _episodic_get_goal(conn, source_goal_id)
            target_g = _episodic_get_goal(conn, b.target_goal_id)
            if not source_g:
                raise HTTPException(404, f"source goal {source_goal_id} not found")
            if not target_g:
                raise HTTPException(404, f"target goal {b.target_goal_id} not found")
            # v0.18 MED-9: count links BEFORE merging; gate bulk relinks behind HMAC.
            link_count = conn.execute(
                "SELECT COUNT(*) FROM episode_links WHERE target_kind = 'goal' AND target_id = ?",
                (str(source_goal_id),),
            ).fetchone()[0]
            if link_count > GOAL_MERGE_HMAC_THRESHOLD:
                if (b.actor or "").strip().lower() != "user-direct":
                    raise HTTPException(
                        403,
                        f"merge would relink {link_count} episode_links "
                        f"(> {GOAL_MERGE_HMAC_THRESHOLD}); bulk merges require "
                        f"actor='user-direct' (got actor={b.actor!r})",
                    )
                from security_invariants import validate_hmac_user_direct
                validate_hmac_user_direct(
                    str(source_goal_id), "merge_goals", b.reason,
                    x_user_direct_token, x_user_direct_ts,
                    x_user_direct_nonce=x_user_direct_nonce,
                )
            # Re-target episode_links from source → target
            cur = conn.execute(
                "UPDATE episode_links SET target_id = ? WHERE target_kind = 'goal' AND target_id = ?",
                (str(b.target_goal_id), str(source_goal_id)),
            )
            relinked = cur.rowcount
            # Mark source as duplicate (bypass VALID_GOAL_STATUSES — 'duplicate' is merge-only)
            conn.execute(
                "UPDATE goals SET status = 'duplicate', updated_at = ? WHERE id = ?",
                (_dt.datetime.now(_dt.timezone.utc).isoformat(), source_goal_id),
            )
            conn.commit()
        try:
            _append_ledger({
                "event": "goal-merged",
                "source_goal_id": source_goal_id,
                "target_goal_id": b.target_goal_id,
                "relinked_episodes": relinked,
                "actor": b.actor,
                "reason": b.reason,
            })
        except Exception:
            log.exception("ledger append failed for goal-merged")
        return {
            "ok": True,
            "source_goal_id": source_goal_id,
            "target_goal_id": b.target_goal_id,
            "relinked_episodes": relinked,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("goal merge failed")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# v0.17 Phase D: Open Questions models + endpoints
# IMPORTANT: /v1/open_questions/search (POST) must come BEFORE /v1/open_questions/{oq_id} (GET)
# to avoid FastAPI parsing 'search' as an integer oq_id.
# ---------------------------------------------------------------------------

class OpenQuestionIn(BaseModel):
    question_text: str
    brand: Optional[str] = None
    topic: Optional[str] = None
    first_seen_session_id: Optional[str] = None
    first_seen_episode_id: Optional[int] = None
    related_goal_id: Optional[int] = None
    priority: int = 3
    initiative: Optional[str] = None  # v0.22 Pillar 1: cwd-derived initiative; None == cross-cutting


class OpenQuestionResolveIn(BaseModel):
    resolved_in_session_id: str
    resolution_text: str
    actor: str


class OpenQuestionStatusIn(BaseModel):
    status: str
    actor: str
    reason: Optional[str] = None


class OpenQuestionSearchIn(BaseModel):
    query: str
    brand: Optional[str] = None
    status: Optional[str] = "open"
    limit: int = 20


@app.post("/v1/open_questions")
def create_open_question_endpoint(b: OpenQuestionIn, x_api_key: Optional[str] = Header(None)):
    """Create an open question manually. Returns {ok, open_question_id}."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            oqid = _episodic_create_open_question(
                conn, question_text=b.question_text, brand=b.brand, topic=b.topic,
                first_seen_session_id=b.first_seen_session_id,
                first_seen_episode_id=b.first_seen_episode_id,
                related_goal_id=b.related_goal_id, priority=b.priority,
                initiative=b.initiative,  # v0.22 Pillar 1
            )
        return {"ok": True, "open_question_id": oqid}
    except Exception as e:
        log.exception("open_question create failed")
        raise HTTPException(500, str(e))


@app.get("/v1/open_questions")
def list_open_questions_endpoint(
    status: str = "open", brand: Optional[str] = None, limit: int = 20,
    initiative: Optional[str] = None,
    x_api_key: Optional[str] = Header(None),
):
    """List open questions with status + brand filters. Default status='open'.

    v0.22 Pillar 1: initiative (when provided) scopes to that initiative +
    cross-cutting (NULL) rows. Omitted == unfiltered on initiative.
    """
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_list_open_questions(conn, status=status, brand=brand, limit=limit, initiative=initiative)
    except Exception as e:
        log.exception("open_questions list failed")
        raise HTTPException(500, str(e))


@app.post("/v1/open_questions/search")
def search_open_questions_endpoint(b: OpenQuestionSearchIn, x_api_key: Optional[str] = Header(None)):
    """FTS5 keyword search across open questions. status='all' to include resolved."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_search_open_questions(conn, query=b.query, brand=b.brand, status=b.status, limit=b.limit)
    except Exception as e:
        log.exception("open_questions search failed")
        raise HTTPException(500, str(e))


@app.get("/v1/open_questions/{oq_id}")
def get_open_question_endpoint(oq_id: int, x_api_key: Optional[str] = Header(None)):
    """Fetch a single open question by integer id."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            oq = _episodic_get_open_question(conn, oq_id)
        if not oq:
            raise HTTPException(404, f"open_question {oq_id} not found")
        return oq
    except HTTPException:
        raise
    except Exception as e:
        log.exception("open_question get failed")
        raise HTTPException(500, str(e))


@app.patch("/v1/open_questions/{oq_id}/resolve")
def resolve_open_question_endpoint(oq_id: int, b: OpenQuestionResolveIn, x_api_key: Optional[str] = Header(None)):
    """Mark a frontier question as resolved with a resolution summary."""
    auth(x_api_key)
    if not (b.actor or "").strip():
        raise HTTPException(400, "actor is required")
    try:
        with _episodic_connect() as conn:
            ok = _episodic_resolve_open_question(
                conn, oq_id=oq_id,
                resolved_in_session_id=b.resolved_in_session_id,
                resolution_text=b.resolution_text,
            )
        if not ok:
            raise HTTPException(404, f"open_question {oq_id} not found or already resolved")
        try:
            _append_ledger({
                "event": "open-question-resolved",
                "open_question_id": oq_id,
                "actor": b.actor,
                "session_id": b.resolved_in_session_id,
                "resolution_preview": b.resolution_text[:200],
            })
        except Exception:
            log.exception("ledger append failed for open-question-resolved")
        return {"ok": True, "open_question_id": oq_id, "status": "resolved"}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("open_question resolve failed")
        raise HTTPException(500, str(e))


@app.patch("/v1/open_questions/{oq_id}/status")
def patch_open_question_status_endpoint(oq_id: int, b: OpenQuestionStatusIn, x_api_key: Optional[str] = Header(None)):
    """Transition open question to abandoned or duplicate status."""
    auth(x_api_key)
    if not (b.actor or "").strip():
        raise HTTPException(400, "actor is required")
    try:
        with _episodic_connect() as conn:
            ok = _episodic_update_open_question_status(conn, oq_id, b.status)
        if not ok:
            raise HTTPException(404, f"open_question {oq_id} not found")
        try:
            _append_ledger({
                "event": "open-question-status-change",
                "open_question_id": oq_id,
                "new_status": b.status,
                "actor": b.actor,
                "reason": b.reason,
            })
        except Exception:
            log.exception("ledger append failed for open-question-status-change")
        return {"ok": True, "open_question_id": oq_id, "status": b.status}
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as e:
        log.exception("open_question status patch failed")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# v0.15: Episode endpoints
# IMPORTANT: /v1/episodes/checkpoint (v0.17) must come FIRST, then /v1/episodes/count
# and /v1/episodes/search (POST), then /v1/episodes/{episode_id}.
# FastAPI matches routes in registration order.
# ---------------------------------------------------------------------------

def _checkpoint_core(b: EpisodeCheckpointIn) -> dict:
    """v0.20 A.3: checkpoint internals shared by POST /v1/episodes/checkpoint
    and POST /v1/context/bundle (which performs the upsert as a server-side
    side effect so the hook needs one round-trip instead of two). Raises on
    failure; callers map exceptions to HTTP."""
    # Security: scrub credential-shaped substrings from prompt_text before it is persisted in the
    # episode checkpoint — the single chokepoint for BOTH /v1/episodes/checkpoint and the daemon's
    # /v1/context/bundle, so no client can store a pasted key/token regardless of which path it took.
    with _episodic_connect() as conn:
        episode_id, action = _episodic_upsert_checkpoint(
            conn,
            session_id=b.session_id,
            transcript_path=b.transcript_path,
            brand=b.brand,
            workspace=b.workspace,
            project=b.project,
            prompt_text=redact_secrets(b.prompt_text),
            commit=True,
        )
    return {"ok": True, "episode_id": episode_id, "action": action, "state": "in_progress"}


@app.post("/v1/episodes/checkpoint")
def episode_checkpoint(b: EpisodeCheckpointIn, x_api_key: Optional[str] = Header(None)):
    """v0.17 Phase 0.A: within-session checkpoint via UserPromptSubmit hook.

    Upserts an in_progress episode for the session. The Stop hook later finalizes
    the episode to state='complete' via POST /v1/episodes (which now calls
    finalize_episode instead of always inserting a new row).

    This endpoint is deliberately fast: no Codex calls, no heavy I/O.
    """
    auth(x_api_key)
    _warn_hook_contract_version("/v1/episodes/checkpoint", b.hook_contract_version)
    try:
        return _checkpoint_core(b)
    except Exception as e:
        log.exception("episode checkpoint failed")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# v0.20 Phase A.3: batched context bundle for the UserPromptSubmit hook.
# The hook previously made 4+ sequential HTTP round-trips per prompt
# (checkpoint + search + goals + open_questions); this endpoint returns all
# of it in ONE response and performs the episode-checkpoint upsert as a
# server-side side effect. Latency directive 2026-06-12 ("strong memory but
# also interactive and efficient").
# ---------------------------------------------------------------------------

class ContextBundleIn(BaseModel):
    session_id: str
    prompt: str                                # snippet used for search + checkpoint
    brand: Optional[str] = None
    workspace: Optional[str] = None
    project: Optional[str] = None
    # v0.22 Pillar 1: cwd-derived initiative (repo leaf). When set, goals/OQ are
    # scoped to this initiative + cross-cutting (NULL) rows so an open goal from
    # another initiative under the same brand never bleeds in. None == unscoped.
    initiative: Optional[str] = None
    # v0.22 Pillar 2 (D4): the consuming model's injection tier (frontier|mid|
    # small), resolved hook-side from the SessionStart model field / transcript.
    # ACCEPTED-BUT-UNUSED this phase — detection + plumbing only. Phase D reads it
    # to scale per-tier caps/threshold/format; until then the bundle is identical
    # regardless of tier (frontier == today's behavior). Default frontier.
    tier: Optional[str] = "frontier"
    transcript_path: Optional[str] = None
    hook_contract_version: Optional[str] = None
    # v1.0 A1 (mandated-pull): the memory_recall MCP verb pulls the bundle on demand
    # because the per-turn UserPromptSubmit hook is dead in the VS Code / Agent-SDK
    # runtime. A manual pull MUST NOT upsert an episode or every recall would pollute
    # the SessionStart resume banner with a synthetic session, so it passes
    # checkpoint=False. The hook path omits it (default True) and keeps the original
    # checkpoint-first contract unchanged.
    checkpoint: bool = True


@app.post("/v1/context/bundle")
def context_bundle(b: ContextBundleIn, x_api_key: Optional[str] = Header(None)):
    """One-round-trip context bundle for the UserPromptSubmit hook.

    Response: {ok, checkpoint: {ok, episode_id, action, state}, memories: [...],
    goals: [...], open_questions: [...]}.

    Ordering guarantee (v0.21 L6): the checkpoint upsert runs BEFORE the search,
    each in its own try/except, so a search failure (incl. an empty or oversized
    prompt) can never lose the episode checkpoint — pinned by
    test_bundle_empty_prompt_degrades_not_500 / test_bundle_oversized_prompt_truncated.

    - `memories` go through _search_core — the EXACT pipeline the hook's
      separate search POST used (retired/intent filters, query_class policy,
      apply_admission, retrieval logging). No parallel ungated path exists.
    - The checkpoint upsert reuses _checkpoint_core (same as
      POST /v1/episodes/checkpoint) and runs FIRST so a search failure can
      never lose the episode checkpoint.
    - goals/open_questions reuse the same episodic queries as GET /v1/goals
      and GET /v1/open_questions with the hook's historical parameters
      (status=open, limit 5/3, optional brand).
    - Sections degrade independently: a failing section returns empty/ok=False
      rather than failing the bundle (mirrors the hook's per-call try/catch).
    """
    auth(x_api_key)
    _warn_hook_contract_version("/v1/context/bundle", b.hook_contract_version)
    out: dict[str, Any] = {"ok": True}

    # v0.22 Phase D: scale the bundle by the consuming model's tier. Unknown/None
    # -> frontier (fail-open, never under-serve). v1.0 R2 frontier values are
    # 2 memories / 5 goals / 3 OQ @ 0.30 (see TIER_BUNDLE_POLICY).
    _tp = resolve_tier_policy(b.tier)

    # 1) episode checkpoint (side effect) — first, never skipped on the hook path.
    #    v1.0 A1: a manual memory_recall pull passes checkpoint=False to suppress the
    #    upsert (no synthetic session in the resume banner); the gated search below
    #    still runs, so a pull returns the identical memories/goals/open_questions the
    #    hook would have injected — only the episode write is skipped.
    if not b.checkpoint:
        out["checkpoint"] = {"ok": True, "skipped": True}
    else:
        try:
            out["checkpoint"] = _checkpoint_core(EpisodeCheckpointIn(
                session_id=b.session_id,
                transcript_path=b.transcript_path,
                prompt_text=(b.prompt or "")[:300],
                brand=b.brand,
                workspace=b.workspace,
                project=b.project,
            ))
        except Exception:
            log.exception("bundle: checkpoint failed (non-fatal)")
            out["checkpoint"] = {"ok": False}

    # 2) admission-gated proactive search (same parameters the hook used:
    #    user_id=DEFAULT_USER_ID, optional brand, limit = memory_cap (tier-scaled),
    #    threshold = relevance_threshold (tier-scaled), no rerank, durable class)
    # v0.22 EmbeddingGemma migration lowered 0.4 -> 0.30. v1.0 R2 KEEPS 0.30 (both
    # tiers) and caps K at 2/1 — the calibration found this threshold gates the
    # HYBRID-search SEMANTIC score (not the higher combined score it returns), whose
    # EmbeddingGemma separation is compressed (off-domain <=0.12, relevant 0.25-0.57),
    # so 0.30 is already correctly placed and a raise would crater recall. See the
    # TIER_BUNDLE_POLICY comment above + eval/injection-gating/. When nothing clears
    # 0.30 the search returns zero memories and the hook emits NO block (abstention).
    # This is the embedding-similarity threshold only — NOT the reranker's
    # MEM0_RELEVANCE_FLOOR_OPERATIONAL.
    try:
        filters: dict[str, Any] = {"user_id": DEFAULT_USER_ID}
        if b.brand:
            filters["brand"] = b.brand
        sr = _search_core(SearchIn(
            query=(b.prompt or "")[:500],
            filters=filters,
            limit=_tp["memory_cap"],            # v1.0 R2: tier-scaled K (frontier 2 / small 1)
            threshold=_tp["relevance_threshold"],  # v1.0 R2: 0.30 both tiers (kept; calibration-confirmed)
            rerank=False,
            query_class="durable",
        ))
        out["memories"] = sr.get("results", []) if isinstance(sr, dict) else []
    except Exception:
        log.exception("bundle: search failed (non-fatal)")
        out["memories"] = []

    # 2b) v0.29 R4 — raw-trace fallback. Only when the condensed semantic search
    # admitted NOTHING (low-confidence) do we attempt a SEMANTIC-cosine match
    # against a past episode and surface ONE compact snippet. The gate (raw cosine
    # >= RAW_FALLBACK_COSINE_FLOOR + fail-closed brand; lexical/bm25 was disproven
    # live, see the v0.29 CHANGELOG) keeps R2 abstention intact for off-domain
    # prompts. Never blocks the bundle. v0.29.3: enabled by default (its episodic
    # test-pollution gate was cleared + live-verified in v0.29.2).
    if RAW_FALLBACK_ENABLED and not out.get("memories"):
        try:
            rf = _episode_raw_fallback((b.prompt or "")[:500], b.brand)
            if rf:
                out["raw_fallback"] = rf
        except Exception:
            log.exception("bundle: raw-trace fallback failed (non-fatal)")

    # 3) open goals (5) + 4) open frontier questions (3)
    try:
        with _episodic_connect() as conn:
            # v0.21 Phase A (M2): fail closed on an unknown-brand session —
            # serve only brand-neutral (NULL-brand) goals/OQ, mirroring the
            # memory Layer-2 brand gate, so cross-brand goals/questions never
            # leak into an unrecognized session.
            # v0.22 Pillar 1: ADDITIONALLY scope by the session's initiative —
            # the request's initiative + cross-cutting (NULL) rows only — so a
            # goal/OQ from another initiative under the SAME brand never bleeds
            # in. initiative=None (unknown initiative) leaves it unscoped, exactly
            # as before. Initiative scoping is additive to the brand gate, not a
            # replacement: both fail-closed semantics still apply.
            # Normalize brand once so a whitespace-only brand collapses to unknown
            # (fail-closed) before deriving only_brand_neutral — `not "  "` is False
            # otherwise, dropping the gate to admit-all (audit MED, goals/OQ variant).
            _bb = b.brand.strip() if isinstance(b.brand, str) else b.brand
            out["goals"] = _episodic_list_goals(conn, status="open", brand=_bb, only_brand_neutral=(not _bb), initiative=b.initiative, limit=_tp["goal_cap"])
            out["open_questions"] = _episodic_list_open_questions(conn, status="open", brand=_bb, only_brand_neutral=(not _bb), initiative=b.initiative, limit=_tp["oq_cap"])
    except Exception:
        log.exception("bundle: goals/open_questions failed (non-fatal)")
        out.setdefault("goals", [])
        out.setdefault("open_questions", [])
    return out


@app.post("/v1/episodes")
def create_episode(b: EpisodeIn, x_api_key: Optional[str] = Header(None)):
    """Write one episode (session goal + summary) to episodic.db.
    Called automatically by the L1a Stop hook at session end.
    v0.16: also processes advanced_goals / blocked_goals / open_questions."""
    import json as _json
    from episodic import end_session as _episodic_end_session
    auth(x_api_key)
    _warn_hook_contract_version("/v1/episodes", b.hook_contract_version)
    try:
        with _episodic_connect() as conn:
            try:
                _episodic_create_session(
                    conn, b.session_id, b.transcript_path,
                    b.brand, b.workspace, b.project, b.started_at,
                    commit=False,
                )
                # v0.17 Phase 0: finalize_episode transitions the in_progress episode
                # (created by UserPromptSubmit hook) to state='complete'.
                # If no in_progress episode exists (e.g. hook wasn't firing yet or direct
                # API call), it inserts a new complete row — backward compat preserved.
                episode_id = _episodic_finalize_episode(
                    conn, b.session_id, b.goal, b.summary,
                    b.ended_at, (b.message_count or 0),
                    commit=False,
                )
                if b.linked_memory_ids:
                    for mid in b.linked_memory_ids:
                        _episodic_add_link(conn, episode_id, "produced_evidence", mid, "mem0", commit=False)
                _episodic_end_session(conn, b.session_id, b.ended_at, (b.message_count or 0), commit=False)

                # v0.16: process advanced_goals / blocked_goals / open_questions
                advanced_serialized = []
                if b.advanced_goals:
                    for item in b.advanced_goals:
                        if not item.goal_title or not item.goal_title.strip():
                            continue
                        # Fuzzy-match in same brand (NULL-safe — HIGH-4 fix)
                        candidates = _episodic_find_goal_by_title_fuzzy(conn, item.goal_title, brand=b.brand, limit=1)
                        if candidates:
                            goal_id = candidates[0]["id"]
                            # MED-B: if goal was previously blocked, unblock it on advance signal
                            if candidates[0].get("status") == "blocked":
                                _episodic_update_goal_status(conn, goal_id, "open", commit=False)
                        else:
                            goal_id = _episodic_create_goal(
                                conn, title=item.goal_title.strip(),
                                description=item.delta_text,
                                brand=b.brand, priority=3,
                                first_seen_session_id=b.session_id,
                                initiative=b.initiative,  # v0.22 Pillar 1: stamp the session's initiative
                                commit=False,
                            )
                        _episodic_link_episode_to_goal(conn, episode_id, goal_id, link_type="advanced_goal", delta_text=item.delta_text, commit=False)
                        advanced_serialized.append({"goal_id": goal_id, "delta_text": item.delta_text})

                blocked_serialized = []
                if b.blocked_goals:
                    for item in b.blocked_goals:
                        if not item.goal_title or not item.goal_title.strip():
                            continue
                        candidates = _episodic_find_goal_by_title_fuzzy(conn, item.goal_title, brand=b.brand, limit=1)
                        if candidates:
                            goal_id = candidates[0]["id"]
                        else:
                            goal_id = _episodic_create_goal(
                                conn, title=item.goal_title.strip(),
                                description=item.block_reason,
                                brand=b.brand, priority=3,
                                first_seen_session_id=b.session_id,
                                initiative=b.initiative,  # v0.22 Pillar 1: stamp the session's initiative
                                commit=False,
                            )
                        _episodic_link_episode_to_goal(conn, episode_id, goal_id, link_type="blocked_goal", delta_text=item.block_reason, commit=False)
                        # Flip status to blocked
                        _episodic_update_goal_status(conn, goal_id, "blocked", commit=False)
                        blocked_serialized.append({"goal_id": goal_id, "block_reason": item.block_reason})

                # Filtered open_questions (skip blank strings)
                oq_filtered = [q for q in (b.open_questions or []) if q and q.strip()]

                # v0.17 Phase D: promote per-episode open_questions to global registry
                if oq_filtered:
                    for q_text in oq_filtered:
                        # Dedupe via FTS5 fuzzy match
                        candidates = _episodic_find_open_question_by_text_fuzzy(
                            conn, q_text, brand=b.brand, status="open", limit=1,
                        )
                        if candidates:
                            continue  # already tracked
                        _episodic_create_open_question(
                            conn, question_text=q_text.strip(),
                            brand=b.brand,
                            first_seen_session_id=b.session_id,
                            first_seen_episode_id=episode_id,
                            priority=3,
                            initiative=b.initiative,  # v0.22 Pillar 1: stamp the session's initiative
                            commit=False,  # part of atomic episode POST transaction
                        )

                # Update episodes JSON columns (only set non-empty; leave None if nothing to store)
                conn.execute(
                    "UPDATE episodes SET advanced_goals = ?, blocked_goals = ?, open_questions = ? WHERE id = ?",
                    (
                        _json.dumps(advanced_serialized) if advanced_serialized else None,
                        _json.dumps(blocked_serialized) if blocked_serialized else None,
                        _json.dumps(oq_filtered) if oq_filtered else None,
                        episode_id,
                    ),
                )
                # Single atomic commit for the entire episode POST (HIGH-5)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        # v0.29 R4: index the finalized episode's summary into the semantic
        # episodes collection so the low-confidence raw-trace fallback can find it.
        # Fail-soft — the committed SQLite episode is the source of truth; a Qdrant
        # hiccup must never fail the episode write (and never re-embeds the noisy
        # in_progress checkpoint, since this fires once per episode at finalize).
        try:
            if _episode_indexable_summary(b.summary):
                _ep_vec = embed_episode_summary(mem.embedding_model, b.summary)
                if _ep_vec is not None:
                    upsert_episode_embedding(
                        mem.vector_store.client, episode_id, _ep_vec,
                        {"brand": b.brand, "goal": (b.goal or "")[:300],
                         "summary": (b.summary or "")[:800]},
                    )
        except Exception:
            log.exception("episode embed/upsert failed (non-fatal)")

        return {"ok": True, "session_id": b.session_id, "episode_id": episode_id}
    except Exception as e:
        log.exception("episode create failed")
        raise HTTPException(500, str(e))


@app.post("/v1/episodes/search")
def search_episodes(b: EpisodeSearchIn, x_api_key: Optional[str] = Header(None)):
    """FTS5 keyword search over episode goal + summary text.
    Optional date range (since/until ISO 8601) and brand filter."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            results = _episodic_search_fts(conn, b.query, b.since, b.until, b.brand, b.limit)
        return {"results": results, "count": len(results)}
    except Exception as e:
        log.exception("episode search failed")
        raise HTTPException(500, str(e))


@app.get("/v1/episodes/count")
def episodes_count(
    since: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None),
):
    """Return {count, last_ended_at} for health checks and Test-MemoryStack."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_count(conn, since, brand)
    except Exception as e:
        log.exception("episode count failed")
        raise HTTPException(500, str(e))


@app.get("/v1/episodes")
def list_episodes(
    recent: int = Query(10),
    brand: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None),
):
    """List last N episodes by ended_at desc. Default recent=10."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_recent(conn, recent, brand)
    except Exception as e:
        log.exception("episode list failed")
        raise HTTPException(500, str(e))


@app.get("/v1/episodes/{episode_id}")
def get_episode_endpoint(episode_id: int, x_api_key: Optional[str] = Header(None)):
    """Fetch a single episode by integer id, including linked mem0 memory IDs."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            ep = _episodic_get(conn, episode_id)
        if not ep:
            raise HTTPException(404, f"episode {episode_id} not found")
        return ep
    except HTTPException:
        raise
    except Exception as e:
        log.exception("episode get failed")
        raise HTTPException(500, str(e))


@app.delete("/v1/memories/{mid}")
def delete(
    mid: str,
    x_api_key: Optional[str] = Header(None),
    x_user_direct_token: Optional[str] = Header(None, alias="X-User-Direct-Token"),
    x_user_direct_ts: Optional[str] = Header(None, alias="X-User-Direct-Ts"),
    x_user_direct_nonce: Optional[str] = Header(None, alias="X-User-Direct-Nonce"),
    actor: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
    cascade: bool = Query(False),
):
    """Hard delete. ALWAYS append a `delete` ledger entry so destructive ops are audit-covered.
    v0.17 Phase A: canonical/insight tier gate applied BEFORE deletion.
    v0.17 Phase F.1: X-User-Direct-Nonce for replay protection; cascade=true for delete_linked."""
    auth(x_api_key)
    # v0.17 Phase A: canonical/insight tier write-path gate (runs BEFORE the Qdrant retrieve below
    # so the 403 fires fast without fetching the payload a second time — assert_writable fetches
    # the tier internally; we accept the cost of one extra Qdrant retrieve for the prior_payload
    # below, which is needed for the ledger's prior_source field).
    # v0.17 Phase F.1: nonce forwarded for replay protection
    from security_invariants import assert_writable
    prior_tier = assert_writable(
        mem.vector_store.client, mem.vector_store.collection_name, mid,
        "delete", x_user_direct_token, x_user_direct_ts,
        actor=(actor or ""), reason=(reason or ""),
        x_user_direct_nonce=x_user_direct_nonce,
    )
    # Fetch payload for restore-info BEFORE deletion (separate from assert_writable's fetch)
    prior_payload = None
    try:
        prior = mem.vector_store.client.retrieve(
            collection_name=mem.vector_store.collection_name,
            ids=[mid],
            with_payload=True, with_vectors=False,
        )
        if prior:
            prior_payload = (prior[0].payload if hasattr(prior[0], 'payload') else prior[0].get('payload'))
    except Exception:
        pass  # if retrieve fails, delete still proceeds; ledger gets minimal record
    # H6/H11 fix: mem0ai 2.0.4 signature is mem.delete(memory_id) -- no delete_linked kwarg.
    # Cascade is implemented here: query Qdrant for superseded-by chain, delete each member
    # individually (non-cascade via mem0 API), and write a separate ledger entry per deletion
    # so the chain is fully reversible. The root deletion is the last step.
    if cascade:
        # Walk the supersession chain rooted at `mid`.
        # Convention: a superseded record has payload.superseded_by == <newer_mid>.
        # We collect ALL IDs in the chain (ancestors of mid that point to it directly or
        # transitively) plus mid itself. Each gets its own delete + ledger entry.
        chain_ids: list[str] = []
        try:
            # Scroll all points where superseded_by == mid to find direct ancestors.
            # (Simple 1-level walk; deeper chains are rare but handled by the loop below.)
            _to_visit = [mid]
            _visited: set[str] = set()
            while _to_visit:
                _cid = _to_visit.pop()
                if _cid in _visited:
                    continue
                _visited.add(_cid)
                # Find points that have superseded_by == _cid in payload
                try:
                    scroll_result = mem.vector_store.client.scroll(
                        collection_name=mem.vector_store.collection_name,
                        scroll_filter={
                            "must": [{"key": "superseded_by", "match": {"value": _cid}}]
                        },
                        with_payload=True,
                        with_vectors=False,
                        limit=100,
                    )
                    ancestors = scroll_result[0] if scroll_result else []
                    for anc in ancestors:
                        anc_id = str(anc.id)
                        if anc_id not in _visited:
                            chain_ids.append(anc_id)
                            _to_visit.append(anc_id)
                except Exception:
                    log.warning("H11: could not scroll supersession chain for %s; continuing", _cid)
        except Exception:
            log.warning("H11: chain walk failed for mid=%s; falling back to single delete", mid)

        # Delete ancestors first (oldest end of chain), then the root (mid)
        _cascade_actor = actor or "rest-api"
        _cascade_reason = reason or f"cascade DELETE /v1/memories/{mid}"
        for _linked_id in chain_ids:
            try:
                _linked_payload = None
                try:
                    _lp = mem.vector_store.client.retrieve(
                        collection_name=mem.vector_store.collection_name,
                        ids=[_linked_id], with_payload=True, with_vectors=False,
                    )
                    if _lp:
                        _linked_payload = _lp[0].payload if hasattr(_lp[0], "payload") else _lp[0].get("payload")
                except Exception:
                    pass
                mem.delete(memory_id=_linked_id)
                try:
                    _append_ledger({
                        "event": "delete",
                        "memory_id": _linked_id,
                        "actor": _cascade_actor,
                        "reason": f"cascade-chain member; root={mid}; {_cascade_reason}",
                        "prior_tier": (_linked_payload or {}).get("tier") if _linked_payload else None,
                        "prior_source": (_linked_payload or {}).get("source") if _linked_payload else None,
                        "prior_payload": _linked_payload,
                        "transport": "cli-user-direct" if x_user_direct_token else "rest-api",
                        "cascade": True,
                        "cascade_root_id": mid,
                    })
                except Exception:
                    log.exception("H11: ledger append failed for cascade chain member %s", _linked_id)
            except Exception as e:
                log.warning("H11: cascade delete of chain member %s failed: %s", _linked_id, e)

    # Delete the root memory (also the only delete when cascade=False)
    try:
        result = mem.delete(memory_id=mid)
    except Exception as e:
        log.exception("delete failed")
        raise HTTPException(500, str(e))
    try:
        _append_ledger({
            "event": "delete",
            "memory_id": mid,
            "actor": (actor or "rest-api"),
            "reason": (reason or "DELETE /v1/memories/{mid}"),
            "prior_tier": prior_tier or ((prior_payload or {}).get("tier") if prior_payload else None),
            "prior_source": (prior_payload or {}).get("source") if prior_payload else None,
            "prior_payload": prior_payload,
            "transport": "cli-user-direct" if x_user_direct_token else "rest-api",
            "cascade": cascade,
            "cascade_root_id": None,  # this IS the root
        })
    except Exception:
        log.exception("ledger append failed for delete")
    return result
