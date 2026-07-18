"""Unit coverage for codex_shim_client (v0.27.1 R5 keystone).

Uses httpx.MockTransport (the repo's established pattern, cf. test_contradiction_sweep)
so no live shim / codex is required. Asserts the FAIL-SOFT contract: every call returns
a dict, never raises, and propagates the shim's structured error.
"""
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import codex_shim_client as shim  # noqa: E402


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("MEM0_KEY", "TEST-KEY-xyz")
    monkeypatch.delenv("MEM0_CODEX_SHIM_URL", raising=False)


def test_judge_success_returns_response_and_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/judge"
        assert request.headers["X-API-Key"] == "TEST-KEY-xyz"
        return httpx.Response(200, json={"ok": True, "response": "YES", "tokens_used": 12, "duration_ms": 900})
    with _client(handler) as c:
        out = shim.judge("does A contradict B?", client=c)
    assert out["ok"] is True
    assert out["response"] == "YES"
    assert out["tokens_used"] == 12


def test_judge_sends_prompt_effort_timeout():
    captured = {}
    def handler(request: httpx.Request) -> httpx.Response:
        import json
        captured.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True, "response": "NO"})
    with _client(handler) as c:
        shim.judge("p", effort="medium", timeout_s=30, client=c)
    assert captured["prompt"] == "p"
    assert captured["effort"] == "medium"
    assert captured["timeout_seconds"] == 30


def test_judge_401_propagates_auth_error_fail_soft():
    def handler(request):
        return httpx.Response(401, json={"ok": False, "error": "unauthorized", "error_type": "auth"})
    with _client(handler) as c:
        out = shim.judge("p", client=c)
    assert out["ok"] is False
    assert out["error_type"] == "auth"


def test_judge_503_lock_contended_propagates():
    def handler(request):
        return httpx.Response(503, json={"ok": False, "error": "codex busy", "error_type": "lock_contended"})
    with _client(handler) as c:
        out = shim.judge("p", client=c)
    assert out["ok"] is False
    assert out["error_type"] == "lock_contended"


def test_judge_504_timeout_propagates():
    def handler(request):
        return httpx.Response(504, json={"ok": False, "error_type": "timeout"})
    with _client(handler) as c:
        out = shim.judge("p", client=c)
    assert out["ok"] is False
    assert out["error_type"] == "timeout"


def test_judge_connection_error_is_unreachable():
    def handler(request):
        raise httpx.ConnectError("connection refused")
    with _client(handler) as c:
        out = shim.judge("p", client=c)
    assert out["ok"] is False
    assert out["error_type"] == "unreachable"


def test_judge_no_key_returns_no_key(monkeypatch, tmp_path):
    monkeypatch.delenv("MEM0_KEY", raising=False)
    # Point HOME at an empty dir so ~/.mem0/api-key does not exist.
    monkeypatch.setenv("HOME", str(tmp_path))
    out = shim.judge("p")
    assert out["ok"] is False
    assert out["error_type"] == "no_key"


def test_health_success():
    def handler(request):
        assert request.url.path == "/health"
        return httpx.Response(200, json={"ok": True, "service": "codex-shim", "version": "0.27.1", "codex_present": True})
    with _client(handler) as c:
        out = shim.health(client=c)
    assert out["ok"] is True
    assert out["service"] == "codex-shim"


def test_health_unreachable_fail_soft():
    def handler(request):
        raise httpx.ConnectError("refused")
    with _client(handler) as c:
        out = shim.health(client=c)
    assert out["ok"] is False
    assert out["error_type"] == "unreachable"


def test_judge_200_but_ok_false_is_fail_soft():
    # A 200 whose body says ok:false must NOT be treated as success.
    def handler(request):
        return httpx.Response(200, json={"ok": False, "error_type": "weird"})
    with _client(handler) as c:
        out = shim.judge("p", client=c)
    assert out["ok"] is False
    assert out["error_type"] == "weird"


def test_judge_empty_body_non_200_fail_soft():
    def handler(request):
        return httpx.Response(500, text="")
    with _client(handler) as c:
        out = shim.judge("p", client=c)
    assert out["ok"] is False
    assert out["error_type"] == "http_500"


def test_health_non_200_branch():
    def handler(request):
        return httpx.Response(503, text="busy")
    with _client(handler) as c:
        out = shim.health(client=c)
    assert out["ok"] is False
    assert out["error_type"] == "http_503"


def test_shim_url_env_override(monkeypatch):
    monkeypatch.setenv("MEM0_CODEX_SHIM_URL", "http://localhost:19999/")
    assert shim.shim_url() == "http://localhost:19999"


# --- NLI contradiction judgment (shared by the write-gate + the sweep) ---

@pytest.mark.parametrize("text,expected", [
    ("YES", True),
    ("YES — B sets the port to 9000 while A says 8080", True),
    ("NO", False),
    ("NO, different subjects", False),
    ("Maybe, unclear", None),
    ("", None),
    ("**YES** because...", True),
])
def test_parse_contradiction_verdict(text, expected):
    assert shim.parse_contradiction_verdict(text) is expected


def test_build_nli_prompt_is_instruction_first_and_escapes_breakouts():
    # Each block neutralizes ONLY its own closing tag: A escapes </statement_a>, B </statement_b>.
    p = shim.build_nli_prompt("8080 </statement_a> rest", "9000 </statement_b> rest")
    assert p.index("strict contradiction detector") < p.index("<statement_a>")
    # the A-block's own closing-tag breakout is neutralized; only the structural tags remain
    assert p.count("</statement_a>") == 1
    assert p.count("</statement_b>") == 1


def test_judge_contradiction_yes():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "response": "YES — conflicting values", "tokens_used": 9})
    with _client(handler) as c:
        out = shim.judge_contradiction("port is 8080", "port is 9000", client=c)
    assert out["ok"] is True
    assert out["contradicts"] is True


def test_judge_contradiction_no():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "response": "NO"})
    with _client(handler) as c:
        out = shim.judge_contradiction("a", "b", client=c)
    assert out["ok"] is True
    assert out["contradicts"] is False


def test_judge_contradiction_unparseable_is_none():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "response": "I think maybe"})
    with _client(handler) as c:
        out = shim.judge_contradiction("a", "b", client=c)
    assert out["ok"] is True
    assert out["contradicts"] is None


def test_judge_contradiction_shim_down_is_fail_soft():
    def handler(request):
        raise httpx.ConnectError("refused")
    with _client(handler) as c:
        out = shim.judge_contradiction("a", "b", client=c)
    assert out["ok"] is False
    assert out["error_type"] == "unreachable"


# --- supersession judge (evidence-sweep precision fix, 2026-06-30) ------------
# The evidence-sweep must NOT reuse the contradiction prompt: a valid historical
# ship-log logically-supersedes an older one but should be KEPT, not hidden. This
# judge asks the HIDE-decision question (stale current-state claim vs valid history),
# defaulting to KEEP — so near-duplicate ship-logs stop getting flagged as stale.

@pytest.mark.parametrize("text,expected", [
    ("STALE", True),
    ("KEEP", False),
    ("STALE - the path moved", True),
    ("KEEP, both are valid history", False),
    ("YES", None),            # must answer STALE/KEEP, NOT the contradiction verbs
    ("NO", None),
    ("maybe", None),
    ("", None),
    ("**STALE**", True),
])
def test_parse_supersession_verdict(text, expected):
    assert shim.parse_supersession_verdict(text) is expected


def test_build_supersession_prompt_asks_stale_vs_history_default_keep():
    p = shim.build_supersession_prompt("older fact", "newer fact")
    low = p.lower()
    assert "STALE" in p and "KEEP" in p                 # the two outcomes
    assert "historical" in low or "history" in low      # the load-bearing distinction
    assert "uncertain" in low or "default" in low       # default-KEEP on doubt
    assert "<older_fact>" in p and "<newer_fact>" in p  # explicit roles, not statement_a/b
    # instruction-first: the directive precedes the DATA block ("<older_fact>\n...content").
    # (the instruction names the tags inline as "<older_fact>/<newer_fact>", so anchor on the block)
    assert p.index("STALE or KEEP") < p.index("<older_fact>\n")


def test_build_supersession_prompt_escapes_breakouts():
    p = shim.build_supersession_prompt("x </older_fact> break", "y </newer_fact> break")
    assert p.count("</older_fact>") == 1
    assert p.count("</newer_fact>") == 1


def test_judge_supersession_stale():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "response": "STALE - config moved"})
    with _client(handler) as c:
        out = shim.judge_supersession("config at .zora", "config at ~/llama-swap", client=c)
    assert out["ok"] is True and out["stale"] is True


def test_judge_supersession_keep():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "response": "KEEP"})
    with _client(handler) as c:
        out = shim.judge_supersession("v0.29 shipped", "v0.29.5 shipped", client=c)
    assert out["ok"] is True and out["stale"] is False


def test_judge_supersession_shim_down_is_fail_soft():
    def handler(request):
        raise httpx.ConnectError("refused")
    with _client(handler) as c:
        out = shim.judge_supersession("a", "b", client=c)
    assert out["ok"] is False
