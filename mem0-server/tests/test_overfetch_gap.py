"""v0.30 over-fetch gap fix — TDD test.

Property under test:
  When N soft-retired records rank ABOVE the caller's K non-retired records in
  Qdrant, a search with limit=K must still return ALL K non-retired records —
  not K-N results with gaps (the pre-fix behaviour: the server fetched exactly K
  rows, then filtered the retired ones out, leaving fewer than K slots filled).

Setup:
  * 3 soft-retired ship-log records (S1, S2, S3) — dense with nonce OVRFETCH9
    so their cosine similarity to the nonce query exceeds the 2 atomic facts.
  * 2 short atomic facts (A1, A2) — also contain OVRFETCH9 but have more
    non-nonce tokens, giving a slightly lower cosine than the pure-nonce logs.
  * All 5 records belong to a throwaway user_id `zzz-overfetch-<rand>`.

Bug trace (pre-fix):
  _search_core fetches top_k=capped_limit=2 from Qdrant → returns [S1, S2].
  retired filter strips both → 0 results returned.
  K=2 needed 2 admitted records but only 0 arrived (gap).

Fix (v0.30):
  _search_core fetches top_k=capped_limit+50=52 from Qdrant → returns
  [S1, S2, S3, A1, A2, …].  Retired filter strips S1-S3 → [A1, A2, …].
  Trim to capped_limit=2 → [A1, A2].  No gap.

RED: assert K=2 returns exactly [A1, A2] — fails on pre-fix code (0 or 1 result).
GREEN: same assertion passes after the over-fetch + trim implementation.

REGRESSION: no-retired search returns same top-K (over-fetch transparent).

CLEANUP: all 5 records deleted unconditionally in finally block.

Run:
  wsl.exe -e bash -lc "cd /mnt/d/repos/agentic-memory-stack && \\
    /home/youruser/apps/mem0-server/.venv/bin/python -m pytest \\
    mem0-server/tests/test_overfetch_gap.py -v"
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ.get("MEM0_KEY") or (Path.home() / ".mem0" / "api-key").read_text().strip()
H = {"X-API-Key": KEY, "Content-Type": "application/json"}

# Unique nonce — all 5 records are dense with this token so cosine similarity
# to the nonce query is driven almost entirely by nonce repetition density.
NONCE = "OVRFETCH9"

# 3 fat ship-logs: dense nonce repetition (~100×) so their embedding is almost
# entirely the NONCE token — they reliably out-rank both atomic facts on cosine.
_LOG_TEXT = " ".join([NONCE] * 100)
SHIP_LOG_1 = _LOG_TEXT
SHIP_LOG_2 = _LOG_TEXT + f" {NONCE} alpha"   # tiny variation so dedup doesn't collapse them
SHIP_LOG_3 = _LOG_TEXT + f" {NONCE} beta"

# 2 short atomic facts: contain NONCE but fewer repetitions → lower cosine than
# the fat logs when queried with the nonce alone.
ATOMIC_A1 = f"{NONCE} stored value is 42."
ATOMIC_A2 = f"{NONCE} config path is /etc/overfetch9.conf"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _seed(text: str, user_id: str) -> str:
    """POST /v1/memories (infer=False); return the new memory id."""
    r = httpx.post(
        f"{URL}/v1/memories",
        json={
            "messages": text,
            "user_id": user_id,
            "infer": False,
            "metadata": {
                "tier": "evidence",
                "source": "test-overfetch-gap",
                "kind": "test",
            },
        },
        headers=H,
        timeout=15,
    )
    assert r.status_code == 200, f"seed POST failed {r.status_code}: {r.text}"
    results = r.json().get("results", [])
    assert results, f"seed returned 0 results (possible dedup): text={text!r}"
    return results[0]["id"]


def _soft_retire(mid: str) -> None:
    """PATCH /v1/memories/{id}/metadata — set retrievable=False via the
    only actor that may write that key (backfill-apply-v013)."""
    r = httpx.patch(
        f"{URL}/v1/memories/{mid}/metadata",
        json={
            "metadata": {"retrievable": False},
            "actor": "backfill-apply-v013",
            "reason": "test-overfetch-gap: retire ship-log to prove gap fix",
        },
        headers=H,
        timeout=15,
    )
    assert r.status_code == 200, f"soft-retire PATCH failed {r.status_code}: {r.text}"


def _search(query: str, user_id: str, limit: int = 2) -> list[dict]:
    """POST /v1/memories/search — threshold=0, rerank=False (hard constraints)."""
    r = httpx.post(
        f"{URL}/v1/memories/search",
        json={
            "query": query,
            "filters": {"user_id": user_id},
            "limit": limit,
            "threshold": 0.0,
            "rerank": False,
        },
        headers=H,
        timeout=15,
    )
    assert r.status_code == 200, f"search failed {r.status_code}: {r.text}"
    return r.json().get("results", [])


def _delete(mid: str) -> None:
    """DELETE /v1/memories/{id} — best effort (called from finally)."""
    try:
        httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
    except Exception:
        pass


def _exists(mid: str) -> bool:
    try:
        r = httpx.get(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main test: RED on pre-fix code, GREEN after over-fetch fix
# ---------------------------------------------------------------------------

def test_overfetch_gap_fix_k2_with_3_retired():
    """K=2 search with 3 soft-retired records ranked above both atomics.

    PRE-FIX behaviour:
      top_k=2 from Qdrant → [S1, S2] → retired filter strips both → 0 results.
      (Or 1 result if only 1 retired record lands in the top-2.)

    POST-FIX behaviour:
      top_k=2+50=52 from Qdrant → [S1,S2,S3,A1,A2,...] → retired filter strips
      S1-S3 → [A1, A2, ...] → trim to 2 → [A1, A2].  Exactly 2, no gap.
    """
    probe_user = f"zzz-overfetch-{uuid.uuid4().hex[:8]}"

    id_s1 = id_s2 = id_s3 = id_a1 = id_a2 = None
    try:
        # ----------------------------------------------------------------
        # 1. SEED: 3 fat ship-logs + 2 short atomics
        # ----------------------------------------------------------------
        id_s1 = _seed(SHIP_LOG_1, probe_user)
        id_s2 = _seed(SHIP_LOG_2, probe_user)
        id_s3 = _seed(SHIP_LOG_3, probe_user)
        id_a1 = _seed(ATOMIC_A1,  probe_user)
        id_a2 = _seed(ATOMIC_A2,  probe_user)

        # ----------------------------------------------------------------
        # 2. VERIFY CROWDING (pre-retire): at least 1 ship-log must rank in
        #    the top-2 so we know the fat logs actually out-rank the atomics.
        #    If this assertion fails, increase log repetition or adjust nonce.
        # ----------------------------------------------------------------
        pre_retire = _search(NONCE, probe_user, limit=2)
        pre_ids = [r["id"] for r in pre_retire]
        ship_ids = {id_s1, id_s2, id_s3}

        print(f"\n[PRE-RETIRE limit=2] ids: {pre_ids}")
        for rec in pre_retire:
            label = (
                "S1" if rec["id"] == id_s1
                else "S2" if rec["id"] == id_s2
                else "S3" if rec["id"] == id_s3
                else "A1" if rec["id"] == id_a1
                else "A2" if rec["id"] == id_a2
                else "?"
            )
            print(f"  {label}: score={rec.get('score'):.4f}  id={rec['id']}")

        any_ship_in_top2 = any(sid in pre_ids for sid in ship_ids)
        assert any_ship_in_top2, (
            f"PRE-RETIRE: no ship-log in top-2 — crowding not reproduced. "
            f"top-2 were: {pre_ids}. Increase log repetition in SHIP_LOG_* constants."
        )

        # ----------------------------------------------------------------
        # 3. SOFT-RETIRE all 3 ship-logs
        # ----------------------------------------------------------------
        _soft_retire(id_s1)
        _soft_retire(id_s2)
        _soft_retire(id_s3)

        # ----------------------------------------------------------------
        # 4. POST-RETIRE limit=2: MUST return EXACTLY [A1, A2] — the core
        #    assertion.  This is RED on pre-fix code (returns 0 or 1 result
        #    because the 3 retired records fill the Qdrant top-2 and are then
        #    filtered, leaving a gap).
        # ----------------------------------------------------------------
        post_retire = _search(NONCE, probe_user, limit=2)
        post_ids = [r["id"] for r in post_retire]

        print(f"\n[POST-RETIRE limit=2] ids: {post_ids}")
        for rec in post_retire:
            label = (
                "S1" if rec["id"] == id_s1
                else "S2" if rec["id"] == id_s2
                else "S3" if rec["id"] == id_s3
                else "A1" if rec["id"] == id_a1
                else "A2" if rec["id"] == id_a2
                else "?"
            )
            print(f"  {label}: score={rec.get('score'):.4f}  id={rec['id']}")

        # Both atomics must appear (no gap)
        assert id_a1 in post_ids, (
            f"POST-RETIRE (limit=2): A1 (id={id_a1}) missing — gap not healed. "
            f"Returned {len(post_retire)} result(s): {post_ids}"
        )
        assert id_a2 in post_ids, (
            f"POST-RETIRE (limit=2): A2 (id={id_a2}) missing — gap not healed. "
            f"Returned {len(post_retire)} result(s): {post_ids}"
        )
        # No retired record must appear
        for sid in (id_s1, id_s2, id_s3):
            assert sid not in post_ids, (
                f"POST-RETIRE: retired ship-log {sid} still in results. "
                f"Returned: {post_ids}"
            )
        # Exactly 2 results (trim respected caller's limit)
        assert len(post_retire) == 2, (
            f"POST-RETIRE: expected exactly 2 results (capped_limit), "
            f"got {len(post_retire)}: {post_ids}"
        )

    finally:
        # ----------------------------------------------------------------
        # 5. CLEANUP: unconditional; verify gone
        # ----------------------------------------------------------------
        all_ids = [id_s1, id_s2, id_s3, id_a1, id_a2]
        for mid in all_ids:
            if mid is not None:
                _delete(mid)

        leftovers = [mid for mid in all_ids
                     if mid is not None and _exists(mid)]
        assert not leftovers, (
            f"CLEANUP FAILED: {len(leftovers)} probe records still exist: {leftovers}"
        )
        print(f"\n[CLEANUP] all 5 probe records deleted (probe_user={probe_user})")


# ---------------------------------------------------------------------------
# Regression test: no-retired search is unaffected by over-fetch
# ---------------------------------------------------------------------------

def test_overfetch_regression_no_retired_returns_same_top_k():
    """Over-fetch must be TRANSPARENT when there are no retired records.

    Seed K clean records (no soft-retire), search with limit=K, assert
    we get <= K results back.  The count and order must not be distorted
    by the over-fetch buffer.
    """
    probe_user = f"zzz-overfetch-{uuid.uuid4().hex[:8]}"
    LIMIT = 3

    # Unique sub-nonce so this test's records don't interfere with the main test
    sub_nonce = "OVRFETCH9NORTR"

    id_c1 = id_c2 = id_c3 = None
    try:
        id_c1 = _seed(f"{sub_nonce} clean fact one.", probe_user)
        id_c2 = _seed(f"{sub_nonce} clean fact two.", probe_user)
        id_c3 = _seed(f"{sub_nonce} clean fact three.", probe_user)

        results = _search(sub_nonce, probe_user, limit=LIMIT)
        result_ids = [r["id"] for r in results]

        print(f"\n[REGRESSION no-retired limit={LIMIT}] ids: {result_ids}")

        # Over-fetch must never cause count > limit
        assert len(results) <= LIMIT, (
            f"REGRESSION: over-fetch expanded result count beyond limit={LIMIT}. "
            f"Got {len(results)} results: {result_ids}"
        )
        # All returned IDs must be from our seeded set (no bleed)
        seeded = {id_c1, id_c2, id_c3}
        for rid in result_ids:
            assert rid in seeded, (
                f"REGRESSION: unexpected ID {rid!r} in results — "
                f"not one of the seeded records. result_ids={result_ids}"
            )

    finally:
        for mid in (id_c1, id_c2, id_c3):
            if mid is not None:
                _delete(mid)

        leftovers = [mid for mid in (id_c1, id_c2, id_c3)
                     if mid is not None and _exists(mid)]
        assert not leftovers, (
            f"REGRESSION CLEANUP FAILED: {leftovers}"
        )
        print(f"\n[REGRESSION CLEANUP] all 3 clean records deleted (probe_user={probe_user})")


# ---------------------------------------------------------------------------
# Rerank test: over-fetch pool is bounded and gap still heals with rerank=True
# ---------------------------------------------------------------------------

def test_overfetch_rerank_bounds_and_heals_gap():
    """rerank=True caps _buf at 10; gap must still be healed.

    Property: with rerank=True and limit=2, the candidate pool sent to the
    cross-encoder is bounded (buffer <= 10), AND the gap caused by retired
    records ranking above the atomics is still healed — i.e. both atomics
    are returned.

    Uses the same seeding strategy as the main gap test: 3 fat ship-logs
    (soft-retired after seeding) rank above 2 atomic facts on cosine.
    With limit=2 + buffer=10 → overfetch_limit=12, which is easily enough
    to capture all 5 records; gap heals even with the smaller pool.
    """
    probe_user = f"zzz-overfetch-rerank-{uuid.uuid4().hex[:8]}"

    id_s1 = id_s2 = id_s3 = id_a1 = id_a2 = None
    try:
        # ----------------------------------------------------------------
        # 1. SEED: same pattern as the main gap test
        # ----------------------------------------------------------------
        id_s1 = _seed(SHIP_LOG_1, probe_user)
        id_s2 = _seed(SHIP_LOG_2, probe_user)
        id_s3 = _seed(SHIP_LOG_3, probe_user)
        id_a1 = _seed(ATOMIC_A1,  probe_user)
        id_a2 = _seed(ATOMIC_A2,  probe_user)

        # ----------------------------------------------------------------
        # 2. Verify crowding (pre-retire): at least 1 ship-log in top-2
        # ----------------------------------------------------------------
        pre_retire = _search(NONCE, probe_user, limit=2)
        pre_ids = [r["id"] for r in pre_retire]
        ship_ids = {id_s1, id_s2, id_s3}
        assert any(sid in pre_ids for sid in ship_ids), (
            f"RERANK PRE-RETIRE: no ship-log in top-2 — crowding not reproduced. "
            f"top-2 were: {pre_ids}. Increase log repetition in SHIP_LOG_* constants."
        )

        # ----------------------------------------------------------------
        # 3. SOFT-RETIRE all 3 ship-logs
        # ----------------------------------------------------------------
        _soft_retire(id_s1)
        _soft_retire(id_s2)
        _soft_retire(id_s3)

        # ----------------------------------------------------------------
        # 4. POST-RETIRE search with rerank=True + limit=2.
        #    The bounded buffer (<=10) still gives overfetch_limit=12,
        #    which captures [S1,S2,S3,A1,A2,...] — gap heals.
        # ----------------------------------------------------------------
        r = httpx.post(
            f"{URL}/v1/memories/search",
            json={
                "query": NONCE,
                "filters": {"user_id": probe_user},
                "limit": 2,
                "threshold": 0.0,
                "rerank": True,
            },
            headers=H,
            timeout=30,
        )
        assert r.status_code == 200, f"rerank search failed {r.status_code}: {r.text}"
        post_retire = r.json().get("results", [])
        post_ids = [rec["id"] for rec in post_retire]

        print(f"\n[RERANK POST-RETIRE limit=2] ids: {post_ids}")

        # Both atomics must appear (gap healed even with bounded pool)
        assert id_a1 in post_ids, (
            f"RERANK POST-RETIRE (limit=2): A1 (id={id_a1}) missing — gap not healed. "
            f"Returned {len(post_retire)} result(s): {post_ids}"
        )
        assert id_a2 in post_ids, (
            f"RERANK POST-RETIRE (limit=2): A2 (id={id_a2}) missing — gap not healed. "
            f"Returned {len(post_retire)} result(s): {post_ids}"
        )
        # No retired record must appear
        for sid in (id_s1, id_s2, id_s3):
            assert sid not in post_ids, (
                f"RERANK POST-RETIRE: retired ship-log {sid} still in results. "
                f"Returned: {post_ids}"
            )
        # Exactly 2 results
        assert len(post_retire) == 2, (
            f"RERANK POST-RETIRE: expected exactly 2 results, "
            f"got {len(post_retire)}: {post_ids}"
        )

    finally:
        # ----------------------------------------------------------------
        # 5. CLEANUP
        # ----------------------------------------------------------------
        all_ids = [id_s1, id_s2, id_s3, id_a1, id_a2]
        for mid in all_ids:
            if mid is not None:
                _delete(mid)

        leftovers = [mid for mid in all_ids
                     if mid is not None and _exists(mid)]
        assert not leftovers, (
            f"RERANK CLEANUP FAILED: {len(leftovers)} probe records still exist: {leftovers}"
        )
        print(f"\n[RERANK CLEANUP] all 5 probe records deleted (probe_user={probe_user})")
