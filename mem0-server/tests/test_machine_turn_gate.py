"""C10: a background task notification is a machine turn — the per-prompt bundle serves it nothing.

The UserPromptSubmit hook fires for human prompts AND for background task notifications (both
the turn a notification opens and one queued behind a running turn reach the hook with the
prompt starting ``<task-notification>``). Measured before this change: the [MEMORY CONTEXT]
block rode ~91% of those machine turns. The Windows clients now skip the bundle for them; the
server applies the same verdict to whatever reaches ``POST /v1/context/bundle``, so no path leaks:
the episode checkpoint still lands, and memories, goals and open questions come back empty
without a search.

The verdict lives in ``hook_contract.is_machine_turn_prompt`` (side-effect free, imported
headless). The corpus is shared with the lib's Test-MachineTurnPrompt and the compiled client's
stdin scan (scripts/windows/tests), so the three gates cannot drift apart.

The two RUNTIME tests drive ``app.context_bundle`` directly with its collaborators patched. They
need ``import app`` (the mem0 package), so they skip in the headless lane and gate in the
live-stack suite — the test_w7_server_smalls.py pattern.
"""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "mem0-server"))

import hook_contract  # noqa: E402  (side-effect free by design)

_CORPUS_PATH = REPO_ROOT / "scripts" / "windows" / "tests" / "fixtures" / "machine-turn-prompts.json"
CORPUS = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))["prompts"]


def test_corpus_is_nonvacuous():
    """Both verdicts are represented, including the queued-notification shape."""
    verdicts = {c["machine_turn"] for c in CORPUS}
    assert verdicts == {True, False}
    assert any("queued" in c["name"] and c["machine_turn"] for c in CORPUS)


@pytest.mark.parametrize("case", CORPUS, ids=[c["name"] for c in CORPUS])
def test_machine_turn_verdict_matches_the_shared_corpus(case):
    assert hook_contract.is_machine_turn_prompt(case["prompt"]) is case["machine_turn"]


def test_none_prompt_is_not_a_machine_turn():
    assert hook_contract.is_machine_turn_prompt(None) is False


# ---------------------------------------------------------------------------
# Runtime: app.context_bundle with its collaborators patched
# ---------------------------------------------------------------------------

def _app_module():
    try:
        import app
    except Exception as e:  # noqa: BLE001 — any import failure is a skip
        pytest.skip(f"needs the live-stack venv (app import failed: "
                    f"{type(e).__name__}: {str(e)[:60]}) — runs in the live suite")
    return app


@pytest.fixture
def wired_app(monkeypatch):
    app = _app_module()
    calls = {"checkpoint": 0, "search": 0, "goals": 0, "open_questions": 0, "raw_fallback": 0}

    def _checkpoint(b):
        calls["checkpoint"] += 1
        return {"ok": True, "episode_id": 7, "action": "updated", "state": "in_progress"}

    def _search(si, _route="search"):
        calls["search"] += 1
        return {"results": [{"id": "m1", "memory": "alpha fact", "metadata": {"tier": "evidence"}}]}

    def _goals(conn, **kw):
        calls["goals"] += 1
        return [{"id": 1, "title": "a goal", "priority": 2, "status": "open"}]

    def _oqs(conn, **kw):
        calls["open_questions"] += 1
        return [{"id": 2, "question_text": "an open question?"}]

    def _raw(prompt, brand):
        calls["raw_fallback"] += 1
        return None

    monkeypatch.setattr(app, "auth", lambda *a, **k: None)
    monkeypatch.setattr(app, "_checkpoint_core", _checkpoint)
    monkeypatch.setattr(app, "_search_core", _search)
    monkeypatch.setattr(app, "_episodic_connect", lambda *a, **k: contextlib.nullcontext(None))
    monkeypatch.setattr(app, "_episodic_list_goals", _goals)
    monkeypatch.setattr(app, "_episodic_list_open_questions", _oqs)
    monkeypatch.setattr(app, "_episode_raw_fallback", _raw)
    return app, calls


def _body(app, prompt):
    return app.ContextBundleIn(session_id="c10-test-session", prompt=prompt, brand=None,
                               workspace="ai-ecosystem", tier="frontier",
                               hook_contract_version="20.0")


@pytest.mark.parametrize("case", [c for c in CORPUS if c["machine_turn"]],
                         ids=[c["name"] for c in CORPUS if c["machine_turn"]])
def test_bundle_serves_a_task_notification_nothing_but_keeps_the_checkpoint(wired_app, case):
    app, calls = wired_app
    out = app.context_bundle(_body(app, case["prompt"][:500]), x_api_key="k")
    assert out["memories"] == []
    assert out["goals"] == []
    assert out["open_questions"] == []
    assert "raw_fallback" not in out
    assert out.get("machine_turn") is True
    assert calls["checkpoint"] == 1, "the 0.A episode checkpoint must still land on a machine turn"
    assert calls["search"] == 0, "no bundle search may run for a machine turn"
    assert calls["goals"] == 0 and calls["open_questions"] == 0


def test_bundle_human_prompt_control_is_unchanged(wired_app):
    app, calls = wired_app
    out = app.context_bundle(_body(app, "what is the state of the admission gate"), x_api_key="k")
    assert calls["checkpoint"] == 1 and calls["search"] == 1
    assert [m["id"] for m in out["memories"]] == ["m1"]
    assert out["goals"] and out["open_questions"]
    assert "machine_turn" not in out
