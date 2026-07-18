"""v0.20 Phase A.3: POST /v1/context/bundle — the batched UserPromptSubmit
context endpoint (admission-gated memories + open goals + open questions in
ONE response, with the episode-checkpoint upsert performed server-side).

The critical invariant: the bundle's `memories` come from _search_core — the
EXACT pipeline POST /v1/memories/search runs (retired/intent filtering,
query_class policy, apply_admission, retrieval logging). These tests prove the
gate holds through the new endpoint (canonical never leaks on the default
class) and that the checkpoint side effect lands in episodic.db.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

import httpx
import pytest

from _debris_patterns import delete_goal_rows, episodic_db_path

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ.get("MEM0_KEY") or (Path.home() / ".mem0" / "api-key").read_text().strip()
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
# Operator-agnostic live tenant: match the server's MEM0_DEFAULT_USER_ID
# (systemd substitutes __WSL_USER__ to the install user), falling back to the
# current user — the bundle's proactive search queries this same default tenant.
import getpass as _getpass
_UID = os.environ.get("MEM0_DEFAULT_USER_ID") or _getpass.getuser()


def _bundle(payload: dict) -> httpx.Response:
    return httpx.post(f"{URL}/v1/context/bundle", json=payload, headers=H, timeout=20)


# ---------------------------------------------------------------------------
# v1.0 Phase 7A (recon defect B2): operator-agnostic bundle tenant.
# The bundle proactive-search MUST NOT hardcode the developer handle as the
# user_id — a third-party install would query the wrong tenant and the
# [MEMORY CONTEXT] injection would return nothing. The default comes from
# MEM0_DEFAULT_USER_ID (systemd unit sets it to the install user via the
# __WSL_USER__ sentinel). Static source assertions (no heavy app reload).
# ---------------------------------------------------------------------------
_APP_SRC = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
_UNIT_SRC = (Path(__file__).resolve().parents[2] / "systemd" / "mem0.service").read_text(encoding="utf-8")


def test_bundle_does_not_hardcode_developer_handle():
    assert 'user_id": "dmmdea"' not in _APP_SRC, (
        "the /v1/context/bundle search must not hardcode the developer handle as user_id "
        "(operator-agnostic regression — recon B2)"
    )


def test_bundle_default_user_id_is_env_configurable():
    assert "MEM0_DEFAULT_USER_ID" in _APP_SRC, "bundle default tenant must read MEM0_DEFAULT_USER_ID"
    assert "DEFAULT_USER_ID" in _APP_SRC, "expected a DEFAULT_USER_ID module constant driving the bundle filter"


def test_mem0_service_sets_default_user_id_sentinel():
    assert "MEM0_DEFAULT_USER_ID=__WSL_USER__" in _UNIT_SRC, (
        "systemd/mem0.service must export MEM0_DEFAULT_USER_ID using the __WSL_USER__ sentinel "
        "(installer substitutes it to the install user)"
    )


# ---------------------------------------------------------------------------
# Shape + checkpoint side effect
# ---------------------------------------------------------------------------

def test_bundle_shape_and_checkpoint_side_effect():
    """One POST returns all four sections, and the checkpoint upsert actually
    lands in episodic.db (created on first call, updated on the second)."""
    session_id = f"test-bundle-{uuid.uuid4()}"
    body = {
        "session_id": session_id,
        "prompt": "v0.20 bundle shape probe - memory stack latency work",
        "brand": "ai-ecosystem",
        "workspace": "ai-ecosystem",
        "transcript_path": None,
        "hook_contract_version": "20.0",
    }
    r1 = _bundle(body)
    assert r1.status_code == 200, f"bundle failed: {r1.status_code} {r1.text}"
    d1 = r1.json()
    for key in ("ok", "checkpoint", "memories", "goals", "open_questions"):
        assert key in d1, f"bundle response missing {key!r}: {sorted(d1)}"
    assert d1["ok"] is True
    assert d1["checkpoint"]["ok"] is True
    assert d1["checkpoint"]["action"] == "created"
    assert d1["checkpoint"]["state"] == "in_progress"
    assert isinstance(d1["memories"], list)
    assert isinstance(d1["goals"], list) and len(d1["goals"]) <= 5
    assert isinstance(d1["open_questions"], list) and len(d1["open_questions"]) <= 3

    # Second call for the same session upserts (not duplicates) the episode
    r2 = _bundle(body)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["checkpoint"]["action"] == "updated"
    assert d2["checkpoint"]["episode_id"] == d1["checkpoint"]["episode_id"]

    # Side-effect row really exists in episodic.db (read-only)
    db = episodic_db_path()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        row = conn.execute(
            "SELECT state, summary_text FROM episodes WHERE id = ?",
            (d1["checkpoint"]["episode_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "checkpoint episode row not found in episodic.db"
    assert row[0] == "in_progress"
    assert "bundle shape probe" in (row[1] or "")


def test_bundle_checkpoint_false_skips_episode_side_effect():
    """v1.0 A1 (mandated-pull): the memory_recall MCP verb pulls the bundle with
    checkpoint=False so a manual recall does NOT upsert an episode — otherwise every
    recall would pollute the SessionStart resume banner with a synthetic session.
    The four sections must still render; the checkpoint section reports skipped."""
    session_id = f"test-bundle-recall-{uuid.uuid4()}"
    r = _bundle({
        "session_id": session_id,
        "prompt": "what ports are reserved on the workstation box",
        "checkpoint": False,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ("ok", "checkpoint", "memories", "goals", "open_questions"):
        assert key in d, f"bundle missing section: {key}"
    assert d["checkpoint"].get("skipped") is True, d["checkpoint"]
    assert "episode_id" not in d["checkpoint"]
    # the checkpoint side-effect row must NOT exist for a manual pull
    db = episodic_db_path()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        srow = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        erow = conn.execute(
            "SELECT 1 FROM episodes WHERE session_id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    assert srow is None, "checkpoint=False must NOT create a sessions row"
    assert erow is None, "checkpoint=False must NOT create an episodes row"


def test_bundle_goals_respect_open_status_and_limit():
    """goals section serves the same query the hook used: status=open, limit 5."""
    r = _bundle({
        "session_id": f"test-bundle-{uuid.uuid4()}",
        "prompt": "bundle goals section probe for the memory stack",
        "hook_contract_version": "20.0",
    })
    assert r.status_code == 200
    for g in r.json()["goals"]:
        assert g["status"] == "open", f"non-open goal leaked into bundle: {g}"


# ---------------------------------------------------------------------------
# v0.21 Phase C (L6): independent per-section degradation + empty/oversized
# prompt handling. The checkpoint-before-search ordering guarantee must survive
# the only externally injectable degraded-search input (empty query), and an
# oversized prompt must be server-truncated (summary <=300, search <=500) and
# still 200 with the checkpoint committed.
# ---------------------------------------------------------------------------

def test_bundle_empty_prompt_degrades_not_500():
    """prompt='' must NOT 500 — checkpoint runs first and succeeds, memories
    degrade to a list, goals/OQ are present, and the episode row lands."""
    session_id = f"test-bundle-{uuid.uuid4()}"
    r = _bundle({
        "session_id": session_id,
        "prompt": "",
        "hook_contract_version": "20.0",
    })
    assert r.status_code == 200, f"empty-prompt bundle 500'd: {r.status_code} {r.text}"
    d = r.json()
    assert d["checkpoint"]["ok"] is True
    assert d["checkpoint"]["action"] == "created"
    assert isinstance(d["memories"], list)
    assert "goals" in d and "open_questions" in d

    # checkpoint side effect actually landed despite the empty (degraded) search
    db = episodic_db_path()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        row = conn.execute(
            "SELECT state FROM episodes WHERE id = ?",
            (d["checkpoint"]["episode_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "checkpoint episode row missing for empty-prompt bundle"
    assert row[0] == "in_progress"


def test_bundle_oversized_prompt_truncated():
    """A ~5000-char prompt must 200 with the checkpoint ok and the episodic
    summary_text server-truncated to <=300 chars; memories stays a list."""
    session_id = f"test-bundle-{uuid.uuid4()}"
    big = "memory stack latency probe " * 200  # ~5400 chars
    r = _bundle({
        "session_id": session_id,
        "prompt": big,
        "hook_contract_version": "20.0",
    })
    assert r.status_code == 200, f"oversized-prompt bundle failed: {r.status_code} {r.text}"
    d = r.json()
    assert d["checkpoint"]["ok"] is True
    assert isinstance(d["memories"], list)

    db = episodic_db_path()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        row = conn.execute(
            "SELECT summary_text FROM episodes WHERE id = ?",
            (d["checkpoint"]["episode_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "checkpoint episode row missing for oversized-prompt bundle"
    assert len(row[0] or "") <= 300, f"summary_text not truncated: {len(row[0] or '')} chars"


# ---------------------------------------------------------------------------
# v0.21 Phase A (M2): fail closed on goals/OQ for an unknown-brand session —
# a brand-tagged open goal / open question must NOT surface in a brandless
# bundle (server-side only_brand_neutral gate), mirroring the memory Layer-2
# brand semantics.
# ---------------------------------------------------------------------------

def test_bundle_brandless_session_excludes_brand_tagged_goal():
    """A brand-tagged open goal must be absent from a bundle POSTed WITHOUT a
    brand (unknown-brand session => brand-neutral goals only), AND a brand-neutral
    goal must STILL surface — the positive control (review L12) that guards against
    an over-broad only_brand_neutral that strips every goal."""
    title = f"manual-{uuid.uuid4()}"  # conftest debris pattern
    gr = httpx.post(f"{URL}/v1/goals", json={"title": title, "brand": "brand-a"}, headers=H, timeout=10)
    assert gr.status_code == 200, f"goal seed failed: {gr.text}"
    gid = gr.json()["goal_id"]
    # brand-neutral (no brand => NULL) control, priority 1 so it is within limit-5
    neutral_title = f"manual-{uuid.uuid4()}"  # NULL-brand control
    nr = httpx.post(f"{URL}/v1/goals", json={"title": neutral_title, "priority": 1}, headers=H, timeout=10)
    assert nr.status_code == 200, f"neutral goal seed failed: {nr.text}"
    ngid = nr.json()["goal_id"]
    try:
        br = _bundle({
            "session_id": f"test-bundle-{uuid.uuid4()}",
            "prompt": "v0.21 M2 brandless goal leak probe",
            "hook_contract_version": "20.0",
        })
        assert br.status_code == 200, f"bundle failed: {br.text}"
        titles = {g["title"] for g in br.json()["goals"]}
        assert title not in titles, (
            f"BRAND LEAK: brand-tagged goal {title!r} surfaced in a brandless bundle: {titles}"
        )
        assert neutral_title in titles, (
            f"REGRESSION: brand-neutral goal {neutral_title!r} was stripped from a brandless "
            f"bundle (only_brand_neutral over-broad): {titles}"
        )
    finally:
        delete_goal_rows([gid, ngid])


def test_bundle_brandless_session_excludes_brand_tagged_open_question():
    """A brand-tagged open question must be absent from a brandless bundle, AND a
    brand-neutral OQ must STILL surface (review L12 positive control)."""
    q_text = f"v0.21 M2 brand-tagged OQ leak probe [test-oq-{uuid.uuid4().hex[:8]}]"
    cr = httpx.post(f"{URL}/v1/open_questions",
                    json={"question_text": q_text, "brand": "brand-a"},
                    headers=H, timeout=10)
    assert cr.status_code == 200, f"OQ seed failed: {cr.text}"
    oq_id = cr.json()["open_question_id"]
    # brand-neutral control, priority 1 so it is within limit-3
    neutral_q = f"v0.21 M2 brand-neutral OQ control [test-oq-{uuid.uuid4().hex[:8]}]"
    nr = httpx.post(f"{URL}/v1/open_questions",
                    json={"question_text": neutral_q, "priority": 1},
                    headers=H, timeout=10)
    assert nr.status_code == 200, f"neutral OQ seed failed: {nr.text}"
    neutral_oq_id = nr.json()["open_question_id"]
    try:
        br = _bundle({
            "session_id": f"test-bundle-{uuid.uuid4()}",
            "prompt": "v0.21 M2 brandless open-question leak probe",
            "hook_contract_version": "20.0",
        })
        assert br.status_code == 200, f"bundle failed: {br.text}"
        texts = {q.get("question_text") for q in br.json()["open_questions"]}
        assert q_text not in texts, (
            f"BRAND LEAK: brand-tagged OQ surfaced in a brandless bundle: {texts}"
        )
        assert neutral_q in texts, (
            f"REGRESSION: brand-neutral OQ was stripped from a brandless bundle "
            f"(only_brand_neutral over-broad): {texts}"
        )
    finally:
        for _id in (oq_id, neutral_oq_id):
            httpx.patch(f"{URL}/v1/open_questions/{_id}/status",
                        json={"status": "abandoned", "actor": "test-cleanup"},
                        headers=H, timeout=5)


def test_bundle_empty_string_brand_normalizes_to_neutral():
    """Review L4: a bundle POSTed with brand='' must behave exactly like a
    brandless (None) bundle — brand-tagged rows excluded, brand-neutral rows
    surface — mirroring the memory Layer-2 str(... or '').strip() normalization.
    Pre-fix, brand='' produced `AND brand = ''`, which returned the EMPTY set
    (no neutral rows) instead of the intended brand-neutral set."""
    tagged = f"manual-{uuid.uuid4()}"
    tr = httpx.post(f"{URL}/v1/goals", json={"title": tagged, "brand": "brand-a"}, headers=H, timeout=10)
    assert tr.status_code == 200, f"tagged goal seed failed: {tr.text}"
    tgid = tr.json()["goal_id"]
    neutral_title = f"manual-{uuid.uuid4()}"  # NULL-brand control
    nr = httpx.post(f"{URL}/v1/goals", json={"title": neutral_title, "priority": 1}, headers=H, timeout=10)
    assert nr.status_code == 200, f"neutral goal seed failed: {nr.text}"
    ngid = nr.json()["goal_id"]
    try:
        br = _bundle({
            "session_id": f"test-bundle-{uuid.uuid4()}",
            "prompt": "review L4 empty-string brand normalization probe",
            "brand": "",
            "hook_contract_version": "20.0",
        })
        assert br.status_code == 200, f"bundle failed: {br.text}"
        titles = {g["title"] for g in br.json()["goals"]}
        assert tagged not in titles, (
            f"BRAND LEAK: brand-tagged goal surfaced for brand='' bundle: {titles}"
        )
        assert neutral_title in titles, (
            f"REGRESSION: brand='' did not normalize to neutral — brand-neutral goal "
            f"absent (the AND brand='' bug): {titles}"
        )
    finally:
        delete_goal_rows([tgid, ngid])


# ---------------------------------------------------------------------------
# v0.22 Pillar 1: initiative-scoped goal injection — stop cross-initiative
# bleed. Two open goals under the SAME brand (ai-ecosystem) but different
# initiatives: a bundle for initiative='agentic-memory-stack' must EXCLUDE the
# 'local-offload' goal yet INCLUDE the cross-cutting (NULL-initiative) one.
# ---------------------------------------------------------------------------

def test_bundle_initiative_scopes_goal_injection():
    """Same-brand, different-initiative open goal must not bleed into a bundle
    scoped to another initiative; a NULL-initiative goal still surfaces."""
    bleed_title = f"manual-{uuid.uuid4()}"   # local-offload, must be EXCLUDED
    cross_title = f"manual-{uuid.uuid4()}"   # NULL initiative, must be INCLUDED
    g_bleed = httpx.post(f"{URL}/v1/goals",
                         json={"title": bleed_title, "brand": "ai-ecosystem", "initiative": "local-offload"},
                         headers=H, timeout=10)
    assert g_bleed.status_code == 200, f"bleed goal seed failed: {g_bleed.text}"
    g_cross = httpx.post(f"{URL}/v1/goals",
                         json={"title": cross_title, "brand": "ai-ecosystem"},  # no initiative => cross-cutting
                         headers=H, timeout=10)
    assert g_cross.status_code == 200, f"cross goal seed failed: {g_cross.text}"
    gid_bleed = g_bleed.json()["goal_id"]
    gid_cross = g_cross.json()["goal_id"]
    try:
        br = _bundle({
            "session_id": f"test-bundle-{uuid.uuid4()}",
            "prompt": "v0.22 initiative goal bleed probe for the memory stack",
            "brand": "ai-ecosystem",
            "initiative": "agentic-memory-stack",
            "hook_contract_version": "20.0",
        })
        assert br.status_code == 200, f"bundle failed: {br.text}"
        titles = {g["title"] for g in br.json()["goals"]}
        assert bleed_title not in titles, (
            f"INITIATIVE BLEED: local-offload goal {bleed_title!r} surfaced in an "
            f"agentic-memory-stack bundle: {titles}"
        )
        assert cross_title in titles, (
            f"cross-cutting (NULL-initiative) goal {cross_title!r} must surface for "
            f"any initiative but was absent: {titles}"
        )
    finally:
        delete_goal_rows([gid_bleed, gid_cross])


# ---------------------------------------------------------------------------
# v0.22 Phase D: tier-scaled bundle caps. The request `tier` selects a per-tier
# policy (frontier|mid|small) for memory/goal/OQ caps + relevance_threshold.
# FRONTIER serves 5 goals / 3 OQ / <=2 memories @ 0.30 (v1.0 R2: K=2; threshold
# KEPT at 0.30 — calibration-confirmed); SMALL scales down (<=3 goals / <=2 OQ /
# <=1 memory @ 0.30). Goal/OQ caps unchanged this phase. Caps are the
# deterministic, store-independent assertion (threshold separation is covered by
# the migration verify-gate, not re-asserted against the live corpus here).
# ---------------------------------------------------------------------------

def _seed_open_goals(n: int) -> list[str]:
    """Seed n brand-neutral, NULL-initiative open goals (debris-tagged title so
    conftest reclaims any leak). Returns their ids."""
    ids: list[str] = []
    for _ in range(n):
        title = f"manual-{uuid.uuid4()}"  # debris pattern
        gr = httpx.post(f"{URL}/v1/goals", json={"title": title}, headers=H, timeout=10)
        assert gr.status_code == 200, f"goal seed failed: {gr.text}"
        ids.append(gr.json()["goal_id"])
    return ids


def _seed_open_questions(n: int) -> list[str]:
    """Seed n brand-neutral open questions (debris-tagged). Returns their ids."""
    ids: list[str] = []
    for _ in range(n):
        q_text = f"v0.22 D tier-cap OQ probe [test-oq-{uuid.uuid4().hex[:8]}]"
        cr = httpx.post(f"{URL}/v1/open_questions", json={"question_text": q_text},
                        headers=H, timeout=10)
        assert cr.status_code == 200, f"OQ seed failed: {cr.text}"
        ids.append(cr.json()["open_question_id"])
    return ids


def _abandon_open_questions(ids) -> None:
    for oq_id in ids:
        httpx.patch(f"{URL}/v1/open_questions/{oq_id}/status",
                    json={"status": "abandoned", "actor": "test-cleanup"},
                    headers=H, timeout=5)


def test_bundle_frontier_tier_caps_unchanged():
    """Regression: tier='frontier' (and the default, no-tier request) must serve
    today's caps — up to 5 goals and 3 open questions. Seed 6 goals + 4 OQ
    (brand-neutral) so the caps, not scarcity, bound the result."""
    gids = _seed_open_goals(6)
    oqids = _seed_open_questions(4)
    try:
        for body in (
            {"session_id": f"test-bundle-{uuid.uuid4()}", "prompt": "frontier cap probe",
             "tier": "frontier", "hook_contract_version": "20.0"},
            {"session_id": f"test-bundle-{uuid.uuid4()}", "prompt": "default cap probe",
             "hook_contract_version": "20.0"},  # no tier => default frontier
        ):
            r = _bundle(body)
            assert r.status_code == 200, f"bundle failed: {r.text}"
            d = r.json()
            assert len(d["goals"]) == 5, f"frontier goal cap regressed: {len(d['goals'])} (want 5)"
            assert len(d["open_questions"]) == 3, (
                f"frontier OQ cap regressed: {len(d['open_questions'])} (want 3)")
    finally:
        delete_goal_rows(gids)
        _abandon_open_questions(oqids)


def test_bundle_small_tier_scales_caps_down():
    """tier='small' must cap goals at 3 and open questions at 2 (strictly below
    frontier's 5/3). Seed 6 goals + 4 OQ so the small caps bound the result."""
    gids = _seed_open_goals(6)
    oqids = _seed_open_questions(4)
    try:
        r = _bundle({
            "session_id": f"test-bundle-{uuid.uuid4()}",
            "prompt": "small tier cap probe",
            "tier": "small",
            "hook_contract_version": "20.0",
        })
        assert r.status_code == 200, f"bundle failed: {r.text}"
        d = r.json()
        assert len(d["goals"]) <= 3, f"small goal cap not applied: {len(d['goals'])} (want <=3)"
        assert len(d["open_questions"]) <= 2, (
            f"small OQ cap not applied: {len(d['open_questions'])} (want <=2)")
        # memories also tier-capped — v1.0 R2 small K=1
        assert len(d["memories"]) <= 1, (
            f"small memory cap not applied: {len(d['memories'])} (want <=1)")
    finally:
        delete_goal_rows(gids)
        _abandon_open_questions(oqids)


def test_bundle_unknown_tier_defaults_to_frontier():
    """An unrecognized tier string must fail-open to frontier caps (5/3), never
    drop the bundle to small."""
    gids = _seed_open_goals(6)
    oqids = _seed_open_questions(4)
    try:
        r = _bundle({
            "session_id": f"test-bundle-{uuid.uuid4()}",
            "prompt": "unknown tier fail-open probe",
            "tier": "bogus-tier-xyz",
            "hook_contract_version": "20.0",
        })
        assert r.status_code == 200, f"bundle failed: {r.text}"
        d = r.json()
        assert len(d["goals"]) == 5, f"unknown tier did not fail-open to frontier: {len(d['goals'])}"
        assert len(d["open_questions"]) == 3, (
            f"unknown tier did not fail-open to frontier: {len(d['open_questions'])}")
    finally:
        delete_goal_rows(gids)
        _abandon_open_questions(oqids)


# ---------------------------------------------------------------------------
# Admission gating — the no-parallel-ungated-path proof
# ---------------------------------------------------------------------------

def test_bundle_memories_admission_gated_canonical_excluded():
    """A canonical-tier record matching the prompt MUST NOT appear in the
    bundle's memories (default durable class strips canonical server-side) —
    while an explicit tier-filtered search proves the record is reachable and
    similar enough that only the admission gate explains its absence."""
    from test_episodic import _qdrant_set_tier  # operator-level tier seed helper

    unique_kw = f"bundle-canon-gate-{uuid.uuid4().hex[:10]}"
    r = httpx.post(
        f"{URL}/v1/memories",
        json={"messages": f"{unique_kw} synthetic canonical ground-truth fact",
              "user_id": _UID, "infer": False,
              "metadata": {"source": "test-bundle-a3", "kind": "test"}},
        headers=H, timeout=15,
    )
    assert r.status_code == 200, f"seed add failed: {r.text}"
    results = r.json().get("results", [])
    if not results:
        pytest.skip("seed add returned 0 results (mem0 dedup)")
    mid = results[0]["id"]

    try:
        _qdrant_set_tier(mid, "canonical")

        # Control: explicit canonical-class search DOES return it (reachable)
        ctl = httpx.post(
            f"{URL}/v1/memories/search",
            json={"query": unique_kw,
                  "filters": {"user_id": _UID, "tier": "canonical"},
                  "limit": 10, "threshold": 0.01},
            headers=H, timeout=15,
        )
        assert ctl.status_code == 200
        assert mid in {x["id"] for x in ctl.json().get("results", [])}, (
            "control failed: canonical record not reachable even with an "
            "explicit tier filter — admission assertion below would be vacuous"
        )

        # The bundle (default durable class, same as the hook) must NOT leak it
        br = _bundle({
            "session_id": f"test-bundle-{uuid.uuid4()}",
            "prompt": unique_kw,
            "hook_contract_version": "20.0",
        })
        assert br.status_code == 200, f"bundle failed: {br.text}"
        bundle_ids = {m.get("id") for m in br.json()["memories"]}
        assert mid not in bundle_ids, (
            f"ADMISSION BREACH: canonical record {mid} leaked through "
            f"/v1/context/bundle on the default class: {bundle_ids}"
        )
    finally:
        try:
            _qdrant_set_tier(mid, "evidence")
        except Exception:
            pass
        httpx.delete(
            f"{URL}/v1/memories/{mid}?actor=test-cleanup&reason=bundle+a3+gate+cleanup",
            headers=H, timeout=10,
        )


def test_bundle_brand_filter_scopes_search():
    """brand in the bundle request becomes a search filter (same as the hook's
    filters.brand) — a cross-brand record must not surface."""
    unique_kw = f"bundle-brand-gate-{uuid.uuid4().hex[:10]}"
    r = httpx.post(
        f"{URL}/v1/memories",
        json={"messages": f"{unique_kw} brand-a-only synthetic fact",
              "user_id": _UID, "infer": False,
              "metadata": {"source": "test-bundle-a3", "kind": "test", "brand": "brand-a"}},
        headers=H, timeout=15,
    )
    assert r.status_code == 200
    results = r.json().get("results", [])
    if not results:
        pytest.skip("seed add returned 0 results (mem0 dedup)")
    mid = results[0]["id"]
    try:
        br = _bundle({
            "session_id": f"test-bundle-{uuid.uuid4()}",
            "prompt": unique_kw,
            "brand": "ai-ecosystem",
            "hook_contract_version": "20.0",
        })
        assert br.status_code == 200
        assert mid not in {m.get("id") for m in br.json()["memories"]}, (
            "cross-brand record leaked through a brand-scoped bundle"
        )
    finally:
        httpx.delete(
            f"{URL}/v1/memories/{mid}?actor=test-cleanup&reason=bundle+a3+brand+cleanup",
            headers=H, timeout=10,
        )


# ---------------------------------------------------------------------------
# Auth + contract version
# ---------------------------------------------------------------------------

def test_bundle_requires_api_key():
    r = httpx.post(
        f"{URL}/v1/context/bundle",
        json={"session_id": f"test-bundle-{uuid.uuid4()}", "prompt": "auth probe"},
        timeout=10,
    )
    assert r.status_code == 401


def test_bundle_unknown_contract_version_accepted_warn_only():
    """MED-17 discipline: unknown hook_contract_version is WARN-only — the
    bundle must still serve (back-compat contract shared with checkpoint).
    The live unknown version carries the '-test' fingerprint (v0.19 M15) so
    Test-MemoryStack's journal drift row ignores it."""
    r = _bundle({
        "session_id": f"test-bundle-{uuid.uuid4()}",
        "prompt": "contract drift probe for the bundle endpoint",
        "hook_contract_version": "99.0-test",
    })
    assert r.status_code == 200, f"unknown version must not be rejected: {r.text}"


def test_bundle_warn_unknown_contract_version_caplog(caplog):
    """The WARN itself is assertable via the side-effect-free hook_contract
    module (app.py is not importable in tests); '20.0' must be silent."""
    import logging as _logging

    from hook_contract import warn_hook_contract_version

    caplog.clear()
    with caplog.at_level(_logging.INFO, logger="mem0-server"):
        warn_hook_contract_version("/v1/context/bundle", "99.0-test")
    recs = [r for r in caplog.records if "MED-17" in r.getMessage()]
    assert len(recs) == 1 and recs[0].levelno == _logging.WARNING
    assert "/v1/context/bundle" in recs[0].getMessage()

    caplog.clear()
    with caplog.at_level(_logging.INFO, logger="mem0-server"):
        warn_hook_contract_version("/v1/context/bundle", "20.0")
    assert not [r for r in caplog.records if "MED-17" in r.getMessage()], (
        "'20.0' is the live v0.20 hook contract version and must not WARN"
    )


# ---------------------------------------------------------------------------
# v1.12 HK-6: the hook client's admission list rejects tier=insight, so the
# bundle must never spend a K-slot (or the client's latency budget) returning
# one — observed live as "0.D admission: 2 of 2 results rejected". The bundle
# search over-fetches +2 and drops insight server-side.
# ---------------------------------------------------------------------------
def test_bundle_filters_insight_tier_server_side():
    marker = f"hk6-insight-{uuid.uuid4().hex[:10]}"
    text = f"insight consolidation {marker}: sessions show the operator prefers dense evidence-first reporting"
    add = httpx.post(
        f"{URL}/v1/memories",
        json={
            "messages": text,
            "user_id": _UID,
            "infer": False,
            # exact-source allowlist for insight writes; the finally-delete below is
            # the cleanup path (conftest's test-record sweep keys off kind+source).
            "metadata": {"tier": "insight", "kind": "test",
                         "source": "c1-consolidator", "test_marker": marker},
        },
        headers=H, timeout=30,
    )
    assert add.status_code == 200, add.text
    added_ids = [r.get("id") for r in (add.json().get("results") or []) if r.get("id")]
    try:
        r = _bundle({
            "session_id": f"test-hk6-{marker}",
            "prompt": f"what did the insight consolidation {marker} conclude?",
            "checkpoint": False,
        })
        assert r.status_code == 200, r.text
        mems = r.json().get("memories") or []
        insight_hits = [m for m in mems if (m.get("metadata") or {}).get("tier") == "insight"]
        assert not insight_hits, (
            f"bundle returned tier=insight memories the hook client will always reject: {insight_hits}"
        )
    finally:
        for mid in added_ids:
            httpx.delete(f"{URL}/v1/memories/{mid}", headers=H, timeout=15)
