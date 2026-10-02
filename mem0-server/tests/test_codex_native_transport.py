# mem0-server/tests/test_codex_native_transport.py
"""Native Codex transport (spec §4 judge transport): `codex exec` as a subprocess behind the
same fail-soft dict as the shim. The subprocess runner is injected so no codex is needed."""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import codex_shim_client as csc  # noqa: E402


from _home_isolation import apply_home  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEM0_KEY", "k")
    monkeypatch.setenv("MEM0_CODEX_TRANSPORT", "native")
    apply_home(monkeypatch, tmp_path)
    monkeypatch.setattr(csc, "NATIVE_LOCK_PATH", str(tmp_path / ".mem0" / "codex-native.lock"))
    monkeypatch.setattr(csc.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=["codex"], returncode=rc, stdout=out, stderr=err)


def test_transport_selection(monkeypatch):
    assert csc.judge_transport() == "native"
    monkeypatch.setenv("MEM0_CODEX_TRANSPORT", "shim")
    assert csc.judge_transport() == "shim"
    monkeypatch.setenv("MEM0_CODEX_TRANSPORT", "auto")
    monkeypatch.delenv("MEM0_HOST_KIND", raising=False)
    assert csc.judge_transport() == "shim"
    monkeypatch.setenv("MEM0_HOST_KIND", "native")
    assert csc.judge_transport() == "native"
    monkeypatch.setattr(csc.shutil, "which", lambda name: None)
    assert csc.judge_transport() == "none"


def test_native_success_reads_last_message_and_tokens():
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        # the -o file is what production reads; stdout carries the token line
        with open(cmd[cmd.index("--output-last-message") + 1], "w", encoding="utf-8") as f:
            f.write('{"plan":[]}\n')
        return _cp(0, out='codex\n{"plan":[]}\ntokens used\n4,037\n')

    out = csc.judge("p", effort="medium", timeout_s=30, model="gpt-5.6-terra", _run=run)
    assert out["ok"] is True
    assert out["response"] == '{"plan":[]}'
    assert out["tokens_used"] == 4037
    assert out["transport"] == "native"
    cmd = seen["cmd"]
    assert "exec" in cmd and "--skip-git-repo-check" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-terra"
    assert 'model_reasoning_effort="medium"' in cmd
    assert cmd[-1] == "p" and cmd[-2] == "--", "the prompt is positional after --, so a prompt starting with - is never parsed as a flag"


def test_native_prompt_starting_with_dash_is_passed_verbatim():
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return _cp(0, out="tokens used\n1\n")

    out = csc.judge("-not a flag", _run=run)
    assert out["ok"] is True
    assert seen["cmd"][-1] == "-not a flag" and seen["cmd"][-2] == "--"


def test_native_usage_limit_is_its_own_error_type():
    out = csc.judge("p", _run=lambda cmd, **kw: _cp(1, err="ERROR: rate_limit_exceeded: You have hit your usage limit."))
    assert out["ok"] is False and out["error_type"] == "usage_limit"


def test_native_nonzero_exit_and_timeout():
    out = csc.judge("p", _run=lambda cmd, **kw: _cp(2, err="boom"))
    assert out["ok"] is False and out["error_type"] == "exit_nonzero" and "boom" in out["error"]

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    out = csc.judge("p", timeout_s=5, _run=slow)
    assert out["ok"] is False and out["error_type"] == "client_timeout"


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")
def test_native_lock_is_single_flight(tmp_path):
    import fcntl
    lock = tmp_path / ".mem0" / "codex-native.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "w")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        out = csc.judge("p", _run=lambda cmd, **kw: _cp(0, out="tokens used\n1\n"))
    finally:
        fh.close()
    assert out["ok"] is False and out["error_type"] == "lock_contended"


def test_native_no_codex_on_path(monkeypatch):
    monkeypatch.setattr(csc.shutil, "which", lambda name: None)
    out = csc.judge("p")
    assert out["ok"] is False and out["error_type"] == "no_codex"


def test_native_health_reports_login():
    out = csc.health(_run=lambda cmd, **kw: _cp(0, out="Logged in using ChatGPT\n"))
    assert out["ok"] is True and out["transport"] == "native" and out["logged_in"] is True
    out = csc.health(_run=lambda cmd, **kw: _cp(1, out="Not logged in\n"))
    assert out["ok"] is False and out["logged_in"] is False


def test_native_health_reads_the_stream_codex_actually_writes_to():
    """Measured on the live authority 2026-09-20: `codex login status` exits 0 and writes
    "Logged in using ChatGPT" to STDERR, leaving stdout EMPTY. The parse read stdout only,
    so every native host reported logged_in=False on a logged-in Codex. That is why
    contradiction-sweep no-op'd with "codex shim unreachable" every Sunday since the brain
    went native, chased the Windows shim it does not have, and still exited 0 while the
    chain's health stamp recorded it green. The test above passed throughout because its
    fixture put the message on stdout - the stream the real binary does not use."""
    out = csc.health(_run=lambda cmd, **kw: _cp(0, out="", err="Logged in using ChatGPT\n"))
    assert out["ok"] is True and out["logged_in"] is True, \
        "a logged-in Codex that answers on stderr must read as healthy"
    # A refusal on stderr must still read as refused - the fix must not just say yes.
    out = csc.health(_run=lambda cmd, **kw: _cp(1, out="", err="Not logged in\n"))
    assert out["ok"] is False and out["logged_in"] is False
    # And "not logged in" anywhere in either stream wins over the substring "logged in".
    out = csc.health(_run=lambda cmd, **kw: _cp(0, out="", err="Not logged in\n"))
    assert out["ok"] is False and out["logged_in"] is False


def test_shim_path_is_untouched_when_transport_is_shim(monkeypatch):
    import httpx
    monkeypatch.setenv("MEM0_CODEX_TRANSPORT", "shim")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "response": "YES", "tokens_used": 1})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        out = csc.judge("p", client=c)
    assert out["ok"] is True and out["response"] == "YES"


def test_native_parses_the_inline_token_line_of_codex_0154():
    out = csc.judge("p", _run=lambda cmd, **kw: _cp(0, out="codex\nok\ntokens used 4,037\n"))
    assert out["ok"] and out["tokens_used"] == 4037
    out = csc.judge("p", _run=lambda cmd, **kw: _cp(0, out="codex\nok\ntokens used\n12\n"))
    assert out["tokens_used"] == 12, "the 0.153 two-line form still parses"


# --- usage telemetry: the codex CLI prints its session header and usage footer on STDERR --------
# (the final message is what goes to stdout). Reading stdout only left tokens_used 0 and the resolved
# model empty on every row of the usage ledger. Fixtures below are the two shapes of `codex exec` output.
_STDERR_SHAPE = (
    "OpenAI Codex v0.154.0 (research preview)\n"
    "--------\n"
    "workdir: /tmp/codex-judge-x\n"
    "model: gpt-6-astra\n"
    "provider: openai\n"
    "approval: never\n"
    "sandbox: read-only\n"
    "reasoning effort: high\n"
    "reasoning summaries: auto\n"
    "session id: 0199-abc\n"
    "--------\n"
    "tokens used\n"
    "12,345\n")
_STDOUT_SHAPE = ("codex\n"
                 '{"plan":[]}\n'
                 "tokens used 4,037\n")


def test_native_reads_tokens_model_and_effort_from_stderr():
    out = csc.judge("p", _run=lambda cmd, **kw: _cp(0, out='{"plan":[]}\n', err=_STDERR_SHAPE))
    assert out["ok"] is True
    assert out["tokens_used"] == 12345
    assert out["model_resolved"] == "gpt-6-astra" and out["effort_resolved"] == "high"


def test_native_reads_tokens_from_stdout_too():
    out = csc.judge("p", _run=lambda cmd, **kw: _cp(0, out=_STDOUT_SHAPE, err=""))
    assert out["tokens_used"] == 4037
    assert out["model_resolved"] is None and out["effort_resolved"] is None, "no header on this shape: unknown, not ''"


def test_native_header_on_stdout_and_footer_on_stderr_combine():
    out = csc.judge("p", _run=lambda cmd, **kw: _cp(0, out="model: gpt-5.6-terra\nreasoning effort: medium\nanswer\n", err="tokens used\n7\n"))
    assert out["tokens_used"] == 7 and out["model_resolved"] == "gpt-5.6-terra" and out["effort_resolved"] == "medium"


def test_native_takes_the_last_token_tally_when_several_are_printed():
    out = csc.judge("p", _run=lambda cmd, **kw: _cp(0, out="ok\n", err="tokens used\n10\nmore work\ntokens used\n99\n"))
    assert out["tokens_used"] == 99


def test_native_a_missed_footer_is_null_never_zero():
    """0 reads as 'this call was free'; None reads as 'not measured'. The ledger sums both the same way,
    but an audit (and the morning summary) can tell them apart."""
    out = csc.judge("p", _run=lambda cmd, **kw: _cp(0, out="just the answer\n", err="some warning\n"))
    assert out["ok"] is True
    assert out["tokens_used"] is None and out["model_resolved"] is None and out["effort_resolved"] is None


def test_the_judge_child_inherits_no_credential_pointer(monkeypatch):
    """1.32.5: the judge reads memory text any API-key holder writes; its codex child must not
    be handed the units' credential directory or a key."""
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/user/1000/credentials/x.service")
    monkeypatch.setenv("MEM0_API_KEY_FILE", "/run/user/1000/credentials/x.service/ams-api-key")
    monkeypatch.setenv("MEM0_API_KEY", "k2")
    monkeypatch.setenv("CODEX_HOME", "/secrets/codex")
    seen = {}

    def run(cmd, **kw):
        seen["env"] = kw.get("env")
        with open(cmd[cmd.index("--output-last-message") + 1], "w", encoding="utf-8") as f:
            f.write("ok\n")
        return _cp(0, out="ok\n")

    assert csc.judge("p", effort="low", timeout_s=30, model="m", _run=run)["ok"] is True
    env = seen["env"]
    assert env is not None, "the judge must pass an explicit, scrubbed environment"
    for k in ("CREDENTIALS_DIRECTORY", "MEM0_API_KEY_FILE", "MEM0_KEY", "MEM0_API_KEY"):
        assert k not in env, k
    assert env.get("CODEX_HOME") == "/secrets/codex", "codex still finds its own login"
