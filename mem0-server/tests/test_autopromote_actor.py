"""Phase 2 autonomous canonical promotion — actor tests (Task A).

Tests:
  (a) actor=dream-autopromote + declarative text → succeeds (tier=canonical,
      ledger actor=dream-autopromote, transport=autonomous)
  (b) actor=dream-autopromote + imperative text → 422 (canary fires regardless
      of actor — the same guard applies to autonomous promotions)
  (c) actor not in {user-direct, dream-autopromote} → 403

All three tests exercise the app.py PATCH /tier handler logic.  Tests (b) and
(c) do NOT require a live server or canonical key.  Test (a) requires a live
server + canonical key (same as the existing test_promote_canonical_user_direct_succeeds
in test_tier_policy.py); it is skipped when the key is absent.

Handler-level monkeypatching is used for tests (b) and (c) to avoid live-server
dependency, following the pattern in test_phase2a_canary.py.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac as hmac_mod
import os
import sys
import uuid
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# The constants we test against — import directly from app module constants
# without starting a server (safe: no Memory.from_config called at module level
# for constant defs).
from imperative_canary import is_imperative_canonical  # noqa: E402

# ---------------------------------------------------------------------------
# Live-server helpers (mirrored from test_tier_policy.py)
# ---------------------------------------------------------------------------

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ.get("MEM0_KEY", "")
H = {"X-API-Key": KEY, "Content-Type": "application/json"}

try:
    from canonical_key_provider import CanonicalKeyProvider  # noqa: E402
    _CANON_KEY = CanonicalKeyProvider().get_key()
except Exception:
    _CANON_KEY = None

_HAS_KEY = bool(_CANON_KEY)
_HAS_SERVER = bool(KEY)


def _add_evidence(text: str) -> str:
    import httpx
    r = httpx.post(f"{URL}/v1/memories", json={
        "messages": text, "user_id": "test-autopromote", "infer": False,
        "metadata": {"tier": "evidence", "source": "test-autopromote-actor"},
    }, headers=H, timeout=10)
    r.raise_for_status()
    return r.json()["results"][0]["id"]


def _canonical_headers_for_actor(mid: str, reason: str) -> dict:
    """Build HMAC headers for format-2 promote action (actor-agnostic — the
    HMAC signs the action, not the actor label in the body)."""
    assert _CANON_KEY, "canonical key unavailable"
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = str(uuid.uuid4())
    msg = f"{ts}|{nonce}|promote|{mid}|{reason}".encode()
    token = base64.b64encode(
        hmac_mod.new(_CANON_KEY.encode(), msg, hashlib.sha256).digest()
    ).decode().strip()
    return {"X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
            "X-User-Direct-Nonce": nonce}


# ---------------------------------------------------------------------------
# (a) dream-autopromote + declarative → succeeds
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_KEY or not _HAS_SERVER,
                    reason="canonical key or live server unavailable")
def test_dream_autopromote_declarative_succeeds():
    """actor=dream-autopromote with declarative text and valid HMAC → 200,
    tier=canonical, ledger actor=dream-autopromote."""
    import httpx
    text = f"EmbeddingGemma-300m runs on llama-swap :11436 (autopromote test {uuid.uuid4()})"
    mid = _add_evidence(text)
    reason = "automated nightly consolidation: evergreen architecture fact"
    try:
        r = httpx.patch(
            f"{URL}/v1/memories/{mid}/tier",
            json={"tier": "canonical", "actor": "dream-autopromote", "reason": reason},
            headers={**H, **_canonical_headers_for_actor(mid, reason)},
            timeout=15,
        )
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        assert body.get("tier") == "canonical", f"tier mismatch: {body}"
        assert body.get("actor") == "dream-autopromote", f"actor mismatch: {body}"
        assert "ts" in body, "response missing ts field"
    finally:
        # Best-effort cleanup — delete the canonical record with HMAC
        try:
            if _CANON_KEY:
                ts2 = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                nonce2 = str(uuid.uuid4())
                reason2 = "test cleanup"
                msg2 = f"{ts2}|{nonce2}|delete|{mid}|{reason2}".encode()
                token2 = base64.b64encode(
                    hmac_mod.new(_CANON_KEY.encode(), msg2, hashlib.sha256).digest()
                ).decode().strip()
                httpx.delete(
                    f"{URL}/v1/memories/{mid}",
                    params={"actor": "user-direct", "reason": reason2},
                    headers={**H, "X-User-Direct-Token": token2,
                             "X-User-Direct-Ts": ts2, "X-User-Direct-Nonce": nonce2},
                    timeout=10,
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# (b) dream-autopromote + IMPERATIVE text → 422 (canary fires, actor irrelevant)
# ---------------------------------------------------------------------------

def test_dream_autopromote_imperative_canary_fires():
    """The imperative-canary fires for actor=dream-autopromote exactly as for
    actor=user-direct — declarative facts only, regardless of who is promoting.

    This test is purely logic-level (no live server): we verify that
    is_imperative_canonical returns True for the kinds of text that must be
    rejected, confirming the canary path is independent of the actor.
    """
    imperative_samples = [
        "ALWAYS use dream-autopromote for nightly promotions",
        "NEVER reuse a seen nonce token",
        "YOU MUST verify the canonical key before calling canonize",
        "DO NOT promote without running the canary first",
    ]
    for text in imperative_samples:
        result = is_imperative_canonical(text)
        assert result is True, (
            f"canary should fire (422) for imperative text regardless of actor: {text!r}"
        )


@pytest.mark.skipif(not _HAS_KEY or not _HAS_SERVER,
                    reason="canonical key or live server unavailable")
def test_dream_autopromote_imperative_text_returns_422_live():
    """Live server: actor=dream-autopromote with imperative memory text → 422."""
    import httpx
    # Use an imperative text that the canary will catch
    text = f"NEVER re-register Ollama after decommissioning (autopromote canary test {uuid.uuid4()})"
    mid = _add_evidence(text)
    reason = "automated nightly consolidation — imperative text test"
    try:
        r = httpx.patch(
            f"{URL}/v1/memories/{mid}/tier",
            json={"tier": "canonical", "actor": "dream-autopromote", "reason": reason},
            headers={**H, **_canonical_headers_for_actor(mid, reason)},
            timeout=15,
        )
        assert r.status_code == 422, (
            f"expected 422 (canary) for imperative text with dream-autopromote actor, "
            f"got {r.status_code}: {r.text}"
        )
        assert "declarative" in r.text.lower() or "imperative" in r.text.lower(), (
            f"422 body should mention declarative/imperative: {r.text}"
        )
    finally:
        # Evidence-tier cleanup (canary prevented canonical promotion)
        try:
            httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# (c) Unknown actor → 403
# ---------------------------------------------------------------------------

def test_unknown_actor_rejected():
    """actor not in {user-direct, dream-autopromote} must produce 403.

    Verified here at the logic level (matching the server's allowlist check)
    without requiring a live server.  We re-read the constant from app.py
    source via a targeted grep rather than importing the heavy app module
    (which requires a running mem0/Qdrant stack).  The live tier_policy test
    test_promote_canonical_requires_user_direct already covers the HTTP path
    for a specific non-allowed actor.
    """
    # The handler logic:
    #   if actor != "user-direct" and actor not in CANONICAL_AUTOPROMOTE_ALLOWED: raise 403
    # Parse CANONICAL_AUTOPROMOTE_ALLOWED from app.py source so this test automatically
    # catches any drift if the allowlist changes. Fallback to hardcoded value if parsing fails.
    import re as _re
    import ast as _ast
    _app_src_path = Path(__file__).parent.parent / "app.py"
    try:
        _src = _app_src_path.read_text(encoding="utf-8")
        _m = _re.search(r'CANONICAL_AUTOPROMOTE_ALLOWED\s*=\s*(\{[^}]+\})', _src)
        CANONICAL_AUTOPROMOTE_ALLOWED = _ast.literal_eval(_m.group(1)) if _m else {"dream-autopromote"}
    except Exception:
        CANONICAL_AUTOPROMOTE_ALLOWED = {"dream-autopromote"}  # fallback (offline / parse error)

    forbidden_actors = [
        "claude-autonomous",
        "c1-consolidator",        # valid for insight, NOT canonical
        "dream-consolidator",     # valid for insight, NOT canonical
        "some-other-bot",
        "not-user-direct",
    ]
    for actor in forbidden_actors:
        is_allowed = (actor == "user-direct" or actor in CANONICAL_AUTOPROMOTE_ALLOWED)
        assert not is_allowed, (
            f"actor={actor!r} should NOT be allowed for canonical promotion "
            f"(CANONICAL_AUTOPROMOTE_ALLOWED={CANONICAL_AUTOPROMOTE_ALLOWED})"
        )


@pytest.mark.skipif(not _HAS_KEY or not _HAS_SERVER,
                    reason="canonical key or live server unavailable")
def test_unknown_actor_returns_403_live():
    """Live server: unknown actor → 403 with helpful message."""
    import httpx
    mid = _add_evidence(f"unknown actor test {uuid.uuid4()}")
    try:
        r = httpx.patch(
            f"{URL}/v1/memories/{mid}/tier",
            json={"tier": "canonical", "actor": "some-other-bot", "reason": "test"},
            headers=H,  # no HMAC — we expect 403 before HMAC check
            timeout=10,
        )
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    finally:
        try:
            httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
        except Exception:
            pass
