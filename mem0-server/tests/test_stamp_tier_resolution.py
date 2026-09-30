"""admission_gate: a contradicts_canonical stamp only hides a record while its target is STILL canonical.

The gate used to trust a bare id forever. 98 % of the stamp rejections in the audit named targets
that had since been demoted to stable or evidence (or were over-broad from the start), and those
stamps were never re-judged, so later operator decisions stayed hidden from durable and operational
recall. The gate now resolves the stamp target's CURRENT tier (one batched lookup per search, cached
ten minutes) and ignores the stamp unless the target is canonical. A lookup that fails is fail-open
and counted: hiding a live record on a guess is the worse error.

The Qdrant retrieve is injected (the same seam the server wires at startup), so this runs headless.
"""
from __future__ import annotations

import pytest

import admission_gate as ag


def _rec(mid, stamp=None, tier="evidence"):
    md = {"tier": tier, "brand": None, "created_at": None}
    if stamp:
        md["contradicts_canonical"] = stamp
    return {"id": mid, "memory": f"text {mid}", "metadata": md}


class Fetcher:
    """Stands in for the batched Qdrant retrieve: records every call, answers from `tiers`."""

    def __init__(self, tiers=None, error=None):
        self.tiers = tiers or {}
        self.error = error
        self.calls: list = []

    def __call__(self, ids):
        self.calls.append(sorted(ids))
        if self.error:
            raise self.error
        return {i: self.tiers[i] for i in ids if i in self.tiers}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)          # audit log stays out of ~/.mem0
    # other suites leave rejection counters behind (a stale day, or ten families that push a new one
    # out of the top-N snapshot); start from zero
    monkeypatch.setattr(ag, "admission_rejection_stats", {"date": None, "total": 0, "reasons": {}})
    monkeypatch.setattr(ag, "_STAMP_TIER_FETCHER", None)
    monkeypatch.setattr(ag, "_stamp_tier_cache", {})
    monkeypatch.setattr(ag, "stamp_resolution_stats",
                        {"date": None, "stamp_target_unresolved": 0, "stamp_ignored_not_canonical": 0})
    return tmp_path


def _admit(results, fetcher=None, qc="durable", stats=None):
    if fetcher is not None:
        ag.set_stamp_tier_fetcher(fetcher)
    return [r["id"] for r in ag.apply_admission(results, scope={}, query_class=qc, stats_out=stats)]


def test_stamp_whose_target_is_now_evidence_admits_the_candidate():
    f = Fetcher({"tgt": "evidence"})
    assert _admit([_rec("cand", stamp="tgt")], f) == ["cand"]
    assert ag.admission_rejections_today()["stamps"]["stamp_ignored_not_canonical"] == 1


def test_stamp_whose_target_is_still_canonical_rejects():
    f = Fetcher({"tgt": "canonical"})
    stats: dict = {}
    assert _admit([_rec("cand", stamp="tgt")], f, stats=stats) == []
    assert stats == {"rejected_contradicted": 1}
    assert ag.admission_rejections_today()["reasons"]["contradicts_canonical"] == 1


def test_stamp_whose_target_is_gone_is_dangling_and_ignored():
    """A missing target is not a canonical: nothing to contradict any more."""
    f = Fetcher({})           # the retrieve returned no point for the id
    assert _admit([_rec("cand", stamp="tgt")], f) == ["cand"]


def test_lookup_error_is_fail_open_and_counted():
    f = Fetcher(error=RuntimeError("qdrant down"))
    assert _admit([_rec("cand", stamp="tgt"), _rec("other")], f) == ["cand", "other"]
    assert ag.admission_rejections_today()["stamps"]["stamp_target_unresolved"] == 1
    # the failure is not cached: the next search retries the lookup
    ag.set_stamp_tier_fetcher(Fetcher({"tgt": "canonical"}))
    assert _admit([_rec("cand", stamp="tgt")]) == []


def test_one_batched_lookup_per_search_and_none_without_stamps():
    f = Fetcher({"t1": "canonical", "t2": "stable"})
    out = _admit([_rec("a", stamp="t1"), _rec("b", stamp="t2"), _rec("c", stamp="t1"), _rec("d")], f)
    assert out == ["b", "d"]
    assert f.calls == [["t1", "t2"]]            # one call, deduplicated ids
    _admit([_rec("only-plain")])
    assert len(f.calls) == 1                    # nothing stamped: no lookup at all


def test_resolved_tiers_are_cached_for_ten_minutes(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(ag.time, "monotonic", lambda: clock["t"])
    f = Fetcher({"tgt": "canonical"})
    assert _admit([_rec("cand", stamp="tgt")], f) == []
    assert _admit([_rec("cand", stamp="tgt")]) == []
    assert len(f.calls) == 1, "second search inside the TTL must not hit Qdrant again"
    clock["t"] += ag.STAMP_TIER_TTL_S + 1
    f.tiers["tgt"] = "stable"                   # the operator demoted it meanwhile
    assert _admit([_rec("cand", stamp="tgt")]) == ["cand"]
    assert len(f.calls) == 2


def test_history_class_never_looks_up_stamps():
    """Forensic queries ignore stamps entirely; there is nothing to resolve."""
    f = Fetcher({"tgt": "canonical"})
    assert _admit([_rec("cand", stamp="tgt")], f, qc="history") == ["cand"]
    assert f.calls == []


def test_pending_stamp_is_still_never_enforced():
    f = Fetcher({"tgt": "canonical"})
    r = _rec("cand")
    r["metadata"]["contradicts_canonical_pending"] = "tgt"
    assert _admit([r], f) == ["cand"]
    assert f.calls == []


def test_without_a_fetcher_the_stamp_is_enforced_as_before():
    """Library use with no server wiring keeps the pre-WP-4 contract: a bare stamp hides."""
    assert _admit([_rec("cand", stamp="tgt")]) == []


def test_evaluate_with_explicit_tiers_is_the_pure_form():
    policy = ag.default_policy_for_class("durable")
    r = _rec("cand", stamp="tgt")
    assert policy.evaluate(r, {}, "durable", stamp_tiers={"tgt": "canonical"}).admit is False
    assert policy.evaluate(r, {}, "durable", stamp_tiers={"tgt": "stable"}).admit is True
    assert policy.evaluate(r, {}, "durable", stamp_tiers={}).admit is True     # unresolved: fail-open
    assert policy.evaluate(r, {}, "durable").admit is False                    # no info: legacy


def test_server_wires_the_fetcher_and_diagnose_uses_the_same_resolution():
    """app.py cannot be imported headless (it builds the live memory client), so pin the wiring in
    source: the fetcher is registered, and the diagnose endpoint resolves stamp tiers rather than
    diverging from the real gate."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert "set_stamp_tier_fetcher(" in src
    assert "resolve_stamp_tiers(" in src
