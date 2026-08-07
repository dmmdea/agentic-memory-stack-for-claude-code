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

V1 rows are PROBE-BACKED ONLY. Capabilities we can name but not yet evaluate
carry probe 'none — W4' and ALWAYS evaluate to 'unknown' — a named admission
of blindness beats silent omission. Detection latency for every row is one
session/night by design: the manifest is read when /health/deep is read.
Do not add a watchdog (house rule: no unattended schedulers).

Human table: docs/capability-manifest.md — generated from this literal;
tests/test_capabilities.py pins the doc's ids against CAPABILITIES.
"""

import datetime as _dt

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
     "probe": "checks.put_carryover_today (daily activity counters)",
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
    # ---- named-but-not-yet-probed (W4): ALWAYS 'unknown' ----
    {"id": "reranker",
     "what": "cross-encoder rerank leg on search",
     "probe": "none — W4", "required": "optional",
     "escalation_documented": False},
    {"id": "l1a-extraction",
     "what": "hook-fired L1a memory extractor",
     "probe": "none — W4", "required": "brain",
     "escalation_documented": False},
    {"id": "sessionstart-banner",
     "what": "SessionStart resume/context banner",
     "probe": "none — W4", "required": "both",
     "escalation_documented": False},
    {"id": "mcp-shim",
     "what": "MCP shim bridging the CLI to the memory API",
     "probe": "none — W4", "required": "both",
     "escalation_documented": False},
    {"id": "admission-gate",
     "what": "server-side retrieval admission gate",
     "probe": "none — W4", "required": "both",
     "escalation_documented": False},
    {"id": "tier-policy",
     "what": "tier enforcement (canonical HMAC, insight allowlist)",
     "probe": "none — W4", "required": "both",
     "escalation_documented": False},
    {"id": "brand-isolation",
     "what": "brand-scoped retrieval isolation",
     "probe": "none — W4", "required": "both",
     "escalation_documented": False},
    {"id": "offline-outbox",
     "what": "shim outbox queue/replay when the authority is unreachable",
     "probe": "none — W4", "required": "replica",
     "escalation_documented": False},
]

_ROLES = ("brain", "replica")


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


def _state_for(row, checks):
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
    return "unknown"           # a row without an evaluator is a named blind spot


def _required_applies(required, role):
    if required == "both":
        return True            # required on every box, role known or not
    if required in _ROLES:
        return required == role  # F8: unknown role -> role-scoped rows never apply
    return False               # 'optional' (and anything else) never convicts


def evaluate(checks, role, capabilities=None, now=None):
    """Pure evaluator: /health/deep checks dict + role -> the manifest verdict.

    Fixed contract shape (cross-track — do not rename keys):
        {role, states: {id: 'alive'|'degraded'|'dead'|'unknown'|'retired'},
         dead_required (list), unknown (list), evaluated_at}

    No I/O, no clock reads beyond the evaluated_at stamp (injectable), so the
    truth table pins headless. ``capabilities`` is injectable for tests only —
    production always evaluates the module literal."""
    caps = CAPABILITIES if capabilities is None else capabilities
    checks = checks if isinstance(checks, dict) else {}
    role = role.strip().lower() if isinstance(role, str) and role.strip() else None
    if role not in _ROLES:
        role = None
    states = {row["id"]: _state_for(row, checks) for row in caps}
    dead_required = sorted(
        row["id"] for row in caps
        if states[row["id"]] == "dead" and _required_applies(row.get("required"), role)
    )
    unknown = sorted(cid for cid, st in states.items() if st == "unknown")
    stamp = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    return {
        "role": role,
        "states": states,
        "dead_required": dead_required,
        "unknown": unknown,
        "evaluated_at": stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp),
    }
