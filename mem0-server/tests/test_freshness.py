"""v1.0 Phase 4 / R5: Weibull freshness read-gate (pure-function unit tests).

The decay weight lives in its own module (mem0-server/freshness.py) so it can be
unit-tested with NO mem0/Qdrant import or running server. The live read path
(_search_core, operational query_class) calls freshness_weight(); R5 replaces the
v0.18 exponential half-life with a Weibull that is BACKWARD-COMPATIBLE at the
default shape kappa=1.0."""
import math
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from freshness import freshness_weight  # noqa: E402


def test_fresh_record_full_weight():
    assert freshness_weight(0.0, 30.0, 1.0) == 1.0
    assert freshness_weight(-5.0, 30.0, 1.0) == 1.0  # clock skew -> never >1


def test_half_life_at_eta_for_any_kappa():
    # eta is the half-life: w(eta) == 0.5 regardless of shape (since (eta/eta)^k = 1).
    for kappa in (0.5, 1.0, 1.2, 2.0):
        assert abs(freshness_weight(30.0, 30.0, kappa) - 0.5) < 1e-9


def test_kappa_1_reproduces_v018_exponential_half_life():
    # No-regression guard: kappa=1.0 must equal the old exp(-age/eta * ln2) exactly.
    eta = 30.0
    for age in (1.0, 7.0, 15.0, 45.0, 90.0):
        old = math.exp(-age / eta * math.log(2))
        assert abs(freshness_weight(age, eta, 1.0) - old) < 1e-12


def test_monotonic_decreasing_in_age():
    prev = 1.01
    for age in range(0, 200, 5):
        w = freshness_weight(float(age), 30.0, 1.2)
        assert w <= prev
        prev = w


def test_kappa_gt1_is_cliffier_around_eta():
    # Steeper shape: BEFORE the half-life it decays slower (rewards recent), AFTER it
    # decays faster (punishes stale) than the exponential.
    assert freshness_weight(15.0, 30.0, 2.0) > freshness_weight(15.0, 30.0, 1.0)
    assert freshness_weight(60.0, 30.0, 2.0) < freshness_weight(60.0, 30.0, 1.0)


def test_bounded_and_resilient():
    for age in (0.0, 1.0, 1000.0, 1e6):
        w = freshness_weight(age, 30.0, 1.2)
        assert 0.0 <= w <= 1.0
    # bad eta falls back to a sane default rather than dividing by zero
    assert 0.0 <= freshness_weight(10.0, 0.0, 1.0) <= 1.0
    assert 0.0 <= freshness_weight(10.0, -3.0, 1.0) <= 1.0


# ---------------------------------------------------------------------------
# v0.29.4 item 1 — tier-scoped durable-path freshness (apply_durable_freshness)
# ---------------------------------------------------------------------------
import datetime as _dt  # noqa: E402
from freshness import apply_durable_freshness, DURABLE_DECAY_TIERS  # noqa: E402

_NOW = _dt.datetime(2026, 6, 16, tzinfo=_dt.timezone.utc)


def _item(mid, tier, score, age_days=None, rerank=None):
    it = {"id": mid, "metadata": {"tier": tier}, "score": score}
    if rerank is not None:
        it["rerank_score"] = rerank
    if age_days is not None:
        it["metadata"]["created_at"] = (_NOW - _dt.timedelta(days=age_days)).isoformat()
    return it


def test_durable_decay_tiers_default_evidence_only():
    # v0.29.4 audit MEDIUM: the durable read path admits only (stable, evidence, insight);
    # temporal is dropped at admission, so the durable decay scope is evidence-only.
    assert DURABLE_DECAY_TIERS == frozenset({"evidence"})


def test_durable_freshness_default_decays_evidence_not_temporal_or_atemporal():
    items = [
        _item("ev-old", "evidence", 0.9, age_days=365),   # 1yr @ 365d half-life -> ~0.5
        _item("temp", "temporal", 0.8, age_days=365),     # time-sensitive BUT not admitted on durable
        _item("canon", "canonical", 0.7, age_days=365),   # atemporal -> NO decay
        _item("stable", "stable", 0.6, age_days=999),     # atemporal -> NO decay
        _item("insight", "insight", 0.5, age_days=999),   # atemporal -> NO decay
    ]
    changed = apply_durable_freshness(items, 365, 1.0, _NOW)
    assert changed is True
    by = {i["id"]: i for i in items}
    assert abs(by["ev-old"]["durable_freshness_weight"] - 0.5) < 0.02   # ~half at 1yr
    for mid in ("temp", "canon", "stable", "insight"):
        assert "durable_freshness_weight" not in by[mid], f"{mid} must not decay on the durable default scope"
        assert by[mid]["durable_freshness_score"] == by[mid]["score"]  # raw score preserved


def test_durable_freshness_helper_is_generic_over_decay_tiers():
    # the helper decays whatever decay_tiers it is GIVEN (so a future class that admits
    # temporal could opt in) — the durable evidence-only scope is a policy choice, not a limit.
    items = [_item("temp", "temporal", 0.8, age_days=365)]
    apply_durable_freshness(items, 365, 1.0, _NOW, decay_tiers=frozenset({"evidence", "temporal"}))
    assert items[0]["durable_freshness_weight"] < 0.6  # ~0.5 at 1yr -> the helper CAN decay temporal


def test_durable_freshness_then_admission_drops_temporal_keeps_evidence_order():
    # v0.29.4 audit MEDIUM (the suggested integration test): freshness re-sort THEN admission.
    # temporal is dropped on the durable class (so decaying it was dead); the surviving evidence
    # records keep the freshness-sorted order.
    from admission_gate import default_policy_for_class
    items = [
        _item("ev-stale", "evidence", 0.80, age_days=365),  # decays to ~0.40
        _item("temp", "temporal", 0.95, age_days=0),        # high raw score but NOT admitted on durable
        _item("ev-fresh", "evidence", 0.60, age_days=1),    # decays to ~0.60
    ]
    apply_durable_freshness(items, 365, 1.0, _NOW)
    pol = default_policy_for_class("durable")
    admitted = [r["id"] for r in items if pol.evaluate(r, scope={}, query_class="durable").admit]
    assert "temp" not in admitted, "temporal must be dropped at admission on the durable class"
    assert admitted == ["ev-fresh", "ev-stale"], f"survivors keep the freshness order; got {admitted}"


def test_durable_freshness_demotes_stale_evidence_below_atemporal():
    items = [
        _item("ev-stale", "evidence", 0.80, age_days=365),  # 0.80*~0.5 = ~0.40
        _item("canon", "canonical", 0.55, age_days=0),      # 0.55 (no decay)
        _item("ev-fresh", "evidence", 0.60, age_days=1),    # 0.60*~1 = ~0.60
    ]
    apply_durable_freshness(items, 365, 1.0, _NOW)
    order = [i["id"] for i in items]
    assert order.index("ev-stale") > order.index("canon"), "stale evidence must drop below canonical"
    assert order.index("ev-fresh") < order.index("ev-stale"), "fresh evidence must stay above stale"


def test_durable_freshness_no_decay_tier_leaves_order_untouched():
    items = [_item("c1", "canonical", 0.5), _item("s1", "stable", 0.9)]
    before = [i["id"] for i in items]
    changed = apply_durable_freshness(items, 365, 1.0, _NOW)
    assert changed is False                       # nothing decayed -> no re-sort
    assert [i["id"] for i in items] == before


def test_durable_freshness_missing_created_at_keeps_full_weight():
    items = [_item("ev", "evidence", 0.7)]        # no created_at
    apply_durable_freshness(items, 365, 1.0, _NOW)
    assert items[0]["durable_freshness_weight"] == 1.0
    assert items[0]["durable_freshness_score"] == 0.7


def test_durable_freshness_prefers_rerank_score_over_raw():
    items = [_item("ev", "evidence", score=0.2, rerank=0.9, age_days=0)]
    apply_durable_freshness(items, 365, 1.0, _NOW)
    assert abs(items[0]["durable_freshness_score"] - 0.9) < 0.02  # rerank wins, fresh ~ full weight
