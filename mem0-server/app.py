"""mem0 v2.0.4 WSL-native FastAPI wrapper — v0.12 stack
Loopback only on 127.0.0.1:18791. X-API-Key auth.
Exposes same REST surface as official mem0 server (POST/GET/PUT/DELETE /v1/memories, POST /v1/memories/search).
"""
import os
import hmac
import json
import contextlib
import hashlib
import logging
import threading
import uuid as _uuid
import datetime as _dt
from pathlib import Path
from typing import Optional, Any, List

from fastapi import FastAPI, Header, HTTPException, Query, BackgroundTasks, Request
from pydantic import BaseModel, Field
from mem0 import Memory

from config import build_config, EMBEDDER_CONFIG, EMBED_PROFILE
import embedder_profile as _embedder_profile
from reranker import rerank as bge_rerank
# W4 (F11): PASSIVE rerank counters. There is deliberately NO active rerank
# probe on /health/deep — deploy.sh gates on this endpoint right after a
# restart, and a cold model behind a probe would block deploys (TMS budgets
# it 90s). Holds regardless of device: the reranker moved CPU->GPU 2026-08-13,
# but cold-load + llama-swap spawn can still exceed the deploy gate's window.
from reranker import rerank_health as _rerank_health
from reranker import warm as _rerank_warm, RAN_STATUSES as _RERANK_RAN_STATUSES
from admission_gate import apply_admission
# WP-4: contradicts_canonical stamps are enforced only while their target is still canonical; the
# gate resolves targets through this fetcher (registered below, once `mem` exists).
from admission_gate import set_stamp_tier_fetcher as _set_stamp_tier_fetcher
from admission_gate import resolve_stamp_tiers as _resolve_stamp_tiers
# W5 T1.3: the PURE evaluate path for the diagnose endpoint — never
# apply_admission there (it mutates the MEM-8 counters + audit log).
from admission_gate import default_policy_for_class
# MEM-8 (2026-07-03): daily rejection counters for /health/deep observability.
from admission_gate import admission_rejections_today as _admission_rejections_today
from redact import redact_secrets  # server-side secret scrub for stored prompt_text
from redact import count_redactions  # W5 T6.1: count-only entrance telemetry
from freshness import freshness_weight as _freshness_weight  # v1.0 R5 Weibull read-gate
import codex_shim_client  # v0.27.1 R5 keystone: Codex judgment via the Windows HTTP shim
import nli_write_gate     # v0.27.2 R5: NLI write-gate decision (pure; deps injected below)
from payload_carryover import compute_carryover  # AMS-01: PUT payload carry-over (P0)
from sparse_health import sparse_leg_health      # AMS-09: BM25 leg liveness (gating)
from sparse_health import encode_with_selfheal   # AMS-09b: bounded sentinel un-poison
# W5 T5 (AMS-56): the SAME lemmatizer that produced every stored
# text_lemmatized — exact store-side parity for the keyword union leg.
from mem0.utils.lemmatization import lemmatize_for_bm25 as _lemmatize_bm25
from mojibake_check import mojibake_health       # AMS-10: CP437 corpus tripwire
from mojibake_check import PAYLOAD_KEYS          # WP-4: fields the scan reads (text + the mojibake_ok allowlist)
from job_liveness import job_liveness_health     # W3: nightly-job receipt ages (informational)
from drift_state import drift_state_health       # W3: retrieval-drift guard state (informational)
from capabilities import evaluate as evaluate_capabilities  # W3: capability manifest (informational)
from capabilities import promotion_gate_health as _promotion_gate_health  # WP-4: effective gate mode
# W4: in-process admission self-probe. Calls AdmissionPolicy.evaluate() DIRECTLY —
# never apply_admission, which would bump the MEM-8 daily counters this endpoint
# reports and append to ~/.mem0/admission-rejected.jsonl on every health read.
from capabilities import admission_selfprobe as _admission_selfprobe
from episodic import (
    _connect as _episodic_connect,
    init_schema as _episodic_init_schema,
    create_session as _episodic_create_session,
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
    # AMS-57 — true counts for the health probes (a LIST page is not a total)
    count_goals as _episodic_count_goals,
    count_open_questions as _episodic_count_open_questions,
    # v0.17 Phase 0 — within-session checkpoint
    upsert_in_progress_episode as _episodic_upsert_checkpoint,
    capture_signals as _episodic_capture_signals,   # CRIT-01: authority-side capture liveness
    connect_readonly as _episodic_connect_readonly,
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
    DeferredEmbedGate,
    run_deferred_embed,
    _indexable_summary as _episode_indexable_summary,
)
# 1.32.4: finalize-time embeds that hit a cold embedder are retried in the background, capped (one per
# episode id, a small global number): see create_episode and episode_embeddings.DeferredEmbedGate.
_episode_embed_gate = DeferredEmbedGate()

# Read API key (file mode 600)
from canonical_key_provider import api_key_path as _api_key_path  # spec §4: MEM0_API_KEY_FILE on the native authority
API_KEY_PATH = _api_key_path()
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
        "or follow docs/systems/dpapi-canonical-key.md Recovery"
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
log.info("mem0 initialized (embedder: %s prefix-shim, model: %s, collection: %s)",
         EMBED_PROFILE.label, EMBEDDER_CONFIG["model"], mem.vector_store.collection_name)
# Hybrid fusion (docs/systems/fusion.md): mem0 ranks its dense pool by (cosine + bm25 + entity) / max,
# which hands the order to the keyword and entity terms; fusion.install binds a ranking that stays on
# the cosine scale in its place. /health/deep reports the binding, so a mem0 release that moves the
# scoring fails the deploy gate instead of silently ranking the old way.
import fusion as _fusion  # noqa: E402
FUSION_STATUS = _fusion.install()
log.info("fusion: %s", FUSION_STATUS)

def _stamp_tier_fetch(ids):
    """One batched retrieve of the CURRENT tier of each contradicts_canonical target (payload-only,
    no vectors). Ids absent from Qdrant are simply not returned (the gate reads that as dangling)."""
    recs = mem.vector_store.client.retrieve(
        collection_name=mem.vector_store.collection_name, ids=list(ids),
        with_payload=["tier"], with_vectors=False)
    return {str(r.id): (getattr(r, "payload", None) or {}).get("tier") for r in recs}


_set_stamp_tier_fetcher(_stamp_tier_fetch)

# v0.29 R4: ensure the semantic episode collection exists (idempotent). Non-fatal
# — if it fails, the raw-trace fallback search simply no-ops (fail-soft).
try:
    _ep_created = ensure_episode_collection(mem.vector_store.client)
    log.info("%s collection %s", EPISODE_COLLECTION, "created" if _ep_created else "present")
except Exception:
    log.exception("ensure_episode_collection failed (non-fatal; raw-fallback search will no-op)")

# MEM-17 (2026-07-03): /health reported only "2.0.4-v012" (mem0 lib pin + the
# ancient phase tag) — no way to tell WHICH stack release a runtime actually
# runs (the v1.11.0 P0 shipped through exactly that blindness: module bytes on
# disk, no version signal at the endpoint). Resolution order:
#   1. MEM0_STACK_VERSION env (operator/unit override),
#   2. ./VERSION beside app.py (deploy.sh copies the repo VERSION into the app
#      dir on every deploy — the production path),
#   3. ../VERSION (running straight from a repo checkout: mem0-server/../VERSION),
#   4. "unknown" (never crash the server over a version cosmetics file).
# Read ONCE at import — a health probe must not add per-request file I/O.
def _resolve_stack_version(app_dir: Optional[Path] = None) -> str:
    env_v = os.environ.get("MEM0_STACK_VERSION", "").strip()
    if env_v:
        return env_v
    base = app_dir if app_dir is not None else Path(__file__).resolve().parent
    for cand in (base / "VERSION", base.parent / "VERSION"):
        try:
            v = cand.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if v:
            return v
    return "unknown"


STACK_VERSION = _resolve_stack_version()

app = FastAPI(title="mem0 WSL", version="2.0.4-v012")
import embedder_503 as _embedder_503  # spec §4 P1-6: embedder outages -> 503 + Retry-After (reason cold-embedder)
_embedder_503.install(app)
# Passive write-path health: the FINAL status of every POST /v1/memories and PUT /v1/memories/{id}
# (the 503 embedder_503 builds, an HTTPException 500, an unhandled exception recorded as 500 and
# re-raised) is counted in-process; /health/maintenance publishes it as `write_path`. Zero I/O, and
# no model load: an endpoint that probed the embedder would keep it resident.
import write_path as _write_path
_write_path.install(app)

def auth(x_api_key: Optional[str] = Header(None)):
    if not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "missing or invalid X-API-Key")


# --- upstream rate-limit -> retryable status (2026-07-26) --------------------
# llama-swap answers 429 under queue saturation. egemma_embedder absorbs a bounded
# burst (3 attempts, backoff), but one that outlives the retry used to fall into
# the generic `except Exception -> HTTPException(500)` at the bottom of every
# endpoint and surface as a flat 500.
#
# That is not cosmetic. The MCP shim's offline failover fires on CONNECT-level
# failures ONLY and never on an HTTP status, precisely so a real answer is never
# masked by a stale replica read. A 500 is therefore taken as a real answer: the
# write is NOT queued to the outbox, it is dropped. A memory add was lost exactly
# this way. 503 + Retry-After says the opposite — "not an answer, ask again" —
# which the shim can act on without weakening the never-mask-a-real-answer rule.
#
# Deliberately narrow: ONLY "the upstream cannot serve right now" maps to 503 - a
# rate-limit (above), or an embedder that cannot start (embedder_503.retry_later: llama-swap
# 500 'upstream command exited prematurely', 502/503/504, a refused or timed-out connection;
# measured 2026-09-24 when the seat beside it left no VRAM headroom and 39 writes were
# answered 500). Every other exception keeps its 500, because a ctx-overflow or a coding
# error must stay loud and must never be replayed.
_RETRY_AFTER_SECONDS = "1"


def _is_upstream_rate_limit(e: BaseException) -> bool:
    """Duck-typed 429 detection, matching episode_embeddings._is_rate_limit:
    openai.RateLimitError carries status_code == 429 and is named RateLimitError;
    either signal qualifies. An httpx.HTTPStatusError wrapping a 429 response
    (the reranker transport) qualifies too. NOTHING else does."""
    if getattr(e, "status_code", None) == 429 or type(e).__name__ == "RateLimitError":
        return True
    resp = getattr(e, "response", None)
    return getattr(resp, "status_code", None) == 429


def _upstream_error(e: Exception) -> HTTPException:
    """The HTTPException to raise for an otherwise-unhandled endpoint exception."""
    if _is_upstream_rate_limit(e):
        return HTTPException(
            503,
            f"upstream embedder rate-limited (llama-swap 429), retry shortly: {e}",
            headers={"Retry-After": _RETRY_AFTER_SECONDS},
        )
    wait = _embedder_503.retry_later(e)
    if wait is not None:
        return HTTPException(
            503,
            f"upstream unavailable (embedder cold start failed or down), retry later: {e}",
            headers={"Retry-After": str(wait)},
        )
    return HTTPException(500, str(e))

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
    # (user-prompt-extract.ps1 0.D; pre-tool-check.ps1 also stamped it until
    # AMS-16 retired that hook, 2026-08-09). The search contract was
    # unversioned in v0.18, so drift on the highest-traffic hook call was
    # undetectable. WARN-only, never rejected (see hook_contract.py).
    hook_contract_version: Optional[str] = None
    # W5 T1.1 (ADOPT-2): per-stage retrieval trace, attached as results['_explain']
    # ONLY when True. The bundle endpoint constructs SearchIn without this field,
    # so the per-prompt hot path structurally never pays for or exposes it.
    explain: bool = False


class DiagnoseIn(BaseModel):
    """W5 T1.3 (ADOPT-2): 'why does memory X not surface for query Q'.

    Review F3: threshold/limit/rerank mirror SearchIn's DEFAULTS and must be
    set to the FAILING SEARCH's values — diagnosing a threshold-0.55 hook
    query at the default 0.1 names the wrong eating stage."""
    query: str
    target_id: str
    user_id: Optional[str] = None          # default: server DEFAULT_USER_ID
    brand: Optional[str] = None
    allow_cross_brand: Optional[bool] = None
    query_class: Optional[str] = "durable"
    threshold: float = 0.1                 # SearchIn parity
    limit: int = 20                        # SearchIn parity
    rerank: bool = False                   # SearchIn parity

class UpdateIn(BaseModel):
    text: str

class TierIn(BaseModel):
    tier: str  # "evidence" | "canonical" | "insight" | "temporal" | "stable"
    reason: Optional[str] = None
    actor: Optional[str] = None  # no default - audit finding 2026-06-08: hardcoded
                                  # "claude" hid autonomous vs user-direct intent
    # 2026-09-07: WHICH MODEL judged this promotion. `actor` is a ROLE label
    # ("dream-autopromote", "user-direct") and never says what did the judging, so a
    # promoted memory carried no way to answer "which model decided this?" after the
    # fact. Optional and unvalidated on purpose: a caller that does not know (a hand
    # PATCH, an older client) records None rather than a guess, and None is honestly
    # distinguishable from a recorded value.
    #
    # NOT part of the HMAC. The canonical-promotion signature covers
    # <ts>|<nonce>|promote|<mid>|<reason>, so this field is UNSIGNED and must never be
    # treated as tamper-evident - it is an audit convenience, not an authorisation
    # input. The signed material was deliberately left alone.
    judge_model: Optional[str] = None

class MetadataIn(BaseModel):
    metadata: dict   # shallow merge with existing payload
    actor: Optional[str] = None   # who triggered this — logged to ledger
    reason: Optional[str] = None

class SupersedeIn(BaseModel):
    # 1.32.4: POST /v1/memories/{id}/supersede, the only writer of superseded_by (supersession.py).
    winner_id: str                  # the newer record that replaces this one (or one claim in it)
    scope: str = "full"             # "full" hides the record; "partial" annotates one stale claim
    detail: Optional[str] = None    # required for "partial": which claim is out of date
    reason: Optional[str] = None    # free text for the ledger
    source: Optional[str] = None    # caller label for audit (e.g. "memory_supersede"); authorises nothing

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
    hook_contract_stats as _hook_contract_stats,
    is_machine_turn_prompt as _is_machine_turn_prompt,  # C10: task notifications get no bundle
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
# 1.32.5: ONE copy, security_invariants' (this module kept its own, which could drift from the one the
# insight PUT/DELETE gate reads), and a label in it counts only with the service key
# (security_invariants.require_service_credential).
from security_invariants import INSIGHT_ALLOWED_ACTORS
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
NLI_GATE_COSINE_FLOOR = _embedder_profile.threshold("nli_floor", EMBED_PROFILE)  # SEMANTIC scale (raw cosine), NOT the hybrid score; per embedding space (env MEM0_NLI_GATE_COSINE_FLOOR wins)
NLI_GATE_TOPK = _env_int("MEM0_NLI_GATE_TOPK", 3)
NLI_GATE_TIMEOUT_S = _env_int("MEM0_NLI_GATE_TIMEOUT_S", 45)  # Codex low-effort NLI runs ~20-30s
# Over-fetch window for the canonical pre-filter: mem.search returns the top-N by the FUSED
# score (fusion.py) above the raw-cosine floor, and _search_core then post-filters that window to
# canonical/stable. A small top_k could truncate a canonical neighbor ranked below several evidence
# neighbors (audit MED), so fetch wide then let the tier filter narrow.
# Cheap — the gate runs ASYNC, off the hot path.
NLI_GATE_FETCH = _env_int("MEM0_NLI_GATE_FETCH", 25)
# Retrieval-gating metadata keys a caller must NOT be able to set via add() (mirrors the PATCH
# /metadata FORBIDDEN_KEYS). contradicts_canonical is the NLI gate's own security primitive;
# only the gate (server-side) or a trusted-actor PATCH may write these (audit HIGH).
_ADD_FORBIDDEN_META = {"retrievable", "expires_at", "created_at", "tier_actor",
                       "superseded_by", "contradicts_canonical", "contradiction_checked_at",
                       "nli_gate_checked_at", "contradicts_canonical_pending",
                       # 1.32.4: written only by POST /v1/memories/{id}/supersede (supersession.py)
                       "superseded_at", "superseded_via", "partially_superseded_by"}

# AMS-01/F4 (2026-08-07): per-record write lock. uvicorn runs single-worker and
# these sync-def handlers execute in the threadpool, so two requests CAN
# interleave a read-modify-write on the same record. Concretely: a canonical
# promotion landing between PUT's payload pre-read and its upsert used to be
# silently demoted by the upsert's stale tier (ledger and store then disagree),
# and the async NLI stamp could land inside the PUT window and be erased.
# One lock per memory id serializes PUT, PATCH /tier, PATCH /metadata and the
# NLI stamp for that id only. Registry grows one small Lock per distinct id
# written this process lifetime — bounded in practice by the corpus.
# The key is the canonical UUID spelling: Qdrant resolves the hyphenated, simple, braced and
# urn:uuid spellings (any case) of one id to the same point, so keying on the raw path string
# gave one record several locks and let a differently spelled request escape the serialization.
_MID_LOCKS: dict = {}
_MID_LOCKS_GUARD = threading.Lock()

def _mid_lock_key(mid) -> str:
    try:
        return str(_uuid.UUID(str(mid).strip()))
    except (ValueError, AttributeError, TypeError):
        return str(mid)

def _mid_write_lock(mid):
    key = _mid_lock_key(mid)
    with _MID_LOCKS_GUARD:
        lk = _MID_LOCKS.get(key)
        if lk is None:
            lk = _MID_LOCKS[key] = threading.Lock()
        return lk

# AMS-01 (2026-08-07): in-process daily carry-over counters (MEM-8 pattern,
# zero I/O). Invocation proof on /health/deep that the repaired PUT path is
# the one serving traffic: puts = PUTs served today; keys_restored = keys the
# post-verify had to re-stamp (should stay ~0 — the pre-merge makes restore a
# fallback); keys_lost = keys still absent after retries (MUST stay 0 —
# nonzero is an active AMS-01 recurrence). Survival itself is proven by the
# verifier's PUT canary and the live pytest, not by this counter.
_put_carryover_today = {"date": None, "puts": 0, "keys_restored": 0, "keys_lost": 0}

# AMS-39 (2026-08-08): the raw-trace fallback is ENABLED by default but emitted
# no receipt on either outcome, so "it fired and helped", "it abstained
# correctly" and "it has been dead for a month" were indistinguishable — the
# exact persistence shape AMS-09 was. Same zero-I/O daily-counter pattern as
# the carry-over counters; surfaced informationally on /health/deep.
_raw_fallback_today = {"date": None, "fired": 0, "abstained": 0, "errors": 0}

def _raw_fallback_bump(fired=0, abstained=0, errors=0):
    today = _dt.date.today().isoformat()
    if _raw_fallback_today["date"] != today:
        _raw_fallback_today.update(
            {"date": today, "fired": 0, "abstained": 0, "errors": 0})
    _raw_fallback_today["fired"] += fired
    _raw_fallback_today["abstained"] += abstained
    _raw_fallback_today["errors"] += errors


def _put_carryover_bump(puts=0, restored=0, lost=0):
    today = _dt.date.today().isoformat()
    if _put_carryover_today["date"] != today:
        _put_carryover_today.update(
            {"date": today, "puts": 0, "keys_restored": 0, "keys_lost": 0})
    _put_carryover_today["puts"] += puts
    _put_carryover_today["keys_restored"] += restored
    _put_carryover_today["keys_lost"] += lost

# W5 T6.1 (count-only entrance-redaction telemetry — the operator fork's
# evidence base). The store DELIBERATELY holds live plaintext credentials; the
# add() entrance therefore counts what the rule set WOULD redact and mutates
# NOTHING. /health/deep exposes the OPAQUE daily total only (review F6: a
# per-rule breakdown on an unauthenticated 0.0.0.0 endpoint is per-credential-
# type targeting telemetry); the per-rule split lives in the local 0600 JSONL
# (~/.mem0/redaction-counter.jsonl, count>0 events only, 10MB-rotated — R11).
_redactions_today = {"date": None, "total": 0}

def _redactions_bump(n=0):
    today = _dt.date.today().isoformat()
    if _redactions_today["date"] != today:
        _redactions_today.update({"date": today, "total": 0})
    _redactions_today["total"] += n

def _record_would_redact(counts: dict) -> None:
    """Daily counter + local per-rule JSONL. Never raises into add()."""
    try:
        _redactions_bump(sum(counts.values()))
        if not counts:
            return
        log_path = Path.home() / ".mem0" / "redaction-counter.jsonl"
        if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
            for i in range(5, 0, -1):
                src = log_path.with_suffix(f".jsonl.{i - 1}") if i > 1 else log_path
                dst = log_path.with_suffix(f".jsonl.{i}")
                if i == 5:
                    dst.unlink(missing_ok=True)
                if src.exists():
                    src.rename(dst)
        with _secure_open(log_path) as f:
            f.write(json.dumps({
                "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "total": sum(counts.values()),
                "rules": counts,
            }) + "\n")
    except Exception:
        log.warning("redaction counter failed (non-fatal)", exc_info=True)

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
RAW_FALLBACK_COSINE_FLOOR = _embedder_profile.threshold("episode_floor", EMBED_PROFILE)  # per embedding space; env MEM0_RAW_FALLBACK_COSINE_FLOOR wins
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
    # rerank=False is LOAD-BEARING here (W5 T5): it structurally excludes the
    # keyword union leg from the NLI gate's internal lookups — flipping it
    # silently activates the leg on a gated path.
    res = _search_core(SearchIn(query=query, filters=filters, query_class="canonical",
                                threshold=threshold,
                                limit=max(int(NLI_GATE_FETCH), int(topk or 1)), rerank=False),
                       _route="nli")
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
        # AMS-01/F4: serialize against a concurrent PUT — the async stamp used
        # to land inside the PUT window and be erased by the upsert.
        with _mid_write_lock(mid):
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
# The literal above is the EmbeddingGemma-300m calibration (kept in parity with
# claude-config/model-tiers.json by tests/test_tier_parity.py). The gate the server applies is the
# ACTIVE embedding space's (embedder_profile): cosine scales are not portable between models, and
# EmbeddingGemma-2 scores off-topic questions where EmbeddingGemma-300m scores relevant ones. Env
# MEM0_RELEVANCE_THRESHOLD overrides it for calibration runs.
RELEVANCE_GATE = _embedder_profile.threshold("relevance_gate", EMBED_PROFILE)
for _tier_policy in TIER_BUNDLE_POLICY.values():
    _tier_policy["relevance_threshold"] = RELEVANCE_GATE
# A threshold knob is not scoped to a space: one fitted on another model's scores and left set is noise
# here. Say so loudly where it matters most, on a space other than the default.
for _name, _ov in _embedder_profile.threshold_overrides(EMBED_PROFILE).items():
    if EMBED_PROFILE.name != _embedder_profile.DEFAULT_PROFILE:
        log.warning("threshold override %s=%s replaces %s's calibrated %s=%s — verify it was fitted on this "
                    "embedding space", _ov["env"], _ov["value"], EMBED_PROFILE.name, _name, _ov["profile_value"])


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

def _find_existing_by_hash(text: str, user_id: str, metadata: Optional[dict]) -> Optional[str]:
    """Return the id of a stored point holding this exact text in the SAME scope, else None.

    Backs the add() hash-idempotency guard (audit 2026-07-14). Exact, not fuzzy: mem0 stamps
    hash=md5(data) on every infer=False write, so an equal hash means a byte-identical string —
    unlike semantic-dedup.py's cosine threshold, this can never collapse two distinct facts.

    Scope = (user_id, workspace, project). The scope match MUST be symmetric: a missing key on the
    incoming write has to match ONLY points that also lack it (`is_empty`), never "any value".
    Review catch 2026-07-14 (proven live): with an asymmetric filter, a brand-NEUTRAL write whose
    metadata carried no workspace collapsed onto a BRANDED point and was never created — and since
    brand-scoped search is fail-closed, the fact then existed only under a brand and was invisible
    in exactly the sessions that needed it. A dedup guard that hides memories is worse than the
    duplicates it removes, so err toward writing.

    Buried points (contradicts_canonical / superseded_by) are EXCLUDED: those are deliberately
    hidden from retrieval, and NOOPing a fresh write onto one would silently bury the new fact too.

    Fail-OPEN: any Qdrant error returns None and the write proceeds.
    """
    try:
        must: list = [{"key": "hash", "match": {"value": hashlib.md5(text.encode()).hexdigest()}}]
        if user_id:
            must.append({"key": "user_id", "match": {"value": user_id}})
        for _k in ("workspace", "project"):
            _v = (metadata or {}).get(_k)
            if _v:
                must.append({"key": _k, "match": {"value": _v}})
            else:
                # symmetric: no workspace on the incoming write => match only points that also
                # have none. Without this the filter degenerates to (hash, user_id) and a
                # brand-neutral write collapses onto a branded record.
                must.append({"is_empty": {"key": _k}})
        # Never dedup onto a record retrieval already hides — NOOPing onto it would bury the
        # incoming fact too. Buried = contradicts_canonical set, superseded, or retrievable=false.
        must.append({"is_empty": {"key": "contradicts_canonical"}})
        must.append({"is_empty": {"key": "superseded_by"}})
        hits = mem.vector_store.client.scroll(
            collection_name=mem.vector_store.collection_name,
            scroll_filter={
                "must": must,
                "must_not": [{"key": "retrievable", "match": {"value": False}}],
            },
            with_payload=True,
            with_vectors=False,
            limit=1,
        )
        points = hits[0] if hits else []
        if not points:
            return None
        pay = points[0].payload or {}
        # Tier must agree. A dream/C1 write of tier=insight must NOT be swallowed by an existing
        # tier=evidence row (it would silently downgrade the insight and skip its ledger entry).
        _incoming_tier = (metadata or {}).get("tier")
        if _incoming_tier and pay.get("tier") and pay.get("tier") != _incoming_tier:
            return None
        return str(points[0].id)
    except Exception:
        log.warning("add(): hash-idempotency probe failed; allowing the write (fail-open)", exc_info=True)
        return None

def _secure_open(path: Path, mode: str = "a", encoding: str = "utf-8"):
    """v0.18 MED-10: open an append-log with chmod 600 enforced at creation.
    These logs carry query text, decision text, and replay nonces — owner-only perms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600, exist_ok=True)
    return path.open(mode, encoding=encoding)


def _ledger_segment_path(mem0_dir: Path, now: Optional[_dt.datetime] = None) -> Path:
    """MEM-16 (2026-07-03): monthly ledger segment path — tier-ledger-YYYY-MM.jsonl.

    The single legacy tier-ledger.jsonl grew unbounded (9.8MB) because every
    writer appended to one file forever. New entries go to a per-month segment;
    the legacy file is FROZEN as the historical archive (never rewritten, never
    migrated — rewriting an append-only audit log would defeat its point).
    Readers (scripts/wsl/ledger-audit.py, the test helpers) walk legacy + all
    segments in chronological order. UTC month, matching the entry 'ts' stamps."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return mem0_dir / f"tier-ledger-{now.strftime('%Y-%m')}.jsonl"


# AMS-42 (2026-08-08): serialize ledger appends. def-endpoints run in AnyIO's
# threadpool, so concurrent appends were unlocked; O_APPEND makes each RAW write
# atomic, but CPython's buffered writer splits a line >8 KB into multiple raw
# writes (a cascade-delete entry embeds the full prior_payload), so two
# concurrent appends could interleave chunks and TEAR a line — and readers skip
# unparseable lines silently, so a torn audit entry is a lost audit entry.
# Same module-level threading.Lock pattern as the nonce replay store.
_LEDGER_LOCK = threading.Lock()


def _append_ledger(record: dict) -> None:
    """Append-only ledger writer for tier-change events. Single source of truth for promotion audit.
    MEM-16: writes to the CURRENT MONTH segment (see _ledger_segment_path);
    legacy ~/.mem0/tier-ledger.jsonl is a frozen historical archive."""
    import json as _json
    ledger = _ledger_segment_path(Path.home() / ".mem0")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if "ts" not in record:
        record["ts"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    # v0.17 F.4.4: every entry stamps its schema version automatically.
    record.setdefault("schema_version", "v17")
    # AMS-42: one writer at a time — a whole line lands as one contiguous append.
    with _LEDGER_LOCK:
        with ledger.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(record) + "\n")

@app.get("/health")
def health() -> dict:
    # MEM-17: "version" stays the historical mem0-lib+phase tag (dashboards may
    # pattern-match it); "stack" is the actual stack release (repo VERSION).
    # "embedder" names the active embedding space's model (embeddinggemma-300m before profiles,
    # unchanged for that profile); "embed_profile" is the profile a reader matches it against.
    return {"ok": True, "version": "2.0.4-v012", "stack": STACK_VERSION,
            "store": "qdrant", "embedder": EMBED_PROFILE.label.lower(), "embed_profile": EMBED_PROFILE.name}

def _capture_signals() -> dict:
    """CRIT-01: the episodic store's capture facts for /health/maintenance (indexed reads, about a millisecond; no
    Qdrant, no model, no cache needed), over a read-only connection that gives up on a lock after 1 s. Raises on a store
    failure: maintenance_health.build reads that as `unknown`."""
    conn = _episodic_connect_readonly()
    try:
        return _episodic_capture_signals(conn)
    finally:
        conn.close()


@app.get("/health/maintenance")
def health_maintenance() -> dict:
    """Spec §9 (P1-5): the nightly chain's last successes, the steps whose latest run failed or
    degraded (`critical_failed_steps`: the failed ones minus the PC-dependent steps), the judge
    transport, pool usage (alarm at 85 %) and pool health, the box's boot ids for 7 days, the write
    path, and the capture liveness of the PC-side L1a extractor. `ok` folds the failed/degraded
    steps, the pool and the write path in; `capture` and `critical_failed_steps` never change it.
    Gatus probes it; the session-start line reads it with a 1.5 s budget and falls back to
    local numbers. `write_path` is the PASSIVE record of real POST/PUT /v1/memories outcomes
    (write_path.py): reading it never touches the embedder, so polling it cannot keep a model
    resident. Never raises on a reader: an unreadable pool/journal reads as unknown, not as an
    error."""
    import os as _os
    import maintenance_health as _mh
    ds = _os.environ.get("MEM0_ZFS_DATASET", "").strip()
    pool = _mh.zfs_pool_reader(ds) if ds else _mh.disk_usage_reader(str(Path.home()))
    maint = Path.home() / ".mem0" / "maintenance"
    return _mh.build(maint / "receipts.jsonl", _dt.datetime.now(_dt.timezone.utc), pool,
                     _mh.journal_boots_reader(), codex_shim_client.judge_transport,
                     usage_reader=_mh.usage_window_reader(maint / "codex-usage.jsonl"),
                     # Pool HEALTH (not capacity) needs a pool to ask: only a ZFS box names one.
                     pool_health_reader=_mh.zpool_health_reader(ds) if ds else None,
                     # The operator's dated pool-health ack (env, else stack.env), read on every call.
                     pool_ack_reader=_mh.read_pool_ack,
                     wiki_stamp_dir=Path.home() / "wiki-index",
                     drift_reader=drift_state_health,
                     # The write path, learned from real writes (in-process, zero I/O, no model load).
                     write_path_reader=_write_path.snapshot,
                     # CRIT-01: is a PC's L1a still finishing runs while PC sessions go on? (informational)
                     capture_reader=_capture_signals)


@app.get("/health/morning-summary")
def health_morning_summary() -> dict:
    """Spec §9: the morning summary is generated by the chain and pulled at session start. The last
    three `## ` sections of ~/.mem0/maintenance/morning-summary.md; 404 before the first night."""
    import re as _re
    p = Path.home() / ".mem0" / "maintenance" / "morning-summary.md"
    if not p.exists():
        raise HTTPException(status_code=404, detail="no summary yet")
    text = p.read_text(encoding="utf-8", errors="replace")
    sections = [s for s in _re.split(r"(?m)^(?=## )", text) if s.strip()]
    return {"path": str(p), "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime, _dt.timezone.utc).isoformat(),
            "sections": [s.rstrip("\n") for s in sections[-3:]]}


def _embed_model() -> str:
    """The llama-swap model name the store is bound to (config.EMBEDDER_CONFIG["model"])."""
    return str(EMBEDDER_CONFIG.get("model") or EMBED_PROFILE.model)


def _embed_base() -> str:
    """The OpenAI-compatible base URL the embedder is served from (embedder_profile.base_url)."""
    return str(EMBEDDER_CONFIG.get("openai_base_url") or _embedder_profile.DEFAULT_BASE_URL).rstrip("/")


def _qdrant_base() -> str:
    """The Qdrant the Memory instance is bound to (config vector_store host/port)."""
    vs = build_config()["vector_store"]["config"]
    host = vs.get("host") or "localhost"
    return f"http://{'127.0.0.1' if host == 'localhost' else host}:{vs.get('port') or 6333}"


@app.get("/health/embedder")
def health_embedder(warm: Optional[str] = Query(None)) -> dict:
    """Spec §4 (P1-6 PC half): the SessionStart pre-warm target. The embedder unloads after
    5 idle minutes (ttl 300, every engine) and takes ~3.4 s to come back, so the first prompt's
    bundle used to pay the cold start. This embeds ONE token as active work (a pre-warm, not
    idle residency) and reports whether llama-swap lists `embeddinggemma` as loaded
    (informational: an unreadable listing reads as `loaded: None`, never as an error).
    The embed exceptions deliberately PROPAGATE — embedder_503.install maps a cold/down seat
    to 503 + Retry-After with reason cold-embedder, the same answer the bundle path gives,
    which the hook client names and retries once.

    `?warm=rerank` additionally issues a one-document rerank so the first deliberate search of
    the session does not pay the reranker's cold load (it unloads after 5 idle minutes too).
    The reranker result rides along as `rerank: {ok, warm_ms|error}` and never fails this
    endpoint: search degrades gracefully without it, so a reranker that cannot load is
    reported, not raised."""
    import httpx as _httpx
    import time as _time
    loaded = None
    try:
        r = _httpx.get(f"{_embed_base()}/models", timeout=3.0)
        r.raise_for_status()
        for entry in (r.json().get("data") or []):
            if str(entry.get("id", "")) != _embed_model():
                continue
            loaded = _embedder_503.listing_loaded(entry)  # flat state OR v256 status.value
            break
    except Exception:
        loaded = None
    t0 = _time.perf_counter()
    r = _httpx.post(f"{_embed_base()}/embeddings",
                    json={"model": _embed_model(), "input": "warm"},
                    timeout=10.0)
    r.raise_for_status()
    out: dict[str, Any] = {"ok": True, "loaded": loaded,
                           "warm_ms": int((_time.perf_counter() - t0) * 1000)}
    if warm == "rerank":
        out["rerank"] = _rerank_warm()
    return out


@app.get("/health/deep")
def health_deep() -> dict:
    """Deeper liveness probe (audit finding 2026-06-08: shallow /health was green-lighting
    broken write paths). Checks Qdrant + embedder + mem0 collection point count. Slow
    enough that callers should use /health for liveness; /health/deep is for diagnostics."""
    import httpx as _httpx
    out: dict[str, Any] = {"ok": True, "checks": {}}
    # MEM-17: stack release on the diagnostics probe too (same startup read).
    out["stack"] = STACK_VERSION
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
    # The embedding space this server is bound to: profile, model alias, template version and the
    # collections that space owns. A reader compares it with the store's embed-identity record.
    out["embed_profile"] = _embedder_profile.describe(EMBED_PROFILE)
    # Qdrant: the collection the Memory instance is bound to (never a literal: a space change moves it).
    try:
        r = _httpx.get(f"{_qdrant_base()}/collections/{out['collection'] or EMBED_PROFILE.memories}", timeout=3.0)
        r.raise_for_status()
        d = r.json().get("result", {})
        out["checks"]["qdrant"] = {"ok": True, "points": d.get("points_count"), "status": d.get("status")}
    except Exception as e:
        out["ok"] = False
        out["checks"]["qdrant"] = {"ok": False, "error": str(e)[:120]}
    # Embedder: the active profile's model on llama-swap (OpenAI-compatible), document prefix.
    # Replaced the nomic-via-Ollama :11435 probe when Ollama was decommissioned.
    try:
        r = _httpx.post(f"{_embed_base()}/embeddings",
                       json={"model": _embed_model(), "input": EMBED_PROFILE.doc_prefix + "health"},
                       timeout=10.0)
        r.raise_for_status()
        dim = len(r.json().get("data", [{}])[0].get("embedding", []))
        out["checks"]["embedder"] = {"ok": dim == EMBED_PROFILE.dims, "dim": dim, "model": _embed_model()}
        if dim != EMBED_PROFILE.dims:
            out["ok"] = False
    except Exception as e:
        out["ok"] = False
        out["checks"]["embedder"] = {"ok": False, "error": str(e)[:120]}
    # Hybrid fusion (fusion.install at start, fusion.end_search per search): not bound, or a search that
    # returned results without reaching it, means mem0 ranks with its own additive formula again (a mem0
    # release moved the scoring). That flips ok, so the deploy gates fail; MEM0_FUSION=mem0 is exempt.
    out["checks"]["fusion"] = fusion_check = _fusion.health(FUSION_STATUS)
    if not fusion_check["ok"]:
        out["ok"] = False
    # v0.19 M10: hook-contract drift counters (in-process, zero I/O). missing =
    # field-less callers (documented-legitimate, logged INFO); unknown = real
    # drift candidates (logged WARN). Informational — never flips ok=False.
    out["checks"]["hook_contract"] = dict(_hook_contract_stats)
    # MEM-8 (2026-07-03): retrieval-starvation observability — top admission
    # rejection reason FAMILIES today (in-process daily counters from
    # admission_gate; a gate silently eating results was invisible short of
    # grepping admission-rejected.jsonl). Informational — never flips ok=False.
    out["checks"]["admission_rejections_today"] = _admission_rejections_today()
    # MEM-13 (2026-07-03): contradiction review-queue depth. The SAFE resolver
    # QUEUES genuine contradictions for human review instead of auto-hiding
    # (Codex over-promoted 3/4 CONSISTENT facts in a live run), so an unwatched
    # queue silently accumulates verdicts nobody promotes. One tiny file read;
    # informational — never flips ok=False. 0 when absent/empty/unreadable.
    try:
        _rq = Path.home() / ".mem0" / "contradiction-promote-review.jsonl"
        out["checks"]["pending_contradiction_reviews"] = (
            sum(1 for ln in _rq.read_text(encoding="utf-8").splitlines() if ln.strip())
            if _rq.exists() else 0
        )
    except OSError:
        out["checks"]["pending_contradiction_reviews"] = 0
    # v0.20 Phase D (M6): surface the keyless-degraded state (ExecStartPre=-
    # swallows a dpapi-fetch-key.sh failure; the server then 503s every
    # canonical/insight HMAC mutation while /health/deep stayed green — exactly
    # the 'shallow health green-lighting broken write paths' failure this
    # endpoint exists to prevent). Key is cached after first read — zero I/O
    # beyond one dpapi_path.exists(). ok=False ONLY when the blob exists but no
    # key loaded; a dev box with no key configured at all stays green.
    # 1.32.5: informational; the brain's capability row 'service-key' turns an absent key dead.
    from security_invariants import _SERVICE_KEY_PROVIDER as _svc_provider
    from canonical_key_provider import service_key_health as _service_key_health
    out["checks"]["service_key"] = _service_key_health(_svc_provider)
    out["checks"]["canonical_key"] = _canonical_key_health(_APP_KEY_PROVIDER)
    if not out["checks"]["canonical_key"]["ok"]:
        out["ok"] = False
    # Spec §4 (P1-2): which judge transport this host uses — native codex exec on the Linux
    # authority, the Windows HTTP shim on WSL boxes, none when neither is available.
    # Informational — never flips ok.
    out["checks"]["judge_transport"] = codex_shim_client.judge_transport()
    # AMS-09 (2026-08-07): BM25 sparse-leg liveness — GATING. The lexical leg
    # died silently for 33 days behind mem0's fastembed ImportError fail-soft
    # while this endpoint stayed green (the exact 'shallow health green-lighting
    # a broken path' class it exists to prevent). A dead leg now fails the
    # deploy gate — a blocked deploy on a rebuilt venv is the CORRECT outcome;
    # the venv rebuild that killed the leg is precisely the event this catches.
    # sparse_leg_health never raises (module contract) — belt here anyway.
    try:
        # AMS-09b: encode via the self-healing wrapper — a transiently-failed
        # encoder init (cache evicted at boot + HF_HUB_OFFLINE) otherwise stays
        # poisoned until a manual restart. Recovery mouths = every /health/deep
        # caller: the nightly dream heartbeat, the deploy gate, Test-MemoryStack,
        # and the MCP memory_health verb. (The SessionStart banner deliberately
        # never calls /health/deep — RegressionGuards pins that.)
        _sl = sparse_leg_health(
            mem.vector_store.client, mem.vector_store.collection_name,
            lambda q: encode_with_selfheal(mem.vector_store, q),
        )
    except Exception as e:
        _sl = {"ok": False, "error": str(e)[:120]}
    out["checks"]["sparse_leg"] = _sl
    if not _sl.get("ok"):
        out["ok"] = False
    # AMS-10 (2026-08-07): CP437 mojibake corpus tripwire — informational,
    # never flips ok (Test-MemoryStack WARNs on hits>0). Qdrant has no regex
    # filter, so this is a client-side scroll — measured ~0.2s on the live
    # corpus; the page cap bounds growth and `scanned` stays honest about it.
    def _mj_scroll(offset, limit):
        return mem.vector_store.client.scroll(
            mem.vector_store.collection_name,
            with_payload=list(PAYLOAD_KEYS), with_vectors=False,
            limit=limit, offset=offset,
        )
    out["checks"]["mojibake"] = mojibake_health(_mj_scroll)
    # AMS-01 (2026-08-07): carry-over counters — informational invocation proof.
    _put_carryover_bump()  # roll the date on idle days so 'date' stays current
    out["checks"]["put_carryover_today"] = dict(_put_carryover_today)
    # AMS-39: raw-fallback fire/abstain receipt (informational, never gates).
    _raw_fallback_bump()   # roll the date on idle days
    out["checks"]["raw_fallback_today"] = dict(_raw_fallback_today)
    # W5 T6.1: OPAQUE total only (F6) — the per-rule split never leaves the box.
    _redactions_bump()
    out["checks"]["redactions_would_apply_today"] = dict(_redactions_today)
    # W3 (2026-08-07): the alarm mouths — nightly-job receipt ages, the
    # retrieval-drift guard's state file, and the capability manifest folded
    # over everything above. ALL informational: the gating checks already
    # flipped ok adjacent to their assignment; nothing here double-gates.
    # Each module is never-raise by contract — belted anyway (house F11: a
    # probe bug must not take down /health/deep for its other consumers).
    try:
        out["checks"]["job_liveness"] = job_liveness_health()
    except Exception as e:
        out["checks"]["job_liveness"] = {"role": None, "error": str(e)[:120]}
    try:
        out["checks"]["retrieval_drift"] = drift_state_health()
    except Exception as e:
        out["checks"]["retrieval_drift"] = {"state_present": None, "error": str(e)[:120]}
    # W4 (F11): the reranker's PASSIVE counters — what real search traffic has
    # already proven about the cross-encoder. Zero I/O; an active probe here
    # would hang deploy.sh's post-restart health gate on a cold model.
    try:
        out["checks"]["reranker"] = _rerank_health()
    except Exception as e:
        out["checks"]["reranker"] = {"error": str(e)[:120]}
    # W4: admission/tier/brand read-half self-probe. Pure — three synthetic
    # records through AdmissionPolicy.evaluate(); no counters, no audit log,
    # no disk (apply_admission would do all three on every health read).
    try:
        out["checks"]["admission_probe"] = _admission_selfprobe()
    except Exception as e:
        out["checks"]["admission_probe"] = {"ok": False, "error": str(e)[:120]}
    try:
        _cap_role = (out["checks"].get("job_liveness") or {}).get("role")
        # WP-4: the effective 4C promotion-gate mode (env > stack.env > shadow) and its WARN row
        out["checks"]["promotion_gate"] = _promotion_gate_health(_cap_role)
        out["promotion_gate_mode"] = out["checks"]["promotion_gate"]["mode"]
        out["checks"]["capabilities"] = evaluate_capabilities(
            out["checks"], _cap_role, stack_version=STACK_VERSION)
    except Exception as e:
        out["checks"]["capabilities"] = {"error": str(e)[:120]}
    return out

@app.post("/v1/memories")
def add(b: AddIn, background_tasks: BackgroundTasks, request: Request, x_api_key: Optional[str] = Header(None),
        x_ams_service_key: Optional[str] = Header(None, alias="X-AMS-Service-Key")):
    auth(x_api_key)
    # Storage cap enforcement (audit finding 2026-06-08: 341/384 backfilled points
    # exceeded the previously-documented 600-char cap which was never enforced).
    text_for_check = _coerce_to_text(b.messages)
    # W5 T6.1: count-only entrance telemetry, BEFORE any gate can raise
    # (attempt semantics — 'what arrives at the door', the operator fork's
    # denominator). ZERO mutation of the stored text, ever.
    _record_would_redact(count_redactions(text_for_check))
    if len(text_for_check) > MAX_MEMORY_CHARS:
        raise HTTPException(
            413,
            f"add: memory exceeds {MAX_MEMORY_CHARS}-char cap (got {len(text_for_check)}). "
            "Split into atomic facts (best for retrieval precision) or trim; raise MEM0_MAX_MEMORY_CHARS if a larger record is truly intended."
        )

    # Empty-write guard (audit 2026-07-14): 163 live points held data="" (hash
    # d41d8cd98f00b204e9800998ecf8427e — the md5 of the empty string), all written during the
    # 2026-07-12 L1a bulk extraction. An empty memory is unretrievable noise that also trips
    # L10's missing-provenance heuristic. Reject it at the door rather than storing it.
    if not text_for_check.strip():
        raise HTTPException(400, "add: refusing to store an empty memory (messages coerced to an empty string).")

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
            # 1.32.5: the consolidator's source label is a claim; only the service key proves it.
            from security_invariants import require_service_credential
            require_service_credential(src, x_ams_service_key, field="metadata.source")
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

    # Tier birth-default (2026-09-01): every record is BORN with a tier. The gate above
    # validates tier when present but let an omitted one through — and this endpoint's own
    # 403 text recommends "(or omit tier)". A tier-less record is a trap: fetch_current_tier
    # fail-closes an absent tier to "canonical" (the H1-race shield), so the record is
    # HMAC-locked against update/delete/patch from birth — an agent can create a memory it
    # can never correct or remove (127 such points found live, including a malformed add
    # whose metadata block leaked into the text). Default to 'evidence' — the documented
    # advisory trust level — AFTER the gates (canonical/insight via omission stays
    # impossible) and BEFORE hash-dedup, so the lookup sees the metadata that gets stored.
    if b.metadata is None:
        b.metadata = {"tier": "evidence"}
    elif "tier" not in b.metadata:
        b.metadata["tier"] = "evidence"

    # Hash idempotency (audit 2026-07-14): 63,350 of 67,787 points (93.5%) were exact-hash
    # duplicates. Cause: the L1a extractor re-extracts the WHOLE transcript on every
    # Stop/PreCompact hook, and this endpoint had no uniqueness check — so every Stop re-inserted
    # every fact of the session (worst single fact: 4,174 copies). mem0 stores hash=md5(data) on
    # infer=False writes, so this match is EXACT, not a similarity heuristic. On a hit we return
    # the existing id (idempotent for the caller) and write nothing.
    #
    # ORDERING IS LOAD-BEARING (review catch 2026-07-14): this MUST sit AFTER the tier gate and the
    # forbidden-metadata strip above. Placed before them, a tier='canonical' add whose text already
    # existed returned 200/NOOP instead of the 403 the gate owes the caller — a security guardrail
    # whose enforcement depended on whether the text happened to be new. Every gate runs first;
    # dedup is the last thing before the write.
    #
    # infer=True is deliberately NOT guarded: those facts are LLM-derived, so their stored hashes
    # are not knowable before the write.
    if b.infer is False:
        _existing = _find_existing_by_hash(text_for_check, b.user_id, b.metadata)
        if _existing:
            log.info("add(): idempotent no-op — identical memory already stored as %s", _existing)
            # Answered from a payload lookup, before any embed call: this 200 says nothing about the write
            # path. Neutral, or an automated writer re-posting its transcript during an embedder outage
            # clears write_path.ok between the 503s it gets for the new facts (write_path.py).
            _write_path.mark_neutral(request)
            return {
                "results": [{"id": _existing, "memory": text_for_check, "event": "NOOP_DUPLICATE"}],
                "deduplicated": True,
            }

    try:
        result = mem.add(
            messages=b.messages,
            user_id=b.user_id,
            agent_id=b.agent_id,
            run_id=b.run_id,
            metadata=b.metadata,
            infer=b.infer,
        )
        # infer=False embeds every message it stores, but skips system-role and malformed ones without an
        # embed call and answers {"results": []}: like a duplicate, that 200 says nothing about the write
        # path. infer=True is different on purpose: mem0 embeds the incoming text (its existing-memory
        # lookup) before it can answer at all, so its 200 is evidence (checked against mem0 2.0.4).
        if b.infer is False and _write_path.stored_nothing(result):
            _write_path.mark_neutral(request)
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
        raise _upstream_error(e)

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
        raise _upstream_error(e)

def _search_core(b: SearchIn, _route: str = "search"):
    """v0.20 A.3: search internals shared by POST /v1/memories/search and
    POST /v1/context/bundle. Contains the FULL retrieval-policy pipeline -
    retired/intent filtering, the W5 keyword-recall union leg, rerank,
    query_class recency policy, the server-side admission gate
    (apply_admission) and retrieval-log observability - so the bundle
    endpoint can never become a parallel ungated path. Raises on failure;
    callers map exceptions to HTTP. hook_contract_version WARN-validation
    stays at the endpoint layer (each endpoint reports its own route name).

    W5 T1.5: ``_route`` discriminates the three callers in the retrieval log
    ('search' | 'bundle' | 'nli') — the actor field is constant and could
    not; ADOPT-4 pair sampling and ADOPT-5 export both key on it.
    W5 T1.1: ``b.explain`` attaches a per-stage trace (counts + scalar
    details only — never result objects, so in-place stamp mutations cannot
    make earlier stages lie) as results['_explain']. Zero cost when False."""
    capped_limit = min(b.limit, 500)
    _explain_on = bool(getattr(b, "explain", False))
    _trace: list = []
    # v0.30 over-fetch: post-fetch filters (retired/_canonical_intent/admission) can drop
    # records and leave a gap; over-fetch a buffer, filter, then trim to capped_limit (below).
    _buf = int(os.environ.get("MEM0_SEARCH_OVERFETCH_BUFFER", "50"))
    _buf = max(0, min(_buf, 500))                      # clamp env to [0,500] (Minor: no range validation)
    if b.rerank:
        _buf = min(_buf, 10)                            # Important: bound the rerank candidate pool so
                                                        # latency stays sane. Cap sized in the CPU era
                                                        # (2.4-4.6s/20 docs); conservative since the
                                                        # 2026-08-13 GPU move (~143ms typical) — raise
                                                        # only with a fresh latency measurement
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
    if _explain_on:
        _trace.append({"stage": "overfetch", "detail": {
            "capped_limit": capped_limit, "buffer": _buf,
            "overfetch_limit": overfetch_limit,
            "collapsed": bool(capped_limit > 0 and overfetch_limit == capped_limit)}})
    # mem0 ranks the dense pool through fusion.py (rank fusion): each result's `score` is the fused
    # score, and end_search adds its raw `cosine`, the value b.threshold compared.
    _fusion.begin_search()
    results = mem.search(
        query=b.query,
        filters=search_filters,
        top_k=overfetch_limit,
        threshold=b.threshold,
    )
    _fusion.end_search(results)
    if _explain_on:
        _n = len(results.get("results") or []) if isinstance(results, dict) else 0
        _trace.append({"stage": "dense_fetch", "out": _n})
        _legs = _fusion.last_legs() or {}
        _trace.append({"stage": "fusion", "detail": {"mode": _fusion.mode(), "legs": {
            str(r.get("id")): _legs.get(str(r.get("id")))
            for r in ((results.get("results") or []) if isinstance(results, dict) else [])[:20]}}})
    # ------------------------------------------------------------------
    # W5 T5 (AMS-56): keyword-recall union leg — DELIBERATE PATH ONLY.
    # mem0's fusion builds candidates exclusively from the dense window, so a
    # bm25-rank-1 / dense-rank->200 target is structurally unreachable. This
    # leg unions keyword-only hits into the pool BEFORE the retired/intent
    # filters, rerank, and admission, so every existing hygiene gate applies
    # to them unchanged. Gated on b.rerank: the bundle (rerank=False
    # hardcoded) and the NLI gate (rerank=False) are structurally excluded,
    # and the gate coincides with the BINDING fail-closed rule below — a
    # lexical item either earns a rerank_score or is dropped. Items carry NO
    # 'score' key by design: a BM25 magnitude on the cosine-calibrated scale
    # would poison the brand-coherence floor (readers are .get()/None-safe —
    # verified admission_gate.py:167-202, freshness.py:68-69).
    # R4 latency guardrail: top_k=12, and the leg stands down entirely at
    # capped_limit>50 (a huge deliberate pool needs no rescue; bounds the
    # rerank pool at dense+12 — sized in the CPU era, still fine on GPU).
    # ------------------------------------------------------------------
    _lex_candidates = 0
    _lex_added = 0
    if b.rerank and capped_limit <= 50 and isinstance(results, dict) \
            and isinstance(results.get("results"), list):
        try:
            _lex_hits = mem.vector_store.keyword_search(
                query=_lemmatize_bm25(b.query), top_k=12, filters=search_filters)
        except Exception:
            log.warning("union leg: keyword_search failed (non-fatal, dense-only)",
                        exc_info=True)
            _lex_hits = None
        if _lex_hits:
            _dense_ids = {str(r.get("id")) for r in results["results"]}
            _CORE_PAYLOAD_KEYS = {"data", "hash", "created_at", "updated_at",
                                  "id", "text_lemmatized", "attributed_to"}
            _PROMOTED_KEYS = ("user_id", "agent_id", "run_id", "actor_id", "role")
            for _p in _lex_hits:
                _lex_candidates += 1
                _pid = str(getattr(_p, "id", "") or "")
                _pl = getattr(_p, "payload", None) or {}
                if not _pid or _pid in _dense_ids or not _pl.get("data"):
                    continue
                # Hydrate to the exact mem0 Step-9 result shape ('memory' is
                # load-bearing for bge_rerank's text_key).
                _item = {
                    "id": _pid,
                    "memory": _pl["data"],
                    "hash": _pl.get("hash"),
                    "created_at": _pl.get("created_at"),
                    "updated_at": _pl.get("updated_at"),
                    "lexical_only": True,
                    "metadata": {k: v for k, v in _pl.items()
                                 if k not in _CORE_PAYLOAD_KEYS
                                 and k not in _PROMOTED_KEYS},
                }
                for _pk in _PROMOTED_KEYS:
                    if _pl.get(_pk) is not None:
                        _item[_pk] = _pl[_pk]
                # Append AFTER dense items so skip heuristics keyed on
                # results[0] keep seeing a dense head.
                results["results"].append(_item)
                _lex_added += 1
    if _explain_on:
        _trace.append({"stage": "union_lexical", "detail": {
            "active": bool(b.rerank and capped_limit <= 50),
            "candidates": _lex_candidates, "added": _lex_added}})
    # v0.13: exclude retired records (retrievable=false) from search results unless caller explicitly opts in
    if not (b.filters or {}).get("include_retired"):
        if isinstance(results, dict) and isinstance(results.get("results"), list):
            _pre = len(results["results"])
            results["results"] = [r for r in results["results"] if (r.get("metadata") or {}).get("retrievable") is not False]
            if _explain_on:
                _trace.append({"stage": "retired_filter", "in": _pre,
                               "out": len(results["results"])})
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
            _pre = len(results["results"])
            results["results"] = [
                r for r in results["results"]
                if not ((r.get("metadata") or {}).get("_canonical_intent"))
            ]
            if _explain_on:
                _trace.append({"stage": "canonical_intent_filter", "in": _pre,
                               "out": len(results["results"])})
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
    if _explain_on:
        _trace.append({"stage": "intent_key_strip",
                       "note": "metadata-only, non-count-changing"})
    items = results.get("results") if isinstance(results, dict) else None
    _rr_status: dict = {}
    if b.rerank and isinstance(items, list) and items:
        # W5 T5.3: FORCE the rerank whenever lexical candidates joined the
        # pool — a silent should_rerank skip (small-N or a unanimous head)
        # would delete every lexical rescue via the fail-closed drop below,
        # exactly the confidently-wrong-dense shape AMS-56 exists to fix.
        reranked_items = bge_rerank(b.query, items, text_key="memory",
                                    force=_lex_added > 0, status_out=_rr_status)
        results = dict(results)
        results["results"] = reranked_items
        # Only mark reranked=True if the reranker actually ran (presence of rerank_score)
        results["reranked"] = any("rerank_score" in r for r in reranked_items)
        # W5 T1.2: honest per-search status — stamped ONLY when rerank was
        # requested, so the bundle path (rerank=False) never carries the key.
        results["rerank_status"] = _rr_status.get("status")
    # W5 T5.4 — BINDING fail-closed drop (review: non-negotiable): a
    # lexical_only item either earned a real rerank_score or it leaves the
    # pipeline. Per-item, which covers reranker fail-open, both skip paths
    # (unreachable under force, kept for defense in depth), and the
    # defensive-append path. Admission floors fail OPEN on missing scores —
    # this drop is the ONLY gate on unscored keyword hits.
    _lex_dropped = 0
    if isinstance(results, dict) and isinstance(results.get("results"), list):
        _pre_drop = len(results["results"])
        results["results"] = [
            r for r in results["results"]
            if not (r.get("lexical_only") and "rerank_score" not in r)
        ]
        _lex_dropped = _pre_drop - len(results["results"])
    if _explain_on:
        _trace.append({"stage": "rerank", "detail": {
            "requested": bool(b.rerank),
            "status": _rr_status.get("status"),
            "forced_for_lexical": bool(b.rerank and _lex_added > 0),
            "lexical_dropped_fail_closed": _lex_dropped}})
    # v0.17 F.4.1: query_class recency policy (applied AFTER rerank, BEFORE return)
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
        # MEM-8: per-call stats — how many results the brandless fail-closed
        # gate hid (brand_scope_required). Echoed on the response below so the
        # MCP shim can hint "pass brand=" instead of starving silently.
        _adm_stats: dict = {}
        results["results"] = apply_admission(
            results["results"],
            scope=scope,
            query_class=_adm_qc,
            layer="server-search",
            stats_out=_adm_stats,
        )
        results["rejected_brand_scoped"] = int(_adm_stats.get("rejected_brand_scoped", 0))
        # W5 T2.1 (ADOPT-3): the two withheld families the shim hints on —
        # a recall that silently drops a superseded/contradicted record is
        # failure-shaped-as-nothing without these.
        results["rejected_superseded"] = int(_adm_stats.get("rejected_superseded", 0))
        results["rejected_contradicted"] = int(_adm_stats.get("rejected_contradicted", 0))
        if _explain_on:
            _trace.append({"stage": "admission", "detail": {
                "query_class": _adm_qc, "stats": dict(_adm_stats)}})
    # v0.30 over-fetch trim: now that retired/intent/admission filters have run on the
    # larger pool, return only the caller's requested capped_limit (the K slots are now
    # filled with non-filtered records, not gapped). Last mutation before logging so the
    # logged returned_count/returned_top_ids reflect what the caller actually receives.
    if isinstance(results, dict) and isinstance(results.get("results"), list):
        _pre_trim = len(results["results"])
        results["results"] = results["results"][:capped_limit]
        if _explain_on:
            _trace.append({"stage": "trim", "in": _pre_trim,
                           "out": len(results["results"]),
                           "n_cut": _pre_trim - len(results["results"])})
    # W5 T1.1: attach the trace strictly behind the flag, OUTSIDE the logging
    # try/except (a trace bug must be visible, not swallowed as log noise).
    if _explain_on and isinstance(results, dict):
        results["_explain"] = {"stages": _trace}
    # W5 T5.5: lexical_kept counted on the FINAL list (post-admission,
    # post-trim) — the M10 path-gating pin reads lexical_candidates.
    _lex_kept = (sum(1 for r in results.get("results") or [] if r.get("lexical_only"))
                 if isinstance(results, dict) else 0)
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
            # W5 T1.5: caller discriminator ('search'|'bundle'|'nli') — actor
            # is constant, so pair-sampling/export could not tell hook-probe
            # traffic from deliberate searches. Historical rows lack the key;
            # readers must treat missing as unknown-legacy (review F7).
            "route": _route,
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
            # W5 T3.3 (ADOPT-4/5): top-10 (was top-3) — pair sampling and
            # replay Jaccard@10 both need the fuller result set. No reader
            # pins the old length (verified); mixed-era rows are handled by
            # both consumers.
            "returned_top_ids": [r.get("id") for r in (results.get("results") or [])[:10]] if isinstance(results, dict) else [],
            "reranked": (results.get("reranked") if isinstance(results, dict) else None),
            # W5 T5.5: the union leg's receipt — candidates seen, unioned into
            # the pool, and surviving the final list. lexical_candidates==0 on
            # every rerank=False row is the M10 path-gating pin.
            "lexical_candidates": _lex_candidates,
            "lexical_added": _lex_added,
            "lexical_kept": _lex_kept,
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
        raise _upstream_error(e)

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
        raise _upstream_error(e)


@app.post("/v1/memories/diagnose")
def diagnose_memory(b: DiagnoseIn, x_api_key: Optional[str] = Header(None)):
    """W5 T1.3 (ADOPT-2, gbrain diagnose-by-target): replay each retrieval
    layer INDEPENDENTLY for one target and name the first stage that eats it.

    POST deliberately — a GET path would be captured by GET /v1/memories/{mid}.
    Read-only by construction: the admission verdict comes from the PURE
    AdmissionPolicy.evaluate (NEVER apply_admission, which bumps the MEM-8
    daily counters and appends to admission-rejected.jsonl — capabilities.py
    documents why). Scope caveat: the bundle path applies an additional
    insight-tier filter + K-cap OUTSIDE _search_core; a durable-class verdict
    of 'returned' therefore does not guarantee bundle inclusion for
    insight-tier records (reported in `caveats`)."""
    auth(x_api_key)
    try:
        # -- target fetch (same reshape as GET /v1/memories/{mid}) --
        records = mem.vector_store.client.retrieve(
            collection_name=mem.vector_store.collection_name,
            ids=[b.target_id], with_payload=True, with_vectors=False,
        )
        if not records:
            raise HTTPException(404, f"memory {b.target_id} not found")
        payload = getattr(records[0], "payload", None) or {}
        target_meta = {k: v for k, v in payload.items()
                       if k not in ("data", "memory")}
        user_id = b.user_id or DEFAULT_USER_ID
        qc = (b.query_class or "durable").strip().lower() or "durable"

        # -- probe 1: dense rank over the SAME tenant, threshold 0.0 --
        capped_limit = min(b.limit, 500)
        _buf = max(0, min(int(os.environ.get("MEM0_SEARCH_OVERFETCH_BUFFER", "50")), 500))
        if b.rerank:
            _buf = min(_buf, 10)
        overfetch_limit = 0 if capped_limit == 0 else min(capped_limit + _buf, 500)
        _fusion.begin_search()
        probe = mem.search(query=b.query, filters={"user_id": user_id},
                           top_k=500, threshold=0.0)
        _fusion.end_search(probe)
        probe_items = (probe.get("results") or []) if isinstance(probe, dict) else []
        dense_rank = None
        dense_score = None
        for _i, _r in enumerate(probe_items):
            if str(_r.get("id")) == b.target_id:
                dense_rank = _i + 1
                dense_score = _r.get("score")
                break
        # The live gate compares the RAW cosine (the fusion keeps the threshold on it), so the
        # threshold verdict must too; the fused score is on another footing. The fusion records every
        # pool candidate's raw cosine for this request in either mode; the fused score stands in only
        # when the search never reached the fusion (checks.fusion reports that as a bypass).
        dense_cosine = ((_fusion.last_legs() or {}).get(b.target_id) or {}).get("cosine")
        gate_score = dense_cosine if dense_cosine is not None else dense_score
        # -- probe 2: flags off the payload --
        retired = payload.get("retrievable") is False
        canonical_intent = bool(payload.get("_canonical_intent"))
        # -- probe 3: pure admission verdict (query_class normalized — the
        # v0.19 L4/L8 class: 'Operational' must not skip the recency branch) --
        adm_record = {"id": b.target_id, "memory": payload.get("data"),
                      "metadata": target_meta, "score": dense_score, "cosine": dense_cosine,
                      "created_at": payload.get("created_at")}
        scope = {"user_id": user_id, "brand": b.brand,
                 "allow_cross_brand": b.allow_cross_brand}
        # WP-4: same stamp-target resolution as the live gate, or the verdict diverges from what
        # search really does (a stamp against a demoted target is ignored there).
        _stamp = target_meta.get("contradicts_canonical")
        adm = default_policy_for_class(qc).evaluate(
            adm_record, scope, qc,
            stamp_tiers=(_resolve_stamp_tiers([_stamp]) if _stamp else None))
        # -- probe 4: rerank delta, bounded to the overfetch-sized pool
        # (review: NEVER the 500-pool — multi-minute CPU) --
        rerank_probe = {"requested": bool(b.rerank), "ran": False,
                        "pre_rank": None, "post_rank": None}
        if b.rerank and dense_rank is not None and dense_rank <= overfetch_limit:
            pool = [dict(r) for r in probe_items[:overfetch_limit]]
            _st: dict = {}
            ranked = bge_rerank(b.query, pool, text_key="memory",
                                force=True, status_out=_st)
            rerank_probe["ran"] = _st.get("status") in _RERANK_RAN_STATUSES
            rerank_probe["pre_rank"] = dense_rank
            for _i, _r in enumerate(ranked):
                if str(_r.get("id")) == b.target_id:
                    rerank_probe["post_rank"] = _i + 1
                    adm_record["rerank_score"] = _r.get("rerank_score")
                    break
        # -- probe 5: freshness weight (same env knobs as the live path) --
        freshness = None
        created = target_meta.get("created_at") or payload.get("created_at")
        if created:
            try:
                # Same env knobs as the live path — incl. kappa (review F4a:
                # a hardcoded 1.0 silently diverges the day the knob is tuned).
                try:
                    _dk = float(os.environ.get("MEM0_WEIBULL_KAPPA", "1.0"))
                    if _dk <= 0:
                        _dk = 1.0
                except (TypeError, ValueError):
                    _dk = 1.0
                c_dt = _dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                age_days = max(0.0, (_dt.datetime.now(_dt.timezone.utc) - c_dt
                                     ).total_seconds() / 86400.0)
                if qc == "operational":
                    _eta = int(os.environ.get("MEM0_OPERATIONAL_HALF_LIFE_DAYS", "30") or 30)
                    freshness = {"age_days": round(age_days, 1),
                                 "weight": round(_freshness_weight(
                                     age_days, float(_eta if _eta > 0 else 30),
                                     _dk), 6)}
                elif (qc == "durable" and _env_flag("MEM0_DURABLE_FRESHNESS_ENABLED")
                      and target_meta.get("tier") == "evidence"):
                    _deta = int(os.environ.get("MEM0_DURABLE_FRESHNESS_HALF_LIFE_DAYS", "365") or 365)
                    freshness = {"age_days": round(age_days, 1),
                                 "weight": round(_freshness_weight(
                                     age_days, float(_deta if _deta > 0 else 365),
                                     _dk), 6)}
            except (ValueError, TypeError):
                pass
        # -- verdict: first eating stage, in live pipeline order --
        if dense_rank is None:
            verdict = "dense_retrieval:below_500_horizon"
        elif gate_score is not None and b.threshold and float(gate_score) < float(b.threshold):
            verdict = f"threshold:{gate_score}_below_{b.threshold}"
        elif dense_rank > overfetch_limit:
            verdict = f"overfetch_pool:rank_{dense_rank}_exceeds_{overfetch_limit}"
        elif retired:
            verdict = "retired_filter"
        elif canonical_intent:
            verdict = "canonical_intent_filter"
        elif not adm.admit:
            verdict = f"admission:{adm.reason}"
        elif (rerank_probe["ran"] and rerank_probe["post_rank"] is not None
              and rerank_probe["post_rank"] > capped_limit):
            verdict = f"trim_after_rerank:rank_{rerank_probe['post_rank']}_exceeds_{capped_limit}"
        elif not b.rerank and dense_rank > capped_limit:
            verdict = f"trim:rank_{dense_rank}_exceeds_{capped_limit}"
        else:
            verdict = "returned"
        return {
            "target_id": b.target_id,
            "verdict": verdict,
            "dense": {"rank_at_500": dense_rank, "score": dense_score, "cosine": dense_cosine,
                      "overfetch_limit": overfetch_limit,
                      "within_overfetch": (dense_rank is not None
                                           and dense_rank <= overfetch_limit)},
            "flags": {"retired": retired, "canonical_intent": canonical_intent},
            "admission": {"admit": adm.admit, "reason": adm.reason,
                          "query_class": qc},
            "rerank": rerank_probe,
            "freshness": freshness,
            "caveats": [
                "bundle path additionally drops insight-tier records and caps K outside _search_core",
                "union lexical leg (rerank=True) can rescue targets past the dense horizon — a below-horizon verdict is dense-only",
                "extra Qdrant filters of the failing search (kind/source/tier) are not replayed here, and the filters.tier=canonical admission-class derivation is not applied",
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("diagnose failed")
        raise _upstream_error(e)


@app.put("/v1/memories/{mid}")
def update(
    mid: str, b: UpdateIn,
    background_tasks: BackgroundTasks,
    x_api_key: Optional[str] = Header(None),
    x_user_direct_token: Optional[str] = Header(None, alias="X-User-Direct-Token"),
    x_user_direct_ts: Optional[str] = Header(None, alias="X-User-Direct-Ts"),
    x_user_direct_nonce: Optional[str] = Header(None, alias="X-User-Direct-Nonce"),
    x_ams_service_key: Optional[str] = Header(None, alias="X-AMS-Service-Key"),
    actor: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
):
    """Update memory text. v0.17 Phase A: canonical/insight tier gate applied BEFORE update.
    v0.17 Phase F.1: X-User-Direct-Nonce header accepted for replay protection.
    AMS-01 (P0, 2026-08-07): the full existing payload is carried over INTO the
    update (mem0 rebuilds the payload from scratch — every custom key used to be
    destroyed on every PUT; only tier was restored). See payload_carryover.
    1.32.5: a privileged actor label needs the service key (require_service_credential)."""
    auth(x_api_key)
    from security_invariants import require_service_credential
    _service_verified = require_service_credential(actor, x_ams_service_key)
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
        service_verified=_service_verified,
    )
    # v0.28 Phase 2a: promote-canary on PUT — after HMAC enforcement, before the write.
    # When the target record is canonical, reject imperative text (declarative facts only).
    if current_tier == "canonical" and is_imperative_canonical(b.text.strip()):
        raise HTTPException(
            422,
            "canonical is declarative facts only; rephrase as a fact, not a standing order. "
            f"(detected imperative phrasing in: {b.text.strip()[:80]!r})",
        )
    # AMS-01/F4: serialize the whole read-modify-write against PATCH /tier,
    # PATCH /metadata and the async NLI stamp for this record. The tier gate
    # above stays OUTSIDE the lock on purpose (gate-before-update semantics;
    # residual value-level race on the imperative canary requires a concurrent
    # user-direct HMAC promotion inside a microsecond window — accepted and
    # documented, the payload itself can no longer be lost).
    with _mid_write_lock(mid):
        # AMS-01/F5: FAIL-CLOSED pre-read. Proceeding without the existing
        # payload IS the wipe — a Qdrant error refuses the PUT (503). A
        # missing record proceeds: mem0's own update raises its not-found.
        try:
            _pre_pts = mem.vector_store.client.retrieve(
                collection_name=mem.vector_store.collection_name,
                ids=[mid], with_payload=True,
            )
        except Exception as e:
            # A client-side 4xx (e.g. malformed id) is a PERMANENT fault —
            # returning 503 would make the MCP shim queue the op to its outbox
            # and retry a never-satisfiable PUT at every drain. Only genuine
            # availability faults may be 503 (the shim's documented
            # queue-on-503 contract).
            _status = getattr(e, "status_code", None)
            if isinstance(_status, int) and 400 <= _status < 500:
                raise HTTPException(
                    400, f"AMS-01: pre-update read rejected by the store "
                         f"(bad memory id?): {str(e)[:120]}",
                )
            raise HTTPException(
                503,
                "AMS-01: pre-update payload read failed; refusing to PUT — a "
                f"blind update would destroy custom metadata: {str(e)[:120]}",
            )
        _pre_payload = dict(_pre_pts[0].payload or {}) if _pre_pts else {}
        _carryover = compute_carryover(_pre_payload)
        try:
            # Pre-merge: mem0 deepcopies metadata= into the rebuilt payload and
            # then overwrites its own keys (data/hash/text_lemmatized/created_at/
            # updated_at), so preservation is ATOMIC in the upsert — no restore
            # window, and carry-over can never poison the recomputed values.
            result = mem.update(memory_id=mid, data=b.text,
                                metadata=(_carryover or None))
            _put_carryover_bump(puts=1)
            # v0.17 F.2.5 / H1, generalized by AMS-01: post-verify the carry-over
            # LANDED. With the pre-merge this is a tripwire for mem0-contract
            # drift (the single external assumption: _update_memory deepcopies
            # metadata= into the new payload). PRESENCE-triggered, not
            # value-compared — race-benign for concurrent value changes.
            _expected = dict(_carryover)
            if current_tier and "tier" not in _expected:
                _expected["tier"] = current_tier
            _missing = {}
            if _expected:
                import time as _time
                _backoff_delays = [0.1, 0.3, 0.9]  # seconds
                _restored_total = 0
                for _attempt in range(len(_backoff_delays) + 1):
                    if _attempt:
                        _time.sleep(_backoff_delays[_attempt - 1])
                    try:
                        _post = mem.vector_store.client.retrieve(
                            collection_name=mem.vector_store.collection_name,
                            ids=[mid], with_payload=True,
                        )
                        _post_payload = dict(_post[0].payload or {}) if _post else {}
                        _missing = {k: v for k, v in _expected.items()
                                    if k not in _post_payload}
                        if not _missing:
                            break
                        mem.vector_store.client.set_payload(
                            collection_name=mem.vector_store.collection_name,
                            payload=_missing, points=[mid],
                        )
                        _restored_total += len(_missing)
                        # Immediate re-verify so a successful restore on the
                        # last attempt is not misreported as lost.
                        _post = mem.vector_store.client.retrieve(
                            collection_name=mem.vector_store.collection_name,
                            ids=[mid], with_payload=True,
                        )
                        _post_payload = dict(_post[0].payload or {}) if _post else {}
                        _missing = {k: v for k, v in _expected.items()
                                    if k not in _post_payload}
                        if not _missing:
                            break
                    except Exception:
                        log.exception(
                            "AMS-01: carry-over post-verify attempt %d failed for mid=%s",
                            _attempt + 1, mid,
                        )
                else:
                    # for/else: the loop exhausted without a clean break. When
                    # _missing is non-empty the restore-failure path below owns
                    # the outcome (500 for gated tiers). When it is EMPTY here,
                    # every post-verify attempt raised: no 500 fires, which is
                    # defensible (the pre-merge already carried the payload
                    # atomically) — but say so, loudly, in the log.
                    if not _missing:
                        log.error(
                            "AMS-01: post-verify NEVER COMPLETED for mid=%s — the PUT "
                            "returned 200 on the strength of the atomic pre-merge alone",
                            mid,
                        )
                if _restored_total:
                    _put_carryover_bump(restored=_restored_total)
                    log.warning(
                        "AMS-01: post-verify had to restore %d carry-over key(s) for mid=%s "
                        "— the pre-merge should make this ~impossible; suspect mem0-contract drift",
                        _restored_total, mid,
                    )
                if _missing:
                    _put_carryover_bump(lost=len(_missing))
                    _tier_now = str(_expected.get("tier") or "")
                    # AMS-40 (2026-08-08): the 500 fires for ANY truthy tier,
                    # not just canonical/insight. A 'stable' record could
                    # silently lose its tier here — the asymmetry made the
                    # loudness of the alarm depend on which tier happened to
                    # be losing data, when the defect is identical.
                    if _tier_now:
                        raise HTTPException(
                            500,
                            f"F.2.5/H1/AMS-01: carry-over restore failed after retries for "
                            f"memory_id={mid!r} (tier={_tier_now!r}); missing keys: "
                            f"{sorted(_missing)}. The record may be in an inconsistent state "
                            "— manual verification required. The update itself succeeded in "
                            "mem0 but the payload carry-over was not restored in Qdrant.",
                        )
                    log.error(
                        "AMS-01: carry-over keys still missing after retries for mid=%s: %s",
                        mid, sorted(_missing),
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
            # F3 (2026-08-07): a PUT changes the text, so it re-enters the NLI
            # write-gate exactly like an add. compute_carryover deliberately
            # dropped the checked-markers; this re-queues judgment of the NEW
            # text. Async (after the response), fail-soft — same contract as add.
            if NLI_GATE_ENABLED:
                background_tasks.add_task(
                    _nli_gate_stamp,
                    [{"id": mid, "memory": b.text}],
                    _pre_payload.get("user_id"),
                    _carryover.get("brand"),
                )
            # 1.32.4: a hand-written "SUPERSEDED ... by mem0 <id>" marker does nothing for
            # retrieval (the gate reads superseded_by, never text). Say so to the caller.
            try:
                import supersession as _ss
                _marker = _ss.classify_text(b.text)
                if (_marker and _marker.kind in ("full", "partial")
                        and not _pre_payload.get("superseded_by") and isinstance(result, dict)):
                    result = {**result, "supersede_note": _ss.SUPERSEDE_NOTE,
                              "supersede_marker": {"kind": _marker.kind,
                                                   "winner_id": _marker.winner_id}}
            except Exception:
                log.exception("supersede marker note failed (non-fatal)")
            return result
        except HTTPException:
            raise
        except Exception as e:
            log.exception("update failed")
            raise _upstream_error(e)

@app.patch("/v1/memories/{mid}/tier")
def update_tier(mid: str, b: TierIn, x_api_key: Optional[str] = Header(None),
                x_user_direct_token: Optional[str] = Header(None, alias="X-User-Direct-Token"),
                x_user_direct_ts: Optional[str] = Header(None, alias="X-User-Direct-Ts"),
                x_user_direct_nonce: Optional[str] = Header(None, alias="X-User-Direct-Nonce"),
                x_ams_service_key: Optional[str] = Header(None, alias="X-AMS-Service-Key")):
    """Update a memory's tier. Server-enforced actor requirements per tier.
    Canonical promotions additionally require a valid HMAC X-User-Direct-Token header (v0.14 B),
    and so does a move OUT of canonical (session 12: signed action "demote"; see the gate below).
    v0.19 Phase G: the token is validated as format-2
    (<ts>|<nonce>|promote|<mid>|<reason>) via security_invariants —
    nonce + replay protection, HMAC verified before the nonce is burned (MED-8).
    v0.20 Phase G: the nonce-less format-1 path (<ts>|<mid>|<reason>) is
    REMOVED — a promotion without X-User-Direct-Nonce is rejected outright.
    AMS-22 (2026-08-08): write-ahead audit — a tier-change-intent ledger line is
    appended BEFORE the mutation (503 refusal if that append fails), then the
    tier-change completion line after; completion-append failure stays fail-soft
    (the mutation already happened — the intent line is the audit floor)."""
    auth(x_api_key)
    if b.tier not in PROMOTE_ALLOWED_TIERS:
        raise HTTPException(400, f"invalid tier: {b.tier}; allowed: {sorted(PROMOTE_ALLOWED_TIERS)}")
    actor = (b.actor or "").strip()
    reason = (b.reason or "").strip()
    if not actor:
        raise HTTPException(400, "actor is required (e.g., 'user-direct', 'c1-consolidator', 'claude-autonomous')")
    # 1.32.5: a privileged actor label (the consolidator's) counts only with the service key.
    from security_invariants import require_service_credential
    require_service_credential(actor, x_ams_service_key)
    # DEMOTION gate: a move OUT of canonical, and since 1.32.5 out of insight, signs its own
    # "demote" action. Without it an API-key holder could demote the record and then PUT or DELETE
    # it with no token, because assert_writable gates those only while the record keeps its tier.
    # No job label exempts an insight: nothing in the stack demotes one, so it leaves insight only
    # through the operator's signed path, like canonical. fetch_current_tier fails closed: a store
    # error is a 503, and a point with no tier field reads as canonical. A missing point is a 404.
    current_tier = None
    _signed_demote = False
    if b.tier != "canonical":
        from security_invariants import fetch_current_tier, tier_change_hmac_action, _NOT_FOUND
        _ct = fetch_current_tier(mem.vector_store.client, mem.vector_store.collection_name, mid)
        if _ct == _NOT_FOUND:
            raise HTTPException(404, f"memory {mid} not found")
        current_tier = _ct
        if tier_change_hmac_action(current_tier, b.tier) == "demote":
            if not reason:
                raise HTTPException(400, f"demoting a {current_tier} record requires non-empty 'reason' (audit-trail policy)")
            from security_invariants import validate_hmac_user_direct
            validate_hmac_user_direct(
                mid, "demote", reason,
                x_user_direct_token, x_user_direct_ts,
                x_user_direct_nonce=x_user_direct_nonce,
            )
            _signed_demote = True
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
    # transport field: "autonomous" when actor is from CANONICAL_AUTOPROMOTE_ALLOWED (dream-autopromote),
    # "cli-user-direct" for HMAC-validated user-direct canonical, "rest-api" otherwise.
    # 1.32.5: a signed demotion out of insight is HMAC-validated too (_signed_demote), so it is
    # ledgered as the signed CLI path like a canonical demotion, never as a plain rest-api change.
    if b.tier == "canonical" and actor in CANONICAL_AUTOPROMOTE_ALLOWED:
        transport = "autonomous"
    elif b.tier == "canonical" and x_user_direct_token:
        transport = "cli-user-direct"
    elif _signed_demote or (current_tier == "canonical" and x_user_direct_token):
        transport = "cli-user-direct"
    else:
        transport = "rest-api"
    class _TierRaced(Exception):
        """The record entered a protected tier between the gate's read and this write."""

    try:
        # AMS-01/F4: serialize against a concurrent PUT's read-modify-write —
        # without this, a promotion landing inside the PUT window was silently
        # demoted by the PUT's stale-tier upsert (store and ledger disagreed).
        with _mid_write_lock(mid):
            # TOCTOU: the demotion gate read the tier BEFORE this lock. A promotion that landed in
            # between would let this unsigned change move a record that is canonical (or, since
            # 1.32.5, insight) NOW, so every change that was not signed for "demote" re-reads the
            # tier under the lock and refuses when the record now needs that signature.
            # 1.32.4: a superseded record never enters a protected tier (it would be a hidden
            # canonical, and its supersession link would expose it to an unsigned cascade delete).
            if b.tier in ("canonical", "insight"):
                import supersession as _ss
                _refusal = _ss.promotion_refusal(_supersede_read(mid), b.tier)
                if _refusal:
                    raise HTTPException(_refusal.status, f"{_refusal.code}: {_refusal.message}")
            if b.tier != "canonical" and not _signed_demote:
                _tier_now = fetch_current_tier(mem.vector_store.client, mem.vector_store.collection_name, mid)
                if _tier_now == _NOT_FOUND:
                    # Deleted while this change was in flight: nothing to change, and no ledger line.
                    raise HTTPException(404, f"memory {mid} not found")
                if tier_change_hmac_action(_tier_now, b.tier) == "demote":
                    raise _TierRaced(_tier_now)
            # AMS-22: write-ahead intent — appended BEFORE the mutation so an authority
            # change can never complete without an audit trace. If this append fails the
            # mutation is REFUSED (503, retryable); a loud failure AFTER the mutation
            # would be worse than useless (the tier would already have changed). It sits
            # after the re-check so a refused (409) change leaves no unpaired intent line;
            # PUT appends its ledger line under the same lock, so the lock order matches.
            try:
                _append_ledger({
                    "ts": now, "event": "tier-change-intent", "memory_id": mid,
                    "tier": b.tier, "actor": actor, "reason": reason or None,
                    "transport": transport, "status": "intent",
                    "judge_model": (b.judge_model or None), "schema_version": "v18",
                })
            except Exception as e:
                log.exception("AMS-22: tier-change intent ledger append failed; refusing mutation")
                raise HTTPException(
                    503,
                    "audit ledger unavailable (intent append failed); tier change refused "
                    f"— retry when ~/.mem0 is writable: {str(e)[:120]}",
                )
            mem.vector_store.client.set_payload(
                collection_name=mem.vector_store.collection_name,
                payload={"tier": b.tier, "updated_at": now, "tier_actor": actor},
                points=[mid],
            )
    except _TierRaced as _raced:
        _rt = _raced.args[0] if _raced.args else "canonical"
        raise HTTPException(409, (
            f"the record became {_rt} while this tier change was in flight; retry. "
            f"Moving it out of {_rt} needs the signed 'demote' token "
            "(mem0-canonize.sh --action demote)."
        ))
    except HTTPException:
        raise
    except Exception as e:
        log.exception("tier-update failed")
        raise _upstream_error(e)
    # Completion ledger append AFTER successful payload update. Fail-SOFT by
    # design (AMS-22): the mutation already happened and the intent line above
    # is the audit floor — a 500 here would misreport a completed change.
    try:
        _append_ledger({
            "ts": now, "event": "tier-change", "memory_id": mid,
            "tier": b.tier, "actor": actor, "reason": reason or None,
            "transport": transport, "status": "done",
            "judge_model": (b.judge_model or None), "schema_version": "v18",
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
    x_ams_service_key: Optional[str] = Header(None, alias="X-AMS-Service-Key"),
):
    """Shallow-merge new metadata fields into the existing Qdrant payload.
    Cannot change `tier` (use PATCH /tier for that). Used by re-extraction
    (marks originals retrievable=false), decay (sets temporal.expires_at),
    and dream-consolidator (stamps touched_by_dream).

    v0.17 Phase A: canonical/insight tier gate runs BEFORE the FORBIDDEN_KEYS check.
    v0.17 Phase F.1: X-User-Direct-Nonce header accepted for replay protection.

    EVERY successful merge is appended to the tier-ledger so all post-hoc
    mutations are audit-covered (lens S1: shallow-merge could otherwise be
    used to undo retirement silently).

    1.32.5: a privileged actor label (TRUSTED_PATCH_ACTORS, LEGACY_PATCH_ACTOR_KEYS, the insight
    consolidators) counts only with the service key, checked BEFORE assert_writable, whose
    trusted-actor early return skips the HMAC gate."""
    auth(x_api_key)
    from security_invariants import require_service_credential
    _service_verified = require_service_credential(b.actor, x_ams_service_key)
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
        service_verified=_service_verified,
    )
    # Key-level policy (forbidden retrieval-gating keys, legacy server-flow actors, per-actor
    # TRUSTED_PATCH_ACTORS allowlists): security_invariants.authorize_metadata_patch, a pure
    # function since 1.32.4 so the whole decision is testable headless.
    from security_invariants import authorize_metadata_patch
    authorize_metadata_patch(current_tier, b.actor, b.metadata.keys(), service_verified=_service_verified)
    # Bump updated_at so lead-7 sort by recency in memory-index-build.py is correct
    merged = dict(b.metadata)
    merged["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        # AMS-01/F4: serialize against a concurrent PUT's read-modify-write.
        with _mid_write_lock(mid):
            mem.vector_store.client.set_payload(
                collection_name=mem.vector_store.collection_name,
                payload=merged,
                points=[mid],
            )
    except Exception as e:
        log.exception("metadata update failed")
        raise _upstream_error(e)
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


def _supersede_read(mid: str) -> Optional[dict]:
    """Fail-closed point read for the supersede door: None = not found, a store error = 503."""
    try:
        pts = mem.vector_store.client.retrieve(
            collection_name=mem.vector_store.collection_name,
            ids=[mid], with_payload=True, with_vectors=False,
        )
    except Exception as e:
        _status = getattr(e, "status_code", None)
        if isinstance(_status, int) and 400 <= _status < 500:
            raise HTTPException(400, f"supersede: the store rejected id {mid!r}: {str(e)[:120]}")
        raise HTTPException(503, f"supersede: store read failed, nothing was written: {str(e)[:120]}")
    return dict(pts[0].payload or {}) if pts else None


class _SupersedeStore:
    """The Qdrant side of supersession.run_supersede / run_unsupersede."""

    def read(self, mid: str) -> Optional[dict]:
        return _supersede_read(mid)

    def set_payload(self, mid: str, payload: dict) -> None:
        mem.vector_store.client.set_payload(
            collection_name=mem.vector_store.collection_name, payload=payload, points=[mid])

    def delete_keys(self, mid: str, keys: list) -> None:
        mem.vector_store.client.delete_payload(
            collection_name=mem.vector_store.collection_name, keys=keys, points=[mid])


@contextlib.contextmanager
def _supersede_locks(*mids):
    """Both records' write locks, taken in sorted key order (deadlock-free against single-lock holders)."""
    with contextlib.ExitStack() as stack:
        for key in sorted({_mid_lock_key(m) for m in mids}):
            stack.enter_context(_mid_write_lock(key))
        yield


def _supersede_call(fn, **kw) -> dict:
    """Run one supersession transaction and map its outcomes to HTTP."""
    import supersession as _ss
    try:
        return fn(_SupersedeStore(), _append_ledger, **kw)
    except _ss.Refused as e:
        raise HTTPException(e.refusal.status, f"{e.refusal.code}: {e.refusal.message}")
    except _ss.LedgerUnavailable as e:
        log.exception("supersede intent ledger append failed; refusing")
        raise HTTPException(503, "audit ledger unavailable (intent append failed); nothing was "
                                 f"written — retry when ~/.mem0 is writable: {str(e)[:120]}")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("supersede write failed")
        raise _upstream_error(e)


def _supersede_finish(out: dict) -> dict:
    """Append the completion ledger line (fail-soft: the intent line is the audit floor)."""
    entry = out.pop("_entry", None)
    if entry is not None:
        try:
            _append_ledger(entry)
        except Exception:
            log.exception("ledger append failed for %s (the intent line is the audit floor)",
                          entry.get("event"))
    return out


@app.post("/v1/memories/{mid}/supersede")
def supersede_memory(mid: str, b: SupersedeIn, x_api_key: Optional[str] = Header(None)):
    """Record that `mid` is superseded by `b.winner_id` (1.32.4; supersession.py has the rules).

    scope="full" sets superseded_by, so the admission gate hides the record outside the history
    class; scope="partial" appends {winner_id, detail, at} to partially_superseded_by and never
    hides it. The server enforces the refusal matrix whoever calls (a canonical or insight record,
    a retired or superseded winner, a different user, a different brand or a branded winner over a
    neutral record are refused), caps detail and reason, and stamps the actor itself. A repeated
    call is a no-op. Undo: DELETE /v1/memories/{id}/supersede."""
    auth(x_api_key)
    import supersession as _ss
    from admission_gate import _shared_brands_from_env
    if not _ss.is_memory_id(mid) or not _ss.is_memory_id(b.winner_id):
        raise HTTPException(400, "bad-id: both ids must be memory ids (UUIDs)")
    with _supersede_locks(mid, b.winner_id):
        out = _supersede_call(
            _ss.run_supersede, mid=mid, winner_id=b.winner_id, scope=b.scope, detail=b.detail,
            reason=b.reason, source=b.source, shared_brands=_shared_brands_from_env(),
            now_iso=_dt.datetime.now(_dt.timezone.utc).isoformat())
    return _supersede_finish(out)


@app.delete("/v1/memories/{mid}/supersede")
def unsupersede_memory(mid: str, scope: str = Query("full"), reason: Optional[str] = Query(None),
                       x_api_key: Optional[str] = Header(None)):
    """Undo a supersession: scope full | partial | all removes those keys (the record reappears in
    default retrieval once superseded_by is gone). Same tier rules as POST; nothing to clear is a
    no-op."""
    auth(x_api_key)
    import supersession as _ss
    if not _ss.is_memory_id(mid):
        raise HTTPException(400, "bad-id: the id must be a memory id (UUID)")
    with _supersede_locks(mid):
        out = _supersede_call(_ss.run_unsupersede, mid=mid, scope=scope, reason=reason,
                              now_iso=_dt.datetime.now(_dt.timezone.utc).isoformat())
    return _supersede_finish(out)

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
                created_by="manual",  # goal redesign 2026-08-09: manual rows are exempt from the 90d auto-abandon
            )
        return {"ok": True, "goal_id": gid}
    except Exception as e:
        log.exception("goal create failed")
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)



# AMS-57 (2026-08-08): declared BEFORE /v1/goals/{goal_id} — that route types
# goal_id as int, so a literal "count" reaching it first would 422 rather than
# fall through. Same ordering rule as /v1/goals/tree above.
@app.get("/v1/goals/count")
def count_goals_endpoint(x_api_key: Optional[str] = Header(None)):
    """True goal count + per-status breakdown.

    Exists because the health probe used to report the size of a `limit=200`
    LIST page as the total, which silently pinned at 200 and read as healthy
    once the real backlog passed it.
    """
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_count_goals(conn)
    except Exception as e:
        log.exception("goals count failed")
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


@app.post("/v1/open_questions/search")
def search_open_questions_endpoint(b: OpenQuestionSearchIn, x_api_key: Optional[str] = Header(None)):
    """FTS5 keyword search across open questions. status='all' to include resolved."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_search_open_questions(conn, query=b.query, brand=b.brand, status=b.status, limit=b.limit)
    except Exception as e:
        log.exception("open_questions search failed")
        raise _upstream_error(e)


@app.get("/v1/open_questions/count")
def count_open_questions_endpoint(x_api_key: Optional[str] = Header(None)):
    """True open-question count + per-status breakdown (AMS-57).

    Declared before /v1/open_questions/{oq_id} for the same path-matching
    reason as the goals counterpart.
    """
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_count_open_questions(conn)
    except Exception as e:
        log.exception("open_questions count failed")
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
        raise _upstream_error(e)


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
    # v1.0 A1 (mandated-pull): the memory_recall MCP verb pulls the bundle on demand,
    # an explicit deeper recall in addition to what the per-prompt UserPromptSubmit
    # hook injects (empty block, canonical facts, another brand's scope). A manual pull
    # MUST NOT upsert an episode or every recall would pollute
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

    # 1b) C10 (2026-09-22): a background task notification is a machine turn, not a prompt anyone
    #     typed. The checkpoint above still lands (0.A is unchanged), nothing is searched, and the
    #     sections come back empty, so no client can render a block for it. The Windows clients
    #     already skip the bundle for these turns; this applies the same verdict at the one place
    #     every path meets (hook_contract.is_machine_turn_prompt, tested against the shared
    #     corpus in scripts/windows/tests/fixtures).
    if _is_machine_turn_prompt(b.prompt):
        out["machine_turn"] = True
        for _wk in ("rejected_brand_scoped", "rejected_superseded", "rejected_contradicted"):
            out[_wk] = 0
        out["memories"] = []
        out["goals"] = []
        out["open_questions"] = []
        return out

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
        # rerank=False is LOAD-BEARING (W5 T5): it structurally excludes the
        # keyword union leg and the cross-encoder call from the per-prompt
        # bundle hot path. Originally a CPU-cost decision; since the
        # 2026-08-13 GPU move the cost argument is weak (~143ms typical), but
        # the decision now rests on the MEASURED quality result (2026-08-11,
        # n=578 paired): bge reranking does not improve recall@1 over dense
        # order (p=0.43). Revisit only with a reranker that measurably does
        # (llama.cpp PR #24083 / nemotron-rerank was such a candidate).
        sr = _search_core(SearchIn(
            query=(b.prompt or "")[:500],
            filters=filters,
            limit=_tp["memory_cap"] + 2,        # v1.12 HK-6: +2 headroom — insight hits are dropped below
            threshold=_tp["relevance_threshold"],  # v1.0 R2: 0.30 both tiers (kept; calibration-confirmed)
            rerank=False,
            query_class="durable",
        ), _route="bundle")
        _mems = sr.get("results", []) if isinstance(sr, dict) else []
        # W5 T2.1 (ADOPT-3): forward the withheld counters onto the bundle
        # response — _search_core computes them but the bundle previously
        # dropped every non-results field, leaving memory_recall blind to
        # superseded/contradicted withholding.
        for _wk in ("rejected_brand_scoped", "rejected_superseded",
                    "rejected_contradicted"):
            out[_wk] = int(sr.get(_wk) or 0) if isinstance(sr, dict) else 0
        # v1.12 HK-6: the hook client's admission list REJECTS tier=insight — observed
        # in production as "0.D admission: 2 of 2 results rejected" (full bundle latency
        # paid, zero memories injected, dead churn in admission-rejected.jsonl). Filter
        # server-side so an insight hit can't burn a K-slot the client will throw away;
        # the +2 over-fetch above lets an admissible memory take that slot instead.
        out["memories"] = [m for m in _mems
                           if ((m.get("metadata") or {}).get("tier")) != "insight"][:_tp["memory_cap"]]
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
                _raw_fallback_bump(fired=1)
                log.info("raw-fallback FIRED for episode %s",
                         rf.get("episode_id"))
            else:
                # AMS-39: an abstain is the DOMINANT outcome and was
                # indistinguishable from the feature being dead — count it.
                _raw_fallback_bump(abstained=1)
        except Exception:
            log.exception("bundle: raw-trace fallback failed (non-fatal)")
            _raw_fallback_bump(errors=1)

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
            # WP-4: never serve a session its OWN goals/open questions (they are minted from the
            # session you are in, so they were echoed straight back: 41 % of prompts), and rank what
            # is left by how recently an episode touched it rather than by priority alone.
            out["goals"] = _episodic_list_goals(conn, status="open", brand=_bb, only_brand_neutral=(not _bb), initiative=b.initiative, limit=_tp["goal_cap"], exclude_session_id=b.session_id, rank_by_recency=True)
            out["open_questions"] = _episodic_list_open_questions(conn, status="open", brand=_bb, only_brand_neutral=(not _bb), initiative=b.initiative, limit=_tp["oq_cap"], exclude_session_id=b.session_id, rank_by_recency=True)
    except Exception:
        log.exception("bundle: goals/open_questions failed (non-fatal)")
        out.setdefault("goals", [])
        out.setdefault("open_questions", [])
    return out


@app.post("/v1/episodes")
def create_episode(b: EpisodeIn, background_tasks: BackgroundTasks, x_api_key: Optional[str] = Header(None)):
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
                # GOAL REDESIGN (operator-approved 2026-08-09, "goals are
                # earned, not minted"): ingest no longer CREATES goal rows.
                # Measured basis: ~99 goals+OQs minted per day, 98.4% never
                # touched again, 2.8% ever seen by a second session — the
                # per-session mint was write-only exhaust. A fuzzy MATCH still
                # links and advances (that is the earning signal); a miss now
                # serializes the intent (title included) onto the episode's own
                # JSON column, where the nightly recurrence promoter mines it
                # and creates a goal only when the same intent recurs across
                # >=2 distinct sessions.
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
                            _episodic_link_episode_to_goal(conn, episode_id, goal_id, link_type="advanced_goal", delta_text=item.delta_text, commit=False)
                            advanced_serialized.append({"goal_id": goal_id, "delta_text": item.delta_text})
                        else:
                            advanced_serialized.append({
                                "goal_title": item.goal_title.strip(),
                                "delta_text": item.delta_text,
                                "unmatched": True,
                            })

                blocked_serialized = []
                if b.blocked_goals:
                    for item in b.blocked_goals:
                        if not item.goal_title or not item.goal_title.strip():
                            continue
                        candidates = _episodic_find_goal_by_title_fuzzy(conn, item.goal_title, brand=b.brand, limit=1)
                        if candidates:
                            goal_id = candidates[0]["id"]
                            _episodic_link_episode_to_goal(conn, episode_id, goal_id, link_type="blocked_goal", delta_text=item.block_reason, commit=False)
                            # Flip status to blocked
                            _episodic_update_goal_status(conn, goal_id, "blocked", commit=False)
                            blocked_serialized.append({"goal_id": goal_id, "block_reason": item.block_reason})
                        else:
                            # Goal redesign 2026-08-09: no mint on miss — the
                            # intent (title kept) rides the episode JSON for
                            # the recurrence promoter.
                            blocked_serialized.append({
                                "goal_title": item.goal_title.strip(),
                                "block_reason": item.block_reason,
                                "unmatched": True,
                            })

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
        # 1.32.4: a COLD embedder (restarting, unloaded, 'exited prematurely') used to drop the vector
        # for good. The hook that posts this episode gives up after 5 s, so nothing waits here: the
        # retry runs as a background task after the response, capped by _episode_embed_gate, and what
        # it cannot recover the daily upkeep step (episodic-reconcile --upkeep) embeds.
        _ep_payload = {"brand": b.brand, "goal": (b.goal or "")[:300], "summary": (b.summary or "")[:800]}
        try:
            if _episode_indexable_summary(b.summary):
                _ep_vec = embed_episode_summary(mem.embedding_model, b.summary)
                if _ep_vec is not None:
                    upsert_episode_embedding(mem.vector_store.client, episode_id, _ep_vec, _ep_payload)
        except Exception as e:
            log.exception("episode embed/upsert failed (non-fatal)")
            _slot = False
            try:
                if _embedder_503.retry_later(e) is not None:
                    _slot = _episode_embed_gate.acquire(episode_id)
                    if _slot:
                        log.warning("episode embed deferred ep=%s", episode_id)
                        background_tasks.add_task(
                            run_deferred_embed, mem.embedding_model,
                            lambda ep, vec, payload: upsert_episode_embedding(mem.vector_store.client, ep, vec, payload),
                            _episode_embed_gate, episode_id, b.summary, _ep_payload)
                    else:
                        log.warning("episode embed not deferred ep=%s (retry cap); the daily upkeep step will "
                                    "embed it", episode_id)
            except Exception:
                if _slot:
                    _episode_embed_gate.release(episode_id)
                log.exception("episode embed deferral failed (non-fatal)")

        return {"ok": True, "session_id": b.session_id, "episode_id": episode_id}
    except Exception as e:
        log.exception("episode create failed")
        raise _upstream_error(e)


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
        raise _upstream_error(e)


@app.get("/v1/episodes/count")
def episodes_count(
    since: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None),
):
    """Return {count, last_ended_at, last_complete_ended_at} for health checks and Test-MemoryStack."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_count(conn, since, brand)
    except Exception as e:
        log.exception("episode count failed")
        raise _upstream_error(e)


@app.get("/v1/episodes")
def list_episodes(
    recent: int = Query(10),
    brand: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None),
):
    """List last N episodes by ended_at desc. Default recent=10.

    ``state`` (complete | in_progress | abandoned) narrows the window to one state; without it the
    unfinished rows, which carry the newest ``ended_at``, can fill it. The summary of a row that is
    not complete comes back without the machine turns (task notifications, relayed agent messages)
    the running summary once recorded."""
    auth(x_api_key)
    try:
        with _episodic_connect() as conn:
            return _episodic_recent(conn, recent, brand, state)
    except Exception as e:
        log.exception("episode list failed")
        raise _upstream_error(e)


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
        raise _upstream_error(e)


@app.delete("/v1/memories/{mid}")
def delete(
    mid: str,
    x_api_key: Optional[str] = Header(None),
    x_user_direct_token: Optional[str] = Header(None, alias="X-User-Direct-Token"),
    x_user_direct_ts: Optional[str] = Header(None, alias="X-User-Direct-Ts"),
    x_user_direct_nonce: Optional[str] = Header(None, alias="X-User-Direct-Nonce"),
    x_ams_service_key: Optional[str] = Header(None, alias="X-AMS-Service-Key"),
    actor: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
    cascade: bool = Query(False),
):
    """Hard delete. AMS-22 (2026-08-08): audit is write-AHEAD — a `delete-intent`
    ledger entry is appended BEFORE any deletion (503 refusal if that append
    fails, nothing deleted), then the `delete` completion entry after; a
    completion-append failure stays fail-soft (the deletion already happened —
    the intent line is the audit floor).
    v0.17 Phase A: canonical/insight tier gate applied BEFORE deletion.
    v0.17 Phase F.1: X-User-Direct-Nonce for replay protection; cascade=true for delete_linked.
    1.32.5: a privileged actor label needs the service key (require_service_credential)."""
    auth(x_api_key)
    from security_invariants import require_service_credential
    _service_verified = require_service_credential(actor, x_ams_service_key)
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
        service_verified=_service_verified,
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
    # AMS-22: write-ahead intent — the audit record precedes the destruction, so
    # a hard delete can never complete with zero ledger trace. Append failure
    # (disk full, perms, ~/.mem0 unavailable) REFUSES the deletion with a
    # retryable 503; nothing has been deleted at this point. The intent stays
    # small on purpose (no prior_payload) — the completion entry carries it.
    try:
        _append_ledger({
            "event": "delete-intent",
            "memory_id": mid,
            "actor": (actor or "rest-api"),
            "reason": (reason or "DELETE /v1/memories/{mid}"),
            "prior_tier": prior_tier or ((prior_payload or {}).get("tier") if prior_payload else None),
            "transport": "cli-user-direct" if x_user_direct_token else "rest-api",
            "cascade": cascade,
            "status": "intent",
        })
    except Exception as e:
        log.exception("AMS-22: delete intent ledger append failed; refusing deletion")
        raise HTTPException(
            503,
            "audit ledger unavailable (intent append failed); delete refused "
            f"— retry when ~/.mem0 is writable: {str(e)[:120]}",
        )
    cascade_skipped: list[str] = []
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
        import supersession as _ss
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
                # 1.32.4: any API-key holder can create a superseded_by link (the supersede door), so
                # the cascade never deletes a protected member through one, nor one it cannot read:
                # the root's authorisation does not extend to a canonical or insight record.
                if _ss.cascade_protected(_linked_payload):
                    cascade_skipped.append(_linked_id)
                    log.warning("cascade: skipped protected or unreadable chain member %s (root=%s)",
                                _linked_id, mid)
                    continue
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
        raise _upstream_error(e)
    # AMS-22: completion entry — fail-SOFT by design (the deletion already
    # happened; the delete-intent line above is the audit floor).
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
            "status": "done",
        })
    except Exception:
        log.exception("ledger append failed for delete")
    extra: dict = {}
    if cascade_skipped:
        extra["cascade_skipped_protected"] = cascade_skipped
    if not cascade:
        # 1.32.4: records superseded by the one just deleted stay hidden behind a missing winner.
        # Name them (fail-soft) so the caller can clear or re-point them; nothing is changed here.
        try:
            _orph = mem.vector_store.client.scroll(
                collection_name=mem.vector_store.collection_name,
                scroll_filter={"must": [{"key": "superseded_by",
                                         "match": {"value": str(mid).strip().lower()}}]},
                with_payload=False, with_vectors=False, limit=50,
            )
            _orph_ids = [str(p.id) for p in ((_orph[0] if _orph else None) or [])]
            if _orph_ids:
                extra["orphaned_supersessions"] = _orph_ids
                log.warning("delete %s left %d record(s) superseded by a deleted winner: %s",
                            mid, len(_orph_ids), _orph_ids[:10])
        except Exception:
            log.warning("delete %s: could not list records superseded by it", mid)
    if extra and isinstance(result, dict):
        result = {**result, **extra}
    return result
