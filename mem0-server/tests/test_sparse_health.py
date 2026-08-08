"""Headless pins for sparse_health (AMS-09 — audit 2026-08-07).

The pure evaluator and the deterministic canary-token rule are pinned here;
the I/O wrapper is exercised live by Test-MemoryStack and the deploy gate.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparse_health import (  # noqa: E402
    evaluate_sparse_leg,
    encode_with_selfheal,
    pick_canary_token,
)


class _FakeStore:
    """Mirrors mem0's Qdrant store lazy-init contract: ``_bm25_encoder`` is
    None (never tried) / False (tried, failed — POISONED) / encoder object.
    ``_encode_bm25`` returns None when the encoder is unavailable."""

    def __init__(self, poisoned=True, heal_works=True):
        self._bm25_encoder = False if poisoned else None
        self._heal_works = heal_works
        self.init_attempts = 0

    def _encode_bm25(self, text):
        if self._bm25_encoder is False:
            return None
        if self._bm25_encoder is None:
            self.init_attempts += 1
            if not self._heal_works:
                self._bm25_encoder = False
                return None
            self._bm25_encoder = object()
        return {"indices": [1], "values": [0.5]}


def test_selfheal_unpoisons_once_and_returns_vector(monkeypatch):
    # AMS-09b: the exact second-death state — sentinel poisoned by a
    # boot-window init failure, cache since returned. One reset must heal.
    # (Headless runners lack fastembed; stub the module-local seam.)
    import sparse_health
    monkeypatch.setattr(sparse_health, "_fastembed_available", lambda: True)
    store = _FakeStore(poisoned=True, heal_works=True)
    assert encode_with_selfheal(store, "qube") is not None
    assert store.init_attempts == 1


def test_selfheal_is_bounded_per_call(monkeypatch):
    # Init keeps failing (cache genuinely absent): exactly ONE retry per
    # call, never a loop; a later call may try once again.
    import sparse_health
    monkeypatch.setattr(sparse_health, "_fastembed_available", lambda: True)
    store = _FakeStore(poisoned=True, heal_works=False)
    assert encode_with_selfheal(store, "qube") is None
    assert store.init_attempts == 1
    assert encode_with_selfheal(store, "qube") is None
    assert store.init_attempts == 2


def test_selfheal_skips_reset_when_dep_missing(monkeypatch):
    # A box genuinely missing fastembed must not pay a doomed re-init on
    # every health call — the guard keeps the sentinel poisoned.
    import sparse_health
    monkeypatch.setattr(sparse_health, "_fastembed_available", lambda: False)
    store = _FakeStore(poisoned=True, heal_works=True)
    assert encode_with_selfheal(store, "qube") is None
    assert store.init_attempts == 0


def test_selfheal_no_reset_on_healthy_encoder():
    store = _FakeStore(poisoned=False, heal_works=True)
    store._bm25_encoder = object()  # already loaded
    assert encode_with_selfheal(store, "qube") is not None
    assert store.init_attempts == 0


def test_selfheal_never_raises():
    class _Exploding:
        _bm25_encoder = False

        def _encode_bm25(self, text):
            raise RuntimeError("boom")

    assert encode_with_selfheal(_Exploding(), "qube") is None


CANARY_OK = {"ran": True, "hit": True, "token": "qdrant"}
CANARY_MISS = {"ran": True, "hit": False, "token": "qdrant"}
CANARY_NOT_RUN = {"ran": False, "hit": False, "token": None}


def test_all_green():
    out = evaluate_sparse_leg(True, True, 100, 100, CANARY_OK)
    assert out["ok"] is True and out["coverage"] == 1.0


def test_fastembed_missing_gates():
    # The exact AMS-09 production state: venv rebuilt without fastembed.
    out = evaluate_sparse_leg(False, True, 100, 25, CANARY_NOT_RUN)
    assert out["ok"] is False and out["fastembed"] is False


def test_slot_missing_gates():
    out = evaluate_sparse_leg(True, False, 100, 0, CANARY_NOT_RUN)
    assert out["ok"] is False


def test_canary_miss_gates():
    # Encoder loads but retrieval does not return the probed point — the
    # tokenizer/hash-drift case a coverage number can never catch.
    out = evaluate_sparse_leg(True, True, 100, 100, CANARY_MISS)
    assert out["ok"] is False


def test_no_canary_point_on_populated_corpus_gates():
    # POPULATED corpus with zero bm25 vectors: nothing to probe IS a dead leg.
    out = evaluate_sparse_leg(True, True, 100, 0, CANARY_NOT_RUN,
                              error="no bm25-bearing point to canary")
    assert out["ok"] is False and "error" in out


def test_empty_collection_is_green_with_note():
    # Diff-review finding 2: a brand-new box (0 points) has nothing that can
    # be dead — gating there would block every deploy on a fresh install.
    out = evaluate_sparse_leg(True, True, 0, 0, CANARY_NOT_RUN)
    assert out["ok"] is True and "note" in out


def test_empty_collection_with_missing_dep_still_gates():
    # The carve-out is for the missing CORPUS, never for a missing DEP: an
    # empty box without fastembed installed must still fail (the installer
    # post-condition contract).
    out = evaluate_sparse_leg(False, True, 0, 0, CANARY_NOT_RUN)
    assert out["ok"] is False
    out = evaluate_sparse_leg(True, False, 0, 0, CANARY_NOT_RUN)
    assert out["ok"] is False


def test_low_coverage_reports_but_does_not_gate_here():
    # Coverage gating is Test-MemoryStack's WARN (<0.95); a half-backfilled
    # corpus with a live encoder is degraded, not dead.
    out = evaluate_sparse_leg(True, True, 1000, 250, CANARY_OK)
    assert out["ok"] is True and out["coverage"] == 0.25


def test_zero_points_coverage_zero_no_crash():
    out = evaluate_sparse_leg(True, True, 0, 0, CANARY_NOT_RUN)
    assert out["coverage"] == 0.0  # ok=True via the empty-collection carve-out


def test_pinned_unpopulated_cache_gates_even_when_canary_green():
    # AMS-09b (W5 review F2): a warm in-process encoder over an UNSEEDED
    # durable cache is a reboot time-bomb — it must read dead BEFORE the
    # reboot, not after. This is the pre-reboot signal the deploy gate and
    # TMS catch.
    out = evaluate_sparse_leg(True, True, 100, 100, CANARY_OK,
                              cache_pinned=True, cache_populated=False)
    assert out["ok"] is False and "cache_note" in out


def test_pinned_populated_cache_keeps_verdict():
    out = evaluate_sparse_leg(True, True, 100, 100, CANARY_OK,
                              cache_pinned=True, cache_populated=True)
    assert out["ok"] is True
    assert out["cache_pinned"] is True and out["cache_populated"] is True


def test_unpinned_cache_is_legacy_no_verdict():
    # cache_pinned=None (legacy caller / env unset): no cache fields, no gate.
    out = evaluate_sparse_leg(True, True, 100, 100, CANARY_OK)
    assert out["ok"] is True and "cache_pinned" not in out


def test_empty_collection_with_pinned_unpopulated_cache_still_gates():
    # The fresh-box carve-out forgives a missing corpus, never a time-bomb:
    # the installer post-condition seeds the cache on every install path.
    out = evaluate_sparse_leg(True, True, 0, 0, CANARY_NOT_RUN,
                              cache_pinned=True, cache_populated=False)
    assert out["ok"] is False and "cache_note" in out


def test_canary_token_rule_is_deterministic():
    # Longest token wins; lexicographic tiebreak; stable across calls.
    assert pick_canary_token("alpha beta gamma-long zzz") == "gamma-long"
    assert pick_canary_token("bbb aaa ccc") == "aaa"  # equal length -> lexical
    assert pick_canary_token("") is None
    assert pick_canary_token(None) is None
    assert pick_canary_token("  one  ") == "one"
