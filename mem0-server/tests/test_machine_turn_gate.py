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
# Relayed agent messages: a second verdict, ported from the Windows lib's Test-RelayedAgentMessage
# ---------------------------------------------------------------------------

def test_every_corpus_case_carries_both_verdicts():
    """The additive relayed_agent_message field is on every case, and both values are represented."""
    assert all(isinstance(c.get("relayed_agent_message"), bool) for c in CORPUS)
    assert {c["relayed_agent_message"] for c in CORPUS} == {True, False}
    names = [c["name"] for c in CORPUS]
    assert len(names) == len(set(names)), "corpus names must be unique (they are the test ids)"


@pytest.mark.parametrize("case", CORPUS, ids=[c["name"] for c in CORPUS])
def test_relayed_verdict_matches_the_shared_corpus(case):
    assert hook_contract.is_relayed_agent_message(case["prompt"]) is case["relayed_agent_message"]


@pytest.mark.parametrize("case", CORPUS, ids=[c["name"] for c in CORPUS])
def test_non_human_verdict_is_either_machine_or_relayed(case):
    want = case["machine_turn"] or case["relayed_agent_message"]
    assert hook_contract.is_non_human_turn(case["prompt"]) is want


def test_a_relayed_message_stays_human_shaped_for_the_memory_block():
    """C10 keeps a relayed message on the human path (the corpus says machine_turn=false); only the
    episode summary treats it as non-human. is_machine_turn_prompt must not absorb the relayed verdict."""
    peers = [c for c in CORPUS if c["relayed_agent_message"]]
    assert peers
    for c in peers:
        assert hook_contract.is_machine_turn_prompt(c["prompt"]) is False
        assert hook_contract.is_non_human_turn(c["prompt"]) is True


@pytest.mark.parametrize("value", [None, "", 0, 7, b"<cross-session-message>", ["<cross-session-message>"], {}])
def test_relayed_verdict_never_raises_and_reads_non_text_as_human(value):
    assert hook_contract.is_relayed_agent_message(value) is False
    assert hook_contract.is_non_human_turn(value) is False


def test_relayed_verdict_is_an_exact_port_of_the_powershell_predicate():
    """Same six leading whitespace characters, case-sensitive, and the wrapper needs a following
    whitespace-or-'>' character. The edges below were also run through Test-RelayedAgentMessage."""
    w = '<cross-session-message from="uds:example">hi</cross-session-message>'
    lead = " \t\r\n\f\v"
    assert hook_contract.is_relayed_agent_message(lead + w) is True
    # a character outside the six-char trim set is NOT skipped
    assert hook_contract.is_relayed_agent_message(" " + w) is False
    assert hook_contract.is_relayed_agent_message("\x1c" + w) is False
    # the announcement is matched ordinally, so any case or spacing difference is a miss
    assert hook_contract.is_relayed_agent_message("another claude session sent a message:\n" + w) is False
    assert hook_contract.is_relayed_agent_message("Another Claude session sent a message:\n" + w) is True
    # the wrapper must be followed by whitespace or '>' (a bare end of string is not enough)
    assert hook_contract.is_relayed_agent_message("<cross-session-message") is False
    assert hook_contract.is_relayed_agent_message("<cross-session-message>") is True
    assert hook_contract.is_relayed_agent_message("<cross-session-message\nfrom=x>") is True
    # .NET's \s covers the Unicode space separators and NEL, but not the C0 separators Python's \s adds
    assert hook_contract.is_relayed_agent_message("<cross-session-message from=x>") is True
    assert hook_contract.is_relayed_agent_message("<cross-session-message　from=x>") is True
    assert hook_contract.is_relayed_agent_message("<cross-session-message\x85from=x>") is True
    assert hook_contract.is_relayed_agent_message("<cross-session-message\x1cfrom=x>") is False
    # the announcement alone is not enough; the wrapper may sit anywhere after it
    assert hook_contract.is_relayed_agent_message("Another Claude session sent a message: hi") is False
    assert hook_contract.is_relayed_agent_message("Another Claude session sent a message: see " + w) is True
    # a wrapper that merely appears mid-text of an ordinary prompt is not a relayed message
    assert hook_contract.is_relayed_agent_message("look: " + w) is False


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
