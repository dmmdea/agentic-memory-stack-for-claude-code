# mem0-server/tests/test_codex_native_transport.py
"""Native Codex transport (spec §4 judge transport): `codex exec` as a subprocess behind the
same fail-soft dict as the shim. The subprocess runner is injected so no codex is needed."""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import codex_shim_client as csc  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEM0_KEY", "k")
    monkeypatch.setenv("MEM0_CODEX_TRANSPORT", "native")
    monkeypatch.setenv("HOME", str(tmp_path))
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
    assert cmd[-1] == "p"


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


def test_shim_path_is_untouched_when_transport_is_shim(monkeypatch):
    import httpx
    monkeypatch.setenv("MEM0_CODEX_TRANSPORT", "shim")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "response": "YES", "tokens_used": 1})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        out = csc.judge("p", client=c)
    assert out["ok"] is True and out["response"] == "YES"
