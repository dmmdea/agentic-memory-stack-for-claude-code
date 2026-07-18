"""v0.17 Final fix-pass: regression tests for H2/H7, H3, H5, H8.

These tests prove the four behavioral invariants introduced by the adversarial
review fix-pass are enforced by the live server:

  H2/H7 — concurrent nonce writes: 50 threads with distinct nonces all land
           in the replay store (no nonce lost due to race or truncation).
  H3     — duplicate finalize protection: two finalize calls for the same session
           within 10s produce exactly one complete episode row, not two.
  H5     — fetch_current_tier fail-closed: a record with tier field stripped from
           Qdrant payload is treated as canonical (gate enforced, not bypassed).
  H8     — stamp-retired-at uses trusted-actor path: PATCH /metadata with
           actor='stamp-retired-v013' and key='retired_at' succeeds on canonical
           records without an HMAC token; other keys are still blocked.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac as _hmac
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import httpx
import pytest

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ.get("MEM0_KEY") or (Path.home() / ".mem0" / "api-key").read_text().strip()
H = {"X-API-Key": KEY, "Content-Type": "application/json"}

# v0.19 Phase H: key via provider (runtime tmpfs > dpapi-on-win > plaintext) —
# conftest.py inserts mem0-server/ into sys.path before this module loads.
from canonical_key_provider import CanonicalKeyProvider  # noqa: E402

CANONICAL_KEY: Optional[str] = CanonicalKeyProvider().get_key()

REPLAY_STORE = Path.home() / ".mem0" / "canonical-replay.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_evidence(text: str, **md_extra) -> str:
    md = {"tier": "evidence", "source": "test-h-fixes", "user_id": "test-hfix"}
    md.update(md_extra)
    r = httpx.post(
        f"{URL}/v1/memories",
        json={"messages": text, "user_id": "test-hfix", "infer": False, "metadata": md},
        headers=H, timeout=10,
    )
    r.raise_for_status()
    return r.json()["results"][0]["id"]


def _promote_to_canonical(mid: str) -> None:
    # v0.20 Phase G: format-1 (<ts>|<mid>|<reason>) is rejected by the server —
    # promote via format-2 (<ts>|<nonce>|promote|<mid>|<reason>) + nonce header.
    if CANONICAL_KEY is None:
        pytest.skip("canonical-key not configured")
    ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    nonce = str(uuid.uuid4())
    msg = f"{ts}|{nonce}|promote|{mid}|test-h-fixes setup".encode()
    token = base64.b64encode(
        _hmac.new(CANONICAL_KEY.encode(), msg, hashlib.sha256).digest()
    ).decode().strip()
    r = httpx.patch(
        f"{URL}/v1/memories/{mid}/tier",
        json={"tier": "canonical", "actor": "user-direct", "reason": "test-h-fixes setup"},
        headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
                 "X-User-Direct-Nonce": nonce},
        timeout=10,
    )
    r.raise_for_status()


def _force_delete(mid: str) -> None:
    if CANONICAL_KEY is None:
        httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
        return
    try:
        # v0.18 MED-7: nonce required — format <ts>|<nonce>|<action>|<mid>|<reason>
        ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        nonce = str(uuid.uuid4())
        msg = f"{ts}|{nonce}|delete|{mid}|test cleanup".encode()
        token = base64.b64encode(
            _hmac.new(CANONICAL_KEY.encode(), msg, hashlib.sha256).digest()
        ).decode().strip()
        httpx.delete(
            f"{URL}/v1/memories/{mid}",
            params={"actor": "user-direct", "reason": "test cleanup"},
            headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
                     "X-User-Direct-Nonce": nonce},
            timeout=10,
        )
    except Exception:
        pass


def _get_episodic_db_path() -> Path:
    """Return the path to the episodic SQLite DB."""
    return Path.home() / ".mem0" / "episodic.db"


# ---------------------------------------------------------------------------
# H2/H7 — concurrent nonce writes
# ---------------------------------------------------------------------------

def test_h2_h7_concurrent_nonce_writes_no_loss():
    """H2/H7: 50 concurrent threads with distinct nonces all land in the replay store.

    Each thread calls _check_and_record_nonce with a unique nonce. After all threads
    finish, the store must contain all 50 nonces (no nonce lost due to truncation race).
    """
    import json
    import sys
    # Import the module directly (not via HTTP) for unit-level testing
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from security_invariants import _check_and_record_nonce, REPLAY_STORE

    # Save original store content and restore after test
    orig_content = REPLAY_STORE.read_text(encoding="utf-8") if REPLAY_STORE.exists() else None

    try:
        # Clear the store for a clean test
        REPLAY_STORE.parent.mkdir(parents=True, exist_ok=True)
        REPLAY_STORE.write_text("", encoding="utf-8")

        ts_base = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        nonces = [f"test-concurrent-{i}-{uuid.uuid4()}" for i in range(50)]
        results = []
        errors = []

        def worker(nonce: str) -> None:
            try:
                r = _check_and_record_nonce(nonce, ts_base)
                results.append((nonce, r))
            except Exception as e:
                errors.append((nonce, str(e)))

        threads = [threading.Thread(target=worker, args=(n,)) for n in nonces]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent nonce writes: {errors}"

        # All 50 should have returned True (fresh nonce)
        true_results = [r for _, r in results if r]
        assert len(true_results) == 50, (
            f"Expected all 50 nonces accepted as fresh; got {len(true_results)} accepted, "
            f"{50 - len(true_results)} rejected (indicating a race or truncation loss)"
        )

        # All 50 nonces must be present in the store
        store_content = REPLAY_STORE.read_text(encoding="utf-8")
        stored_nonces = set()
        for line in store_content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                stored_nonces.add(entry.get("nonce", ""))
            except Exception:
                continue

        missing = set(nonces) - stored_nonces
        assert not missing, (
            f"H2/H7: {len(missing)} nonces lost from replay store (truncation race): {list(missing)[:5]}"
        )

    finally:
        # Restore original store content
        if orig_content is not None:
            REPLAY_STORE.write_text(orig_content, encoding="utf-8")
        else:
            REPLAY_STORE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# H3 — duplicate finalize protection
# ---------------------------------------------------------------------------

def test_h3_finalize_episode_duplicate_within_window():
    """H3: two finalize calls within 10s produce exactly one complete episode row."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from episodic import finalize_episode

    db_path = _get_episodic_db_path()
    if not db_path.exists():
        pytest.skip("episodic.db not found — skip H3 unit test")

    session_id = f"test-h3-{uuid.uuid4()}"
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Ensure session row exists
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, started_at) VALUES (?, ?)",
            (session_id, now_iso),
        )
        conn.commit()

        # First finalize
        ep1 = finalize_episode(conn, session_id, "goal1", "summary1", now_iso, 5)

        # Second finalize within 10s (same timestamp = definitely within window)
        ep2 = finalize_episode(conn, session_id, "goal2", "summary2", now_iso, 6)

        # Must update the same row, not insert a duplicate
        rows = conn.execute(
            "SELECT * FROM episodes WHERE session_id = ? AND state = 'complete'",
            (session_id,),
        ).fetchall()

        assert len(rows) == 1, (
            f"H3: expected 1 complete row for session {session_id}, got {len(rows)}"
        )
        assert ep1 == ep2, f"H3: expected same episode_id on duplicate finalize; got {ep1} vs {ep2}"
        # Second finalize should update the goal/summary
        assert rows[0]["goal_text"] == "goal2", (
            f"H3: goal_text should be updated by second finalize; got {rows[0]['goal_text']!r}"
        )

    finally:
        # Cleanup test session
        conn.execute("DELETE FROM episodes WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()


def test_h3_finalize_episode_no_dedup_beyond_window():
    """H3: two finalize calls more than 10s apart must produce two separate complete rows."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from episodic import finalize_episode

    db_path = _get_episodic_db_path()
    if not db_path.exists():
        pytest.skip("episodic.db not found — skip H3 unit test")

    session_id = f"test-h3-beyond-{uuid.uuid4()}"
    # Two timestamps 20 seconds apart — beyond the 10s dedup window
    ts1 = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
    ts2 = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, started_at) VALUES (?, ?)",
            (session_id, ts1),
        )
        conn.commit()

        ep1 = finalize_episode(conn, session_id, "goal1", "summary1", ts1, 5)
        ep2 = finalize_episode(conn, session_id, "goal2", "summary2", ts2, 6)

        rows = conn.execute(
            "SELECT * FROM episodes WHERE session_id = ? AND state = 'complete'",
            (session_id,),
        ).fetchall()

        assert len(rows) == 2, (
            f"H3: expected 2 complete rows for beyond-window finalizes, got {len(rows)}"
        )
        assert ep1 != ep2, "H3: beyond-window finalizes should produce distinct episode_ids"

    finally:
        conn.execute("DELETE FROM episodes WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# H5 — fetch_current_tier fail-closed on tier-missing
# ---------------------------------------------------------------------------

def test_h5_fetch_current_tier_fail_closed_when_tier_missing():
    """H5: fetch_current_tier returns 'canonical' (not None) when point exists but tier absent."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from security_invariants import fetch_current_tier

    # Build a mock Qdrant client that returns a point with no tier field in payload
    mock_record = MagicMock()
    mock_record.payload = {"memory": "some text", "user_id": "test"}  # no 'tier' key
    mock_record.id = str(uuid.uuid4())

    mock_client = MagicMock()
    mock_client.retrieve.return_value = [mock_record]

    result = fetch_current_tier(mock_client, "memories", mock_record.id)

    assert result == "canonical", (
        f"H5: expected 'canonical' (fail-closed) when tier field absent; got {result!r}"
    )


def test_h5_fetch_current_tier_not_found_returns_sentinel():
    """H5: fetch_current_tier returns _NOT_FOUND (not None) when point does not exist."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from security_invariants import fetch_current_tier, _NOT_FOUND

    mock_client = MagicMock()
    mock_client.retrieve.return_value = []  # empty list = point not found

    result = fetch_current_tier(mock_client, "memories", str(uuid.uuid4()))

    assert result == _NOT_FOUND, (
        f"H5: expected _NOT_FOUND sentinel when point absent; got {result!r}"
    )


def test_h5_tier_missing_blocks_write_without_hmac():
    """H5 integration: a record with tier field stripped is treated as canonical by the gate.

    We cannot strip the tier field from a live Qdrant record in a test without side effects,
    so this tests the gate logic directly: assert_writable with a mock that returns 'canonical'
    (from the fail-closed path) must enforce the HMAC gate.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from security_invariants import assert_writable
    from fastapi import HTTPException as FastAPIHTTPException

    # Mock client: point exists but tier is absent -> fetch_current_tier returns "canonical"
    mock_record = MagicMock()
    mock_record.payload = {"memory": "some text"}  # tier absent
    mock_client = MagicMock()
    mock_client.retrieve.return_value = [mock_record]

    mid = str(uuid.uuid4())
    with pytest.raises(FastAPIHTTPException) as exc_info:
        assert_writable(
            mock_client, "memories", mid,
            "put", None, None,  # no HMAC token
            actor="attacker", reason="test",
        )

    assert exc_info.value.status_code == 403 or exc_info.value.status_code == 503, (
        f"H5: expected 403/503 when gate enforced on tier-absent point; got {exc_info.value.status_code}"
    )


# ---------------------------------------------------------------------------
# H8 — stamp-retired-at uses trusted-actor HMAC path
# ---------------------------------------------------------------------------

@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_h8_stamp_retired_at_trusted_actor_allowed_on_canonical():
    """H8: actor='stamp-retired-v013' may PATCH retired_at on canonical records without HMAC."""
    mid = _post_evidence(f"h8-canonical-retired-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    try:
        r = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={
                "metadata": {"retired_at": dt.datetime.now(dt.timezone.utc).isoformat()},
                "actor": "stamp-retired-v013",
                "reason": "H8 test: trusted-actor retired_at stamp",
            },
            headers=H, timeout=10,
        )
        assert r.status_code == 200, (
            f"H8: stamp-retired-v013 should be allowed to PATCH retired_at on canonical; "
            f"got {r.status_code}: {r.text}"
        )
    finally:
        _force_delete(mid)


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_h8_stamp_retired_at_trusted_actor_blocked_for_other_keys():
    """H8: actor='stamp-retired-v013' is blocked from writing keys other than retired_at."""
    mid = _post_evidence(f"h8-canonical-other-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    try:
        r = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={
                "metadata": {
                    "retired_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "injected_key": "evil_value",  # not in TRUSTED_ACTOR_ALLOWED_KEYS
                },
                "actor": "stamp-retired-v013",
                "reason": "H8 test: should be blocked",
            },
            headers=H, timeout=10,
        )
        assert r.status_code == 403, (
            f"H8: stamp-retired-v013 writing disallowed keys should get 403; "
            f"got {r.status_code}: {r.text}"
        )
    finally:
        _force_delete(mid)


def test_h8_untrusted_actor_still_blocked_from_retired_at_on_canonical():
    """H8: an untrusted actor cannot PATCH retired_at on a canonical record without HMAC."""
    if CANONICAL_KEY is None:
        pytest.skip("canonical-key not present")
    mid = _post_evidence(f"h8-untrusted-{uuid.uuid4()}")
    _promote_to_canonical(mid)
    try:
        r = httpx.patch(
            f"{URL}/v1/memories/{mid}/metadata",
            json={
                "metadata": {"retired_at": "2026-01-01T00:00:00Z"},
                "actor": "some-other-actor",
                "reason": "H8 test: untrusted actor",
            },
            headers=H, timeout=10,
        )
        assert r.status_code == 403, (
            f"H8: untrusted actor PATCH on canonical should get 403; "
            f"got {r.status_code}: {r.text}"
        )
    finally:
        _force_delete(mid)


# ---------------------------------------------------------------------------
# v0.20 Phase B (M1+M3+M11) — retrieval-gating keys forbidden via PATCH /metadata
# ---------------------------------------------------------------------------
# The v0.19 admission gates read superseded_by / contradicts_canonical (and the
# sweep's idempotency marker contradiction_checked_at). v0.20 adds all three to
# FORBIDDEN_KEYS so an arbitrary API-key holder cannot censor retrieval via the
# generic shallow-merge endpoint; the per-actor TRUSTED_PATCH_ACTORS dict
# remains the ONLY write path (contradiction-sweep-v019 keeps its two keys;
# superseded_by has NO API writer and stays fully blocked until a future
# supersession-writer registers as a trusted actor).

_V020_GATE_KEYS = ("superseded_by", "contradicts_canonical", "contradiction_checked_at")


def _patch_md(mid: str, metadata: dict, actor: str | None, reason: str):
    body = {"metadata": metadata, "reason": reason}
    if actor is not None:
        body["actor"] = actor
    return httpx.patch(f"{URL}/v1/memories/{mid}/metadata", json=body, headers=H, timeout=10)


def test_v020_gate_keys_plain_actor_403_on_evidence():
    """v0.20 M1/M3/M11: PATCH /metadata of each retrieval-gating key with a
    plain API key (arbitrary actor) on an evidence record -> 403, and the key
    is NOT written (the record stays admissible)."""
    for key in _V020_GATE_KEYS:
        mid = _post_evidence(f"v020-gatekey-evidence-{key}-{uuid.uuid4()}")
        try:
            r = _patch_md(mid, {key: "m-evil"}, "evil-client",
                          "v020 test: untrusted actor must not flip retrieval gates")
            assert r.status_code == 403, (
                f"plain-actor PATCH of {key} on evidence must be 403; "
                f"got {r.status_code}: {r.text}"
            )
            g = httpx.get(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
            g.raise_for_status()
            assert not (g.json().get("metadata") or {}).get(key), (
                f"{key} must not have been written after the 403"
            )
        finally:
            _force_delete(mid)


def _qdrant_set_tier_stable(memory_id: str) -> None:
    """Flip a record to tier=stable through the Qdrant client directly —
    POST /v1/memories only allows evidence|temporal (ADD_ALLOWED_TIERS), so
    stable seeding mirrors the MED-19 FORBIDDEN_KEYS-bypass precedent
    (test_brand_isolation._qdrant_set_payload)."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct
    from config import build_config
    vs_cfg = build_config()["vector_store"]["config"]
    client = QdrantClient(host=vs_cfg["host"], port=vs_cfg["port"])
    collection = vs_cfg["collection_name"]
    recs = client.retrieve(collection, ids=[memory_id], with_payload=True, with_vectors=True)
    assert recs, f"point {memory_id} not found"
    rec = recs[0]
    payload = dict(rec.payload or {})
    payload["tier"] = "stable"
    client.upsert(collection, points=[PointStruct(id=rec.id, vector=rec.vector, payload=payload)])


def test_v020_gate_keys_no_actor_403_on_stable():
    """v0.20 M1/M3/M11: same negative test on a stable-tier record with NO
    actor field at all (the laziest client shape)."""
    for key in _V020_GATE_KEYS:
        mid = _post_evidence(f"v020-gatekey-stable-{key}-{uuid.uuid4()}")
        _qdrant_set_tier_stable(mid)
        try:
            r = _patch_md(mid, {key: "m-evil"}, None, "v020 test: actorless gate-key write")
            assert r.status_code == 403, (
                f"actorless PATCH of {key} on stable must be 403; "
                f"got {r.status_code}: {r.text}"
            )
        finally:
            _force_delete(mid)


@pytest.mark.skipif(CANONICAL_KEY is None, reason="canonical-key not present")
def test_v020_gate_keys_plain_actor_403_on_canonical():
    """v0.20 M1/M3/M11: plain-key PATCH of each gating key on a CANONICAL
    record -> 403 (the HMAC tier gate fires first for untrusted actors; the
    FORBIDDEN_KEYS check backstops it)."""
    for key in _V020_GATE_KEYS:
        mid = _post_evidence(f"v020-gatekey-canonical-{key}-{uuid.uuid4()}")
        _promote_to_canonical(mid)
        try:
            r = _patch_md(mid, {key: "m-evil"}, "evil-client",
                          "v020 test: untrusted gate-key write on canonical")
            assert r.status_code == 403, (
                f"plain-actor PATCH of {key} on canonical must be 403; "
                f"got {r.status_code}: {r.text}"
            )
        finally:
            _force_delete(mid)


def test_v020_contradiction_sweep_actor_still_writes_its_two_keys():
    """v0.20 M1/M3/M11: contradiction-sweep-v019 (the one legitimate writer)
    still stamps its YES verdict ({contradicts_canonical,
    contradiction_checked_at}) and its NO verdict (contradiction_checked_at
    alone) through the new FORBIDDEN_KEYS gate."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    mid = _post_evidence(f"v020-sweep-yes-{uuid.uuid4()}")
    try:
        r = _patch_md(
            mid,
            {"contradicts_canonical": "m-canonical-x", "contradiction_checked_at": now},
            "contradiction-sweep-v019", "v020 test: sweep YES verdict",
        )
        assert r.status_code == 200, (
            f"sweep YES-verdict stamp must still be 200; got {r.status_code}: {r.text}"
        )
    finally:
        _force_delete(mid)
    mid2 = _post_evidence(f"v020-sweep-no-{uuid.uuid4()}")
    try:
        r2 = _patch_md(mid2, {"contradiction_checked_at": now},
                       "contradiction-sweep-v019", "v020 test: sweep NO verdict")
        assert r2.status_code == 200, (
            f"sweep NO-verdict stamp must still be 200; got {r2.status_code}: {r2.text}"
        )
    finally:
        _force_delete(mid2)


def test_v020_sweep_actor_cannot_write_superseded_by():
    """v0.20 M1/M3/M11: superseded_by has NO trusted API writer — even
    contradiction-sweep-v019 (a trusted actor for OTHER keys) gets 403."""
    mid = _post_evidence(f"v020-sweep-superseded-{uuid.uuid4()}")
    try:
        r = _patch_md(mid, {"superseded_by": "m-x"},
                      "contradiction-sweep-v019", "v020 test: sweep must not supersede")
        assert r.status_code == 403, (
            f"sweep actor writing superseded_by must be 403; got {r.status_code}: {r.text}"
        )
    finally:
        _force_delete(mid)


# ---------------------------------------------------------------------------
# v0.20 Final (adversarial-review MED) — mixed-key bypass via legacy actors
# ---------------------------------------------------------------------------
# The old FORBIDDEN_KEYS gate computed a single global `allowed` flag: if ANY
# legacy per-key/actor rule matched (e.g. tier_actor + actor='system'), the
# WHOLE forbidden_hit cleared the gate, so a legacy actor string could smuggle
# the retrieval-gating keys (superseded_by / contradicts_canonical) in
# alongside its authorized key. The gate is now a per-actor subset check; these
# tests pin 403 + not-written for each legacy actor's mixed-key payload.

def _assert_mixed_key_403_and_not_written(actor: str, metadata: dict, smuggled: str):
    mid = _post_evidence(f"v020-mixedkey-{actor}-{uuid.uuid4()}")
    try:
        r = _patch_md(mid, metadata, actor,
                      "v020 test: legacy actor must not smuggle retrieval-gating keys")
        assert r.status_code == 403, (
            f"actor={actor!r} mixed-key PATCH {sorted(metadata)} must be 403; "
            f"got {r.status_code}: {r.text}"
        )
        g = httpx.get(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
        g.raise_for_status()
        assert not (g.json().get("metadata") or {}).get(smuggled), (
            f"{smuggled} must not have been written after the 403"
        )
    finally:
        _force_delete(mid)


def test_v020_system_actor_cannot_smuggle_superseded_by_with_tier_actor():
    """actor='system' is legacy-authorized for tier_actor (and expires_at) ONLY;
    sending {tier_actor, superseded_by} must 403 and write nothing."""
    _assert_mixed_key_403_and_not_written(
        "system",
        {"tier_actor": "x", "superseded_by": "m-evil"},
        "superseded_by",
    )


def test_v020_decay_scan_actor_cannot_smuggle_superseded_by_with_expires_at():
    """actor='decay-scan' is legacy-authorized for expires_at ONLY; sending
    {expires_at, superseded_by} must 403 and write nothing."""
    _assert_mixed_key_403_and_not_written(
        "decay-scan",
        {"expires_at": dt.datetime.now(dt.timezone.utc).isoformat(),
         "superseded_by": "m-evil"},
        "superseded_by",
    )


def test_v020_backfill_actor_cannot_smuggle_superseded_by_with_retrievable():
    """actor='backfill-apply-v013' is legacy-authorized for retrievable ONLY;
    sending {retrievable, superseded_by} must 403 and write nothing."""
    _assert_mixed_key_403_and_not_written(
        "backfill-apply-v013",
        {"retrievable": False, "superseded_by": "m-evil"},
        "superseded_by",
    )


# ---------------------------------------------------------------------------
# v0.20 Phase D (M6): /health/deep exposes canonical_key presence + source
# ---------------------------------------------------------------------------

def test_v020_health_deep_exposes_canonical_key():
    """M6: keyless-degraded servers were invisible to /health/deep (it checked
    qdrant/embedder/hook_contract only). The endpoint now reports
    checks.canonical_key = {ok, present, source, dpapi_blob}; on this stack the
    key MUST be present (a keyless gate box is itself a failure worth failing)."""
    r = httpx.get(f"{URL}/health/deep", timeout=20)
    assert r.status_code == 200, f"/health/deep failed: {r.status_code} {r.text}"
    ck = r.json().get("checks", {}).get("canonical_key")
    assert isinstance(ck, dict), f"checks.canonical_key missing from /health/deep: {r.json()}"
    assert set(ck) == {"ok", "present", "source", "dpapi_blob"}, f"unexpected shape: {ck}"
    assert ck["source"] in ("runtime", "dpapi", "plaintext", "none")
    # ok semantics: only the provisioned-but-unreadable state (blob, no key) fails
    assert ck["ok"] == (ck["present"] or not ck["dpapi_blob"])
    assert ck["present"] is True, (
        "live server is KEYLESS — canonical/insight HMAC mutations are 503ing; "
        "restart mem0 (re-runs dpapi-fetch-key ExecStartPre) or follow "
        "docs/systems/dpapi-canonical-key.md Recovery"
    )
    assert ck["source"] != "none"
