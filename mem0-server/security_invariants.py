"""v0.17 Phase A: shared security helpers for write-path policy enforcement.
v0.17 Phase F.1: HMAC nonce/jti replay protection added.
v0.17 Final fix-pass: H2/H7 nonce threading.Lock+fsync+atomic-rename; H5 fail-closed tier lookup; H8 trusted-actor allowlist.

The policy matrix (current_tier × action):
- canonical × PUT/DELETE/PATCH-metadata  → require HMAC user-direct token
- canonical × PATCH-tier-demote          → require HMAC user-direct token signed for action "demote"
                                           (session 12; tier_change_hmac_action). Before it, PATCH /tier
                                           gated only promotions, so an API-key holder could demote a
                                           canonical record and then PUT/DELETE it with no token.
- insight   × PUT/DELETE/PATCH-metadata  → require actor in INSIGHT_ALLOWED_ACTORS OR valid HMAC user-direct
                                           (1.32.5: the actor counts only when proven by the service key)
- insight   × PATCH-tier-demote          → require HMAC user-direct "demote" (1.32.5; no job exemption)
- any tier  × a server-side job label    → refused (403 service-credential-required) unless the request
                                           carries the authority's service key (1.32.5)
- stable / evidence / temporal × any     → no extra gate (existing flow unchanged)
- canonical × PATCH-metadata with a hide key (RETRIEVAL_HIDE_KEYS: superseded_by, contradicts_canonical)
                                         → refused (403) for EVERY actor, trusted ones included
                                           (authorize_metadata_patch, 1.32.4): a trusted actor skips the
                                           HMAC check, so an actor string must never be enough to hide a
                                           canonical; it leaves default retrieval only through the signed path.

Two signed-payload formats (INTENTIONALLY DISTINCT for backward compat):
  1. Tier-promotion legacy (v0.14, PATCH /tier path — DEPRECATED in v0.19,
     removed in v0.20; each accepted use logs a deprecation WARN):
       <ts>|<memory_id>|<reason>
     Produced by: pre-v0.19 mem0-canonize.sh <mid> "<reason>"

  2. Mutation actions (v0.17 Phase A + F.1; nonce REQUIRED since v0.18 MED-7):
       <ts>|<nonce>|<action>|<memory_id>|<reason>
     Produced by: bash mem0-canonize.sh --action put|delete|patch_metadata|demote <mid> "<reason>"
     (demote: session 12, PATCH /tier moving a record out of canonical)
     (script generates a uuid4 nonce, sends X-User-Direct-Nonce header, and includes
     the nonce in the signed payload)
     v0.18 MED-9 adds action "merge_goals" (POST /v1/goals/{id}/merge bulk-relink
     guard; the memory_id slot carries the source goal id as a string).
     v0.19 Phase G adds action "promote" (PATCH /tier canonical promotion;
     mem0-canonize.sh's promotion path now signs format-2 with this action).

NONCE / REPLAY PROTECTION (v0.17 Phase F.1; mandatory since v0.18 MED-7):
  X-User-Direct-Nonce is REQUIRED on every format-2 validation. The server:
    1. Validates the HMAC signature FIRST (v0.18 MED-8 — see below).
    2. Checks the replay store (~/.mem0/canonical-replay.jsonl) for this nonce.
    3. If seen → 403 "replay detected".
    4. If fresh → records {nonce, ts} and continues.
  v0.18 MED-7: the v0.17 no-nonce backward-compat fallback (<ts>|<action>|<mid>|<reason>)
  is REMOVED — it left a 300s replay window. Missing nonce → 403.
  v0.18 MED-8: the nonce is recorded only AFTER the HMAC verifies. Recording first
  let an attacker spam invalid tokens with fresh nonces and grow the replay store
  on disk (DoS). A VALID token with a reused nonce is still rejected (replay
  semantics intact).
  GC note: nonce entries older than 600s (2× skew window) are lazily pruned.

H2/H7 fix (v0.17 Final): _check_and_record_nonce protected by module-level threading.Lock().
  New nonces appended with fsync for crash-safety. GC uses atomic os.replace(tmp, store).
  File-size threshold (1 MB) limits GC rewrites to rare events.

H5 fix (v0.17 Final): fetch_current_tier distinguishes three outcomes:
  - _NOT_FOUND sentinel  -> point does not exist in Qdrant (let caller handle 404).
  - "canonical" fallback -> point exists but tier field absent (fail-closed): protects
                            records whose tier was stripped by H1 race before set_payload retry.
  - tier string          -> normal path.

AMS-21 fix (2026-08-08): a retrieve EXCEPTION (Qdrant connectivity blip) no longer
  returns None — it raises HTTPException(503) so the mutation is REFUSED. The old
  fail-open let a canonical record be mutated without HMAC when the tier gate hit
  a transient store error but the mutation itself succeeded moments later. Mirrors
  the imperative-canary in app.py, which 503s on the identical failure.

H8 fix (v0.17 Final): TRUSTED_PATCH_ACTORS allowlist; stamp-retired-v013 actor allowed to
  PATCH retired_at on canonical/insight records via the mem0 API (bypasses direct Qdrant write).

RATIONALE for separate formats:
  - Keeping them distinct means a tier-promotion token cannot be replayed as a
    PUT/DELETE/PATCH-metadata token even if the attacker captures one — the server
    rejects the wrong format outright (HMAC mismatch against expected format).
    Format-2 promote tokens carry the action word "promote", so they are equally
    non-replayable against the other mutation endpoints.
  - v0.19 Phase G: PATCH /tier routes through this module (format 2,
    action="promote"). v0.20 Phase G: the nonce-less format-1 inline gate in
    app.py is REMOVED — a tier promotion without X-User-Direct-Nonce is
    rejected 403 before any validation.

TOCTOU note (v0.17 accepted risk):
  fetch_current_tier + actual mutation are not atomic. An attacker who has BOTH
  the API key AND the canonical-key could exploit this window — but with both
  keys they could just issue the mutation directly. v0.18+ may add optimistic
  locking. Documented in plan Phase A TOCTOU note.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional, Set

from fastapi import HTTPException

log = logging.getLogger(__name__)


# ---------- Module-level config (loaded once at import time) ----------

from canonical_key_provider import CanonicalKeyProvider
_KEY_PROVIDER = CanonicalKeyProvider()


def _get_canonical_key() -> Optional[str]:
    """Indirection so tests can reset cache via _KEY_PROVIDER._cache_loaded = False."""
    return _KEY_PROVIDER.get_key()

CANONICAL_TOKEN_MAX_SKEW_S = 300  # 5-minute wall-clock tolerance (sole skew gate since v0.20 — the inline PATCH /tier format-1 gate is gone)

# Replay store GC window: entries older than 2× skew window are safe to discard.
REPLAY_GC_SECONDS = CANONICAL_TOKEN_MAX_SKEW_S * 2  # 600s
REPLAY_GC_THRESHOLD_BYTES = 1024 * 1024  # 1 MB — trigger GC-rewrite only when file exceeds this

REPLAY_STORE = Path.home() / ".mem0" / "canonical-replay.jsonl"

# H2/H7: module-level lock protecting the replay store read-modify-write.
# Prevents concurrent requests from both reading pre-state, each deciding their nonces
# are fresh, and the second truncating write losing the first nonce.
_NONCE_LOCK = threading.Lock()

INSIGHT_ALLOWED_ACTORS: Set[str] = {
    "c1-consolidator",
    "dream-consolidator",
    "c1-dream-consolidator",
}

# Valid action tokens for the format-2 signed payload.
# v0.18 MED-9 adds "merge_goals" (POST /v1/goals/{id}/merge bulk-relink guard;
# the memory_id slot of the signed payload carries the source goal id).
# v0.19 Phase G adds "promote" (PATCH /v1/memories/{mid}/tier canonical
# promotion — closes the v0.18 LOW-4 residual 300s replay window). v0.20
# Phase G: format-1 (<ts>|<mid>|<reason>, no nonce) is rejected outright —
# "promote" format-2 is the only tier-promotion token format.
# "demote" (PATCH /v1/memories/{mid}/tier moving a record OUT of canonical): without it an
# API-key holder could demote a canonical record and then PUT or DELETE it ungated, a two-step
# bypass of the canonical write gate. Its own action word keeps a promote token from being
# replayed as a demotion.
VALID_HMAC_ACTIONS = {"put", "delete", "patch_metadata", "merge_goals", "promote", "demote"}


def tier_change_hmac_action(current_tier: Optional[str], target_tier: str) -> Optional[str]:
    """The signed user-direct action a PATCH /tier needs, or None when none is required.

    Any move INTO canonical signs "promote" (unchanged since v0.19 Phase G). Any move OUT of
    canonical signs "demote". current_tier is the record's tier now (None when the record is
    unknown, which never requires "demote").

    1.32.5: a move OUT of insight signs "demote" too. Before it, insight -> evidence needed no
    token, after which PUT and DELETE were ungated: the insight gate fell to two plain requests,
    the same two-step hole session 12 closed for canonical. No job label exempts it (nothing in
    the stack demotes an insight): the operator's signed demote is the only way out.
    """
    if target_tier == "canonical":
        return "promote"
    if current_tier == "canonical":
        return "demote"
    if current_tier == "insight" and target_tier != "insight":
        return "demote"
    return None

# H8: actors trusted to PATCH normally-gated metadata on canonical/insight
# records via the mem0 API, each restricted to an EXACT per-actor key allowlist
# (v0.19 I.3 converted the former shared TRUSTED_ACTOR_ALLOWED_KEYS set to this
# per-actor mapping so contradiction-sweep-v019 cannot write retired_at and
# stamp-retired-v013 cannot write contradiction stamps). Membership checks
# (`actor in TRUSTED_PATCH_ACTORS`) keep working — dict iterates its keys.
TRUSTED_PATCH_ACTORS: dict[str, frozenset[str]] = {
    # v0.17 F.4.2 retired_at backfill (scripts/wsl/stamp-retired-at.py)
    "stamp-retired-v013": frozenset({"retired_at"}),
    # v0.19 Phase I.3 offline contradiction sweep
    # (scripts/wsl/contradiction-sweep.py): YES verdicts stamp both keys,
    # NO verdicts stamp only contradiction_checked_at (idempotency marker).
    # v0.29.4: contradicts_canonical_pending is the LOCAL (advisory) judge's stamp —
    # the admission gate IGNORES it (never hides a record); only an authoritative Codex
    # re-judge promotes it to contradicts_canonical (enforced). Same trusted actor.
    "contradiction-sweep-v019": frozenset({"contradicts_canonical", "contradiction_checked_at",
                                           "contradicts_canonical_pending"}),
    # superseded_by is deliberately absent. AMS-36 (2026-08-09) gave it one PATCH writer, the actor
    # string "supersession-resolve-v030", but an actor string is not a credential: any API-key
    # holder could send it, and the trusted-actor early return in assert_writable skips the
    # canonical HMAC check, so it could hide ANY record. Since 1.32.4 the only writer is
    # POST /v1/memories/{id}/supersede, whose refusal matrix (supersession.precheck) the server
    # enforces whoever calls it.
}

# Metadata keys the admission gate reads to HIDE a record from default retrieval
# (admission_gate.AdmissionPolicy.evaluate, steps 1b and 1c). No metadata PATCH may put one on a
# canonical record, whatever its actor string: a canonical leaves default retrieval only through
# the operator's signed path (demote first: mem0-canonize.sh --action demote).
# The sweep stamps insight candidates by design, so insight is not added here. The rest of what 1.32.4
# named as open (an API-key holder sending the sweep's, the backfill's or the decay labels to stamp
# their keys on a non-canonical record) closed in 1.32.5: every such label needs the authority's
# service key (require_service_credential below), and the policy honours it only when proven.
RETRIEVAL_HIDE_KEYS = frozenset({"superseded_by", "contradicts_canonical"})

# PATCH /v1/memories/{id}/metadata key policy. It lived inline in the app.py handler; 1.32.4 moved
# it here, unchanged, so the whole metadata-write decision (assert_writable, then this) is a pure
# function the headless suite can drive.
#
# v0.13 + v0.20 Phase B (M1/M3/M11): lifecycle and retrieval-gating keys a caller must not set
# through the generic shallow-merge endpoint, or any API-key holder could censor retrieval.
METADATA_FORBIDDEN_KEYS = frozenset({
    "retrievable", "expires_at", "created_at", "tier_actor",
    "superseded_by", "contradicts_canonical", "contradiction_checked_at",
    "contradicts_canonical_pending",  # v0.29.4: only the sweep actor writes it
    "nli_gate_checked_at",            # W2: the NLI gate's server-side stamp is its only writer
    # 1.32.4: the supersede door's own keys (supersession.SUPERSEDE_KEYS); no PATCH actor lists them.
    "superseded_at", "superseded_via", "partially_superseded_by",
})

# Legacy server-flow actors, each scoped to the EXACT forbidden keys it may write, so a forbidden
# key cannot be smuggled in alongside an authorized one (v0.20 Final, mixed-key bypass).
LEGACY_PATCH_ACTOR_KEYS: dict[str, frozenset[str]] = {
    "backfill-apply-v013": frozenset({"retrievable"}),
    "decay-scan": frozenset({"expires_at"}),
    "system": frozenset({"expires_at", "tier_actor"}),
}


# ---------- 1.32.5: the service credential behind privileged labels ----------
#
# Every label above (TRUSTED_PATCH_ACTORS, LEGACY_PATCH_ACTOR_KEYS, INSIGHT_ALLOWED_ACTORS) is
# free text in a request body or query string, and the ordinary API key is held by every PC and
# every MCP shim session. So a label grants nothing unless the request also carries the
# authority-only service key (canonical_key_provider.service_key_provider: systemd credential
# `ams-service-key` on the native authority, ~/.mem0/service-key on a WSL authority, absent on
# replicas and PCs). Each write handler calls require_service_credential on the label it is
# about to hand to the policy functions, BEFORE it calls them, so the pure policy below keeps
# reading a plain string and only ever sees a privileged one that was proven.

from canonical_key_provider import service_key_provider as _service_key_provider

_SERVICE_KEY_PROVIDER = _service_key_provider()

SERVICE_KEY_HEADER = "X-AMS-Service-Key"


def _get_service_key() -> Optional[str]:
    """Indirection so tests can swap the key (reset _SERVICE_KEY_PROVIDER._cache_loaded)."""
    return _SERVICE_KEY_PROVIDER.get_key()


def normalize_label(label: Optional[str]) -> str:
    """The one normalisation every label comparison in this module uses. A non-string (a missing
    query param's FieldInfo default when a handler is called outside FastAPI) is no label."""
    return label.strip().lower() if isinstance(label, str) else ""


def is_privileged_label(label: Optional[str]) -> bool:
    """True when the label is one a server-side job uses to claim a privilege. Read live from the
    three tables, so a label added to any of them is covered without a second edit."""
    n = normalize_label(label)
    return bool(n) and (n in TRUSTED_PATCH_ACTORS or n in LEGACY_PATCH_ACTOR_KEYS
                        or n in INSIGHT_ALLOWED_ACTORS)


def service_credential_ok(presented: Optional[str]) -> bool:
    """Constant-time check of the X-AMS-Service-Key header against the loaded service key. False
    when either side is missing or empty: a server without the key accepts no privileged label."""
    key = _get_service_key()
    p = presented.strip() if isinstance(presented, str) else ""
    if not key or not p:
        return False
    return hmac.compare_digest(p.encode("utf-8"), key.encode("utf-8"))


def require_service_credential(label: Optional[str], presented: Optional[str],
                               field: str = "actor") -> bool:
    """Gate a privileged label on the service credential.

    Returns False for a label that is not privileged (nothing to prove; the caller's request
    proceeds under the ordinary rules), True for a privileged label whose request carries the
    service key, and raises 403 `service-credential-required` for a privileged label without it.
    `field` names where the label came from (actor, metadata.source) for the error text.
    """
    if not is_privileged_label(label):
        return False
    if service_credential_ok(presented):
        return True
    if not _get_service_key():
        why = ("this server holds no service key, so it accepts no server-side job label "
               "(a replica or a PC never does; on the authority, re-run the installer)")
    else:
        why = f"the request did not carry the authority's service key in {SERVICE_KEY_HEADER}"
    raise HTTPException(
        403,
        f"service-credential-required: {field}={normalize_label(label)!r} is a server-side job "
        f"label and {why}. Ordinary writes need no label: omit it, or use your own (for example "
        "'claude-autonomous').",
    )


def authorize_metadata_patch(current_tier: Optional[str], actor: Optional[str], keys,
                             service_verified: bool = False) -> None:
    """Key-level authorisation for PATCH /metadata. Runs after assert_writable.

    Every forbidden key in the request must be individually allowed for the actor (legacy
    server-flow keys unioned with the actor's TRUSTED_PATCH_ACTORS keys), and a trusted actor may
    write ONLY its own keys. A hide key (RETRIEVAL_HIDE_KEYS) is refused on a canonical record for
    every actor: the trusted-actor early return in assert_writable skips the HMAC check, so the
    actor string alone must never be enough to hide a canonical. Raises HTTPException(403).

    1.32.5: service_verified says the handler proved the actor with the service key. An unproven
    label gets no key allowance at all (it reads as no label), so a handler that forgot
    require_service_credential is refused rather than trusted.
    """
    keys = set(keys)
    actor_lower = normalize_label(actor) if service_verified else ""   # the allowlist lookups
    actor_sent = normalize_label(actor)                                    # what the caller sent, for the message
    hide_hit = RETRIEVAL_HIDE_KEYS & keys
    if hide_hit and current_tier == "canonical":
        raise HTTPException(
            403,
            f"metadata keys {sorted(hide_hit)} would hide a CANONICAL record; a canonical leaves "
            "default retrieval only through the signed path (demote it first: "
            "mem0-canonize.sh --action demote <id> \"<reason>\")",
        )
    forbidden_hit = METADATA_FORBIDDEN_KEYS & keys
    if forbidden_hit:
        allowed_keys = set(LEGACY_PATCH_ACTOR_KEYS.get(actor_lower, frozenset()))
        allowed_keys |= set(TRUSTED_PATCH_ACTORS.get(actor_lower, frozenset()))
        if not (forbidden_hit <= allowed_keys):
            raise HTTPException(
                403,
                f"forbidden metadata keys {sorted(forbidden_hit - allowed_keys)} "
                f"require trusted actor; got actor={actor_sent!r}"
                + ("" if service_verified or not is_privileged_label(actor) else " (not proven: no service key)"),
            )
    if actor_lower in TRUSTED_PATCH_ACTORS:
        actor_allowed_keys = TRUSTED_PATCH_ACTORS[actor_lower]
        not_allowed_keys = keys - actor_allowed_keys
        if not_allowed_keys:
            raise HTTPException(
                403,
                f"trusted actor {actor_lower!r} may only write keys {sorted(actor_allowed_keys)}; "
                f"disallowed keys in request: {sorted(not_allowed_keys)}",
            )


# H5: sentinel for fetch_current_tier when the point does NOT exist in Qdrant.
_NOT_FOUND = "__NOT_FOUND__"


# ---------- Nonce / replay protection (v0.17 Phase F.1 + H2/H7 fix) ----------

def _check_and_record_nonce(nonce: str, ts: str) -> bool:
    """Check whether *nonce* is fresh and record it if so.

    Returns True  → nonce has NOT been seen before (record it, accept the request).
    Returns False → nonce has ALREADY been seen within the live window (reject).

    H2/H7 fix: protected by _NONCE_LOCK to prevent concurrent read-modify-write races.
    New nonces are appended with fsync (crash-safe; no truncation risk).
    GC rewrite is triggered only when file exceeds REPLAY_GC_THRESHOLD_BYTES and uses
    atomic os.replace(tmp, store) so a crash cannot corrupt the store.
    """
    with _NONCE_LOCK:
        return _check_and_record_nonce_locked(nonce, ts)


def _check_and_record_nonce_locked(nonce: str, ts: str) -> bool:
    """Inner implementation — must be called with _NONCE_LOCK held."""
    if not REPLAY_STORE.exists():
        REPLAY_STORE.parent.mkdir(parents=True, exist_ok=True)
        # v0.18 MED-10: replay store carries nonces — owner-only perms at creation.
        REPLAY_STORE.touch(mode=0o600)

    cutoff_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=REPLAY_GC_SECONDS)

    seen = False
    fresh_entries: list[str] = []

    if REPLAY_STORE.stat().st_size > 0:
        for line in REPLAY_STORE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            # Parse entry ts for comparison — handle both "Z" and "+00:00" suffixes.
            # String comparison is NOT safe here: "...Z" > "...+00:00" lexicographically.
            entry_ts_str = entry.get("ts", "")
            try:
                entry_dt = _dt.datetime.fromisoformat(entry_ts_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue  # malformed entry — drop it
            if entry_dt < cutoff_dt:
                continue  # GC: drop ancient entries
            if entry.get("nonce") == nonce:
                seen = True
            fresh_entries.append(line)

    if seen:
        return False

    new_entry = json.dumps({"nonce": nonce, "ts": ts})
    file_size = REPLAY_STORE.stat().st_size

    if file_size < REPLAY_GC_THRESHOLD_BYTES:
        # Append-only with fsync — crash-safe; avoids truncation risk.
        with REPLAY_STORE.open("a", encoding="utf-8") as f:
            f.write(new_entry + "\n")
            f.flush()
            os.fsync(f.fileno())
    else:
        # GC rewrite — atomic os.replace so a crash cannot destroy the store
        # (v0.17 H2/H7; verified complete for v0.18 MED-11: no direct-overwrite
        # path remains — appends are fsync'd append-only, rewrites go through
        # write-tmp + os.replace under _NONCE_LOCK).
        tmp_path = REPLAY_STORE.with_suffix(".jsonl.tmp")
        survivors = fresh_entries + [new_entry]
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write("\n".join(survivors) + "\n")
            f.flush()
            os.fsync(f.fileno())
        # v0.18 MED-10: tmp is created with umask perms; restore 0600 before it
        # atomically replaces the store, or the rewrite would widen permissions.
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, REPLAY_STORE)

    return True


# ---------- Helper: tier lookup (H5 fail-closed) ----------

def fetch_current_tier(client, collection_name: str, memory_id: str):
    """Fetch tier from Qdrant payload.

    H5 fix (v0.17 Final) + AMS-21 (2026-08-08): three distinct return outcomes,
    plus a raise:
      _NOT_FOUND sentinel  → point does not exist in Qdrant (let caller handle 404).
      "canonical" fallback → point exists but tier field absent; fail-closed to protect
                             records whose tier was stripped by a transient H1 race.
      tier string          → normal path: returns the stored tier value.
      raises HTTPException(503) → the retrieve itself failed (connectivity blip).
        AMS-21: the old `return None` here was fail-OPEN — assert_writable passed
        None through and the canonical/insight mutation proceeded ungated when the
        store recovered by mutation time. Mirrors the imperative-canary's 503
        ("fail-open would be wrong for a write gate").

    Parameters
    ----------
    client:          mem.vector_store.client  (qdrant_client.QdrantClient)
    collection_name: mem.vector_store.collection_name  (str, typically "memories")
    memory_id:       UUID string of the Qdrant point
    """
    try:
        records = client.retrieve(
            collection_name=collection_name,
            ids=[memory_id],
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        # AMS-21: connectivity error → REFUSE the mutation (fail-closed).
        raise HTTPException(
            503,
            f"cannot verify tier for memory_id={memory_id!r} "
            f"(store retrieve failed: {str(e)[:120]}); "
            "mutation rejected — retry when the store is reachable",
        ) from e
    if not records:
        return _NOT_FOUND  # H5: point genuinely does not exist
    rec = records[0]
    payload = rec.payload if hasattr(rec, "payload") else rec.get("payload")
    tier = (payload or {}).get("tier")
    if tier is None:
        # H5 fail-closed: point exists but tier field is absent.
        # Treat as canonical to enforce the gate rather than silently bypass it.
        # This handles the H1 path where mem0.update() strips the tier field transiently.
        log.warning(
            "fetch_current_tier: memory_id=%s has no tier field in Qdrant payload; "
            "treating as 'canonical' (fail-closed) to preserve immutability invariant. "
            "Possible H1 tier-strip race — verify the record manually.",
            memory_id,
        )
        return "canonical"
    return tier


# ---------- Core validator: HMAC user-direct token (format 2) ----------

def validate_hmac_user_direct(
    memory_id: str,
    action: str,
    reason: str,
    x_user_direct_token: Optional[str],
    x_user_direct_ts: Optional[str],
    x_user_direct_nonce: Optional[str] = None,
) -> None:
    """Validate an HMAC X-User-Direct-Token for a mutation action (format 2).

    Signed payload format (nonce REQUIRED since v0.18 MED-7):
      <ts>|<nonce>|<action>|<memory_id>|<reason>

    action ∈ VALID_HMAC_ACTIONS = {"put", "delete", "patch_metadata", "merge_goals", "promote", "demote"}.

    v0.18 MED-7: x_user_direct_nonce is REQUIRED. The v0.17 no-nonce backward-compat
    format (<ts>|<action>|<memory_id>|<reason>) is removed — it allowed token replay
    within the 300s skew window. Missing nonce → 403.

    v0.18 MED-8: the HMAC signature is verified BEFORE the nonce is checked/recorded
    in the replay store (~/.mem0/canonical-replay.jsonl), so invalid-token spam cannot
    grow the store on disk. A valid token with a reused nonce → 403 "replay detected".

    Raises HTTPException on any failure. Returns None on success.

    Callers:
    - assert_writable() when current_tier == "canonical"
    - validate_insight_actor() when actor not in INSIGHT_ALLOWED_ACTORS
    - app.py merge_goals_endpoint when source goal has >100 episode_links (MED-9)
    - app.py update_tier (PATCH /tier) for every canonical promotion
      (v0.19 Phase G, action="promote"; sole path since v0.20 removed format-1)
    - app.py update_tier (PATCH /tier) for every move OUT of canonical
      (session 12, action="demote"; see tier_change_hmac_action)
    """
    # v0.20 Phase D (L1): truthiness, not is-not-None — '' must read as keyless.
    if not _get_canonical_key():
        # v0.20 Phase D (M9): post-Phase-H remediation — on a DPAPI box the fix is
        # restoring/re-fetching the EXISTING key, never generating a fresh one.
        raise HTTPException(
            503,
            "cannot validate user-direct token: server has no canonical key "
            "(runtime injection failed? check `journalctl --user -u mem0` for "
            "dpapi-fetch-key). If ~/.mem0/canonical-key.dpapi exists, restore the "
            "key per docs/systems/dpapi-canonical-key.md Recovery and restart mem0; "
            "only run generate-canonical-key.sh on a box with no DPAPI blob.",
        )

    if action not in VALID_HMAC_ACTIONS:
        raise HTTPException(
            500,
            f"security_invariants internal error: invalid action {action!r}; "
            f"must be one of {sorted(VALID_HMAC_ACTIONS)}",
        )

    if not x_user_direct_token or not x_user_direct_ts:
        raise HTTPException(
            403,
            f"action={action!r} on this tier requires a user-direct HMAC token. "
            f"Use the CLI from your stack repo: bash scripts/wsl/mem0-canonize.sh "
            f"--action {action} {memory_id} \"<reason>\"",
        )

    # v0.18 MED-7: nonce is mandatory — no-nonce backward-compat path removed
    # (it left a 300s replay window inside the skew tolerance).
    if not x_user_direct_nonce:
        raise HTTPException(
            403,
            "X-User-Direct-Nonce required on canonical/insight write. "
            "The v0.17 no-nonce token format is no longer accepted (v0.18 MED-7); "
            "mem0-canonize.sh generates and sends the nonce automatically.",
        )

    # Timestamp skew check — reject tokens outside the tolerance window
    try:
        ts_dt = _dt.datetime.fromisoformat(x_user_direct_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise HTTPException(
            400,
            f"X-User-Direct-Ts not parseable as ISO 8601: {x_user_direct_ts!r}",
        )
    skew = abs((_dt.datetime.now(_dt.timezone.utc) - ts_dt).total_seconds())
    if skew > CANONICAL_TOKEN_MAX_SKEW_S:
        raise HTTPException(
            403,
            f"X-User-Direct-Token timestamp skew {skew:.1f}s exceeds {CANONICAL_TOKEN_MAX_SKEW_S}s limit",
        )

    # HMAC validation — v0.18 MED-8: signature verified BEFORE the nonce is
    # checked/recorded, so invalid-token spam cannot grow the replay store (DoS).
    # Format (nonce required, v0.18 MED-7): <ts>|<nonce>|<action>|<memory_id>|<reason>
    # Note: using reason="" is acceptable; the empty string is still included in
    # the signed payload so a token with reason="" cannot be replayed against
    # a request that includes a non-empty reason.
    msg = f"{x_user_direct_ts}|{x_user_direct_nonce}|{action}|{memory_id}|{reason}".encode("utf-8")

    expected = base64.b64encode(
        hmac.new(_get_canonical_key().encode("utf-8"), msg, hashlib.sha256).digest()
    ).decode("ascii").strip()

    if not hmac.compare_digest(expected, x_user_direct_token.strip()):
        raise HTTPException(
            403,
            f"X-User-Direct-Token HMAC mismatch for action={action!r}. "
            "Ensure you are using mem0-canonize.sh with matching --action flag, "
            "memory_id, and reason string.",
        )

    # v0.17 Phase F.1 nonce replay protection — runs AFTER HMAC verification
    # (v0.18 MED-8) so only authentic tokens can append to the replay store.
    # Replay semantics intact: a VALID token with a reused nonce is rejected here.
    if not _check_and_record_nonce(x_user_direct_nonce, x_user_direct_ts):
        raise HTTPException(
            403,
            f"X-User-Direct-Nonce {x_user_direct_nonce!r} has already been used "
            "(replay detected). Generate a new request with a fresh nonce.",
        )

# v0.20 Phase G: warn_deprecated_format1_tier_promotion (v0.19 Phase G) retired
# with the format-1 path itself — the 403 body now carries the migration
# instruction to the caller, and uvicorn access logs record the rejected hits.


# ---------- Insight-tier validator ----------

def validate_insight_actor(
    actor: str,
    x_user_direct_token: Optional[str],
    x_user_direct_ts: Optional[str],
    memory_id: str,
    action: str,
    reason: str,
    x_user_direct_nonce: Optional[str] = None,
    service_verified: bool = False,
) -> None:
    """Insight-tier write: a PROVEN actor in INSIGHT_ALLOWED_ACTORS OR valid HMAC user-direct.

    The OR gives the operator a direct-override route: even if he is not a consolidator
    actor, he can provide a signed HMAC token to mutate an insight record.
    x_user_direct_nonce is forwarded to validate_hmac_user_direct (v0.17 F.1).
    1.32.5: service_verified says the handler proved the label with the service key
    (require_service_credential). Without it the label is just text and the HMAC applies, so a
    caller that forgets the handler-level gate is denied, never let through.
    """
    if service_verified and normalize_label(actor) in INSIGHT_ALLOWED_ACTORS:
        return  # proven consolidator — accept without HMAC
    # HMAC required as the fallback gate
    validate_hmac_user_direct(
        memory_id, action, reason,
        x_user_direct_token, x_user_direct_ts,
        x_user_direct_nonce=x_user_direct_nonce,
    )


# ---------- Orchestrator: assert_writable ----------

def assert_writable(
    client,
    collection_name: str,
    memory_id: str,
    intended_action: str,
    x_user_direct_token: Optional[str],
    x_user_direct_ts: Optional[str],
    actor: str,
    reason: str,
    x_user_direct_nonce: Optional[str] = None,
    service_verified: bool = False,
) -> Optional[str]:
    """Fetch current tier and enforce the policy matrix for mutation actions.

    intended_action must be one of: "put", "delete", "patch_metadata".
    (PATCH /tier is handled by its own inline gate in app.py — do NOT route it here.)

    Policy matrix:
      canonical × put/delete/patch_metadata   → HMAC user-direct required
      insight   × put/delete/patch_metadata   → actor in INSIGHT_ALLOWED_ACTORS OR HMAC
      stable / evidence / temporal × any      → no extra gate

    x_user_direct_nonce (v0.17 F.1): forwarded to validate_hmac_user_direct for
    replay protection. v0.18 MED-7: required — absent nonce → 403 on the HMAC path.

    service_verified (1.32.5): True only when the handler proved the actor label with the service
    key (require_service_credential). Every label privilege below (the trusted-actor bypass, the
    consolidator's insight path) needs it; without it a privileged label is ordinary text.

    Returns current_tier str (or None if memory not found) so callers can include
    it in ledger entries. Raises HTTPException on policy violation.

    Raises HTTPException(500) if intended_action is not a recognised mutation action
    (programming error guard — should never happen from correct callers in app.py).
    """
    if intended_action not in VALID_HMAC_ACTIONS:
        raise HTTPException(
            500,
            f"assert_writable called with invalid action {intended_action!r}; "
            f"valid actions: {sorted(VALID_HMAC_ACTIONS)}",
        )

    # AMS-21: a connectivity error now RAISES 503 inside fetch_current_tier
    # (fail-closed) instead of returning None — only genuine not-found passes.
    current_tier = fetch_current_tier(client, collection_name, memory_id)

    # H5: _NOT_FOUND (point absent) → pass through (None kept as a defensive belt)
    if current_tier is None or current_tier == _NOT_FOUND:
        # Let the underlying PUT/DELETE/PATCH fail naturally (404/error)
        return None

    # H8: TRUSTED_PATCH_ACTORS bypass for patch_metadata only.
    # stamp-retired-v013 may PATCH retired_at on canonical/insight records without HMAC.
    # The app.py PATCH /metadata handler additionally enforces the allowed-keys constraint.
    # 1.32.5: only a label the handler PROVED with the service key gets the bypass.
    if (intended_action == "patch_metadata" and service_verified
            and normalize_label(actor) in TRUSTED_PATCH_ACTORS):
        return current_tier  # trusted-actor bypass; app.py enforces allowed-keys

    if current_tier == "canonical":
        validate_hmac_user_direct(
            memory_id, intended_action, reason,
            x_user_direct_token, x_user_direct_ts,
            x_user_direct_nonce=x_user_direct_nonce,
        )
    elif current_tier == "insight":
        validate_insight_actor(
            actor, x_user_direct_token, x_user_direct_ts,
            memory_id, intended_action, reason,
            x_user_direct_nonce=x_user_direct_nonce,
            service_verified=service_verified,
        )
    # stable / evidence / temporal — no extra gate; fall through

    return current_tier
