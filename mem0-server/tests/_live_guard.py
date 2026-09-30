"""_live_guard.py - the safety interlock for the live-stack pytest suites.

The suites under mem0-server/tests that talk HTTP (test_tier_policy, test_actor_auth,
test_security_invariants, test_episodic, ...) are built to run against a real deployment,
and a run against the production authority lands rows in the memory people actually retrieve
from. Not exporting MEM0_URL used to be the only protection. This module is the rest:

  * a target check  - MEM0_URL (default loopback) must be a loopback host, unless the run says
                      out loud that it means it (AMS_ALLOW_LIVE_PROD_TESTS=1);
  * a tenant check  - the suites write under `test-*` tenants only, never the stack's own
                      tenant (MEM0_DEFAULT_USER_ID, stack.env, or the login user the systemd
                      unit substitutes). Per request to the target: a user_id that is the
                      stack's tenant or does not start with `test-` is refused, and so is an
                      add with no user_id (server default tenant), all before the request
                      leaves the process. Reads and by-id calls that carry no tenant pass.

conftest.py runs the target check at session start (before collection imports a single suite)
and again from a session-scoped autouse fixture, and wraps httpx so the tenant check applies to
every request. Everything here is pure and takes its environment as an argument so the guard
can be tested without touching the real one.
"""
from __future__ import annotations

import contextlib
import getpass
import ipaddress
import json
import os
from pathlib import Path
from typing import Iterator, Mapping, Optional
from urllib.parse import parse_qsl, urlsplit

DEFAULT_URL = "http://127.0.0.1:18791"
ALLOW_ENV = "AMS_ALLOW_LIVE_PROD_TESTS"
TEST_USER_ENV = "MEM0_TEST_USER_ID"
TEST_PREFIX = "test-"
DEFAULT_TEST_TENANT = "test-live"


class LiveGuardRefused(RuntimeError):
    """The run is aimed somewhere, or at someone, a test must not touch."""


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def target_url(env: Optional[Mapping[str, str]] = None) -> str:
    return (_env(env).get("MEM0_URL") or DEFAULT_URL).strip()


def is_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _opted_in(env: Mapping[str, str]) -> bool:
    return (env.get(ALLOW_ENV) or "").strip() == "1"


def _host_of(url: str) -> Optional[str]:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def check_target(env: Optional[Mapping[str, str]] = None) -> str:
    """Return the resolved MEM0_URL, or raise LiveGuardRefused when it is not loopback and the
    run has not opted in. Pure: no network."""
    e = _env(env)
    url = target_url(e)
    if is_loopback_host(_host_of(url)) or _opted_in(e):
        return url
    raise LiveGuardRefused(
        f"MEM0_URL={url!r} is not a loopback address. The live-stack suites write test rows "
        f"into whatever they are pointed at; refusing before any request. Point MEM0_URL at a "
        f"scratch instance on this machine, or set {ALLOW_ENV}=1 to say you mean to run "
        f"against that target."
    )


def check_qdrant(env: Optional[Mapping[str, str]] = None) -> None:
    """A few suites write the vector store directly (tier seeds); QDRANT_URL gets the same rule."""
    e = _env(env)
    url = (e.get("QDRANT_URL") or "").strip()
    if not url or is_loopback_host(_host_of(url)) or _opted_in(e):
        return
    raise LiveGuardRefused(
        f"QDRANT_URL={url!r} is not a loopback address; refusing before any request "
        f"(set {ALLOW_ENV}=1 to say you mean to write to it)"
    )


def _stack_env_file(env: Mapping[str, str]) -> Path:
    explicit = (env.get("MEM0_STACK_ENV") or "").strip()
    return Path(explicit) if explicit else Path.home() / ".mem0" / "stack.env"


def stack_tenants(env: Optional[Mapping[str, str]] = None) -> set[str]:
    """Every user_id the stack itself treats as the operator's tenant: the env var, stack.env,
    and the login user (the systemd unit substitutes it when neither is set)."""
    e = _env(env)
    out = {(e.get("MEM0_DEFAULT_USER_ID") or "").strip()}
    try:
        for line in _stack_env_file(e).read_text(encoding="utf-8").splitlines():
            key, _, val = line.strip().partition("=")
            if key.strip() in ("MEM0_DEFAULT_USER_ID", "MEM0_WSL_USER"):
                out.add(val.strip())
    except OSError:
        pass
    try:
        out.add(getpass.getuser())
    except Exception:  # no passwd entry in a bare container
        pass
    out.discard("")
    return out


def check_tenant(user_id: str, env: Optional[Mapping[str, str]] = None) -> str:
    """A test tenant must look like one and must not be the stack's own."""
    uid = (user_id or "").strip()
    if uid in stack_tenants(env):
        raise LiveGuardRefused(
            f"user_id {uid!r} is the stack's own tenant; live suites write under {TEST_PREFIX}* "
            f"tenants only"
        )
    if not uid.startswith(TEST_PREFIX):
        raise LiveGuardRefused(f"user_id {uid!r} is not a {TEST_PREFIX}* tenant")
    return uid


def live_test_tenant(env: Optional[Mapping[str, str]] = None) -> str:
    """The tenant a live suite writes under: MEM0_TEST_USER_ID when set (it must pass
    check_tenant), else test-live. Never derived from the operator's own identity."""
    e = _env(env)
    chosen = (e.get(TEST_USER_ENV) or "").strip()
    return check_tenant(chosen, e) if chosen else DEFAULT_TEST_TENANT


def check_session(env: Optional[Mapping[str, str]] = None) -> str:
    """Everything decidable before the first test: the target, and the tenant override."""
    e = _env(env)
    url = check_target(e)
    check_qdrant(e)
    if (e.get(TEST_USER_ENV) or "").strip():
        live_test_tenant(e)
    return url


def announcement(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """One line for the pytest header when the run is allowed only by the opt-in."""
    e = _env(env)
    url = target_url(e)
    if _opted_in(e) and not is_loopback_host(_host_of(url)):
        return f"live-suite guard: {ALLOW_ENV}=1 - tests WILL write to {url}"
    return None


def _user_ids(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "user_id" and isinstance(v, str):
                yield v
            else:
                yield from _user_ids(v)
    elif isinstance(node, list):
        for v in node:
            yield from _user_ids(v)


_ADD_PATHS = ("/v1/memories", "/memories")


def check_request(url: str, body: Optional[bytes], env: Optional[Mapping[str, str]] = None,
                  method: str = "GET") -> None:
    """Refuse, before it is sent, a request to the live target that is not confined to a test
    tenant: one carrying the stack's own tenant, one carrying any user_id that is not `test-*`,
    and an add (POST /v1/memories) carrying no user_id at all (it would land in the server's
    default tenant, which on a brain is the operator's). Requests to anything else (mock
    transports, fixtures) are not judged. Other calls without a user_id (health, by-id reads,
    deletes, tier patches, diagnose) are legitimate and pass: the tenant is not on the wire."""
    e = _env(env)
    target = urlsplit(target_url(e))
    req = urlsplit(url)
    if (req.hostname, req.port) != (target.hostname, target.port):
        return
    seen = [v for k, v in parse_qsl(req.query) if k == "user_id"]
    parsed: object = None
    if body:
        try:
            parsed = json.loads(body)
            seen.extend(_user_ids(parsed))
        except (ValueError, UnicodeDecodeError):
            pass
    tenants = stack_tenants(e)
    for uid in seen:
        if uid.strip() in tenants:
            raise LiveGuardRefused(
                f"refusing a request carrying user_id {uid!r}: that is the stack's own tenant, "
                f"and a live suite must only touch {TEST_PREFIX}* tenants"
            )
        if not uid.startswith(TEST_PREFIX):
            raise LiveGuardRefused(
                f"refusing a request carrying user_id {uid!r}: a live suite must only touch "
                f"{TEST_PREFIX}* tenants"
            )
    if (not seen and method.upper() == "POST" and isinstance(parsed, dict)
            and req.path.rstrip("/") in _ADD_PATHS):
        raise LiveGuardRefused(
            f"refusing an add with no user_id: it would land in the server's default tenant; "
            f"a live suite must name a {TEST_PREFIX}* tenant (live_test_tenant())"
        )


@contextlib.contextmanager
def request_guard() -> Iterator[None]:
    """Wrap httpx.Client.send so check_request runs before any byte leaves the process."""
    try:
        import httpx
    except ImportError:  # the guard has nothing to wrap
        yield
        return
    original = httpx.Client.send

    def guarded(self, request, *args, **kwargs):
        # A streamed body is not readable here (RequestNotRead); the tenant then rides in the
        # URL or not at all, so judge the URL only. httpx.AsyncClient is not wrapped: no live
        # suite uses it.
        try:
            body = request.content
        except httpx.RequestNotRead:
            body = None
        check_request(str(request.url), body, method=request.method)
        return original(self, request, *args, **kwargs)

    httpx.Client.send = guarded
    try:
        yield
    finally:
        httpx.Client.send = original
