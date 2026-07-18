"""Phase B actor-auth regression tests — v0.14 (format-2 since v0.20 Phase G).

Four tests covering the HMAC X-User-Direct-Token credential layer for canonical promotions:
  1. No token → 403 (must name the missing nonce / format-2 since v0.20)
  2. Bad token → 403 (must mention HMAC or mismatch)
  3. Valid token → 200  (skipped if canonical-key not yet generated)
  4. Stale token → 403 (must mention skew)

v0.20 Phase G: format-1 (<ts>|<mid>|<reason>, no nonce) is rejected outright —
all signing here is format-2 (<ts>|<nonce>|promote|<mid>|<reason>) + nonce header.
"""
import os
import uuid
import hmac
import base64
import hashlib
import datetime as dt

import httpx
import pytest

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ["MEM0_KEY"]
# v0.19 Phase H: key via provider (runtime tmpfs > dpapi-on-win > plaintext) —
# conftest.py inserts mem0-server/ into sys.path before this module loads.
from canonical_key_provider import CanonicalKeyProvider  # noqa: E402

CANON_KEY = CanonicalKeyProvider().get_key()

H = {"X-API-Key": KEY, "Content-Type": "application/json"}


def _add_evidence(text: str) -> str:
    """Add a fresh evidence record and return its memory_id."""
    r = httpx.post(f"{URL}/v1/memories", json={
        "messages": text,
        "user_id": "test-auth",
        "infer": False,
        "metadata": {"tier": "evidence", "source": "test-actor-auth"},
    }, headers=H, timeout=15)
    r.raise_for_status()
    data = r.json()
    results = data.get("results", [])
    assert results, f"add returned no results: {data}"
    return results[0]["id"]


def _delete(mid: str) -> None:
    """Best-effort cleanup — ignore errors."""
    try:
        httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
    except Exception:
        pass


def test_canonical_without_token_rejected():
    """No X-User-Direct-Token header → 403 with helpful message."""
    mid = _add_evidence(f"canon test no-token {uuid.uuid4()}")
    try:
        r = httpx.patch(f"{URL}/v1/memories/{mid}/tier", json={
            "tier": "canonical",
            "actor": "user-direct",
            "reason": "test: no token",
        }, headers=H, timeout=10)
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
        # v0.20 Phase G: nonce-less promotion is refused first — the error must
        # name the missing header and the format-2 payload to sign instead
        assert "X-User-Direct-Nonce" in r.text and "format-2" in r.text, \
            f"expected X-User-Direct-Nonce + format-2 in error: {r.text}"
    finally:
        _delete(mid)


def test_canonical_bad_token_rejected():
    """Garbage X-User-Direct-Token → 403 with HMAC/mismatch in message."""
    mid = _add_evidence(f"canon test bad-token {uuid.uuid4()}")
    try:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = httpx.patch(f"{URL}/v1/memories/{mid}/tier", json={
            "tier": "canonical",
            "actor": "user-direct",
            "reason": "test: bad token",
        }, headers={
            **H,
            "X-User-Direct-Token": "AAAAAAAAAAAAAAAAAAAAAAAA",
            "X-User-Direct-Ts": ts,
            # v0.20 Phase G: nonce required — without it the nonce gate fires
            # before HMAC verification and this would not test the mismatch path
            "X-User-Direct-Nonce": str(uuid.uuid4()),
        }, timeout=10)
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
        assert "HMAC" in r.text or "mismatch" in r.text.lower(), \
            f"expected HMAC/mismatch in error: {r.text}"
    finally:
        _delete(mid)


@pytest.mark.skipif(CANON_KEY is None, reason="canonical key unavailable (runtime tmpfs / dpapi / plaintext all absent)")
def test_canonical_valid_token_accepted():
    """Correctly signed token → 200."""
    mid = _add_evidence(f"canon test valid-token {uuid.uuid4()}")
    try:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        reason = "test: valid token accepted"
        # v0.20 Phase G: format-2 promote payload (format-1 is rejected)
        nonce = str(uuid.uuid4())
        msg = f"{ts}|{nonce}|promote|{mid}|{reason}".encode()
        token = base64.b64encode(
            hmac.new(CANON_KEY.encode(), msg, hashlib.sha256).digest()
        ).decode().strip()
        r = httpx.patch(f"{URL}/v1/memories/{mid}/tier", json={
            "tier": "canonical",
            "actor": "user-direct",
            "reason": reason,
        }, headers={
            **H,
            "X-User-Direct-Token": token,
            "X-User-Direct-Ts": ts,
            "X-User-Direct-Nonce": nonce,
        }, timeout=10)
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        assert body.get("tier") == "canonical", f"unexpected response: {body}"
    finally:
        _delete(mid)


def test_canonical_stale_token_rejected():
    """Token with timestamp > 5 min old → 403 with 'skew' in message."""
    mid = _add_evidence(f"canon test stale-token {uuid.uuid4()}")
    try:
        # 10 minutes ago — well beyond the 5-min tolerance
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        reason = "test: stale token"
        # v0.20 Phase G: format-2 (nonce required) — skew is checked before HMAC,
        # so even the no-key garbage-token fallback still exercises the skew gate
        nonce = str(uuid.uuid4())
        if CANON_KEY:
            msg = f"{old}|{nonce}|promote|{mid}|{reason}".encode()
            token = base64.b64encode(
                hmac.new(CANON_KEY.encode(), msg, hashlib.sha256).digest()
            ).decode().strip()
        else:
            token = "AAAAAAAAAAAAAAAAAAAAAAAA"
        r = httpx.patch(f"{URL}/v1/memories/{mid}/tier", json={
            "tier": "canonical",
            "actor": "user-direct",
            "reason": reason,
        }, headers={
            **H,
            "X-User-Direct-Token": token,
            "X-User-Direct-Ts": old,
            "X-User-Direct-Nonce": nonce,
        }, timeout=10)
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
        assert "skew" in r.text.lower(), f"expected 'skew' in error: {r.text}"
    finally:
        _delete(mid)
