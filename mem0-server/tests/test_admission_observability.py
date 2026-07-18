"""MEM-8 (2026-07-03): retrieval-starvation observability — app wiring.

The counters themselves are unit-tested in test_admission_gate.py; this file
pins the app.py wiring: /health/deep surfaces
checks.admission_rejections_today, and every _search_core response carries the
rejected_brand_scoped count (what the MCP shim's "pass brand=" hint reads).

Direct app-function calls (import app) — the live :18791 server runs
pre-remediation code until the orchestrator deploys, so HTTP assertions on the
new fields would test the wrong bytes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402  (heavy import; mem0 init runs once, shared across the suite)


def test_health_deep_surfaces_admission_rejections_today():
    d = app.health_deep()
    snap = d["checks"]["admission_rejections_today"]
    assert set(snap.keys()) == {"date", "total", "reasons"}
    assert isinstance(snap["total"], int)
    assert isinstance(snap["reasons"], dict)


def test_health_deep_rejections_are_informational_only():
    """The counters must never flip ok=False — starvation visibility is a
    diagnostic, not a liveness failure (mirrors the hook_contract convention)."""
    from admission_gate import admission_rejection_stats
    saved = dict(admission_rejection_stats)
    try:
        admission_rejection_stats["total"] = 10_000
        admission_rejection_stats["reasons"] = {"brand_scope_required": 10_000}
        d = app.health_deep()
        checks_ok = [v.get("ok") for k, v in d["checks"].items()
                     if isinstance(v, dict) and "ok" in v]
        # whatever the box's real health, the rejection count contributed nothing
        assert d["ok"] == all(checks_ok)
    finally:
        admission_rejection_stats.update(saved)


def test_search_response_carries_rejected_brand_scoped():
    """Every gated search response now has the count field (0 when nothing was
    hidden) — the shim keys its hint off it, so absence would silently disable
    the ergonomics fix."""
    res = app._search_core(app.SearchIn(
        query="mem-8 observability probe",
        filters={"user_id": "test-mem8-nonexistent"},
        limit=1,
        threshold=0.99,   # nothing real clears this — deterministic empty result
        rerank=False,
        query_class="durable",
    ))
    assert "rejected_brand_scoped" in res
    assert isinstance(res["rejected_brand_scoped"], int)
    assert res["rejected_brand_scoped"] >= 0
