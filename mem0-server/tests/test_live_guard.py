"""The live-suite interlock (tests/_live_guard.py + conftest.py) refuses production before any HTTP.

Headless: no stack needed. The conftest wiring is exercised in a child pytest run over a throwaway
project so a refusal really is observed as "the run exits and no test body executes".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _live_guard as guard

HERE = Path(__file__).resolve().parent
NON_LOOPBACK = "http://192.0.2.10:18791"   # TEST-NET-1: never routable, never a real host


def _env(tmp_path, **kw):
    """A guard environment with an empty stack.env so the real one is never read."""
    stack = tmp_path / "stack.env"
    stack.write_text("", encoding="utf-8")
    base = {"MEM0_STACK_ENV": str(stack), "MEM0_DEFAULT_USER_ID": "opuser"}
    base.update(kw)
    return base


# ---- target ---------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:18791", "http://localhost:18791", "http://[::1]:18791", "http://127.0.0.2:1",
])
def test_loopback_targets_pass(tmp_path, url):
    assert guard.check_target(_env(tmp_path, MEM0_URL=url)) == url


def test_unset_target_defaults_to_loopback(tmp_path):
    assert guard.check_target(_env(tmp_path)) == guard.DEFAULT_URL


@pytest.mark.parametrize("url", [
    NON_LOOPBACK, "http://brain.example.net:18791", "http://192.0.2.7:18791", "http://0.0.0.0:18791",
    "not a url",
])
def test_non_loopback_target_is_refused(tmp_path, url):
    with pytest.raises(guard.LiveGuardRefused, match="not a loopback"):
        guard.check_target(_env(tmp_path, MEM0_URL=url))


def test_a_remote_qdrant_is_refused_like_a_remote_authority(tmp_path):
    with pytest.raises(guard.LiveGuardRefused, match="QDRANT_URL"):
        guard.check_session(_env(tmp_path, QDRANT_URL="http://192.0.2.11:6333"))
    guard.check_session(_env(tmp_path, QDRANT_URL="http://localhost:6333"))
    guard.check_session(_env(tmp_path, QDRANT_URL="http://192.0.2.11:6333", AMS_ALLOW_LIVE_PROD_TESTS="1"))


def test_opt_in_allows_a_remote_target_and_announces_it(tmp_path):
    env = _env(tmp_path, MEM0_URL=NON_LOOPBACK, AMS_ALLOW_LIVE_PROD_TESTS="1")
    assert guard.check_target(env) == NON_LOOPBACK
    assert NON_LOOPBACK in guard.announcement(env)


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_only_the_literal_one_opts_in(tmp_path, value):
    with pytest.raises(guard.LiveGuardRefused):
        guard.check_target(_env(tmp_path, MEM0_URL=NON_LOOPBACK, AMS_ALLOW_LIVE_PROD_TESTS=value))


def test_no_announcement_for_a_loopback_run(tmp_path):
    assert guard.announcement(_env(tmp_path, AMS_ALLOW_LIVE_PROD_TESTS="1")) is None


# ---- tenant ---------------------------------------------------------------------------------

def test_default_test_tenant_is_a_test_tenant(tmp_path):
    assert guard.live_test_tenant(_env(tmp_path)) == "test-live"


def test_stack_tenant_is_refused_from_every_source(tmp_path):
    stack = tmp_path / "stack.env"
    stack.write_text("MEM0_WSL_USER=wsluser\nMEM0_DEFAULT_USER_ID=fileuser\n", encoding="utf-8")
    env = {"MEM0_STACK_ENV": str(stack), "MEM0_DEFAULT_USER_ID": "envuser"}
    for uid in ("envuser", "fileuser", "wsluser"):
        with pytest.raises(guard.LiveGuardRefused, match="own tenant"):
            guard.check_tenant(uid, env)
    with pytest.raises(guard.LiveGuardRefused, match="own tenant"):
        guard.check_tenant(guard.getpass.getuser(), env)


def test_a_non_test_tenant_is_refused(tmp_path):
    with pytest.raises(guard.LiveGuardRefused, match="not a test-"):
        guard.check_tenant("somebody", _env(tmp_path))


def test_test_user_override_must_be_a_test_tenant(tmp_path):
    assert guard.live_test_tenant(_env(tmp_path, MEM0_TEST_USER_ID="test-scratch")) == "test-scratch"
    with pytest.raises(guard.LiveGuardRefused):
        guard.live_test_tenant(_env(tmp_path, MEM0_TEST_USER_ID="opuser"))
    with pytest.raises(guard.LiveGuardRefused):
        guard.check_session(_env(tmp_path, MEM0_TEST_USER_ID="opuser"))


_OPERATOR_TENANT = re.compile(r"""environ\.get\(\s*["']MEM0_DEFAULT_USER_ID|getpass\.getuser|import getuser""")


def test_no_live_suite_takes_its_tenant_from_the_operators_identity():
    """The suites that talk to MEM0_URL write under test-* tenants (live_test_tenant()); one that
    still derived its user_id from MEM0_DEFAULT_USER_ID / the login name wrote 69 debris points
    into the operator's own retrieval namespace."""
    offenders = []
    for path in sorted(HERE.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        if "MEM0_URL" in text and _OPERATOR_TENANT.search(text):
            offenders.append(path.name)
    assert not offenders, f"live suites deriving user_id from the operator identity: {offenders}"


# ---- request guard --------------------------------------------------------------------------

def test_request_with_the_stack_tenant_is_refused_before_send(tmp_path):
    env = _env(tmp_path, MEM0_URL="http://127.0.0.1:18791")
    url = "http://127.0.0.1:18791/v1/memories"
    for body in (
        {"messages": "x", "user_id": "opuser"},
        {"query": "x", "filters": {"user_id": "opuser"}},
        {"a": [{"metadata": {"user_id": "opuser"}}]},
    ):
        with pytest.raises(guard.LiveGuardRefused, match="own tenant"):
            guard.check_request(url, json.dumps(body).encode(), env)
    with pytest.raises(guard.LiveGuardRefused):
        guard.check_request(url + "?user_id=opuser&limit=5", None, env)


def test_request_with_a_test_tenant_or_another_host_is_left_alone(tmp_path):
    env = _env(tmp_path, MEM0_URL="http://127.0.0.1:18791")
    guard.check_request("http://127.0.0.1:18791/v1/memories", b'{"user_id": "test-inv"}', env)
    guard.check_request("http://mock.invalid/v1/memories", b'{"user_id": "opuser"}', env)
    guard.check_request("http://127.0.0.1:18791/x", b"not json", env)


def test_request_guard_wraps_httpx_send_and_restores_it(monkeypatch, tmp_path):
    httpx = pytest.importorskip("httpx")
    for k, v in _env(tmp_path, MEM0_URL="http://127.0.0.1:18791").items():
        monkeypatch.setenv(k, v)
    original = httpx.Client.send
    sent = []
    transport = httpx.MockTransport(lambda req: (sent.append(req), httpx.Response(200))[1])
    with guard.request_guard():
        with httpx.Client(transport=transport) as c:
            with pytest.raises(guard.LiveGuardRefused):
                c.post("http://127.0.0.1:18791/v1/memories", json={"user_id": "opuser"})
            assert sent == [], "the request left the process"
            c.post("http://127.0.0.1:18791/v1/memories", json={"user_id": "test-inv"})
            assert len(sent) == 1
    assert httpx.Client.send is original


# ---- conftest wiring: the run exits before any test body, hence before any HTTP -------------

def _probe_project(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "probe"
    proj.mkdir()
    for name in ("conftest.py", "_live_guard.py", "_home_isolation.py"):
        shutil.copy(HERE / name, proj / name)
    marker = tmp_path / "test-body-ran"
    (proj / "test_probe.py").write_text(
        "import pathlib\n"
        "def test_probe():\n"
        f"    pathlib.Path({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    return proj, marker


def _run_probe(proj: Path, env_extra: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("MEM0_URL", "AMS_ALLOW_LIVE_PROD_TESTS", "MEM0_TEST_USER_ID")}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(proj)],
        capture_output=True, text=True, env=env, timeout=120, cwd=str(proj),
    )


def test_session_exits_before_any_test_body_for_a_remote_target(tmp_path):
    proj, marker = _probe_project(tmp_path)
    r = _run_probe(proj, {"MEM0_URL": NON_LOOPBACK})
    assert r.returncode != 0, r.stdout + r.stderr
    assert "not a loopback" in (r.stdout + r.stderr)
    assert not marker.exists(), "a test body ran against a refused target"


def test_session_runs_for_a_loopback_target(tmp_path):
    proj, marker = _probe_project(tmp_path)
    r = _run_probe(proj, {"MEM0_URL": "http://127.0.0.1:1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert marker.exists()


def test_session_opt_in_runs_and_announces_the_target(tmp_path):
    proj, marker = _probe_project(tmp_path)
    r = _run_probe(proj, {"MEM0_URL": NON_LOOPBACK, "AMS_ALLOW_LIVE_PROD_TESTS": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert marker.exists()
    assert "tests WILL write to " + NON_LOOPBACK in r.stdout


def test_deploy_gate_states_the_opt_in_for_its_own_authority():
    """deploy.sh's post-restart retrieval gate targets the box's own authority, which may bind a
    tailnet address (MEM0_BIND) rather than loopback. The guard would refuse it, so the gate has
    to say it means it - and only on the command that runs the families suite."""
    deploy = (HERE.parents[1] / "scripts" / "wsl" / "deploy.sh").read_text(encoding="utf-8")
    gate = deploy[deploy.index("retrieval families gate (post-restart"):deploy.index("test_retrieval_families.py")]
    assert guard.ALLOW_ENV + "=1" in gate
