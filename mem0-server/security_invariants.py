"""v0.17 Phase A: shared security helpers for write-path policy enforcement.
v0.17 Phase F.1: HMAC nonce/jti replay protection added.
v0.17 Final fix-pass: H2/H7 nonce threading.Lock+fsync+atomic-rename; H5 fail-closed tier lookup; H8 trusted-actor allowlist.

The policy matrix (current_tier × action):
- canonical × PUT/DELETE/PATCH-metadata  → require HMAC user-direct token
- canonical × PATCH-tier-demote          → already enforced by v0.14 inline gate (untouched)
- insight   × PUT/DELETE/PATCH-metadata  → require actor in INSIGHT_ALLOWED_ACTORS OR valid HMAC user-direct
- stable / evidence / temporal × any     → no extra gate (existing flow unchanged)

Two signed-payload formats (INTENTIONALLY DISTINCT for backward compat):
  1. Tier-promotion legacy (v0.14, PATCH /tier path — DEPRECATED in v0.19,
     removed in v0.20; each accepted use logs a deprecation WARN):
       <ts>|<memory_id>|<reason>
     Produced by: pre-v0.19 mem0-canonize.sh <mid> "<reason>"

  2. Mutation actions (v0.17 Phase A + F.1; nonce REQUIRED since v0.18 MED-7):
       <ts>|<nonce>|<action>|<memory_id>|<reason>
     Produced by: bash mem0-canonize.sh --action put|delete|patch_metadata <mid> "<reason>"
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
VALID_HMAC_ACTIONS = {"put", "delete", "patch_metadata", "merge_goals", "promote"}

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
}

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

    H5 fix (v0.17 Final): returns one of four distinct outcomes:
      _NOT_FOUND sentinel  → point does not exist in Qdrant (let caller handle 404).
      None                 → Qdrant connectivity error (caller treats as not-found).
      "canonical" fallback → point exists but tier field absent; fail-closed to protect
                             records whose tier was stripped by a transient H1 race.
      tier string          → normal path: returns the stored tier value.

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
    except Exception:
        return None  # connectivity error
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

    action ∈ VALID_HMAC_ACTIONS = {"put", "delete", "patch_metadata", "merge_goals", "promote"}.

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
            "key per docs/modular/dpapi-canonical-key.md Recovery and restart mem0; "
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
) -> None:
    """Insight-tier write: actor in INSIGHT_ALLOWED_ACTORS OR valid HMAC user-direct.

    The OR gives the operator a direct-override route: even if he is not a consolidator
    actor, he can provide a signed HMAC token to mutate an insight record.
    x_user_direct_nonce is forwarded to validate_hmac_user_direct (v0.17 F.1).
    """
    actor_lower = (actor or "").strip().lower()
    if actor_lower in INSIGHT_ALLOWED_ACTORS:
        return  # consolidator path — accept without HMAC
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

    current_tier = fetch_current_tier(client, collection_name, memory_id)

    # H5: None (connectivity error) or _NOT_FOUND (point absent) → pass through
    if current_tier is None or current_tier == _NOT_FOUND:
        # Let the underlying PUT/DELETE/PATCH fail naturally (404/error)
        return None

    # H8: TRUSTED_PATCH_ACTORS bypass for patch_metadata only.
    # stamp-retired-v013 may PATCH retired_at on canonical/insight records without HMAC.
    # The app.py PATCH /metadata handler additionally enforces the allowed-keys constraint.
    _actor_lower = (actor or "").strip().lower()
    if intended_action == "patch_metadata" and _actor_lower in TRUSTED_PATCH_ACTORS:
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
        )
    # stable / evidence / temporal — no extra gate; fall through

    return current_tier
