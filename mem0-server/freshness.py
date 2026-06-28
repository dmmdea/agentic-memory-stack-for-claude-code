"""Freshness decay for the read path (v1.0 Phase 4 / R5).

Pure, dependency-free (math only) so it unit-tests with no mem0/Qdrant/server.
Used by app.py `_search_core` for the `operational` query_class (and any future
temporal/evidence read-gate).

R5 replaces the v0.18 plain exponential half-life with a **Weibull** decay that is
BACKWARD-COMPATIBLE at the default shape:

    w = exp(-ln2 * (age_days / eta_days) ** kappa)

- `eta_days` is the HALF-LIFE: w(eta) == 0.5 for ANY kappa (since (eta/eta)^k = 1).
  Sourced from the existing MEM0_OPERATIONAL_HALF_LIFE_DAYS env (default 30) so
  current deployments keep their tuning.
- `kappa` is the SHAPE (MEM0_WEIBULL_KAPPA, default 1.0):
    kappa = 1.0 -> exp(-age/eta * ln2)  == the exact v0.18 behavior (no regression).
    kappa > 1.0 -> steeper "cliff": decays SLOWER than exponential before the
                   half-life (rewards genuinely-recent records) and FASTER after
                   (punishes stale evidence) — the SSGM-style anti-staleness gate.
    kappa < 1.0 -> heavier tail (older records retained longer).
"""
import datetime as _dt
import math


def freshness_weight(age_days: float, eta_days: float, kappa: float) -> float:
    """Weibull freshness weight in [0, 1]. w(0)=1; w(eta)=0.5 for any kappa.

    Resilient: non-positive age -> 1.0 (clock skew never inflates a score above the
    base); non-positive eta -> falls back to 30d; overflow/domain errors -> 0.0."""
    if age_days <= 0:
        return 1.0
    if eta_days <= 0:
        eta_days = 30.0
    if kappa <= 0:
        kappa = 1.0
    try:
        return math.exp(-math.log(2) * (age_days / eta_days) ** kappa)
    except (OverflowError, ValueError):
        return 0.0


# v0.29.4 item 1: the DURABLE read path decays only `evidence`. `temporal` is also a
# time-sensitive tier (ADD_ALLOWED_TIERS), but admission_gate.default_policy_for_class
# ("durable") admits only (stable, evidence, insight) — a temporal record is dropped at
# admission, so decaying it here would be dead code + a docs-vs-behavior mismatch (audit
# 2026-06-16 MEDIUM). evidence is the live time-sensitive tier on the durable path. The
# helper stays generic (decay_tiers is a parameter) — add 'temporal' back here ONLY if
# admission ever admits temporal on the durable class, and add a decay-then-admission test.
DURABLE_DECAY_TIERS = frozenset({"evidence"})


def apply_durable_freshness(items, eta_days, kappa, now, decay_tiers=DURABLE_DECAY_TIERS):
    """v0.29.4 item 1 / R5: tier-scoped Weibull freshness for the DURABLE read path.

    Decays ONLY the time-sensitive tiers (decay_tiers = evidence, temporal); atemporal
    records (canonical, stable, insight, or untiered) keep their RAW score so the re-sort
    compares decayed and undecayed records on one common scale. Mutates each item in place
    (durable_freshness_score, plus durable_freshness_weight on decayed ones) and re-sorts
    `items` in place by the (possibly-decayed) score, DESCENDING. Returns True iff at least
    one record was decayed (and the list re-sorted); False leaves the original order intact.

    Pure (no I/O / no server). base score = rerank_score if present (explicit None check so a
    legitimate 0.0 is kept) else score else 0.0 — mirrors the operational block in app.py."""
    any_decayed = False
    for r in items:
        meta = r.get("metadata") or {}
        base = r["rerank_score"] if r.get("rerank_score") is not None else (
            r.get("score") if r.get("score") is not None else 0.0)
        if meta.get("tier") in decay_tiers:
            created = meta.get("created_at") or r.get("created_at")
            decay = 1.0
            if created:
                try:
                    cd = _dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    age = max(0.0, (now - cd).total_seconds() / 86400.0)
                    decay = freshness_weight(age, float(eta_days), float(kappa))
                except (ValueError, TypeError):
                    decay = 1.0
            r["durable_freshness_weight"] = round(decay, 6)
            r["durable_freshness_score"] = base * decay
            any_decayed = True
        else:
            r["durable_freshness_score"] = base
    if any_decayed:
        items.sort(key=lambda x: x.get("durable_freshness_score", -1.0), reverse=True)
    return any_decayed
