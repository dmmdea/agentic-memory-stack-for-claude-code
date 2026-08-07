"""W3 (2026-08-07): the capability/liveness manifest for /health/deep.

The stack accumulated capabilities faster than it accumulated mouths: a
capability could die (a scheduled task unregistered, a queue unwatched, an
auth token expired) and nothing structural would ever say so. This module is
the single answer sheet: every named capability, what proves it alive, and a
pure evaluator that folds the /health/deep checks dict into one honest
per-capability verdict.

Two honesty rules dominate the design (review-bound):

- **F9 — zero-signal is never 'alive'.** Activity counters (put_carryover_today
  and friends) prove liveness only when they COUNTED something; a zero on a
  quiet day is silence, not health, and maps to 'unknown'. Only a probe that
  positively exercised the path may say 'alive'.
- **F8 — an unknown role cannot convict.** ``dead_required`` is computed
  against the box's role: 'both' rows count on every box, 'brain'/'replica'
  rows only when the role matches, and when the role is UNKNOWN the
  role-scoped rows never enter dead_required (you cannot claim a brain-required
  capability is required-dead on a box you cannot identify as a brain).

Rows are PROBE-BACKED ONLY. A capability we can name but not evaluate carries
probe 'none — ...' and ALWAYS evaluates to 'unknown' — a named admission of
blindness beats silent omission. Detection latency for every row is one
session/night by design: the manifest is read when /health/deep is read.
Do not add a watchdog (house rule: no unattended schedulers).

W4 (2026-08-07) retired the eight 'none — W4' rows into real verdicts. Three
review-bound constraints shaped how:

- **F11 — no active reranker probe here.** The reranker is a CPU cross-encoder
  and scripts/wsl/deploy.sh gates on /health/deep with a bounded curl right
  after a restart; an active rerank probe would hang deploys on a cold model.
  The row reads reranker.py's PASSIVE counters, bumped by real search traffic.
  The active 3-doc probe stays in Test-MemoryStack (L5), where 90s is affordable.
- **F12 — one stamp is not a signal.** ``last-l1a`` only stamps on a SUCCESSFUL
  extraction, so an honest quiet stretch was indistinguishable from a dead
  extractor. Conviction now needs attempt-fresh AND success-stale across a
  MULTI-DAY window, and is capped at 'degraded' — never 'dead'.
- **NEVER probe through ``apply_admission``.** ``admission_selfprobe`` calls
  ``AdmissionPolicy.evaluate()`` directly: apply_admission bumps the MEM-8
  daily rejection counters (which /health/deep reports two lines earlier) and
  appends to ~/.mem0/admission-rejected.jsonl, so probing through it would
  pollute the audit log and the starvation metrics on every health read.

Human table: docs/capability-manifest.md — generated from this literal;
tests/test_capabilities.py pins the doc's ids against CAPABILITIES.
"""

import datetime as _dt

from admission_gate import AdmissionPolicy, default_policy_for_class

# Freshness thresholds for the nightly receipts (dream, prune/gather, backup
# manifest, dedup report — all daily cadence). One missed night is tolerated
# (alive <= 48h: detection latency is one session/night by design); two to
# four missed nights is late ('degraded'); beyond ~4 nights the job is not
# late, it is dead.
FRESH_H = 48.0
DEAD_H = 96.0
# sparse-leg coverage below this is a half-backfilled corpus: degraded, not
# dead (mirrors Test-MemoryStack's WARN threshold on the gating check).
SPARSE_COVERAGE_FLOOR = 0.95
# contradiction review queue depth beyond this is an unwatched backlog.
QUEUE_BACKLOG_DEGRADED = 50

# --- W4 windows ---
# Session-scoped receipts (l1a, the SessionStart banner, the MCP shim receipt)
# are written every session, so 48h of silence is already generous: it means no
# session at all, not a broken path. Their 'fresh' window is therefore FRESH_H.
#
# F12 conviction window: a stale SUCCESS stamp only accuses the extractor once
# it is stale by DAYS while attempts kept arriving. 10 minutes (the extractor's
# own throttle) or even a day is normal — many sessions legitimately produce
# nothing durable to extract.
L1A_CONVICT_H = 96.0
# consecutive rerank transport failures before the leg reads dead. The row is
# 'optional' so this never convicts; it is the human signal.
RERANK_DEAD_FAILURES = 3
# A replay that ran within this window proves the offline-outbox mechanism was
# actually exercised. Replays are RARE by nature (they need an offline stint),
# so the nightly 48h window would leave the row permanently unknown.
OUTBOX_PROOF_H = 720.0

# The manifest literal. Fixed row shape (cross-track contract):
#   id                    stable capability id (kebab-case)
#   what                  one-line human description
#   probe                 short human string naming the source check/file;
#                         'none — W4' rows always evaluate 'unknown';
#                         a 'retired — ...' prefix always evaluates 'retired'
#   required              'brain' | 'replica' | 'both' | 'optional'
#   escalation_documented True for rows whose SOURCE CHECK is informational
#                         (never flips /health/deep ok) but the row is
#                         required — deadness escalates via dead_required
#                         here, not via the deploy gate (F17).
CAPABILITIES = [
    {"id": "mem0-api",
     "what": "REST memory API (the /v1 surface the MCP shim and hooks call)",
     "probe": "/health/deep responded (this evaluation ran inside it)",
     "required": "both", "escalation_documented": False},
    {"id": "qdrant-store",
     "what": "vector store holding the memory corpus",
     "probe": "checks.qdrant (collection status + point count)",
     "required": "both", "escalation_documented": False},
    {"id": "embedder",
     "what": "dense embedder (768-dim round-trip)",
     "probe": "checks.embedder",
     "required": "both", "escalation_documented": False},
    {"id": "bm25-sparse-leg",
     "what": "lexical BM25 leg of hybrid retrieval",
     "probe": "checks.sparse_leg (deterministic oldest-point canary)",
     "required": "both", "escalation_documented": False},
    {"id": "canonical-key",
     "what": "HMAC canonical-key chain (runtime/DPAPI/plaintext provider)",
     "probe": "checks.canonical_key",
     "required": "brain", "escalation_documented": False},
    {"id": "put-carryover",
     "what": "PUT payload carry-over (metadata survives text rewrites)",
     "probe": ("checks.put_carryover_today (daily activity counters); "
               "unknown-on-idle by design (F9) — exerciser: Test-MemoryStack I13 PUT canary"),
     "required": "both", "escalation_documented": True},
    {"id": "mojibake-tripwire",
     "what": "CP437 corpus-encoding tripwire",
     "probe": "checks.mojibake (payload scan)",
     "required": "both", "escalation_documented": True},
    {"id": "contradiction-review-queue",
     "what": "human review queue for contradiction verdicts is watched",
     "probe": "checks.pending_contradiction_reviews (queue depth)",
     "required": "brain", "escalation_documented": True},
    {"id": "dream-cycle",
     "what": "nightly dream consolidation runs",
     "probe": "job_liveness.last_dream_age_h (throttle marker content)",
     "required": "brain", "escalation_documented": True},
    {"id": "drift-guard",
     "what": "retrieval-drift guard compares around each consolidation",
     "probe": "retrieval_drift (~/.mem0/retrieval-drift-state.json)",
     "required": "brain", "escalation_documented": True},
    {"id": "backup-pipeline",
     "what": "daily stack snapshot + manifest writer",
     "probe": "job_liveness.backup_manifest_age_h (newest manifest age)",
     "required": "brain", "escalation_documented": True},
    {"id": "dedup-job",
     "what": "daily semantic dedup sweep",
     "probe": "job_liveness.dedup_report_age_h (report rewritten every run)",
     "required": "brain", "escalation_documented": True},
    {"id": "memory-index",
     "what": "dream gather step (memory index refresh)",
     "probe": "job_liveness.gather_age_h (gather.json receipt age)",
     "required": "brain", "escalation_documented": True},
    {"id": "sweep-job",
     "what": "dream prune step (consolidation-completed receipt)",
     "probe": "job_liveness.prune_age_h (prune.json receipt age)",
     "required": "brain", "escalation_documented": True},
    {"id": "codex-auth",
     "what": "Codex CLI auth serving dream judgment",
     "probe": "derived: a fresh dream ran, therefore its judge authenticated",
     "required": "brain", "escalation_documented": True},
    # ---- W4: the eight formerly-blind rows, now probe-backed ----
    {"id": "reranker",
     "what": "cross-encoder rerank leg on search",
     "probe": ("checks.reranker (PASSIVE counters bumped by real search reranks; "
               "no active probe here by F11 — active 3-doc probe is Test-MemoryStack L5)"),
     "required": "optional", "escalation_documented": True},
    {"id": "l1a-extraction",
     "what": "hook-fired L1a memory extractor",
     "probe": ("job_liveness.l1a_attempt_age_h vs l1a_success_age_h "
               "(two-signal, F12: convicts only attempt-fresh + success-stale >96h; capped at degraded)"),
     "required": "brain", "escalation_documented": True},
    {"id": "sessionstart-banner",
     "what": "SessionStart resume/context banner",
     "probe": ("job_liveness.sessionstart_banner_age_h (unconditional stamp at the top of "
               "storage-cap-check.sh) cross-checked against l1a_attempt_age_h"),
     "required": "both", "escalation_documented": True},
    {"id": "mcp-shim",
     "what": "MCP shim bridging the CLI to the memory API",
     "probe": ("job_liveness.mcp_shim_receipt_age_h + mcp_shim_host_match + "
               "mcp_shim_stack_version (host-keyed start receipt; version skew = stale launch path)"),
     "required": "brain", "escalation_documented": True},
    {"id": "admission-gate",
     "what": "server-side retrieval admission gate",
     "probe": "checks.admission_probe (in-process AdmissionPolicy.evaluate() self-probe, zero I/O)",
     "required": "both", "escalation_documented": True},
    {"id": "tier-policy",
     "what": "tier enforcement (canonical HMAC, insight allowlist)",
     "probe": ("checks.admission_probe.tier_rejected — read-half server-probed; "
               "write-half proven by Test-MemoryStack I-rows on demand"),
     "required": "both", "escalation_documented": True},
    {"id": "brand-isolation",
     "what": "brand-scoped retrieval isolation",
     "probe": ("checks.admission_probe.brand_rejected (read half) + "
               "job_liveness.brand_scope_misscoped/brand_scope_age_h (nightly write-side audit)"),
     "required": "both", "escalation_documented": True},
    {"id": "offline-outbox",
     "what": "shim outbox queue/replay when the authority is unreachable",
     "probe": ("job_liveness.outbox_depth + outbox_replayed_age_h + outbox_drain_log_age_h "
               "— evaluated on the REPLICA only (F8 keeps it non-convicting on a brain box)"),
     "required": "replica", "escalation_documented": True},
]

_ROLES = ("brain", "replica")


# ----------------------------------------------------------------------
# In-process admission self-probe (zero I/O, zero side effects)
# ----------------------------------------------------------------------

# Three synthetic records, one per dimension the manifest names. They are
# SYNTHETIC on purpose: the probe must not depend on what happens to be in the
# corpus, and it must be able to run on an empty store.
_PROBE_SCOPE_BRANDLESS = {}
_PROBE_CANONICAL = {"id": "selfprobe-canonical",
                    "metadata": {"tier": "canonical"}}
_PROBE_BRANDED = {"id": "selfprobe-branded",
                  "metadata": {"tier": "evidence", "brand": "selfprobe-brand"}}
_PROBE_NEUTRAL = {"id": "selfprobe-neutral",
                  "metadata": {"tier": "evidence"}}


def admission_selfprobe():
    """Exercise the admission gate IN PROCESS and report what it decided.

    NEVER routes through ``apply_admission``. That function bumps the MEM-8
    daily rejection counters (surfaced on this very endpoint as
    checks.admission_rejections_today) and appends every rejection to
    ~/.mem0/admission-rejected.jsonl — so probing through it would corrupt the
    starvation metrics and write three audit lines on EVERY /health/deep read,
    including the one deploy.sh performs and the one the session banner
    triggers. ``AdmissionPolicy.evaluate()`` is the pure decision function
    underneath: same code path, no counters, no log, no disk.

    Assertions (durable class, brandless scope — the shim's default shape):
      tier_rejected     tier=canonical is NOT in the durable allowlist -> reject
      brand_rejected    a BRANDED record on a brandless scope -> reject
                        (the v0.19 M4 fail-closed rule; before it, a brandless
                        search returned every brand's records)
      neutral_admitted  a brand-neutral evidence record -> admit
                        (the negative control: a gate that rejects EVERYTHING
                        would otherwise pass the two checks above)

    Never raises: a policy-construction failure is reported as ok=False with an
    error string, not as an exception out of /health/deep."""
    out = {"ok": False, "tier_rejected": None, "brand_rejected": None,
           "neutral_admitted": None, "query_class": "durable"}
    try:
        policy = default_policy_for_class("durable")
        if not isinstance(policy, AdmissionPolicy):
            out["error"] = "default_policy_for_class returned a non-policy"
            return out
        out["tier_rejected"] = not policy.evaluate(
            _PROBE_CANONICAL, _PROBE_SCOPE_BRANDLESS, "durable").admit
        out["brand_rejected"] = not policy.evaluate(
            _PROBE_BRANDED, _PROBE_SCOPE_BRANDLESS, "durable").admit
        out["neutral_admitted"] = policy.evaluate(
            _PROBE_NEUTRAL, _PROBE_SCOPE_BRANDLESS, "durable").admit
        out["ok"] = bool(out["tier_rejected"] and out["brand_rejected"]
                         and out["neutral_admitted"])
    except Exception as e:                       # noqa: BLE001 — probe must not 500
        out["error"] = f"{e.__class__.__name__}: {e}"[:160]
    return out


# ----------------------------------------------------------------------
# Per-capability verdicts (pure helpers over the checks dict)
# ----------------------------------------------------------------------

def _age_state(age_h):
    """Nightly-receipt verdict: fresh -> alive, late -> degraded, gone ->
    dead, absent signal -> unknown (never 'alive' from silence)."""
    if age_h is None:
        return "unknown"
    if age_h <= FRESH_H:
        return "alive"
    if age_h <= DEAD_H:
        return "degraded"
    return "dead"


def _ok_state(check):
    if not isinstance(check, dict):
        return "unknown"
    return "alive" if check.get("ok") else "dead"


def _sparse_leg_state(check):
    if not isinstance(check, dict):
        return "unknown"
    if not check.get("ok"):
        return "dead"
    cov = check.get("coverage")
    if isinstance(cov, (int, float)) and cov < SPARSE_COVERAGE_FLOOR:
        return "degraded"
    return "alive"


def _canonical_key_state(check):
    if not isinstance(check, dict):
        return "unknown"
    if not check.get("ok"):
        return "dead"          # keyless-degraded: blob exists, nothing served it
    if check.get("present"):
        return "alive"
    # ok without a key = no key configured at all: promotions are disabled by
    # design there, but the capability is not operative either.
    return "degraded"


def _put_carryover_state(check):
    if not isinstance(check, dict):
        return "unknown"
    if (check.get("keys_lost") or 0) > 0:
        return "dead"          # an active AMS-01 recurrence — keys are being lost
    if not check.get("puts"):
        return "unknown"       # F9: zero-signal (no PUTs today) is NOT alive
    if (check.get("keys_restored") or 0) > 0:
        return "degraded"      # surviving on the post-verify fallback
    return "alive"


def _mojibake_state(check):
    if not isinstance(check, dict) or "error" in check:
        return "unknown"
    if (check.get("hits") or 0) > 0:
        return "degraded"      # tripwire alive, corpus dirty
    return "alive"


def _review_queue_state(depth):
    # Queue DEPTH, not an activity counter: 0 is the healthy empty-queue state
    # (F9 targets counters whose zero means "nothing exercised the path"; an
    # empty review queue is itself the good outcome), so 0 -> alive.
    if not isinstance(depth, int) or isinstance(depth, bool):
        return "unknown"
    return "degraded" if depth > QUEUE_BACKLOG_DEGRADED else "alive"


def _drift_guard_state(check):
    if not isinstance(check, dict):
        return "unknown"
    if check.get("state_present") is not True:
        return "unknown"       # the private side may simply not be deployed yet
    if check.get("error"):
        return "unknown"
    if (check.get("consecutive_snapshot_failures") or 0) >= 2:
        return "dead"          # the guard itself stopped comparing (fail-open)
    age = check.get("age_hours")
    if age is not None and age > DEAD_H:
        return "dead"
    if check.get("alarm") or check.get("compat_fallback"):
        return "degraded"
    if age is None or age > FRESH_H:
        return "degraded"
    return "alive"


def _codex_auth_state(jl):
    # V1 keeps this honest and simple: a fresh dream ran end-to-end, therefore
    # its Codex judge authenticated. Anything else is 'unknown' — a stale or
    # absent dream proves nothing about the auth token either way.
    if not isinstance(jl, dict):
        return "unknown"
    age = jl.get("last_dream_age_h")
    if age is not None and age <= FRESH_H:
        return "alive"
    return "unknown"


def _fresh(age_h):
    """True when a session-scoped receipt is inside the tolerated window."""
    return age_h is not None and age_h <= FRESH_H


def _reranker_state(check, now_s=None):
    """PASSIVE verdict (F11). The counters are in-process, so a restart zeroes
    them: no traffic since boot is silence, not health (F9 -> unknown). A
    SUCCESS older than the fresh window is silence too — the leg has not been
    exercised since, and an old success cannot vouch for the leg today."""
    if not isinstance(check, dict):
        return "unknown"
    fails = check.get("consecutive_rerank_failures") or 0
    if not isinstance(fails, int) or isinstance(fails, bool):
        fails = 0
    if fails >= RERANK_DEAD_FAILURES:
        return "dead"              # the transport is consistently refusing
    last_ok = check.get("last_rerank_ok_ts")
    if last_ok is None:
        # No success since boot. A failure or two with no success is still a
        # real (thin) signal; zero of both means nothing exercised the leg.
        return "degraded" if fails else "unknown"
    if fails:
        return "degraded"          # succeeding sometimes, failing now
    age_h = None
    if now_s is not None:
        try:
            age_h = (float(now_s) - float(last_ok)) / 3600.0
        except (TypeError, ValueError):
            age_h = None
    if age_h is not None and age_h > FRESH_H:
        return "unknown"           # last exercised days ago: silence, not health
    return "alive"


def _l1a_state(jl):
    """Two-signal extractor verdict (review F12, BLOCKER).

    ``last-l1a`` stamps ONLY on a successful extraction and ``last-sessionstart-
    capture`` is not written on five early-exit paths, so the naive
    'success stamp is stale => dead' rule convicted the commonest healthy
    state: sessions happened and had nothing durable to extract. The extractor
    now writes ``last-l1a-attempt`` unconditionally at entry, and this rule
    convicts only when attempts are STILL ARRIVING while success has been
    absent for DAYS — and even then caps at 'degraded', never 'dead'.

    An extractor that has never once succeeded stays 'unknown': from one
    last-attempt stamp we cannot distinguish a broken extractor from a box
    whose first session simply had nothing to keep."""
    attempt = jl.get("l1a_attempt_age_h")
    success = jl.get("l1a_success_age_h")
    if attempt is None and success is None:
        return "unknown"                      # zero signal
    if _fresh(success):
        return "alive"                        # a real extraction landed recently
    if success is None:
        return "unknown"                      # never succeeded here — see docstring
    if _fresh(attempt) and success > L1A_CONVICT_H:
        return "degraded"                     # capped by F12: never 'dead'
    return "unknown"                          # quiet box, or attempts also stopped


def _sessionstart_banner_state(jl):
    """Same two-signal shape as l1a: the banner stamp is unconditional (it is
    written above storage-cap-check.sh's cold-server gate), so a stale stamp
    while SESSIONS are demonstrably still happening — l1a fires on the very
    same lifecycle hooks — means the hook stopped running. Capped at degraded:
    a quiet box must never read 'dead'."""
    banner = jl.get("sessionstart_banner_age_h")
    if _fresh(banner):
        return "alive"
    if _fresh(jl.get("l1a_attempt_age_h")):
        # Sessions are running; the banner is not stamping (absent or stale).
        return "degraded"
    return "unknown"                          # no corroborating session signal


def _mcp_shim_state(jl, stack_version=None):
    """Host-keyed receipt verdict (review F13).

    ~/.mem0 travels in stack backups, so a receipt whose host is NOT this box
    is treated as absent — it proves nothing local. A stack_version that
    disagrees with the running server means the launched copy is stale (the
    AMS-02 class: the Windows .claude/scripts trio is refreshed only by
    install/2-windows-config.ps1, not by deploy.sh). Capped at 'degraded' for
    the same reason as l1a: an operator who has not opened a session is idle,
    not broken."""
    age = jl.get("mcp_shim_receipt_age_h")
    if jl.get("mcp_shim_host_match") is False:
        age = None                            # a foreign box's receipt
    if age is None:
        return "degraded" if _fresh(jl.get("l1a_attempt_age_h")) else "unknown"
    if age > FRESH_H:
        return "degraded" if _fresh(jl.get("l1a_attempt_age_h")) else "unknown"
    theirs = jl.get("mcp_shim_stack_version")
    if stack_version and theirs and str(theirs) != str(stack_version):
        return "degraded"                     # stale launch-path copy
    return "alive"


def _admission_probe_state(check):
    if not isinstance(check, dict):
        return "unknown"
    if check.get("error") and check.get("ok") is not True:
        return "unknown"                      # the probe itself could not run
    return "alive" if check.get("ok") else "dead"


def _tier_policy_state(check):
    """Read-half only: the durable class must refuse a canonical-tier record.
    The WRITE half (403 on add, HMAC-signed PATCH /tier) has no cheap in-process
    probe — Test-MemoryStack's I-rows exercise it on demand, and the manifest
    probe string says exactly that rather than implying full coverage."""
    if not isinstance(check, dict):
        return "unknown"
    rejected = check.get("tier_rejected")
    if rejected is None:
        return "unknown"
    return "alive" if rejected else "dead"


def _brand_isolation_state(check, jl):
    """Read half from the in-process probe; the nightly brand-scope audit is a
    write-side corroborator that can only DEGRADE. An absent audit file does not
    degrade (the job may not be deployed on this box) — a stale one does, which
    is the mouth this row exists to give the audit."""
    if not isinstance(check, dict):
        return "unknown"
    rejected = check.get("brand_rejected")
    if rejected is None:
        return "unknown"
    if not rejected:
        return "dead"                         # brandless scope leaked a branded record
    mis = jl.get("brand_scope_misscoped")
    if isinstance(mis, int) and not isinstance(mis, bool) and mis > 0:
        return "degraded"                     # canonical facts invisible to their brand
    age = jl.get("brand_scope_age_h")
    if age is not None and age > DEAD_H:
        return "degraded"                     # the audit stopped running
    return "alive"


def _offline_outbox_state(jl):
    """Replica-scoped. Depth is the alarm; a recent REPLAY is the only positive
    proof the mechanism works (an empty outbox on a box that never went offline
    is silence, not health — F9)."""
    depth = jl.get("outbox_depth")
    drain = jl.get("outbox_drain_log_age_h")
    replayed = jl.get("outbox_replayed_age_h")
    if depth is None and drain is None and replayed is None:
        return "unknown"
    if isinstance(depth, int) and not isinstance(depth, bool) and depth > 0:
        # Queued writes exist. Draining recently -> working through it;
        # nothing draining -> the writes are stranded.
        if drain is not None and drain <= FRESH_H:
            return "degraded"
        return "dead"
    if replayed is not None and replayed <= OUTBOX_PROOF_H:
        return "alive"                        # a real replay exercised the path
    return "unknown"                          # empty queue, never exercised


def _state_for(row, checks, stack_version=None, now_s=None):
    probe = row.get("probe") or ""
    if probe.startswith("retired"):
        return "retired"
    if probe.startswith("none"):
        return "unknown"
    cid = row["id"]
    jl = checks.get("job_liveness")
    jl = jl if isinstance(jl, dict) else {}
    if cid == "mem0-api":
        # evaluate() runs inside the served /health/deep handler: the checks
        # dict existing IS the probe result.
        return "alive" if isinstance(checks, dict) else "unknown"
    if cid == "qdrant-store":
        return _ok_state(checks.get("qdrant"))
    if cid == "embedder":
        return _ok_state(checks.get("embedder"))
    if cid == "bm25-sparse-leg":
        return _sparse_leg_state(checks.get("sparse_leg"))
    if cid == "canonical-key":
        return _canonical_key_state(checks.get("canonical_key"))
    if cid == "put-carryover":
        return _put_carryover_state(checks.get("put_carryover_today"))
    if cid == "mojibake-tripwire":
        return _mojibake_state(checks.get("mojibake"))
    if cid == "contradiction-review-queue":
        return _review_queue_state(checks.get("pending_contradiction_reviews"))
    if cid == "dream-cycle":
        return _age_state(jl.get("last_dream_age_h"))
    if cid == "drift-guard":
        return _drift_guard_state(checks.get("retrieval_drift"))
    if cid == "backup-pipeline":
        return _age_state(jl.get("backup_manifest_age_h"))
    if cid == "dedup-job":
        return _age_state(jl.get("dedup_report_age_h"))
    if cid == "memory-index":
        return _age_state(jl.get("gather_age_h"))
    if cid == "sweep-job":
        return _age_state(jl.get("prune_age_h"))
    if cid == "codex-auth":
        return _codex_auth_state(jl)
    # ---- W4 rows ----
    if cid == "reranker":
        return _reranker_state(checks.get("reranker"), now_s)
    if cid == "l1a-extraction":
        return _l1a_state(jl)
    if cid == "sessionstart-banner":
        return _sessionstart_banner_state(jl)
    if cid == "mcp-shim":
        return _mcp_shim_state(jl, stack_version)
    if cid == "admission-gate":
        return _admission_probe_state(checks.get("admission_probe"))
    if cid == "tier-policy":
        return _tier_policy_state(checks.get("admission_probe"))
    if cid == "brand-isolation":
        return _brand_isolation_state(checks.get("admission_probe"), jl)
    if cid == "offline-outbox":
        return _offline_outbox_state(jl)
    return "unknown"           # a row without an evaluator is a named blind spot


def _required_applies(required, role):
    if required == "both":
        return True            # required on every box, role known or not
    if required in _ROLES:
        return required == role  # F8: unknown role -> role-scoped rows never apply
    return False               # 'optional' (and anything else) never convicts


def evaluate(checks, role, capabilities=None, now=None, stack_version=None):
    """Pure evaluator: /health/deep checks dict + role -> the manifest verdict.

    Fixed contract shape (cross-track — do not rename keys):
        {role, states: {id: 'alive'|'degraded'|'dead'|'unknown'|'retired'},
         dead_required (list), unknown (list), evaluated_at}

    No I/O, no clock reads beyond the evaluated_at stamp (injectable), so the
    truth table pins headless. ``capabilities`` is injectable for tests only —
    production always evaluates the module literal. ``stack_version`` is the
    RUNNING server's release, compared against the MCP shim receipt's copy so a
    stale launch path (AMS-02 class) is caught server-side; omitting it simply
    skips that comparison."""
    caps = CAPABILITIES if capabilities is None else capabilities
    checks = checks if isinstance(checks, dict) else {}
    role = role.strip().lower() if isinstance(role, str) and role.strip() else None
    if role not in _ROLES:
        role = None
    stamp = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    # Epoch form of the SAME injected stamp — the only clock the row evaluators
    # see, so the truth table still pins headless.
    try:
        now_s = stamp.timestamp()
    except (AttributeError, TypeError, ValueError, OSError, OverflowError):
        now_s = None
    states = {row["id"]: _state_for(row, checks, stack_version, now_s)
              for row in caps}
    dead_required = sorted(
        row["id"] for row in caps
        if states[row["id"]] == "dead" and _required_applies(row.get("required"), role)
    )
    unknown = sorted(cid for cid, st in states.items() if st == "unknown")
    return {
        "role": role,
        "states": states,
        "dead_required": dead_required,
        "unknown": unknown,
        "evaluated_at": stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp),
    }
