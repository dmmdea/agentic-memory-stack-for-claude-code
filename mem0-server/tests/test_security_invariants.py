"""v0.17 Phase C: adversarial invariant tests.

For every (tier × action) cell in the policy matrix, ATTEMPT the forbidden write
and assert BOTH:
  1. HTTP status matches expected
  2. tier-ledger row count unchanged
  3. record state unchanged (text, tier, metadata)

The adversarial lens: prove the gates HOLD against attack, not just that valid
calls succeed. v0.16 D adversarial review caught the lack of this discipline.

v0.17 Phase F.1.2: regression test for _canonical_intent search exclusion.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx
import pytest

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ.get("MEM0_KEY") or (Path.home() / ".mem0" / "api-key").read_text().strip()
H = {"X-API-Key": KEY, "Content-Type": "application/json"}

# v0.19 Phase H: key via provider (runtime tmpfs > dpapi-on-win > plaintext) —
# conftest.py inserts mem0-server/ into sys.path before this module loads.
from canonical_key_provider import CanonicalKeyProvider  # noqa: E402
# MEM-16: ledger row counts span legacy tier-ledger.jsonl + monthly segments.
from _ledger_paths import ledger_line_count  # noqa: E402

CANONICAL_KEY: Optional[str] = CanonicalKeyProvider().get_key()


# ---------- helpers ----------

def _ledger_count() -> int:
    return ledger_line_count()


def _post_evidence(text: str, **md_extra) -> str:
    """POST a fresh evidence-tier memory; return its id."""
    md = {"tier": "evidence", "source": "test-security-invariants", "user_id": "test-inv"}
    md.update(md_extra)
    r = httpx.post(
        f"{URL}/v1/memories",
        json={"messages": text, "user_id": "test-inv", "infer": False, "metadata": md},
        headers=H, timeout=10,
    )
    r.raise_for_status()
    return r.json()["results"][0]["id"]


def _sign_hmac(
    memory_id: str, action: str, reason: str,
    ts: Optional[str] = None, nonce: Optional[str] = None,
) -> tuple[str, str, str]:
    """Sign a format-2 action-prefixed payload. Returns (token, ts, nonce).

    Format 2 (nonce REQUIRED since v0.18 MED-7): <ts>|<nonce>|<action>|<memory_id>|<reason>
    Used for PUT/DELETE/PATCH-metadata on canonical records.
    """
    if CANONICAL_KEY is None:
        pytest.skip("canonical-key not configured; HMAC tests need it for setup/cleanup")
    if ts is None:
        ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if nonce is None:
        nonce = str(uuid.uuid4())
    msg = f"{ts}|{nonce}|{action}|{memory_id}|{reason}".encode("utf-8")
    expected = base64.b64encode(
        hmac.new(CANONICAL_KEY.encode(), msg, hashlib.sha256).digest()
    ).decode("ascii").strip()
    return expected, ts, nonce


def _sign_legacy_tier(memory_id: str, reason: str, ts: Optional[str] = None) -> tuple[str, str]:
    """Sign the REMOVED v0.14 format-1 tier-promotion payload (no action prefix).

    Format 1: <ts>|<memory_id>|<reason>
    v0.20 Phase G: PATCH /tier rejects this outright — kept ONLY so the
    rejection test can prove a validly-signed format-1 token is refused.
    """
    if CANONICAL_KEY is None:
        pytest.skip("canonical-key not configured")
    if ts is None:
        ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    msg = f"{ts}|{memory_id}|{reason}".encode("utf-8")
    expected = base64.b64encode(
        hmac.new(CANONICAL_KEY.encode(), msg, hashlib.sha256).digest()
    ).decode("ascii").strip()
    return expected, ts


def _promote_to_canonical(mid: str, reason: str = "test setup") -> None:
    """Setup helper: promote evidence → canonical via HMAC (format 2 / PATCH /tier).

    v0.20 Phase G: format-1 is rejected by the server — setup promotes via the
    format-2 promote token (<ts>|<nonce>|promote|<mid>|<reason>) + nonce header."""
    token, ts, nonce = _sign_hmac(mid, "promote", reason)
    r = httpx.patch(
        f"{URL}/v1/memories/{mid}/tier",
        json={"tier": "canonical", "actor": "user-direct", "reason": reason},
        headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
                 "X-User-Direct-Nonce": nonce},
        timeout=10,
    )
    r.raise_for_status()


def _post_insight(text: str) -> str:
    """Create an insight-tier memory via consolidator actor (the only path)."""
    r = httpx.post(
        f"{URL}/v1/memories",
        json={
            "messages": text, "user_id": "test-inv", "infer": False,
            "metadata": {"tier": "insight", "source": "c1-consolidator", "user_id": "test-inv"},
        },
        headers=H, timeout=10,
    )
    r.raise_for_status()
    return r.json()["results"][0]["id"]


def _force_delete(mid: str) -> None:
    """Cleanup helper: delete via HMAC (format 2 / delete action). Best-effort."""
    if CANONICAL_KEY is None:
        # Fallback: try plain delete (works for non-canonical tiers)
        httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
        return
    try:
        token, ts, nonce = _sign_hmac(mid, "delete", "test cleanup")
        httpx.delete(
            f"{URL}/v1/memories/{mid}",
            params={"actor": "user-direct", "reason": "test cleanup"},
            headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
    except Exception:
        pass


def _get_memory_text(mid: str) -> Optional[str]:
    """Read current text via mem0 list endpoint (search would re-rank; list is exact)."""
    r = httpx.get(f"{URL}/v1/memories?user_id=test-inv&limit=500", headers=H, timeout=10)
    if r.status_code != 200:
        return None
    for m in r.json().get("results", []):
        if m.get("id") == mid:
            return m.get("memory")
    return None


def _get_memory_tier(mid: str) -> Optional[str]:
    r = httpx.get(f"{URL}/v1/memories?user_id=test-inv&limit=500", headers=H, timeout=10)
    if r.status_code != 200:
        return None
    for m in r.json().get("results", []):
        if m.get("id") == mid:
            return (m.get("metadata") or {}).get("tier")
    return None


# ---------- (canonical × PUT) ----------

@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present — need it for promote-then-attack setup")
def test_canonical_put_no_token_rejected_no_ledger_no_state_change():
    """Attack: PUT text on canonical without any HMAC token.
    Assert: 403, ledger count unchanged, text unchanged."""
    mid = _post_evidence(f"canonical-put-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    original_text = _get_memory_text(mid)
    ledger_before = _ledger_count()
    r = httpx.put(
        f"{URL}/v1/memories/{mid}",
        json={"text": "tampered text"},
        headers=H, timeout=10,
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert _ledger_count() == ledger_before, "ledger must not record a denied attempt"
    assert _get_memory_text(mid) == original_text, "text must be unchanged after denied PUT"
    _force_delete(mid)


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_canonical_put_stale_token_rejected_no_state_change():
    """Attack: PUT with a 10-minute-old timestamp (beyond 300s skew tolerance).
    Assert: 403, error mentions skew/timestamp, ledger unchanged, text unchanged."""
    mid = _post_evidence(f"canonical-put-stale-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    original_text = _get_memory_text(mid)
    ledger_before = _ledger_count()
    # 10-min-old timestamp = >300s skew
    stale_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    token, _, nonce = _sign_hmac(mid, "put", "stale", ts=stale_ts)
    r = httpx.put(
        f"{URL}/v1/memories/{mid}",
        json={"text": "tampered with stale token"},
        headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": stale_ts,
                 "X-User-Direct-Nonce": nonce},
        timeout=10,
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert "skew" in r.text.lower() or "stale" in r.text.lower() or "timestamp" in r.text.lower(), \
        f"error should mention skew/timestamp: {r.text}"
    assert _ledger_count() == ledger_before, "ledger must not record a denied attempt"
    assert _get_memory_text(mid) == original_text, "text must be unchanged after stale-token PUT"
    _force_delete(mid)


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_canonical_put_bad_hmac_rejected_no_state_change():
    """Attack: PUT with a garbage HMAC token (correct format but wrong signature).
    Assert: 403, ledger unchanged, text unchanged."""
    mid = _post_evidence(f"canonical-put-bad-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    original_text = _get_memory_text(mid)
    ledger_before = _ledger_count()
    ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    # v0.18 MED-7: nonce header included so the request reaches the HMAC check
    # (without it the 403 fires earlier at the missing-nonce gate).
    r = httpx.put(
        f"{URL}/v1/memories/{mid}",
        json={"text": "tampered with garbage token"},
        headers={**H, "X-User-Direct-Token": "AAAAAAAAAAAAAAAAAAAAAAAAAAAA", "X-User-Direct-Ts": ts,
                 "X-User-Direct-Nonce": str(uuid.uuid4())},
        timeout=10,
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert "HMAC" in r.text or "mismatch" in r.text.lower(), \
        f"error should mention HMAC/mismatch: {r.text}"
    assert _ledger_count() == ledger_before, "ledger must not record a denied attempt"
    assert _get_memory_text(mid) == original_text, "text must be unchanged after bad-HMAC PUT"
    _force_delete(mid)


# ---------- (canonical × DELETE) ----------

@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_canonical_delete_no_token_rejected_record_exists():
    """Attack: DELETE canonical without any token.
    Assert: 403, ledger unchanged, record still exists."""
    mid = _post_evidence(f"canonical-delete-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    ledger_before = _ledger_count()
    r = httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert _ledger_count() == ledger_before, "ledger must not record a denied attempt"
    assert _get_memory_text(mid) is not None, "memory must still exist after denied DELETE"
    _force_delete(mid)


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_canonical_delete_bad_hmac_rejected_record_exists():
    """Attack: DELETE canonical with a garbage HMAC.
    Assert: 403, record still exists."""
    mid = _post_evidence(f"canonical-delete-bad-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    ledger_before = _ledger_count()
    ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    r = httpx.delete(
        f"{URL}/v1/memories/{mid}",
        params={"actor": "attacker", "reason": "trying garbage HMAC"},
        headers={**H, "X-User-Direct-Token": "Z" * 44, "X-User-Direct-Ts": ts},
        timeout=10,
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert _ledger_count() == ledger_before, "ledger must not record a denied attempt"
    assert _get_memory_text(mid) is not None, "memory must still exist after bad-HMAC DELETE"
    _force_delete(mid)


# ---------- (canonical × PATCH metadata) ----------

@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_canonical_patch_metadata_no_token_rejected_tier_unchanged():
    """Attack: PATCH /metadata on canonical without HMAC token.
    Assert: 403, ledger unchanged, tier still canonical."""
    mid = _post_evidence(f"canonical-patch-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    ledger_before = _ledger_count()
    r = httpx.patch(
        f"{URL}/v1/memories/{mid}/metadata",
        json={"metadata": {"injected_key": "value"}, "actor": "attacker", "reason": "test"},
        headers=H, timeout=10,
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert _ledger_count() == ledger_before, "ledger must not record a denied attempt"
    assert _get_memory_tier(mid) == "canonical", "tier must remain canonical after denied PATCH"
    _force_delete(mid)


# ---------- (insight × forbidden actor) ----------

def test_insight_put_unauthorized_actor_rejected_no_ledger_no_state_change():
    """v0.17 Phase A: PUT on insight without consolidator actor or HMAC → 403.
    Assert: 403, ledger unchanged, text unchanged."""
    mid = _post_insight(f"insight-put-{uuid.uuid4()}")
    original_text = _get_memory_text(mid)
    ledger_before = _ledger_count()
    r = httpx.put(
        f"{URL}/v1/memories/{mid}",
        params={"actor": "attacker", "reason": "test"},
        json={"text": "tampered"},
        headers=H, timeout=10,
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert _ledger_count() == ledger_before, "ledger must not record a denied attempt"
    assert _get_memory_text(mid) == original_text, "text must be unchanged after denied PUT on insight"
    _force_delete(mid)


def test_insight_delete_unauthorized_rejected_record_exists():
    """Attack: DELETE insight without proper actor or HMAC.
    Assert: 403, ledger unchanged, record still exists."""
    mid = _post_insight(f"insight-delete-{uuid.uuid4()}")
    ledger_before = _ledger_count()
    r = httpx.delete(
        f"{URL}/v1/memories/{mid}",
        params={"actor": "attacker", "reason": "test"},
        headers=H, timeout=10,
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert _ledger_count() == ledger_before, "ledger must not record a denied attempt"
    assert _get_memory_text(mid) is not None, "memory must still exist after denied DELETE on insight"
    _force_delete(mid)


def test_insight_patch_metadata_unauthorized_rejected():
    """Attack: PATCH /metadata on insight without proper actor or HMAC.
    Assert: 403."""
    mid = _post_insight(f"insight-patch-{uuid.uuid4()}")
    ledger_before = _ledger_count()
    r = httpx.patch(
        f"{URL}/v1/memories/{mid}/metadata",
        json={"metadata": {"injected": True}, "actor": "attacker", "reason": "test"},
        headers=H, timeout=10,
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert _ledger_count() == ledger_before, "ledger must not record a denied attempt"
    _force_delete(mid)


# ---------- (regression: stable/evidence still writable) ----------

def test_evidence_put_no_token_accepted():
    """v0.17 Phase A invariant: non-canonical/non-insight tier PUT requires no extra gate.
    Regression: ensure the gate did not over-block lower tiers."""
    mid = _post_evidence(f"evidence-put-{uuid.uuid4()}")
    r = httpx.put(
        f"{URL}/v1/memories/{mid}",
        json={"text": "updated evidence text"},
        headers=H, timeout=10,
    )
    assert r.status_code == 200, \
        f"evidence PUT should succeed without HMAC; got {r.status_code}: {r.text}"
    # Cleanup: plain DELETE works for evidence (no HMAC needed)
    httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)


# ---------- (regression: valid HMAC PUT succeeds + ledger records it) ----------

@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_canonical_put_with_valid_hmac_succeeds_and_ledger_records():
    """v0.17 Phase A: HMAC PUT on canonical succeeds; ledger gets memory-update entry.
    This is the positive control — ensures gates are not so strict they block valid ops."""
    mid = _post_evidence(f"canonical-put-valid-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    ledger_before = _ledger_count()
    token, ts, nonce = _sign_hmac(mid, "put", "valid PUT test")
    r = httpx.put(
        f"{URL}/v1/memories/{mid}",
        params={"actor": "user-direct", "reason": "valid PUT test"},
        json={"text": "validly updated canonical text"},
        headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
                 "X-User-Direct-Nonce": nonce},
        timeout=10,
    )
    assert r.status_code == 200, f"valid HMAC PUT should succeed; got {r.status_code}: {r.text}"
    assert _ledger_count() > ledger_before, \
        "ledger must record a valid memory-update (positive control for ledger assertions)"
    _force_delete(mid)


# ---------- (regression: POST tier=canonical still blocked) ----------

def test_post_canonical_still_rejected():
    """Regression from v0.13: POST with tier=canonical → 4xx, no record created.
    Also validates the error message mentions the correct remediation path."""
    r = httpx.post(
        f"{URL}/v1/memories",
        json={
            "messages": f"post-canon-{uuid.uuid4()}",
            "user_id": "test-inv", "infer": False,
            "metadata": {"tier": "canonical"},
        },
        headers=H, timeout=10,
    )
    assert r.status_code in (400, 403), \
        f"POST with tier=canonical must be rejected; got {r.status_code}: {r.text}"
    body = r.text.lower()
    assert "canonize" in body or "evidence" in body or "user-direct" in body, \
        f"rejection message should mention remediation path (canonize/evidence/user-direct): {r.text}"


# ---------- v0.17 Phase F.1.2: _canonical_intent excluded from default search ----------

@pytest.mark.parametrize("marker", [True, "true", 1])
def test_canonical_intent_excluded_from_default_search(marker):
    """v0.17 F.1.2: memories with a truthy metadata._canonical_intent are hidden
    from default search results (privilege-escalation oracle: bad agent could
    batch-promote all marked records if they surfaced in normal search). Opt-in
    via filters.include_canonical_intent=True must surface them.

    v0.19 L12: parametrized over truthy non-bool markers ("true", 1) so a revert
    of the v0.18 MED-5 truthy check back to `is True` fails this test (the
    mutation previously survived the whole suite)."""
    text = f"v017-F1-canonical-intent-{uuid.uuid4()}"
    r = httpx.post(
        f"{URL}/v1/memories",
        json={
            "messages": text,
            "user_id": "test-inv",
            "infer": False,
            "metadata": {
                "tier": "evidence",
                "source": "test-F1",
                "_canonical_intent": marker,
                "user_id": "test-inv",
            },
        },
        headers=H, timeout=10,
    )
    r.raise_for_status()
    mid = r.json()["results"][0]["id"]

    try:
        # Default search: must NOT return the _canonical_intent record
        sr = httpx.post(
            f"{URL}/v1/memories/search",
            json={"query": text, "filters": {"user_id": "test-inv"}, "limit": 20},
            headers=H, timeout=10,
        )
        sr.raise_for_status()
        found_ids = [m["id"] for m in sr.json().get("results", [])]
        assert mid not in found_ids, (
            "default search must exclude _canonical_intent memories "
            f"(found {mid} in results: {found_ids})"
        )

        # Opt-in: must surface the record
        sr2 = httpx.post(
            f"{URL}/v1/memories/search",
            json={
                "query": text,
                "filters": {"user_id": "test-inv", "include_canonical_intent": True},
                "limit": 20,
            },
            headers=H, timeout=10,
        )
        sr2.raise_for_status()
        found_ids2 = [m["id"] for m in sr2.json().get("results", [])]
        assert mid in found_ids2, (
            "include_canonical_intent=True must surface _canonical_intent memories "
            f"({mid} not found; results: {found_ids2})"
        )
    finally:
        # Cleanup — evidence tier, no HMAC needed
        httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)


# ---------- v0.17 Phase F.2.5: PUT strips tier regression ----------

@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_put_canonical_then_put_again_still_requires_hmac():
    """v0.17 F.2.5: after valid HMAC PUT, the second PUT without HMAC must still be 403
    (mem0 update() strips tier from Qdrant payload; F.2.5 restores it via set_payload)."""
    mid = _post_evidence(f"f25-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    try:
        # First PUT: valid HMAC succeeds (v0.18 MED-7: nonce required — format
        # ts|nonce|action|mid|reason). reason in the signed payload must match
        # the reason query param exactly.
        first_reason = "first valid put"
        token, ts, nonce = _sign_hmac(mid, "put", first_reason)
        r1 = httpx.put(
            f"{URL}/v1/memories/{mid}",
            params={"actor": "user-direct", "reason": first_reason},
            json={"text": "first valid update"},
            headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
        assert r1.status_code == 200, f"F.2.5 setup: first valid HMAC PUT failed {r1.status_code}: {r1.text}"
        # Second PUT without HMAC — must be 403 (Phase A gate must hold after tier restore)
        r2 = httpx.put(
            f"{URL}/v1/memories/{mid}",
            json={"text": "ungated attempt after valid PUT"},
            headers=H, timeout=10,
        )
        assert r2.status_code == 403, (
            f"F.2.5 regression: second PUT was {r2.status_code} {r2.text} — "
            "tier was stripped by mem0 update() and not restored by F.2.5 set_payload fix"
        )
    finally:
        _force_delete(mid)


# ---------- v0.17 Phase F.1.1: nonce replay protection ----------

@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_canonical_replay_rejected():
    """v0.17 F.1.1: replaying a valid HMAC token with the same nonce is rejected (403).

    Uses PATCH /metadata rather than PUT because mem0's update() strips custom Qdrant
    payload fields (including tier) on write, which would cause the second PUT to see
    tier=None and bypass the gate. PATCH /metadata uses set_payload() which preserves tier.

    Test plan:
      1. Create evidence → promote to canonical.
      2. First PATCH /metadata with nonce → 200.
      3. Second PATCH /metadata with SAME nonce+token → 403 replay detected.
    """
    mid = _post_evidence(f"canonical-replay-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    try:
        ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        nonce = str(uuid.uuid4())
        action = "patch_metadata"
        reason = "replay test"
        msg = f"{ts}|{nonce}|{action}|{mid}|{reason}".encode("utf-8")
        token = base64.b64encode(
            hmac.new(CANONICAL_KEY.encode(), msg, hashlib.sha256).digest()
        ).decode("ascii").strip()

        headers_with_nonce = {
            **H,
            "X-User-Direct-Token": token,
            "X-User-Direct-Ts": ts,
            "X-User-Direct-Nonce": nonce,
        }

        # First use: must succeed
        r1 = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={"metadata": {"replay_test_marker": "first"}, "actor": "user-direct", "reason": reason},
            headers=headers_with_nonce,
            timeout=10,
        )
        assert r1.status_code == 200, (
            f"first use of valid nonce+token should succeed; got {r1.status_code}: {r1.text}"
        )

        # Second use of the SAME nonce + token: must be rejected as replay
        r2 = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={"metadata": {"replay_test_marker": "second"}, "actor": "user-direct", "reason": reason},
            headers=headers_with_nonce,
            timeout=10,
        )
        assert r2.status_code == 403, (
            f"replay of same nonce must be rejected with 403; got {r2.status_code}: {r2.text}"
        )
        assert "replay" in r2.text.lower() or "nonce" in r2.text.lower(), (
            f"error should mention replay/nonce: {r2.text}"
        )
    finally:
        _force_delete(mid)


# ---------- v0.18 MED-7: nonce REQUIRED on canonical/insight HMAC paths ----------

@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_med7_canonical_write_without_nonce_rejected():
    """v0.18 MED-7: the v0.17 no-nonce backward-compat token format is no longer
    accepted. A canonical PATCH /metadata with a token validly signed in the OLD
    format (<ts>|<action>|<mid>|<reason>) but WITHOUT X-User-Direct-Nonce → 403
    mentioning the nonce; record state unchanged."""
    mid = _post_evidence(f"med7-no-nonce-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    try:
        ledger_before = _ledger_count()
        ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        reason = "med7 no-nonce attempt"
        # Old v0.17 Phase A format — no nonce in payload, no nonce header
        msg = f"{ts}|patch_metadata|{mid}|{reason}".encode("utf-8")
        token = base64.b64encode(
            hmac.new(CANONICAL_KEY.encode(), msg, hashlib.sha256).digest()
        ).decode("ascii").strip()
        r = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={"metadata": {"med7_marker": True}, "actor": "user-direct", "reason": reason},
            headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts},
            timeout=10,
        )
        assert r.status_code == 403, (
            f"MED-7: no-nonce canonical write must be 403; got {r.status_code}: {r.text}"
        )
        assert "nonce" in r.text.lower(), f"error should mention the missing nonce: {r.text}"
        assert _ledger_count() == ledger_before, "ledger must not record a denied attempt"
        assert _get_memory_tier(mid) == "canonical", "tier must remain canonical"
    finally:
        _force_delete(mid)


# ---------- v0.18 MED-8: HMAC verify BEFORE nonce record ----------

@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_med8_invalid_token_does_not_burn_nonce():
    """v0.18 MED-8: an INVALID token with nonce N must not record N in the replay
    store (invalid-token spam would otherwise grow the store — disk DoS).

    Proof: (1) garbage token + nonce N → 403 HMAC mismatch;
           (2) VALID token reusing the same nonce N → 200 (N was never recorded);
           (3) replaying the valid token + N → 403 replay (semantics intact)."""
    mid = _post_evidence(f"med8-verify-first-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    try:
        reason = "med8 ordering test"
        nonce = str(uuid.uuid4())

        # (1) invalid token, fresh nonce → 403 HMAC mismatch
        ts1 = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        r1 = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={"metadata": {"med8_marker": "invalid"}, "actor": "user-direct", "reason": reason},
            headers={**H, "X-User-Direct-Token": "A" * 44, "X-User-Direct-Ts": ts1,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
        assert r1.status_code == 403, f"garbage token must be 403; got {r1.status_code}: {r1.text}"
        assert "hmac" in r1.text.lower() or "mismatch" in r1.text.lower(), (
            f"expected HMAC mismatch (not replay) for the invalid token: {r1.text}"
        )

        # (2) valid token with the SAME nonce → 200 (nonce was not burned by step 1)
        token, ts2, _ = _sign_hmac(mid, "patch_metadata", reason, nonce=nonce)
        r2 = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={"metadata": {"med8_marker": "valid"}, "actor": "user-direct", "reason": reason},
            headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts2,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
        assert r2.status_code == 200, (
            f"MED-8 regression: invalid token burned the nonce (valid reuse got "
            f"{r2.status_code}: {r2.text})"
        )

        # (3) replay of the now-recorded valid token+nonce → 403 (replay semantics intact)
        r3 = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={"metadata": {"med8_marker": "replay"}, "actor": "user-direct", "reason": reason},
            headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts2,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
        assert r3.status_code == 403, (
            f"replay of a recorded nonce must still be 403; got {r3.status_code}: {r3.text}"
        )
        assert "replay" in r3.text.lower() or "nonce" in r3.text.lower(), (
            f"error should mention replay/nonce: {r3.text}"
        )
    finally:
        _force_delete(mid)


# ---------- v0.18 LOW-8: nonce replay window is FINITE ----------

REPLAY_STORE = Path.home() / ".mem0" / "canonical-replay.jsonl"
# Mirrors security_invariants.REPLAY_GC_SECONDS (2x the 300s skew window):
# entries older than this are lazily pruned, after which the nonce is reusable.
REPLAY_GC_SECONDS = 600


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_low8_nonce_replay_window_expires():
    """v0.18 LOW-8: replay protection has a FINITE window — a nonce older than
    the GC window (REPLAY_GC_SECONDS=600s, 2x the 300s skew tolerance) is pruned
    and becomes reusable with a fresh HMAC.

    No 600s sleep: instead the recorded store entry is backdated in-place (the
    store's own JSONL format: {"nonce": ..., "ts": ...}) to >600s ago, then the
    same nonce with a freshly-signed token must succeed — proving the lazy-GC /
    window-expiry logic in _check_and_record_nonce works.

    Plan:
      1. evidence → canonical; PATCH /metadata with nonce N → 200 (N recorded).
      2. Same N, FRESH token → 403 replay (window active — sanity check).
      3. Backdate N's stored ts to REPLAY_GC_SECONDS+100s ago in canonical-replay.jsonl.
      4. Same N, fresh token → 200 (entry GC'd; window expired).
    """
    mid = _post_evidence(f"low8-nonce-window-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    try:
        reason = "low8 window test"
        nonce = str(uuid.uuid4())

        # (1) first use: valid token + nonce N → 200, N recorded in the store
        token1, ts1, _ = _sign_hmac(mid, "patch_metadata", reason, nonce=nonce)
        r1 = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={"metadata": {"low8_marker": "first"}, "actor": "user-direct", "reason": reason},
            headers={**H, "X-User-Direct-Token": token1, "X-User-Direct-Ts": ts1,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
        assert r1.status_code == 200, (
            f"first use of nonce should succeed; got {r1.status_code}: {r1.text}"
        )

        # (2) sanity: same nonce + FRESH token inside the window → 403 replay
        token2, ts2, _ = _sign_hmac(mid, "patch_metadata", reason, nonce=nonce)
        r2 = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={"metadata": {"low8_marker": "inside-window"}, "actor": "user-direct", "reason": reason},
            headers={**H, "X-User-Direct-Token": token2, "X-User-Direct-Ts": ts2,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
        assert r2.status_code == 403, (
            f"reuse inside the window must be 403 replay; got {r2.status_code}: {r2.text}"
        )

        # (3) backdate the recorded entry for our nonce to beyond the GC window,
        # preserving every other line of the store verbatim.
        assert REPLAY_STORE.exists(), "replay store must exist after a recorded nonce"
        backdated_ts = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=REPLAY_GC_SECONDS + 100)
        ).isoformat().replace("+00:00", "Z")
        rewritten = False
        out_lines: list[str] = []
        for line in REPLAY_STORE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except Exception:
                out_lines.append(line)
                continue
            if entry.get("nonce") == nonce:
                entry["ts"] = backdated_ts
                out_lines.append(json.dumps(entry))
                rewritten = True
            else:
                out_lines.append(line)
        assert rewritten, f"nonce {nonce} not found in {REPLAY_STORE} — store format changed?"
        REPLAY_STORE.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

        # (4) same nonce + freshly-signed token → 200: the backdated entry is
        # outside the GC window, so the nonce is treated as fresh again.
        token3, ts3, _ = _sign_hmac(mid, "patch_metadata", reason, nonce=nonce)
        r3 = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={"metadata": {"low8_marker": "after-window"}, "actor": "user-direct", "reason": reason},
            headers={**H, "X-User-Direct-Token": token3, "X-User-Direct-Ts": ts3,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
        assert r3.status_code == 200, (
            f"LOW-8: nonce older than the {REPLAY_GC_SECONDS}s window must be reusable "
            f"(finite window); got {r3.status_code}: {r3.text}"
        )
    finally:
        _force_delete(mid)


# ---------- v0.18 MED-6: GET /v1/memories/{id} strips intent keys ----------

# v0.19 M12/L3: the FULL real-world marker set — underscore (mem0-mcp-shim
# convention) + non-underscore (the key the 0.B hook wrote pre-v0.19, still on
# stored records). Must mirror _INTENT_KEYS in app.py.
ALL_INTENT_KEYS = {
    "_canonical_intent", "canonical_intent",
    "_insight_intent", "insight_intent",
    "_stable_intent", "stable_intent",
}


def test_med6_get_by_id_strips_intent_keys():
    """v0.18 MED-6: GET /v1/memories/{id} must not leak promotion-intent markers
    (F.1.2 enumeration backdoor: search hid them, but the by-id read still
    returned them).

    v0.19 M12/L3: seeds ALL variants — the v0.18 test seeded only the three
    underscore keys, so 'stable_intent' (the key the 0.B hook actually wrote)
    passed the suite unnoticed."""
    text = f"med6-intent-keys-{uuid.uuid4()}"
    r = httpx.post(
        f"{URL}/v1/memories",
        json={
            "messages": text, "user_id": "test-inv", "infer": False,
            "metadata": {
                "tier": "evidence", "source": "test-med6", "user_id": "test-inv",
                **{k: True for k in ALL_INTENT_KEYS},
            },
        },
        headers=H, timeout=10,
    )
    r.raise_for_status()
    mid = r.json()["results"][0]["id"]
    try:
        g = httpx.get(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
        assert g.status_code == 200, f"get-by-id failed: {g.status_code}: {g.text}"
        md = g.json().get("metadata") or {}
        leaked = ALL_INTENT_KEYS & set(md.keys())
        assert not leaked, f"MED-6: intent keys leaked from GET by-id: {sorted(leaked)}"
        # Control: non-intent metadata must still be returned
        assert md.get("source") == "test-med6", f"non-intent metadata must survive the filter: {md}"
    finally:
        httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)


def test_m12_search_strips_intent_keys_from_metadata():
    """v0.19 M12: POST /v1/memories/search must strip promotion-intent markers
    from every surviving result's metadata. F.1.2 excludes whole
    _canonical_intent records, but _insight_intent / stable_intent (the 0.B
    hook's pre-v0.19 key) rode out via search metadata — the same enumeration
    oracle MED-6 closed on the by-id path."""
    # Record A: every variant EXCEPT _canonical_intent (which excludes the whole
    # record from default search) — must SURFACE, with all markers stripped.
    surfacing_keys = ALL_INTENT_KEYS - {"_canonical_intent"}
    text_a = f"v019-m12-search-strip-{uuid.uuid4()}"
    ra = httpx.post(
        f"{URL}/v1/memories",
        json={
            "messages": text_a, "user_id": "test-inv", "infer": False,
            "metadata": {
                "tier": "evidence", "source": "test-m12", "user_id": "test-inv",
                **{k: True for k in surfacing_keys},
            },
        },
        headers=H, timeout=10,
    )
    ra.raise_for_status()
    mid_a = ra.json()["results"][0]["id"]
    # Record B: _canonical_intent — surfaced only via the opt-in flag; even then
    # the marker itself must be stripped from the returned metadata.
    text_b = f"v019-m12-optin-strip-{uuid.uuid4()}"
    rb = httpx.post(
        f"{URL}/v1/memories",
        json={
            "messages": text_b, "user_id": "test-inv", "infer": False,
            "metadata": {
                "tier": "evidence", "source": "test-m12", "user_id": "test-inv",
                "_canonical_intent": True,
            },
        },
        headers=H, timeout=10,
    )
    rb.raise_for_status()
    mid_b = rb.json()["results"][0]["id"]
    try:
        sr = httpx.post(
            f"{URL}/v1/memories/search",
            json={"query": text_a, "filters": {"user_id": "test-inv"}, "limit": 20},
            headers=H, timeout=15,
        )
        sr.raise_for_status()
        by_id = {m["id"]: m for m in sr.json().get("results", [])}
        assert mid_a in by_id, (
            f"M12: non-excluded intent-marked record must still surface in default "
            f"search (got ids: {sorted(by_id)})"
        )
        md_a = by_id[mid_a].get("metadata") or {}
        leaked = ALL_INTENT_KEYS & set(md_a.keys())
        assert not leaked, f"M12: intent keys leaked via search metadata: {sorted(leaked)}"
        assert md_a.get("source") == "test-m12", (
            f"non-intent metadata must survive the search strip: {md_a}"
        )

        sr2 = httpx.post(
            f"{URL}/v1/memories/search",
            json={
                "query": text_b,
                "filters": {"user_id": "test-inv", "include_canonical_intent": True},
                "limit": 20,
            },
            headers=H, timeout=15,
        )
        sr2.raise_for_status()
        by_id2 = {m["id"]: m for m in sr2.json().get("results", [])}
        assert mid_b in by_id2, (
            f"include_canonical_intent=True must still surface the record "
            f"(got ids: {sorted(by_id2)})"
        )
        md_b = by_id2[mid_b].get("metadata") or {}
        assert "_canonical_intent" not in md_b, (
            f"M12: _canonical_intent marker leaked via opt-in search metadata: {md_b}"
        )
    finally:
        httpx.delete(f"{URL}/v1/memories/{mid_a}", headers=H, timeout=10)
        httpx.delete(f"{URL}/v1/memories/{mid_b}", headers=H, timeout=10)


# ---------- v0.18 MED-10: log files chmod 600 ----------

def test_med10_retrieval_log_and_replay_store_owner_only():
    """v0.18 MED-10: after a search (which appends to retrieval-log.jsonl), the
    log must be mode 0600. The replay store, if present, must also be 0600
    (startup normalization + _secure_open / touch(mode=0o600) at creation)."""
    r = httpx.post(
        f"{URL}/v1/memories/search",
        json={"query": "med10 perms probe", "filters": {"user_id": "test-inv"}, "limit": 1},
        headers=H, timeout=15,
    )
    assert r.status_code == 200, f"search failed: {r.status_code}: {r.text}"
    log_path = Path.home() / ".mem0" / "retrieval-log.jsonl"
    assert log_path.exists(), "retrieval-log.jsonl should exist after a search"
    mode = log_path.stat().st_mode & 0o777
    assert mode == 0o600, f"retrieval-log.jsonl must be 0600; got {oct(mode)}"
    replay = Path.home() / ".mem0" / "canonical-replay.jsonl"
    if replay.exists():
        rmode = replay.stat().st_mode & 0o777
        assert rmode == 0o600, f"canonical-replay.jsonl must be 0600; got {oct(rmode)}"


# ---------- v0.20 Phase E (M4): retrieval-log records query_class ----------

def _last_retrieval_log_entry_for(query: str) -> dict:
    """Most recent retrieval-log line whose query_hash matches `query`
    (hash-keyed lookup so concurrent hook traffic cannot race the tail line)."""
    qh = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    log_path = Path.home() / ".mem0" / "retrieval-log.jsonl"
    assert log_path.exists(), "retrieval-log.jsonl should exist after a search"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    for line in reversed(lines[-200:]):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("query_hash") == qh:
            return rec
    raise AssertionError(f"no retrieval-log entry with query_hash={qh}")


def test_m4_retrieval_log_records_query_class_default_and_history():
    """v0.20 Phase E (M4): every retrieval-log entry records query_class, and
    history-class forensic reads are flagged forensic=true — the 'audited
    escape hatch' (admission-gate.md) is now actually audited. A default
    search logs query_class='durable' / forensic=false. Logging-only change:
    authz is by design; the bundle endpoint shares _search_core, so its
    searches carry the field too."""
    q_default = f"m4 default probe {uuid.uuid4()}"
    r = httpx.post(
        f"{URL}/v1/memories/search",
        json={"query": q_default, "filters": {"user_id": "test-inv"}, "limit": 1},
        headers=H, timeout=15,
    )
    assert r.status_code == 200, f"search failed: {r.status_code}: {r.text}"
    rec = _last_retrieval_log_entry_for(q_default)
    assert rec.get("query_class") == "durable", (
        f"default search must log query_class='durable': {rec}"
    )
    assert rec.get("forensic") is False, f"default search must log forensic=false: {rec}"

    q_hist = f"m4 history probe {uuid.uuid4()}"
    r = httpx.post(
        f"{URL}/v1/memories/search",
        json={"query": q_hist, "query_class": "history",
              "filters": {"user_id": "test-inv"}, "limit": 1},
        headers=H, timeout=15,
    )
    assert r.status_code == 200, f"history search failed: {r.status_code}: {r.text}"
    rec = _last_retrieval_log_entry_for(q_hist)
    assert rec.get("query_class") == "history", (
        f"history search must log query_class='history': {rec}"
    )
    assert rec.get("forensic") is True, f"history search must log forensic=true: {rec}"


# ---------- v0.19/v0.20 Phase G: PATCH /tier format-2 (promote) + format-1 REJECTED ----------

def _patch_tier_canonical(mid: str, reason: str, headers_extra: dict) -> httpx.Response:
    """PATCH /tier → canonical with caller-supplied HMAC headers."""
    return httpx.patch(
        f"{URL}/v1/memories/{mid}/tier",
        json={"tier": "canonical", "actor": "user-direct", "reason": reason},
        headers={**H, **headers_extra},
        timeout=10,
    )


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_g_format2_promote_succeeds_and_updates_tier():
    """v0.19 Phase G: PATCH /tier with a format-2 token
    (<ts>|<nonce>|promote|<mid>|<reason>) + X-User-Direct-Nonce → 200, tier
    actually updated to canonical."""
    mid = _post_evidence(f"g-format2-promote-{uuid.uuid4()}")
    try:
        reason = "phase-g format-2 promote"
        token, ts, nonce = _sign_hmac(mid, "promote", reason)
        r = _patch_tier_canonical(mid, reason, {
            "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
            "X-User-Direct-Nonce": nonce,
        })
        assert r.status_code == 200, (
            f"format-2 promote must succeed; got {r.status_code}: {r.text}"
        )
        assert _get_memory_tier(mid) == "canonical", (
            "tier must be canonical after format-2 promote"
        )
    finally:
        _force_delete(mid)


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_g_format2_promote_nonce_replay_rejected():
    """v0.19 Phase G: a fresh VALID format-2 promote token reusing an
    already-burned nonce → 403 replay (PATCH /tier now consults the replay
    store — the v0.18 LOW-4 residual window is closed on the nonce path)."""
    mid = _post_evidence(f"g-format2-replay-{uuid.uuid4()}")
    try:
        reason = "phase-g replay probe"
        nonce = str(uuid.uuid4())
        token1, ts1, _ = _sign_hmac(mid, "promote", reason, nonce=nonce)
        r1 = _patch_tier_canonical(mid, reason, {
            "X-User-Direct-Token": token1, "X-User-Direct-Ts": ts1,
            "X-User-Direct-Nonce": nonce,
        })
        assert r1.status_code == 200, (
            f"first format-2 promote must succeed; got {r1.status_code}: {r1.text}"
        )
        # Fresh ts → fresh VALID token, SAME nonce → must be rejected as replay
        token2, ts2, _ = _sign_hmac(mid, "promote", reason, nonce=nonce)
        r2 = _patch_tier_canonical(mid, reason, {
            "X-User-Direct-Token": token2, "X-User-Direct-Ts": ts2,
            "X-User-Direct-Nonce": nonce,
        })
        assert r2.status_code == 403, (
            f"replayed nonce on promote must be 403; got {r2.status_code}: {r2.text}"
        )
        assert "replay" in r2.text.lower() or "nonce" in r2.text.lower(), (
            f"error should mention replay/nonce: {r2.text}"
        )
    finally:
        _force_delete(mid)


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_g_format1_promote_rejected_403_tier_unchanged():
    """v0.20 Phase G: format-1 (<ts>|<mid>|<reason>, no nonce) is REJECTED —
    the deprecation committed in v0.19 lands. Even a VALIDLY-signed format-1
    token without X-User-Direct-Nonce → 403 whose message names the format-2
    payload and mem0-canonize.sh; tier stays evidence (flipped from the v0.19
    back-compat test, which asserted the 200 this now forbids)."""
    mid = _post_evidence(f"g-format1-rejected-{uuid.uuid4()}")
    try:
        reason = "phase-g format-1 rejection"
        token, ts = _sign_legacy_tier(mid, reason)
        r = _patch_tier_canonical(mid, reason, {
            "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
        })
        assert r.status_code == 403, (
            f"format-1 promote must be rejected in v0.20; "
            f"got {r.status_code}: {r.text}"
        )
        assert "format-2" in r.text, (
            f"403 must name the format-2 payload the caller should sign: {r.text}"
        )
        assert "mem0-canonize.sh" in r.text, (
            f"403 must point the caller at mem0-canonize.sh: {r.text}"
        )
        assert "X-User-Direct-Nonce" in r.text, (
            f"403 must name the missing header: {r.text}"
        )
        assert _get_memory_tier(mid) == "evidence", (
            "tier must be unchanged after rejected format-1 promote"
        )
    finally:
        _force_delete(mid)


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_g_format2_promote_garbage_token_does_not_burn_nonce():
    """v0.19 Phase G: garbage format-2 promote token + nonce N → 403 HMAC
    mismatch, tier unchanged, and N is NOT recorded (MED-8 semantics: HMAC is
    verified BEFORE the nonce is burned). A subsequent VALID promote reusing N
    must succeed."""
    mid = _post_evidence(f"g-format2-garbage-{uuid.uuid4()}")
    try:
        reason = "phase-g garbage-token probe"
        nonce = str(uuid.uuid4())
        ts1 = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        r1 = _patch_tier_canonical(mid, reason, {
            "X-User-Direct-Token": "A" * 44, "X-User-Direct-Ts": ts1,
            "X-User-Direct-Nonce": nonce,
        })
        assert r1.status_code == 403, (
            f"garbage token must be 403; got {r1.status_code}: {r1.text}"
        )
        assert "hmac" in r1.text.lower() or "mismatch" in r1.text.lower(), (
            f"expected HMAC mismatch (not replay) for the garbage token: {r1.text}"
        )
        assert _get_memory_tier(mid) == "evidence", (
            "tier must be unchanged after denied garbage-token promote"
        )
        # Same nonce + VALID token → 200 (the garbage attempt did not burn it)
        token, ts2, _ = _sign_hmac(mid, "promote", reason, nonce=nonce)
        r2 = _patch_tier_canonical(mid, reason, {
            "X-User-Direct-Token": token, "X-User-Direct-Ts": ts2,
            "X-User-Direct-Nonce": nonce,
        })
        assert r2.status_code == 200, (
            f"MED-8 regression on promote path: garbage token burned the nonce "
            f"(valid reuse got {r2.status_code}: {r2.text})"
        )
        assert _get_memory_tier(mid) == "canonical"
    finally:
        _force_delete(mid)


# ---------- v0.20 Phase E (L12): cross-action HMAC + promote skew negatives ----------

@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_g_cross_action_promote_token_rejected_on_delete():
    """v0.20 Phase E (L12): a VALID format-2 PROMOTE token presented to DELETE
    → 403 HMAC mismatch (the action prefix keys the token domains apart — this
    pins Phase G's 'non-replayable against the other mutation endpoints'
    claim), the record survives, and the nonce is NOT burned (MED-8: mismatch
    fires BEFORE the replay store) — a fresh VALID delete token reusing the
    same nonce must succeed."""
    mid = _post_evidence(f"g-cross-action-{uuid.uuid4()}")
    try:
        _promote_to_canonical(mid)
        reason = "phase-e cross-action probe"
        token, ts, nonce = _sign_hmac(mid, "promote", reason)
        r = httpx.delete(
            f"{URL}/v1/memories/{mid}",
            params={"actor": "user-direct", "reason": reason},
            headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
        assert r.status_code == 403, (
            f"promote token on DELETE must be 403; got {r.status_code}: {r.text}"
        )
        assert "mismatch" in r.text.lower(), (
            f"expected HMAC mismatch for the cross-action token: {r.text}"
        )
        g = httpx.get(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
        assert g.status_code == 200, (
            f"record must survive the cross-action attempt; GET got {g.status_code}"
        )
        # Nonce NOT burned: fresh VALID delete token, SAME nonce → 200
        reason2 = "phase-e cross-action cleanup"
        token2, ts2, _ = _sign_hmac(mid, "delete", reason2, nonce=nonce)
        r2 = httpx.delete(
            f"{URL}/v1/memories/{mid}",
            params={"actor": "user-direct", "reason": reason2},
            headers={**H, "X-User-Direct-Token": token2, "X-User-Direct-Ts": ts2,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
        assert r2.status_code == 200, (
            f"MED-8 regression: cross-action mismatch burned the nonce "
            f"(valid delete reusing it got {r2.status_code}: {r2.text})"
        )
    finally:
        _force_delete(mid)


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_g_format2_promote_stale_ts_rejected_tier_unchanged():
    """v0.20 Phase E (L12): format-2 promote with a 10-minute-old timestamp
    (>300s skew) → 403 mentioning skew, tier unchanged. Mirrors
    test_canonical_put_stale_token_rejected_no_state_change for the PATCH /tier
    promote wiring, so a format-1-removal refactor of update_tier cannot
    silently drop the skew gate."""
    mid = _post_evidence(f"g-format2-stale-{uuid.uuid4()}")
    try:
        reason = "phase-e stale promote probe"
        stale_ts = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)
        ).isoformat().replace("+00:00", "Z")
        token, _, nonce = _sign_hmac(mid, "promote", reason, ts=stale_ts)
        r = _patch_tier_canonical(mid, reason, {
            "X-User-Direct-Token": token, "X-User-Direct-Ts": stale_ts,
            "X-User-Direct-Nonce": nonce,
        })
        assert r.status_code == 403, (
            f"stale-ts promote must be 403; got {r.status_code}: {r.text}"
        )
        assert "skew" in r.text.lower(), f"error should mention skew: {r.text}"
        assert _get_memory_tier(mid) == "evidence", (
            "tier must be unchanged after denied stale-ts promote"
        )
    finally:
        _force_delete(mid)
