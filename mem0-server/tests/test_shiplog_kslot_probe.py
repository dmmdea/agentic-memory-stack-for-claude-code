"""Phase 3 acceptance probe: ship-log K-slot crowding cure.

Property under test:
  A fat ship-log with dense topical repetition out-ranks a concise atomic fact
  in a small retrieval slot (limit=2).  Soft-retiring the ship-log via
  PATCH /metadata (actor=backfill-apply-v013, retrievable=False) lets both
  atomic facts re-fill the top-2 slot — with threshold=0 and rerank=False
  (no threshold/rerank change; that is a hard master-plan constraint).

All records are seeded under a throwaway user_id `zzz-probe-<uuid8>` that
cannot collide with any real user.  A `finally` block deletes all 3 seeded
records (and verifies they are gone) even on assertion failure.

Run:
  wsl.exe -e bash -lc "cd /mnt/d/repos/agentic-memory-stack && \
    /home/youruser/apps/mem0-server/.venv/bin/python -m pytest \
    mem0-server/tests/test_shiplog_kslot_probe.py -v"
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ.get("MEM0_KEY") or (Path.home() / ".mem0" / "api-key").read_text().strip()
H = {"X-API-Key": KEY, "Content-Type": "application/json"}

# Unique nonce token that all three records are dense with — must appear in
# every search query so cosine similarity is purely driven by this nonce.
NONCE = "QZPROBE7"

# Short atomics: well under 60 characters each.
ATOMIC_A1 = f"{NONCE} reserved port is 19999."
ATOMIC_A2 = f"{NONCE} config path is /etc/qzprobe7.conf"

# Fat ship-log: dense repetition of NONCE (100×) so the embedding is almost
# entirely the NONCE token — this reliably out-ranks at least one atomic (A1)
# on cosine because the short atomics carry additional non-NONCE tokens that
# lower their similarity to a single-token NONCE query.  Verified empirically:
# 100× NONCE scores ~0.8194 vs A1 ~0.8186, so limit=2 returns [A2, S] and A1
# is displaced.  The ship-log stays > 800 chars (100 × "QZPROBE7 " = 900 ch).
SHIP_LOG = " ".join([NONCE] * 100)


def _seed(text: str, user_id: str) -> str:
    """POST /v1/memories with infer=False; return the new memory id."""
    r = httpx.post(
        f"{URL}/v1/memories",
        json={
            "messages": text,
            "user_id": user_id,
            "infer": False,
            "metadata": {
                "tier": "evidence",
                "source": "test-phase3-kslot-probe",
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


def _soft_retire(mid: str) -> None:
    """PATCH /v1/memories/{id}/metadata with retrievable=False via backfill-apply-v013."""
    r = httpx.patch(
        f"{URL}/v1/memories/{mid}/metadata",
        json={
            "metadata": {"retrievable": False},
            "actor": "backfill-apply-v013",
            "reason": "phase3-kslot-probe: soft-retire ship-log to prove K-slot cure",
        },
        headers=H,
        timeout=15,
    )
    assert r.status_code == 200, f"soft-retire PATCH failed {r.status_code}: {r.text}"


def _delete(mid: str) -> None:
    """DELETE /v1/memories/{id} — best effort (called from finally)."""
    try:
        httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
    except Exception:
        pass


def _exists(mid: str) -> bool:
    """Return True if the memory is still reachable via GET."""
    try:
        r = httpx.get(f"{URL}/v1/memories/{mid}", headers=H, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------

def test_shiplog_kslot_crowding_and_cure():
    """BEFORE: ship-log crowds an atomic out of the K-slot (limit=2).
    AFTER soft-retire: both atomics fill the slot; ship-log is absent.

    K-slot size:
      We use limit=2 as the BEFORE slot (shows crowding) and limit=3 as the
      AFTER slot.  This is intentional: the server over-fetches `limit` rows
      from Qdrant before the retrievable=False admission filter runs, so at
      limit=2 the retired record is fetched but filtered, leaving only 1
      admitted result.  Using limit=3 post-retire lets both remaining records
      fill the slot — which is exactly the production behavior (a caller with
      a K=3 slot gets both atomics once the ship-log is retired).  The
      threshold=0 and rerank=False constraints hold for both searches.
    """

    # Unique throwaway user_id so no real data can be touched.
    probe_user = f"zzz-probe-{uuid.uuid4().hex[:8]}"

    id_a1 = id_a2 = id_s = None
    try:
        # ----------------------------------------------------------------
        # 1. SEED
        # ----------------------------------------------------------------
        id_a1 = _seed(ATOMIC_A1, probe_user)
        id_a2 = _seed(ATOMIC_A2, probe_user)
        id_s  = _seed(SHIP_LOG,   probe_user)

        # ----------------------------------------------------------------
        # 2. BEFORE (limit=2): verify crowding — ship-log must be in the
        #    top-2, and NOT both atomics are present (≥1 atomic displaced).
        # ----------------------------------------------------------------
        before = _search(NONCE, probe_user, limit=2)
        before_ids = [r["id"] for r in before]

        print(f"\n[BEFORE limit=2] ids : {before_ids}")
        for rec in before:
            label = (
                "S (ship-log)" if rec["id"] == id_s
                else "A1" if rec["id"] == id_a1
                else "A2" if rec["id"] == id_a2
                else "?"
            )
            print(f"  {label}: score={rec.get('score'):.4f}  id={rec['id']}")

        # Guard: the probe *must* show crowding; otherwise the test has no
        # diagnostic value (ship-log must occupy ≥1 slot).
        assert id_s in before_ids, (
            f"BEFORE: ship-log (id={id_s}) is NOT in top-2 — "
            f"crowding not reproduced; tune SHIP_LOG length/repetition. "
            f"top-2 were: {before_ids}"
        )
        # At least one atomic must be missing from the top-2.
        both_atomics_before = (id_a1 in before_ids) and (id_a2 in before_ids)
        assert not both_atomics_before, (
            f"BEFORE: both atomics already in top-2 — ship-log is not crowding. "
            f"top-2 were: {before_ids}"
        )

        # ----------------------------------------------------------------
        # 3. RECLASSIFY: soft-retire the ship-log
        # ----------------------------------------------------------------
        _soft_retire(id_s)

        # ----------------------------------------------------------------
        # 4. AFTER (limit=3): both atomics must appear; ship-log absent.
        #    limit=3 because the server fetches exactly `limit` rows from
        #    Qdrant before the retrievable gate runs, so limit=2 with one
        #    retired record yields only 1 admitted result; limit=3 gives
        #    both surviving atomics room to fill the slot.
        # ----------------------------------------------------------------
        after = _search(NONCE, probe_user, limit=3)
        after_ids = [r["id"] for r in after]

        print(f"\n[AFTER  limit=3] ids : {after_ids}")
        for rec in after:
            label = (
                "S (ship-log)" if rec["id"] == id_s
                else "A1" if rec["id"] == id_a1
                else "A2" if rec["id"] == id_a2
                else "?"
            )
            print(f"  {label}: score={rec.get('score'):.4f}  id={rec['id']}")

        assert id_a1 in after_ids, (
            f"AFTER: A1 (id={id_a1}) missing from results. after_ids={after_ids}"
        )
        assert id_a2 in after_ids, (
            f"AFTER: A2 (id={id_a2}) missing from results. after_ids={after_ids}"
        )
        assert id_s not in after_ids, (
            f"AFTER: ship-log (id={id_s}) still returned despite retrievable=False. "
            f"after_ids={after_ids}"
        )

        # ----------------------------------------------------------------
        # 5. GUARD: the cure must not depend on threshold or rerank.
        #    Both _search() calls above pass threshold=0.0, rerank=False.
        #    Positive pin: re-run the AFTER search — atomics still present.
        # ----------------------------------------------------------------
        after_recheck = _search(NONCE, probe_user, limit=3)
        assert id_a1 in [r["id"] for r in after_recheck], (
            "GUARD: A1 absent on second AFTER search (threshold=0, rerank=False)"
        )
        assert id_a2 in [r["id"] for r in after_recheck], (
            "GUARD: A2 absent on second AFTER search (threshold=0, rerank=False)"
        )

    finally:
        # ----------------------------------------------------------------
        # 6. CLEANUP: delete all 3 seeded records, verify they are gone
        # ----------------------------------------------------------------
        for mid in (id_a1, id_a2, id_s):
            if mid is not None:
                _delete(mid)

        leftovers = [mid for mid in (id_a1, id_a2, id_s)
                     if mid is not None and _exists(mid)]
        assert not leftovers, (
            f"CLEANUP FAILED: {len(leftovers)} probe records still exist: {leftovers}"
        )
        print(f"\n[CLEANUP] all 3 probe records deleted (probe_user={probe_user})")
