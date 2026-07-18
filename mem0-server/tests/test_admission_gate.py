"""v0.18 Phase C: admission_gate.AdmissionPolicy unit tests."""
from __future__ import annotations

import datetime as dt
from admission_gate import AdmissionPolicy


def _result(mid="m1", tier="evidence", brand=None, created_at=None, text="hello"):
    return {
        "id": mid,
        "memory": text,
        "metadata": {"tier": tier, "brand": brand, "created_at": created_at},
    }


def test_brand_mismatch_rejected():
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand="brand-a")
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is False
    assert d.reason.startswith("brand_mismatch")


def test_brand_match_admitted():
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand="ai-ecosystem")
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is True


# --- v1.0 Phase 4 / R5: brand-coherence floor (within-brand weak-match cut) ---

def test_brand_coherence_floor_disabled_by_default_admits_low_score():
    """Default (None): a same-brand match is admitted regardless of score — no
    behavior change vs pre-R5."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand="ai-ecosystem"); r["score"] = 0.05
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is True


def test_brand_coherence_floor_rejects_weak_same_brand_match():
    """Floor enabled: a branded result whose score is below the floor is cut as a
    near-but-wrong-domain match."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None,
                             brand_coherence_floor=0.20)
    r = _result(brand="ai-ecosystem"); r["score"] = 0.08
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is False
    assert d.reason.startswith("brand_coherence")


def test_brand_coherence_floor_admits_strong_same_brand_match():
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None,
                             brand_coherence_floor=0.20)
    r = _result(brand="ai-ecosystem"); r["score"] = 0.51
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is True


def test_brand_coherence_floor_fails_open_without_score():
    """No score on the result -> fail-open (admitted), like the relevance floor."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None,
                             brand_coherence_floor=0.20)
    r = _result(brand="ai-ecosystem")  # no 'score' key
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is True


def test_brand_coherence_floor_ignores_null_brand_records():
    """The floor targets BRANDED results; a brand-neutral (null-brand) record is
    not subject to it (it carries no domain to be incoherent with)."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None,
                             brand_coherence_floor=0.20)
    r = _result(brand=None); r["score"] = 0.01
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is True


def test_null_brand_admitted_legacy_data():
    """Legacy data without brand metadata accepted (no regression)."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand=None)
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is True
    assert d.reason == "admitted"  # v0.19 L11: was a tautology ('... or d.admit')


def test_brandless_scope_rejects_branded_record_fail_closed():
    """v0.19 M4: a request scope with NO brand admits only null-brand records —
    brand-scoped records are rejected with brand_scope_required (fail-closed;
    before v0.19 this path was fail-open and leaked every brand)."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand="brand-a")
    d = policy.evaluate(r, scope={}, query_class="durable")
    assert d.admit is False
    assert d.reason == "brand_scope_required:brand-a"
    # null-brand record still admitted in the same brandless scope
    d2 = policy.evaluate(_result(brand=None), scope={}, query_class="durable")
    assert d2.admit is True


def test_brandless_scope_allow_cross_brand_opt_in():
    """v0.19 M4: scope['allow_cross_brand'] is the explicit opt-in restoring the
    pre-v0.19 cross-brand behavior for brandless searches."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand="brand-a")
    d = policy.evaluate(r, scope={"allow_cross_brand": True}, query_class="durable")
    assert d.admit is True


def test_brand_match_case_insensitive():
    """v0.19 M14: brand compare is case-insensitive (aligns with the client
    layer's PowerShell -eq) — 'Brand-A' record matches 'brand-a' scope."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand="Brand-A")
    d = policy.evaluate(r, scope={"brand": "brand-a"}, query_class="durable")
    assert d.admit is True


def test_empty_string_brand_treated_as_legacy():
    """v0.19 M14: empty-string metadata.brand is falsy -> legacy/null on both
    layers — admitted under a branded scope AND under a brandless scope."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand="")
    assert policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable").admit is True
    assert policy.evaluate(r, scope={}, query_class="durable").admit is True


def test_tier_canonical_rejected_in_default_class():
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(tier="canonical")
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is False
    assert "tier_disallowed" in d.reason


def test_tier_insight_admitted_in_default_durable_class():
    """v0.18 fix-pass HIGH: insight is consolidator-distilled durable knowledge.
    Before the fix NO query_class admitted tier='insight' (durable/operational
    allowed stable+evidence, canonical allowed stable+canonical) — the tier was
    unreachable through every shipped search consumer."""
    from admission_gate import default_policy_for_class
    policy = default_policy_for_class("durable")
    r = _result(tier="insight")
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is True
    # canonical class unchanged — insight stays excluded there
    # (operational admits insight since v0.19 M2-residual; see dedicated test)
    assert "insight" not in default_policy_for_class("canonical").allowed_tiers


def test_tier_insight_admitted_in_operational_class():
    """v0.19 M2-residual: operational class admits tier='insight' (consolidator
    insights are durable knowledge); the 180d recency cap still applies."""
    from admission_gate import default_policy_for_class
    policy = default_policy_for_class("operational")
    assert "insight" in policy.allowed_tiers
    fresh = dt.datetime.now(dt.timezone.utc).isoformat()
    d = policy.evaluate(_result(tier="insight", created_at=fresh),
                        scope={"brand": "ai-ecosystem"}, query_class="operational")
    assert d.admit is True
    # recency cap intact for insight too
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).isoformat()
    d2 = policy.evaluate(_result(tier="insight", created_at=old),
                         scope={"brand": "ai-ecosystem"}, query_class="operational")
    assert d2.admit is False
    assert "recency" in d2.reason


def test_recency_reject_operational_old():
    """query_class='operational' with 30d half-life rejects very-old records (>180d)."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=180)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).isoformat()
    r = _result(created_at=old)
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="operational")
    assert d.admit is False
    assert "recency" in d.reason


def test_recency_accept_durable_old():
    """query_class='durable' ignores recency — old stable facts admitted."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=180)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).isoformat()
    r = _result(created_at=old)
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert d.admit is True


# ---------------------------------------------------------------------------
# v0.19 Phase C (M6/M11/L4/L5/L8/L11): admission-gate robustness + fail-open pins
# ---------------------------------------------------------------------------


def test_apply_admission_survives_log_rejected_failure(monkeypatch):
    """v0.19 M6/M11: an audit-write failure must NEVER fail the search —
    log_rejected raising inside apply_admission is swallowed (WARN) and the
    admitted results are still returned."""
    import admission_gate as ag

    def _boom(**kwargs):
        raise OSError("audit file unwritable")

    monkeypatch.setattr(ag, "log_rejected", _boom)
    ok = _result(mid="m-ok", tier="evidence")
    bad = _result(mid="m-bad", tier="canonical")  # rejected in durable class
    out = ag.apply_admission([ok, bad], scope={"brand": "ai-ecosystem"}, query_class="durable")
    assert [r["id"] for r in out] == ["m-ok"]


def test_log_rejected_unwritable_target_is_nonfatal(tmp_path):
    """v0.19 M6: log_rejected itself swallows OSError (target is a directory
    -> open fails) instead of propagating to the search handler."""
    from admission_gate import log_rejected
    log_rejected(memory_id="m9", reason="tier_disallowed:canonical",
                 layer="server-search", target_path=tmp_path)  # a dir, not a file
    # no exception == pass; nothing was written
    assert list(tmp_path.iterdir()) == []


def test_rejected_log_rotates_at_10mb(tmp_path):
    """v0.19 L5: admission-rejected.jsonl rotates at 10MB with the same .1-.5
    scheme as retrieval-log.jsonl (app.py); fresh file starts after rotation."""
    import json
    from admission_gate import log_rejected
    target = tmp_path / "admission-rejected.jsonl"
    with target.open("wb") as f:  # sparse >10MB file, instant
        f.seek(10 * 1024 * 1024)
        f.write(b"x")
    log_rejected(memory_id="m-rot", reason="tier_disallowed:canonical",
                 layer="server-search", target_path=target)
    rotated = tmp_path / "admission-rejected.jsonl.1"
    assert rotated.exists()
    assert rotated.stat().st_size > 10 * 1024 * 1024
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["memory_id"] == "m-rot"


def test_rejected_log_rotation_six_cycles_keeps_dot1_through_dot5(tmp_path):
    """v0.20 Phase E (L11): six successive rotations must leave .1-.5 ALL
    present. The v0.19 code unlinked SRC (.4) at i==5 instead of DST (.5), so
    .4 vanished every cycle and .5 was never created — contradicting its own
    comment and admission-gate.md's '(.1-.5)'. Same 4-line fix applied to the
    copied rotation block in app.py (retrieval-log.jsonl)."""
    import json
    from admission_gate import log_rejected
    target = tmp_path / "admission-rejected.jsonl"
    for cycle in range(6):
        with target.open("wb") as f:  # sparse >10MB file, instant
            f.seek(10 * 1024 * 1024)
            f.write(b"x")
        log_rejected(memory_id=f"m-rot-{cycle}", reason="tier_disallowed:canonical",
                     layer="server-search", target_path=target)
    for i in range(1, 6):
        rotated = tmp_path / f"admission-rejected.jsonl.{i}"
        assert rotated.exists(), f".jsonl.{i} must exist after 6 rotations"
    assert not (tmp_path / "admission-rejected.jsonl.6").exists(), \
        ".5 is the oldest generation kept — no .6 may appear"
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["memory_id"] == "m-rot-5"


def test_case_variant_query_class_still_enforces_recency(tmp_path, monkeypatch):
    """v0.19 L4/L8: 'Operational' (case variant) must reject a >180d record
    exactly like 'operational' — apply_admission normalizes query_class once,
    so default_policy_for_class and evaluate's recency branch always agree.
    Before the fix, the 180d policy was selected but the recency rejection
    silently never fired (raw-string compare in evaluate)."""
    import json
    from pathlib import Path
    from admission_gate import apply_admission
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # keep audit out of real ~/.mem0
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).isoformat()
    out = apply_admission([_result(created_at=old)], scope={}, query_class="Operational")
    assert out == []
    entry = json.loads((tmp_path / ".mem0" / "admission-rejected.jsonl")
                       .read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["reason"].startswith("recency:")
    assert entry["schema_version"] == "v18"


def test_recency_unparseable_created_at_fails_open():
    """v0.19 L11: pins the documented fail-open (admission-gate.md) — a naive
    datetime (no tz -> TypeError on aware-minus-naive subtraction) and a
    garbage string (ValueError in fromisoformat) are both swallowed and the
    record is ADMITTED (fail-open on recency, fail-closed on tier)."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=180)
    for bad in ("2020-01-01T00:00:00", "garbage"):  # naive -> TypeError; junk -> ValueError
        d = policy.evaluate(_result(created_at=bad), scope={}, query_class="operational")
        assert d.admit is True, f"created_at={bad!r} must fail open"


def test_tier_none_admitted_in_canonical_class():
    """v0.19 L11: pins the documented legacy pass-through — tier=None is
    admitted in EVERY class, including canonical."""
    from admission_gate import default_policy_for_class
    d = default_policy_for_class("canonical").evaluate(
        _result(tier=None), scope={}, query_class="canonical")
    assert d.admit is True


def test_default_policy_mapping():
    """v0.19 L11: pins default_policy_for_class itself — allowlists/caps per
    known class (post-Phase-B shape: insight in durable+operational), and the
    unknown/missing-class fallback to durable."""
    from admission_gate import default_policy_for_class
    assert default_policy_for_class("operational") == AdmissionPolicy(("stable", "evidence", "insight"), 180)
    assert default_policy_for_class("canonical") == AdmissionPolicy(("stable", "canonical"), None)
    assert default_policy_for_class("durable") == AdmissionPolicy(("stable", "evidence", "insight"), None)
    # unknown class and missing class both fall back to durable
    assert default_policy_for_class("no-such-class") == AdmissionPolicy(("stable", "evidence", "insight"), None)
    assert default_policy_for_class(None) == AdmissionPolicy(("stable", "evidence", "insight"), None)


# ---------------------------------------------------------------------------
# v0.19 Phase I.2: task-relevance floor (operational class, rerank_score)
# ---------------------------------------------------------------------------


def test_relevance_floor_rejects_below_floor_in_operational():
    """v0.19 I.2: an operational-class result whose rerank_score sits below the
    policy's relevance_floor is rejected with relevance_floor:<score>_below_<floor>."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None,
                             relevance_floor=-15.0)
    r = _result(mid="m-irrelevant")
    r["rerank_score"] = -16.5
    d = policy.evaluate(r, scope={}, query_class="operational")
    assert d.admit is False
    assert d.reason == "relevance_floor:-16.5_below_-15.0"


def test_relevance_floor_absent_score_fails_open():
    """v0.19 I.2: a result WITHOUT rerank_score (rerank off, reranker down, or
    defensive passthrough) is admitted — the floor is fail-open on missing score."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None,
                             relevance_floor=-15.0)
    d = policy.evaluate(_result(), scope={}, query_class="operational")
    assert d.admit is True


def test_relevance_floor_disabled_by_default(monkeypatch):
    """v0.19 I.2: env unset/empty/'0' sentinel -> floor disabled (None) -> no
    rejections even for a catastrophically low rerank_score. Garbage env value
    also falls back to disabled (fail-open knob, never a surprise rejection)."""
    from admission_gate import default_policy_for_class
    # v0.20 Phase F (L10): 'inf' (reject-everything floor), '-inf' and 'nan'
    # (never-fires floor) parse as floats but are non-finite — all must disable
    # the floor exactly like garbage/empty values.
    for env_val in (None, "", "0", "0.0", "garbage", "inf", "-inf", "nan"):
        if env_val is None:
            monkeypatch.delenv("MEM0_RELEVANCE_FLOOR_OPERATIONAL", raising=False)
        else:
            monkeypatch.setenv("MEM0_RELEVANCE_FLOOR_OPERATIONAL", env_val)
        policy = default_policy_for_class("operational")
        assert policy.relevance_floor is None, f"env={env_val!r} must disable the floor"
        r = _result()
        r["rerank_score"] = -50.0
        assert policy.evaluate(r, scope={}, query_class="operational").admit is True


def test_relevance_floor_env_override_respected(monkeypatch):
    """v0.19 I.2: MEM0_RELEVANCE_FLOOR_OPERATIONAL=-12.5 rejects below, admits at/above."""
    from admission_gate import default_policy_for_class
    monkeypatch.setenv("MEM0_RELEVANCE_FLOOR_OPERATIONAL", "-12.5")
    policy = default_policy_for_class("operational")
    assert policy.relevance_floor == -12.5
    below = _result(mid="m-below")
    below["rerank_score"] = -13.0
    d = policy.evaluate(below, scope={}, query_class="operational")
    assert d.admit is False
    assert d.reason == "relevance_floor:-13.0_below_-12.5"
    at = _result(mid="m-at")
    at["rerank_score"] = -12.5
    assert policy.evaluate(at, scope={}, query_class="operational").admit is True


def test_relevance_floor_only_applies_to_operational_class():
    """v0.19 I.2: the floor is operational-only — the same policy object with a
    floor set never rejects in durable/history classes."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None,
                             relevance_floor=-15.0)
    for qc in ("durable", "history"):
        r = _result()
        r["rerank_score"] = -20.0
        assert policy.evaluate(r, scope={}, query_class=qc).admit is True, qc


# ---------------------------------------------------------------------------
# v0.19 Phase I.1: supersession-aware filtering + history (forensic) class
# ---------------------------------------------------------------------------


def test_superseded_rejected_in_durable_and_operational():
    """v0.19 I.1: a result carrying truthy metadata.superseded_by is rejected
    in BOTH the durable and operational classes with reason superseded_by:<mid>
    — the newer record should surface instead of the superseded one."""
    from admission_gate import default_policy_for_class
    for qc in ("durable", "operational"):
        r = _result(mid="m-old")
        r["metadata"]["superseded_by"] = "m-new"
        d = default_policy_for_class(qc).evaluate(r, scope={}, query_class=qc)
        assert d.admit is False, f"superseded record must be rejected in {qc}"
        assert d.reason == "superseded_by:m-new"


def test_superseded_admitted_in_history_class():
    """v0.19 I.1: query_class='history' is the forensic escape hatch — durable
    allowlist (+canonical since v0.20 M13), with the supersession check disabled
    so superseded records stay retrievable for forensic/audit queries."""
    from admission_gate import default_policy_for_class
    policy = default_policy_for_class("history")
    assert policy.forensic is True
    # v0.20 Phase F (M13): history = durable allowlist + canonical
    assert set(policy.allowed_tiers) == set(default_policy_for_class("durable").allowed_tiers) | {"canonical"}
    r = _result(mid="m-old")
    r["metadata"]["superseded_by"] = "m-new"
    d = policy.evaluate(r, scope={}, query_class="history")
    assert d.admit is True


def test_null_or_absent_superseded_by_admitted():
    """v0.19 I.1: null and absent superseded_by are both falsy -> admitted
    (the check only fires on a truthy supersession pointer)."""
    from admission_gate import default_policy_for_class
    policy = default_policy_for_class("durable")
    r_null = _result()
    r_null["metadata"]["superseded_by"] = None
    assert policy.evaluate(r_null, scope={}, query_class="durable").admit is True
    r_absent = _result()  # no superseded_by key at all
    assert policy.evaluate(r_absent, scope={}, query_class="durable").admit is True


# ---------------------------------------------------------------------------
# v0.19 Phase I.3: contradiction-stamped record rejection (no LLM involved —
# the offline sweep stamps contradicts_canonical; the gate only reads it)
# ---------------------------------------------------------------------------


def test_contradicts_canonical_rejected_in_durable_and_operational():
    """v0.19 I.3: a result stamped contradicts_canonical=<canonical_mid> by the
    offline sweep is rejected in durable AND operational with reason
    contradicts_canonical:<mid> — a record contradicting locked ground truth
    must not surface in default retrieval."""
    from admission_gate import default_policy_for_class
    for qc in ("durable", "operational"):
        r = _result(mid="m-contra")
        r["metadata"]["contradicts_canonical"] = "c-truth"
        d = default_policy_for_class(qc).evaluate(r, scope={}, query_class=qc)
        assert d.admit is False, f"contradiction-stamped record must be rejected in {qc}"
        assert d.reason == "contradicts_canonical:c-truth"


def test_contradicts_canonical_pending_is_admitted_not_hidden():
    """v0.29.4: a record stamped ONLY contradicts_canonical_pending (a LOCAL/advisory
    judge verdict) must still be ADMITTED — the gate enforces only the authoritative
    contradicts_canonical (Codex). A weak local verdict never hides a live record; a
    Codex re-judge promotes pending -> contradicts_canonical before anything is hidden."""
    from admission_gate import default_policy_for_class
    for qc in ("durable", "operational"):
        r = _result(mid="m-pending")
        r["metadata"].pop("contradicts_canonical", None)  # explicitly NO confirmed stamp
        r["metadata"]["contradicts_canonical_pending"] = "c-truth"
        d = default_policy_for_class(qc).evaluate(r, scope={}, query_class=qc)
        assert d.admit is True, f"pending-only (local advisory) record must be admitted in {qc}"


def test_contradicts_canonical_admitted_in_history_class():
    """v0.19 I.3: the history class (forensic=True, same flag as I.1) admits
    contradiction-stamped records for forensic queries."""
    from admission_gate import default_policy_for_class
    r = _result(mid="m-contra")
    r["metadata"]["contradicts_canonical"] = "c-truth"
    d = default_policy_for_class("history").evaluate(r, scope={}, query_class="history")
    assert d.admit is True


def test_null_or_absent_contradicts_canonical_admitted():
    """v0.19 I.3: null/absent contradicts_canonical is falsy -> admitted
    (contradiction_checked_at alone — the sweep's NO verdict — never rejects)."""
    from admission_gate import default_policy_for_class
    policy = default_policy_for_class("durable")
    r_checked = _result()
    r_checked["metadata"]["contradicts_canonical"] = None
    r_checked["metadata"]["contradiction_checked_at"] = "2026-06-12T00:00:00+00:00"
    assert policy.evaluate(r_checked, scope={}, query_class="durable").admit is True
    assert policy.evaluate(_result(), scope={}, query_class="durable").admit is True


def test_rejected_results_logged_to_jsonl(tmp_path, monkeypatch):
    """Rejected candidates write to admission-rejected.jsonl with reason."""
    from admission_gate import log_rejected
    rejected_path = tmp_path / "admission-rejected.jsonl"
    log_rejected(
        memory_id="m9",
        reason="brand_mismatch:brand-a_vs_ai-ecosystem",
        layer="server-search",
        target_path=rejected_path,
    )
    assert rejected_path.exists()
    line = rejected_path.read_text(encoding="utf-8").strip()
    import json
    entry = json.loads(line)
    assert entry["memory_id"] == "m9"
    assert entry["reason"].startswith("brand_mismatch")
    assert entry["layer"] == "server-search"


# ---------------------------------------------------------------------------
# v0.20 Phase B (L3) — allow_cross_brand strict parsing
# ---------------------------------------------------------------------------

def test_brandless_scope_string_false_does_not_opt_in():
    """v0.20 L3: allow_cross_brand is strict-parsed — Python truthiness no
    longer applies. A hand-rolled REST client sending the string 'false', '0',
    'False', or '' (or JSON numbers 0/1, lists, dicts) must NOT enable
    cross-brand: only bool True or a recognized true-string opts in."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand="brand-a")
    for v in ("false", "0", "no", "False", "", "off", 0, 1, None, [], ["true"], {"x": 1}):
        d = policy.evaluate(r, scope={"allow_cross_brand": v}, query_class="durable")
        assert d.admit is False, f"cell {v!r} must fail closed"
        assert d.reason == "brand_scope_required:brand-a"


def test_brandless_scope_true_spellings_opt_in():
    """v0.20 L3: bool True and the conventional true-string spellings
    ('1'/'true'/'yes', case-insensitive, whitespace-tolerant) still opt in."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand="brand-a")
    for v in (True, "true", "TRUE", " true ", "True", "1", "yes", "YES"):
        d = policy.evaluate(r, scope={"allow_cross_brand": v}, query_class="durable")
        assert d.admit is True, f"cell {v!r} must opt in"


# ---------------------------------------------------------------------------
# v0.20 Phase F (M13) — history class admits canonical (forensic superset)
# ---------------------------------------------------------------------------


def test_stamped_canonical_reachable_only_in_history_class():
    """v0.20 M13: a superseded canonical-tier record is rejected in
    durable/operational (tier), rejected in the canonical class (stamp), and
    ADMITTED in history — before the fix it was unreachable in EVERY class,
    so the documented forensic escape hatch excluded exactly the records whose
    history matters most. No trust-boundary change: the same API key already
    reads canonical via query_class='canonical'."""
    from admission_gate import default_policy_for_class
    r = _result(mid="m-canon-old", tier="canonical")
    r["metadata"]["superseded_by"] = "m-new"
    for qc in ("durable", "operational"):
        d = default_policy_for_class(qc).evaluate(r, scope={}, query_class=qc)
        assert d.admit is False, f"stamped canonical must stay rejected in {qc}"
        assert d.reason == "tier_disallowed:canonical"
    d_canon = default_policy_for_class("canonical").evaluate(r, scope={}, query_class="canonical")
    assert d_canon.admit is False
    assert d_canon.reason == "superseded_by:m-new"
    d_hist = default_policy_for_class("history").evaluate(r, scope={}, query_class="history")
    assert d_hist.admit is True


# ---------------------------------------------------------------------------
# v0.20 Phase F (M12) — history-class negatives: the forensic flag relaxes ONLY
# the supersession/contradiction checks; tier allowlist + brand guard hold.
# ---------------------------------------------------------------------------


def test_history_class_still_rejects_disallowed_tier():
    """history is forensic for supersession/contradiction ONLY — the tier
    allowlist still applies. Post-M13 the history allowlist is
    stable+evidence+insight+canonical, so 'temporal' (a real ADD_ALLOWED_TIERS
    tier) pins the rejection cell."""
    from admission_gate import default_policy_for_class
    d = default_policy_for_class("history").evaluate(
        _result(tier="temporal"), scope={}, query_class="history")
    assert d.admit is False
    assert d.reason == "tier_disallowed:temporal"


def test_history_class_still_enforces_brand_fail_closed():
    """forensic=True must NOT extend to the brand guard — a superseded
    cross-brand record stays rejected in the history class (both the
    brand_mismatch and the M4 brand_scope_required branches)."""
    from admission_gate import default_policy_for_class
    policy = default_policy_for_class("history")
    r = _result(brand="brand-a")
    r["metadata"]["superseded_by"] = "m-new"
    d = policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="history")
    assert d.admit is False
    assert d.reason.startswith("brand_mismatch")
    d2 = policy.evaluate(r, scope={}, query_class="history")
    assert d2.admit is False
    assert d2.reason == "brand_scope_required:brand-a"


# ---------------------------------------------------------------------------
# v0.20 Phase F (M14) — brand decision-table holes
# ---------------------------------------------------------------------------


def test_allow_cross_brand_does_not_override_explicit_brand_mismatch():
    """M14 ordering cell: allow_cross_brand opts in to cross-brand results on a
    BRANDLESS search only — with an explicit scope brand set, a mismatched
    record is still rejected (the mismatch check runs first and the opt-in
    never reaches it). Pins the current code; no production change needed."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    d = policy.evaluate(_result(brand="brand-a"),
                        scope={"brand": "ai-ecosystem", "allow_cross_brand": True},
                        query_class="durable")
    assert d.admit is False
    assert d.reason.startswith("brand_mismatch")


def test_whitespace_only_brand_treated_as_legacy():
    """M14 whitespace cell: a whitespace-only metadata.brand ('  ') normalizes
    to the legacy-empty convention — admitted under a branded scope AND under a
    brandless scope, exactly like '' (the strip already used for comparison now
    also feeds the falsiness checks). The client layer mirrors this via
    [string]::IsNullOrWhiteSpace (user-prompt-lib.ps1)."""
    policy = AdmissionPolicy(allowed_tiers=("stable", "evidence"), max_age_days=None)
    r = _result(brand="  ")
    assert policy.evaluate(r, scope={"brand": "ai-ecosystem"}, query_class="durable").admit is True
    assert policy.evaluate(r, scope={}, query_class="durable").admit is True
    # symmetric cell: whitespace-only SCOPE brand is a brandless scope —
    # fail-closed against branded records, open to null-brand ones
    d = policy.evaluate(_result(brand="brand-a"), scope={"brand": "  "}, query_class="durable")
    assert d.admit is False
    assert d.reason == "brand_scope_required:brand-a"
    assert policy.evaluate(_result(brand=None), scope={"brand": "  "}, query_class="durable").admit is True


# ---------------------------------------------------------------------------
# MEM-8 (2026-07-03): retrieval-starvation observability — daily rejection
# counters (per reason FAMILY) + per-call stats_out (rejected_brand_scoped).
# ---------------------------------------------------------------------------

def _reset_daily_counters():
    from admission_gate import admission_rejection_stats
    admission_rejection_stats["date"] = None
    admission_rejection_stats["total"] = 0
    admission_rejection_stats["reasons"] = {}


def test_stats_out_counts_brand_scope_hides(tmp_path, monkeypatch):
    """A brandless search over branded records: stats_out reports exactly how
    many were hidden by the fail-closed gate (and ONLY that family — a tier
    rejection must not inflate the brand-hide count the shim hints on)."""
    from pathlib import Path
    from admission_gate import apply_admission
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # keep audit out of real ~/.mem0
    _reset_daily_counters()
    stats: dict = {}
    results = [
        _result(mid="m1", brand="brand-a"),
        _result(mid="m2", brand="brand-d"),
        _result(mid="m3", brand=None),                    # neutral -> admitted
        _result(mid="m4", tier="temporal", brand=None),   # tier reject, not brand
    ]
    admitted = apply_admission(results, scope={}, query_class="durable", stats_out=stats)
    assert [r["id"] for r in admitted] == ["m3"]
    assert stats == {"rejected_brand_scoped": 2}


def test_stats_out_absent_keeps_legacy_signature(tmp_path, monkeypatch):
    """Callers that don't pass stats_out (client layers, older code) are
    untouched — same returns, no error."""
    from pathlib import Path
    from admission_gate import apply_admission
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _reset_daily_counters()
    out = apply_admission([_result(brand="brand-a")], scope={}, query_class="durable")
    assert out == []


def test_daily_counters_group_by_reason_family(tmp_path, monkeypatch):
    """Counters key on the reason FAMILY (prefix before ':') — the suffix
    carries per-record ids/brands and would explode cardinality."""
    from pathlib import Path
    from admission_gate import apply_admission, admission_rejections_today
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _reset_daily_counters()
    apply_admission([
        _result(mid="m1", brand="brand-a"),
        _result(mid="m2", brand="brand-d"),
        _result(mid="m3", tier="temporal"),
    ], scope={}, query_class="durable")
    snap = admission_rejections_today()
    assert snap["total"] == 3
    assert snap["reasons"]["brand_scope_required"] == 2
    assert snap["reasons"]["tier_disallowed"] == 1
    assert snap["date"] == dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def test_daily_counters_reset_on_day_roll(tmp_path, monkeypatch):
    """Lazy day-roll: a stale date resets the counters on the next rejection
    (simple dict, reset daily — the MEM-8 contract)."""
    from pathlib import Path
    from admission_gate import (admission_rejection_stats, admission_rejections_today,
                                apply_admission)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _reset_daily_counters()
    admission_rejection_stats["date"] = "1999-12-31"
    admission_rejection_stats["total"] = 500
    admission_rejection_stats["reasons"] = {"tier_disallowed": 500}
    apply_admission([_result(brand="brand-a")], scope={}, query_class="durable")
    snap = admission_rejections_today()
    assert snap["total"] == 1, "stale day must reset, not accumulate"
    assert snap["reasons"] == {"brand_scope_required": 1}


def test_rejections_today_caps_top_n(tmp_path, monkeypatch):
    from admission_gate import admission_rejection_stats, admission_rejections_today
    _reset_daily_counters()
    admission_rejection_stats["date"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    admission_rejection_stats["total"] = 55
    admission_rejection_stats["reasons"] = {f"family{i}": i for i in range(1, 11)}
    snap = admission_rejections_today(top_n=3)
    assert list(snap["reasons"].values()) == [10, 9, 8], "top families by count, capped"
    assert snap["total"] == 55
