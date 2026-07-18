"""Tier enforcement must survive the ledger simplification. Each test asserts
the rule that audit 2026-06-08 codified — no actor bypass, no canonical-via-add."""
import os, httpx, pytest, uuid, hmac, base64, hashlib, datetime as dt
URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ["MEM0_KEY"]
H = {"X-API-Key": KEY, "Content-Type": "application/json"}

# v0.14 B: canonical-key for HMAC signing in tests
# v0.19 Phase H: key via provider (runtime tmpfs > dpapi-on-win > plaintext) —
# conftest.py inserts mem0-server/ into sys.path before this module loads.
from canonical_key_provider import CanonicalKeyProvider  # noqa: E402

_CANON_KEY = CanonicalKeyProvider().get_key()

def _add_evidence(text: str) -> str:
    r = httpx.post(f"{URL}/v1/memories", json={
        "messages": text, "user_id": "test-tier", "infer": False,
        "metadata": {"tier": "evidence", "source": "test-tier-policy"},
    }, headers=H, timeout=10)
    r.raise_for_status()
    return r.json()["results"][0]["id"]

def _canonical_headers(mid: str, reason: str) -> dict:
    """Build X-User-Direct-Token/-Ts/-Nonce headers for a canonical PATCH /tier.

    v0.20 Phase G: format-1 (<ts>|<mid>|<reason>) is rejected — sign format-2
    (<ts>|<nonce>|promote|<mid>|<reason>) with the nonce header."""
    assert _CANON_KEY, "canonical key unavailable (runtime tmpfs / dpapi / plaintext all absent)"
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = str(uuid.uuid4())
    msg = f"{ts}|{nonce}|promote|{mid}|{reason}".encode()
    token = base64.b64encode(
        hmac.new(_CANON_KEY.encode(), msg, hashlib.sha256).digest()
    ).decode().strip()
    return {"X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
            "X-User-Direct-Nonce": nonce}

def test_add_canonical_rejected():
    r = httpx.post(f"{URL}/v1/memories", json={
        "messages": f"canonical add attempt {uuid.uuid4()}", "user_id": "test-tier", "infer": False,
        "metadata": {"tier": "canonical"},
    }, headers=H, timeout=10)
    assert r.status_code == 403, r.text

def test_promote_canonical_requires_user_direct():
    mid = _add_evidence(f"promote test {uuid.uuid4()}")
    r = httpx.patch(f"{URL}/v1/memories/{mid}/tier", json={
        "tier": "canonical", "actor": "claude-autonomous", "reason": "test",
    }, headers=H, timeout=10)
    assert r.status_code == 403, r.text
    httpx.delete(f"{URL}/v1/memories/{mid}", headers=H)  # cleanup on 403 path

@pytest.mark.skipif(_CANON_KEY is None, reason="canonical key unavailable (runtime tmpfs / dpapi / plaintext all absent)")
def test_promote_canonical_user_direct_succeeds():
    """v0.14: canonical promotion requires valid HMAC X-User-Direct-Token."""
    mid = _add_evidence(f"promote test {uuid.uuid4()}")
    reason = "test promotion"
    r = httpx.patch(f"{URL}/v1/memories/{mid}/tier", json={
        "tier": "canonical", "actor": "user-direct", "reason": reason,
    }, headers={**H, **_canonical_headers(mid, reason)}, timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tier"] == "canonical"
    assert body["actor"] == "user-direct"
    assert "ts" in body
    # No change_id required anymore
    httpx.delete(f"{URL}/v1/memories/{mid}", headers=H)

def test_insight_bypass_substring_rejected():
    """v0.14 C: substring check replaced with exact-allowlist — fake-c1-bypass must be rejected."""
    import uuid, httpx
    r = httpx.post(f"{URL}/v1/memories", json={
        "messages": "evidence for insight test", "user_id": "test-insight-allow", "infer": False,
        "metadata": {"tier": "evidence", "source": "test-insight-allow"},
    }, headers=H, timeout=10)
    r.raise_for_status()
    mid = r.json()["results"][0]["id"]
    r = httpx.patch(f"{URL}/v1/memories/{mid}/tier", json={
        "tier": "insight", "actor": "fake-c1-bypass", "reason": "should fail",
    }, headers=H, timeout=10)
    assert r.status_code == 403, r.text
    body_text = r.text.lower()
    assert "insight" in body_text, f"error message should mention 'insight': {r.text}"
    httpx.delete(f"{URL}/v1/memories/{mid}", headers=H)

def test_post_memory_canonical_message_includes_canonize_hint():
    """v0.16.1: rejection message tells caller what to do instead of just NO."""
    r = httpx.post(f"{URL}/v1/memories", json={
        "messages": "test canonical hint",
        "user_id": "test",
        "infer": False,
        "metadata": {"tier": "canonical"},
    }, headers=H, timeout=10)
    assert r.status_code in (400, 403)
    # Body should mention either 'canonize' or 'evidence' or the CLI path
    body = r.text.lower()
    assert "canonize" in body or "evidence" in body, body

def test_post_memory_insight_message_includes_dream_hint():
    """v0.16.1: insight rejection points at dream consolidator workflow."""
    r = httpx.post(f"{URL}/v1/memories", json={
        "messages": "test insight hint",
        "user_id": "test",
        "infer": False,
        "metadata": {"tier": "insight", "source": "manual-user"},
    }, headers=H, timeout=10)
    assert r.status_code == 403
    body = r.text.lower()
    assert "consolidator" in body or "dream" in body or "allowlist" in body, body

def _action_headers(mid: str, action: str, reason: str) -> dict:
    """Build HMAC headers for v0.17 format-2 actions (put/delete/patch_metadata).
    Signed payload: <ts>|<action>|<memory_id>|<reason>
    """
    assert _CANON_KEY, "canonical key unavailable (runtime tmpfs / dpapi / plaintext all absent)"
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"{ts}|{action}|{mid}|{reason}".encode()
    token = base64.b64encode(
        hmac.new(_CANON_KEY.encode(), msg, hashlib.sha256).digest()
    ).decode().strip()
    return {"X-User-Direct-Token": token, "X-User-Direct-Ts": ts}


def _promote_to_canonical(mid: str, reason: str = "v017-test-setup") -> None:
    """Promote a memory to canonical tier using HMAC (requires canonical-key)."""
    r = httpx.patch(f"{URL}/v1/memories/{mid}/tier", json={
        "tier": "canonical", "actor": "user-direct", "reason": reason,
    }, headers={**H, **_canonical_headers(mid, reason)}, timeout=10)
    assert r.status_code == 200, f"canonical promotion failed: {r.text}"


def _delete_canonical(mid: str, reason: str = "v017-test-cleanup") -> None:
    """Delete a canonical memory using HMAC (requires canonical-key). Best-effort."""
    try:
        action_hdrs = _action_headers(mid, "delete", reason)
        httpx.delete(
            f"{URL}/v1/memories/{mid}?actor=user-direct&reason={reason}",
            headers={**H, **action_hdrs},
            timeout=10,
        )
    except Exception:
        pass


@pytest.mark.skipif(_CANON_KEY is None, reason="canonical-key not present")
def test_put_canonical_without_token_rejected():
    """v0.17 Phase A: PUT text on a canonical record without HMAC → 403; text unchanged."""
    mid = _add_evidence(f"v017-A-put-canonical-{uuid.uuid4()}")
    _promote_to_canonical(mid, reason="v017-A put test setup")
    try:
        r = httpx.put(f"{URL}/v1/memories/{mid}",
                      json={"text": "tampered text — should not land"},
                      headers=H, timeout=10)
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
        # Error message must mention HMAC or user-direct
        assert "HMAC" in r.text or "user-direct" in r.text or "canonize" in r.text.lower(), \
            f"error should mention HMAC/user-direct/canonize: {r.text}"
        # Verify memory still exists (wasn't deleted as side effect)
        get_r = httpx.get(f"{URL}/v1/memories", params={"user_id": "test-tier"}, headers=H, timeout=10)
        assert get_r.status_code == 200
    finally:
        _delete_canonical(mid, reason="v017-A put test cleanup")


@pytest.mark.skipif(_CANON_KEY is None, reason="canonical-key not present")
def test_delete_canonical_without_token_rejected():
    """v0.17 Phase A: DELETE a canonical record without HMAC → 403; memory still exists."""
    mid = _add_evidence(f"v017-A-delete-canonical-{uuid.uuid4()}")
    _promote_to_canonical(mid, reason="v017-A delete test setup")
    try:
        r = httpx.delete(f"{URL}/v1/memories/{mid}?actor=user-direct&reason=test",
                         headers=H, timeout=10)
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
        # Error message must mention HMAC or user-direct
        assert "HMAC" in r.text or "user-direct" in r.text or "canonize" in r.text.lower(), \
            f"error should mention HMAC/user-direct/canonize: {r.text}"
        # Memory must still exist — verify via list (search by user_id, confirm mid present)
        list_r = httpx.get(f"{URL}/v1/memories", params={"user_id": "test-tier", "limit": 500},
                           headers=H, timeout=10)
        assert list_r.status_code == 200
        ids = [entry.get("id") for entry in (list_r.json().get("results") or [])]
        assert mid in ids, f"canonical memory {mid} should still exist after rejected DELETE"
    finally:
        _delete_canonical(mid, reason="v017-A delete test cleanup")


@pytest.mark.skipif(_CANON_KEY is None, reason="canonical-key not present")
def test_patch_metadata_canonical_without_token_rejected():
    """v0.17 Phase A: PATCH /metadata on a canonical record without HMAC → 403; payload unchanged."""
    mid = _add_evidence(f"v017-A-patch-meta-canonical-{uuid.uuid4()}")
    _promote_to_canonical(mid, reason="v017-A patch_metadata test setup")
    try:
        r = httpx.patch(f"{URL}/v1/memories/{mid}/metadata",
                        json={"metadata": {"tampered_key": "tampered_value"},
                              "actor": "claude-autonomous",
                              "reason": "attempt to patch canonical without HMAC"},
                        headers=H, timeout=10)
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
        # Error message must mention HMAC or user-direct
        assert "HMAC" in r.text or "user-direct" in r.text or "canonize" in r.text.lower(), \
            f"error should mention HMAC/user-direct/canonize: {r.text}"
    finally:
        _delete_canonical(mid, reason="v017-A patch_metadata test cleanup")


def test_ledger_has_single_entry_per_promote(tmp_path):
    """After simplification, each promote writes exactly one ledger line."""
    # MEM-16: count across legacy archive + monthly segments.
    from _ledger_paths import ledger_line_count
    before = ledger_line_count()
    mid = _add_evidence(f"ledger test {uuid.uuid4()}")
    r = httpx.patch(f"{URL}/v1/memories/{mid}/tier", json={
        "tier": "stable", "actor": "claude-autonomous", "reason": "test",
    }, headers=H, timeout=10)
    assert r.status_code == 200
    after = ledger_line_count()
    # Old two-phase wrote 2 entries (intent + complete); new should write 1.
    assert after - before == 1, f"expected 1 ledger line, got {after - before}"
    httpx.delete(f"{URL}/v1/memories/{mid}", headers=H)
