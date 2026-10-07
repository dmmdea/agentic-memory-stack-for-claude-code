# mem0-server/tests/test_shim_offline.py
from __future__ import annotations
import importlib.util
import re
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM_PATH = REPO_ROOT / "scripts" / "wsl" / "mem0-mcp-shim.py"
APP_PATH = REPO_ROOT / "mem0-server" / "app.py"


from _home_isolation import apply_home  # noqa: E402


def test_no_endpoint_raises_a_bare_500_for_an_unclassified_exception():
    """The shim's 503 handling below is only worth anything if the server actually
    SENDS 503 rather than a flat 500. Every endpoint's generic handler must route
    through app._upstream_error; one missed site is one endpoint that still drops
    writes during a 429 burst.

    Source-level on purpose: `import app` builds the live Memory client, so it cannot
    run on a headless CI runner — but this guard must. The behavioural counterpart
    lives in test_upstream_rate_limit_status.py (local-only)."""
    src = APP_PATH.read_text(encoding="utf-8")
    stragglers = re.findall(r"raise HTTPException\(\s*500\s*,\s*str\(e\)\s*\)", src)
    assert not stragglers, (
        f"{len(stragglers)} endpoint(s) still raise a bare 500 for an unclassified "
        "exception; route them through _upstream_error(e)")

@pytest.fixture()
def shim(monkeypatch, tmp_path):
    monkeypatch.setenv("MEM0_URL", "http://authority.invalid:18791")
    # api-key file is required at import; point HOME at a tmp dir with one
    apply_home(monkeypatch, tmp_path)
    (tmp_path / ".mem0").mkdir()
    (tmp_path / ".mem0" / "api-key").write_text("test-key", encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("shim_ut", SHIM_PATH)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    except Exception as e:
        pytest.skip(f"shim import needs fastmcp: {e}")
    return mod

def test_request_fails_over_to_local_on_connect_error(shim, monkeypatch):
    import httpx
    calls = []
    def fake_request(method, url, **kw):
        calls.append(url)
        if url.startswith(shim.AUTHORITY_URL):
            raise httpx.ConnectError("refused")
        req = httpx.Request(method, url)
        return httpx.Response(200, json={"results": [{"memory": "x"}]}, request=req)
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    payload, source = shim._request("POST", "/v1/memories/search", json={"query": "q"})
    assert source == "local-replica"
    assert any(u.startswith(shim.LOCAL_URL) for u in calls)

def test_request_does_not_fail_over_on_http_status(shim, monkeypatch):
    import httpx
    def fake_request(method, url, **kw):
        req = httpx.Request(method, url)
        return httpx.Response(500, json={"detail": "boom"}, request=req)
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    with pytest.raises(httpx.HTTPStatusError):
        shim._request("POST", "/v1/memories/search", json={"query": "q"})

def test_request_fails_over_to_local_on_503(shim, monkeypatch):
    """503 is the server saying "not an answer, ask again" (app.py._upstream_error,
    raised when llama-swap 429s outlive the embedder's bounded retry). Unlike every
    other status it must NOT be handed to the caller as a real answer."""
    import httpx
    calls = []
    def fake_request(method, url, **kw):
        calls.append(url)
        req = httpx.Request(method, url)
        if url.startswith(shim.AUTHORITY_URL):
            return httpx.Response(503, json={"detail": "upstream embedder rate-limited"},
                                  headers={"Retry-After": "1"}, request=req)
        return httpx.Response(200, json={"results": [{"memory": "x"}]}, request=req)
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    payload, source = shim._request("POST", "/v1/memories/search", json={"query": "q"})
    assert source == "local-replica"
    assert any(u.startswith(shim.LOCAL_URL) for u in calls)


def test_request_both_503_raises_offline_error(shim, monkeypatch):
    """Authority AND replica rate-limited => offline, not a masked 503."""
    import httpx
    def fake_request(method, url, **kw):
        req = httpx.Request(method, url)
        return httpx.Response(503, json={"detail": "rate-limited"}, request=req)
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    with pytest.raises(shim.OfflineError):
        shim._request("POST", "/v1/memories/search", json={"query": "q"})


def test_authority_only_503_raises_offline_error_so_the_write_queues(shim, monkeypatch):
    """The data-loss fix. A write meeting a 503 must raise OfflineError, because every
    mutation tool catches exactly that to queue into the outbox. Before this, the 503's
    predecessor (a flat 500) escaped as HTTPStatusError past those handlers and the
    write was DROPPED — a memory add was lost that way."""
    import httpx
    def fake_request(method, url, **kw):
        req = httpx.Request(method, url)
        return httpx.Response(503, json={"detail": "upstream embedder rate-limited"},
                              headers={"Retry-After": "1"}, request=req)
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    with pytest.raises(shim.OfflineError):
        shim._authority_only("POST", "/v1/memories", json={"messages": "m", "user_id": "u"})


def test_authority_only_500_still_propagates_and_never_queues(shim, monkeypatch):
    """Scope guard: ONLY 503 became retryable. A 500 is still a real answer — queueing
    it would replay a genuinely bad op forever."""
    import httpx
    def fake_request(method, url, **kw):
        req = httpx.Request(method, url)
        return httpx.Response(500, json={"detail": "boom"}, request=req)
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    with pytest.raises(httpx.HTTPStatusError):
        shim._authority_only("POST", "/v1/memories", json={"messages": "m", "user_id": "u"})


def test_read_timeout_propagates_and_never_fails_over(shim, monkeypatch):
    # A ReadTimeout means the authority ACCEPTED the connection and is merely slow —
    # failing over would mask a real answer with a stale replica read. It must escape.
    import httpx
    calls = []
    def fake_request(method, url, **kw):
        calls.append(url)
        raise httpx.ReadTimeout("authority slow")
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    with pytest.raises(httpx.ReadTimeout):
        shim._request("POST", "/v1/memories/search", json={"query": "q"})
    assert calls and all(u.startswith(shim.AUTHORITY_URL) for u in calls)
    assert not any(u.startswith(shim.LOCAL_URL) for u in calls)

def test_memory_add_queues_offline(shim, monkeypatch, tmp_path):
    import httpx
    monkeypatch.setattr(shim, "OUTBOX", tmp_path / "outbox.jsonl")
    monkeypatch.setattr(shim.httpx, "request",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    res = shim.memory_add(text="offline fact", metadata={"tier": "evidence"})
    assert res["event"] == "QUEUED_OFFLINE" and res["op"] == "add"
    lines = (tmp_path / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    import json
    rec = json.loads(lines[0]); assert rec["op"] == "add" and rec["args"]["text"] == "offline fact"

def test_memory_delete_queues_offline(shim, monkeypatch, tmp_path):
    import httpx, json
    monkeypatch.setattr(shim, "OUTBOX", tmp_path / "outbox.jsonl")
    monkeypatch.setattr(shim.httpx, "request",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    res = shim.memory_delete(memory_id="abc")
    assert res["event"] == "QUEUED_OFFLINE" and res["op"] == "delete"
    rec = json.loads((tmp_path / "outbox.jsonl").read_text().splitlines()[0])
    assert rec["args"]["memory_id"] == "abc"

def test_offline_search_merges_pending_adds(shim, monkeypatch, tmp_path):
    import httpx, json
    ob = tmp_path / "outbox.jsonl"
    ob.write_text(json.dumps({"op": "add", "args": {"text": "the reranker is bge"}, "queued_ts": "t", "key": "k"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(shim, "OUTBOX", ob)
    def fake_request(method, url, **kw):
        if url.startswith(shim.AUTHORITY_URL):
            raise httpx.ConnectError("refused")
        req = httpx.Request(method, url)
        return httpx.Response(200, json={"results": []}, request=req)
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    data = shim.memory_search(query="reranker")
    assert any(r.get("pending_sync") for r in data["results"])


# --- 2026-07-21: authority resolution ---------------------------------------------------------
# The bug this covers: the shim resolved its authority from MEM0_URL, but the MCP entry launches
# it as `wsl.exe -d <distro> -e <python> <shim>`, which execs the binary directly — no login
# shell, no WSLENV pass-through — so the env var never arrived. The replica fell back to loopback,
# found no local server, and returned QUEUED_OFFLINE on every write.

def _load_shim(tmp_path, monkeypatch, env_url, file_url):
    """Import a fresh shim with HOME pointed at tmp_path, and MEM0_URL / the authority file set
    (or absent) as specified. Returns the module, or skips if fastmcp is unavailable."""
    apply_home(monkeypatch, tmp_path)
    mem0 = tmp_path / ".mem0"
    mem0.mkdir(exist_ok=True)
    (mem0 / "api-key").write_text("test-key", encoding="utf-8")
    if env_url is None:
        monkeypatch.delenv("MEM0_URL", raising=False)
    else:
        monkeypatch.setenv("MEM0_URL", env_url)
    if file_url is not None:
        (mem0 / "authority-url").write_text(file_url, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("shim_auth_ut", SHIM_PATH)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    except Exception as e:
        pytest.skip(f"shim import needs fastmcp: {e}")
    return mod


def test_drain_triggers_on_stranded_replaying_file(shim, monkeypatch, tmp_path):
    """2026-09-01: a drain that stops on a retryable 503 keeps its unfinished ops in
    outbox.replaying.jsonl and deletes/empties outbox.jsonl. The startup trigger
    checked only outbox.jsonl, so the stranded ops never got a resume — one queued
    update sat 15 hours across many session starts. The trigger must fire for a
    non-empty .replaying file, and still stay quiet when neither file has content."""
    spawned = []
    monkeypatch.setattr(shim.subprocess, "Popen",
                        lambda *a, **kw: spawned.append(a) or None)
    ob = tmp_path / "outbox.jsonl"
    monkeypatch.setattr(shim, "OUTBOX", ob)
    # neither file → no drain
    shim._drain_outbox_async()
    assert spawned == []
    # only a stranded .replaying file → drain fires
    ob.with_suffix(".replaying.jsonl").write_text('{"op":"update"}\n', encoding="utf-8")
    shim._drain_outbox_async()
    assert len(spawned) == 1, "a stranded outbox.replaying.jsonl must trigger the startup drain"
    # classic outbox.jsonl path still fires
    ob.write_text('{"op":"add"}\n', encoding="utf-8")
    shim._drain_outbox_async()
    assert len(spawned) == 2


def test_authority_file_is_used_when_env_is_absent(tmp_path, monkeypatch):
    """The core fix: with no MEM0_URL in the environment — exactly how the shim is launched —
    the authority still resolves to the brain instead of loopback."""
    mod = _load_shim(tmp_path, monkeypatch, env_url=None, file_url="http://brain-host:18791\n")
    assert mod.AUTHORITY_URL == "http://brain-host:18791"


def test_env_overrides_the_authority_file(tmp_path, monkeypatch):
    """MEM0_URL stays an ad-hoc override for one-off runs."""
    mod = _load_shim(tmp_path, monkeypatch, env_url="http://override:18791",
                     file_url="http://brain-host:18791\n")
    assert mod.AUTHORITY_URL == "http://override:18791"


def test_falls_back_to_loopback_with_neither(tmp_path, monkeypatch):
    """A single-machine install has no authority file and needs no configuration."""
    mod = _load_shim(tmp_path, monkeypatch, env_url=None, file_url=None)
    assert mod.AUTHORITY_URL == "http://127.0.0.1:18791"


def test_authority_file_ignores_comments_blanks_and_trailing_slash(tmp_path, monkeypatch):
    mod = _load_shim(tmp_path, monkeypatch, env_url=None,
                     file_url="# written by the installer\n\nhttp://brain-host:18791/\n")
    assert mod.AUTHORITY_URL == "http://brain-host:18791"


# ---------------------------------------------------------------------------------------------
# 1.32.6: the shim on a native Linux authority. The API key is a systemd credential (a file named
# by MEM0_API_KEY_FILE, no ~/.mem0/api-key) and install/linux-authority.sh copies the script raw, so
# the tenant placeholder that the other installers substitute stays in the signature defaults.
# The replay script's twin of these tests lives in test_replay_ops.py.
# ---------------------------------------------------------------------------------------------

_SENTINEL = "__WSL_USER__"   # tests are never deployed, so the literal may appear here
_TOKENS = ("__WSL_USER__", "__WIN_USER__", "__WSL_DISTRO__")


def _load_native(name="shim_native_ut", path=SHIM_PATH):
    pytest.importorskip("fastmcp")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def nhome(monkeypatch, tmp_path):
    """A redirected home with an empty ~/.mem0 and none of the resolver's env inputs set."""
    monkeypatch.setenv("MEM0_URL", "http://authority.invalid:18791")
    for var in ("MEM0_API_KEY_FILE", "MEM0_DEFAULT_USER_ID", "MEM0_KEY", "MEM0_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    h = apply_home(monkeypatch, tmp_path / "home")
    (h / ".mem0").mkdir(parents=True)
    return h


def _credential(tmp_path, text="credential-key"):
    p = tmp_path / "creds" / "ams-api-key"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text + "\n", encoding="utf-8")
    return p


def test_shim_credential_file_beats_the_home_key(nhome, monkeypatch, tmp_path):
    (nhome / ".mem0" / "api-key").write_text("home-key", encoding="utf-8")
    monkeypatch.setenv("MEM0_API_KEY_FILE", str(_credential(tmp_path)))
    assert _load_native()._headers()["X-API-Key"] == "credential-key"


def test_shim_import_succeeds_with_only_the_credential_file(nhome, monkeypatch, tmp_path):
    assert not (nhome / ".mem0" / "api-key").exists()
    monkeypatch.setenv("MEM0_API_KEY_FILE", str(_credential(tmp_path)))
    assert _load_native()._headers()["X-API-Key"] == "credential-key"


@pytest.mark.parametrize("bad", ["empty", "blank", "missing", "unreadable-dir", "not-utf8"])
def test_shim_unusable_credential_file_falls_back_to_the_home_key(bad, nhome, monkeypatch, tmp_path):
    (nhome / ".mem0" / "api-key").write_text("home-key\n", encoding="utf-8")
    p = tmp_path / "creds" / "ams-api-key"
    p.parent.mkdir()
    if bad == "empty":
        p.write_text("", encoding="utf-8")
    elif bad == "blank":
        p.write_text("  \n", encoding="utf-8")
    elif bad == "unreadable-dir":
        p.mkdir()                      # reading a directory raises OSError on every platform
    elif bad == "not-utf8":
        p.write_bytes(bytes([0xFF, 0xFE, 0x80]) + b" not text")
    monkeypatch.setenv("MEM0_API_KEY_FILE", str(p))
    assert _load_native()._headers()["X-API-Key"] == "home-key"


def test_shim_no_key_anywhere_is_a_fail_exit(nhome):
    with pytest.raises(SystemExit) as ei:
        _load_native()
    assert "mem0 API key not found" in str(ei.value)


def test_shim_empty_credential_and_no_home_key_is_a_fail_exit(nhome, monkeypatch, tmp_path):
    monkeypatch.setenv("MEM0_API_KEY_FILE", str(_credential(tmp_path, text="")))
    with pytest.raises(SystemExit):
        _load_native()


def test_shim_never_reads_the_key_from_a_plaintext_env_var(nhome, monkeypatch):
    monkeypatch.setenv("MEM0_KEY", "env-key")
    monkeypatch.setenv("MEM0_API_KEY", "env-key")
    with pytest.raises(SystemExit):
        _load_native()


def _stack_env(home, text):
    (home / ".mem0" / "stack.env").write_text(text, encoding="utf-8")


def _user_ids(obj):
    """Every user_id value anywhere in a captured call (json body, params, nested filters)."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "user_id":
                found.append(v)
            found.extend(_user_ids(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found.extend(_user_ids(v))
    return found


@pytest.fixture()
def nshim(nhome):
    (nhome / ".mem0" / "api-key").write_text("home-key", encoding="utf-8")
    return _load_native()


@pytest.fixture()
def ncaptured(nshim, monkeypatch):
    calls = []

    def fake_request(method, path, **kw):
        calls.append({"path": path, **kw})
        return {"results": [], "memories": []}, "authority"

    def fake_authority_only(method, path, **kw):
        calls.append({"path": path, **kw})
        return {"results": []}

    monkeypatch.setattr(nshim, "_request", fake_request)
    monkeypatch.setattr(nshim, "_authority_only", fake_authority_only)
    return calls


_TENANT_TOOLS = {
    "memory_add": lambda s, **kw: s.memory_add(text="a fact", **kw),
    "memory_search": lambda s, **kw: s.memory_search(query="q", **kw),
    "memory_recall": lambda s, **kw: s.memory_recall(query="q", **kw),
    "memory_list": lambda s, **kw: s.memory_list(**kw),
    "memory_diagnose": lambda s, **kw: s.memory_diagnose(query="q", target_id="t", **kw),
}


def test_shim_tenant_tools_are_exactly_the_five_with_the_placeholder_default():
    src = SHIM_PATH.read_text(encoding="utf-8")
    owners = set(re.findall(r"def (\w+)\([^)]*user_id: str = \"" + _SENTINEL + r"\"", src, flags=re.S))
    assert owners == set(_TENANT_TOOLS)


@pytest.mark.parametrize("tool", sorted(_TENANT_TOOLS))
def test_shim_stack_env_tenant_is_used_for_the_default_placeholder(tool, nshim, ncaptured, nhome):
    _stack_env(nhome, "# written by the installer\nMEM0_HOST_KIND=native\nMEM0_WSL_USER = 'tenant-a'\nOTHER=x\n")
    _TENANT_TOOLS[tool](nshim)                  # the default: the placeholder, exactly as installed
    ids = _user_ids(ncaptured)
    assert ids and set(ids) == {"tenant-a"}, ncaptured


@pytest.mark.parametrize("tool", sorted(_TENANT_TOOLS))
def test_shim_env_tenant_beats_stack_env(tool, nshim, ncaptured, nhome, monkeypatch):
    _stack_env(nhome, "MEM0_WSL_USER=tenant-a\n")
    monkeypatch.setenv("MEM0_DEFAULT_USER_ID", "tenant-env")
    _TENANT_TOOLS[tool](nshim)
    assert set(_user_ids(ncaptured)) == {"tenant-env"}


@pytest.mark.parametrize("tool", sorted(_TENANT_TOOLS))
def test_shim_explicit_user_id_is_untouched(tool, nshim, ncaptured, nhome, monkeypatch):
    _stack_env(nhome, "MEM0_WSL_USER=tenant-a\n")
    monkeypatch.setenv("MEM0_DEFAULT_USER_ID", "tenant-env")
    _TENANT_TOOLS[tool](nshim, user_id="explicit-tenant")
    assert set(_user_ids(ncaptured)) == {"explicit-tenant"}


@pytest.mark.parametrize("tool", sorted(_TENANT_TOOLS))
@pytest.mark.parametrize("stack_env", [None, "# nothing useful\nMEM0_HOST_KIND=native\nMEM0_WSL_USER=\n"])
def test_shim_unresolvable_placeholder_passes_through(tool, stack_env, nshim, ncaptured, nhome):
    if stack_env is not None:
        _stack_env(nhome, stack_env)
    _TENANT_TOOLS[tool](nshim)
    assert set(_user_ids(ncaptured)) == {_SENTINEL}


def test_shim_tenant_resolver_only_acts_on_the_placeholder_shape(nshim, nhome):
    _stack_env(nhome, 'MEM0_WSL_USER="tenant-a"\n')
    assert nshim._resolve_tenant(_SENTINEL) == "tenant-a"
    assert nshim._resolve_tenant("__WIN_USER__") == "tenant-a"           # the shape, not three names
    for untouched in ("tenant-b", "", "__lower__", "_X_", "x__ABC__", "__ABC__x", "__", "____"):
        assert nshim._resolve_tenant(untouched) == untouched
    assert nshim._resolve_tenant(None) is None


def test_shim_keeps_the_placeholder_literal_in_the_five_signature_defaults():
    """install/linux-client.sh and the Windows installer substitute it (test_linux_client_installer pins
    that they do); resolving at runtime must not remove what they substitute."""
    text = SHIM_PATH.read_text(encoding="utf-8")
    assert text.count(_SENTINEL) == 5
    assert text.count("__WIN_USER__") == 0 and text.count("__WSL_DISTRO__") == 0


def test_the_linux_client_substitution_leaves_no_sentinel_in_the_shim():
    """install/linux-client.sh runs sed "s|__WSL_USER__|$USER_ID|g" and hard-fails when any of the three
    tokens is still present, so no new code, docstring or comment may carry one."""
    deployed = SHIM_PATH.read_text(encoding="utf-8").replace(_SENTINEL, "someuser")
    assert not any(tok in deployed for tok in _TOKENS)


def test_a_substituted_shim_resolves_nothing_and_keeps_its_tenant(nhome, monkeypatch, tmp_path):
    """The sed-installed copy (a Windows or thin-client host) behaves as before: its default is a real
    tenant, so stack.env and the env are never consulted."""
    (nhome / ".mem0" / "api-key").write_text("home-key", encoding="utf-8")
    _stack_env(nhome, "MEM0_WSL_USER=tenant-a\n")
    monkeypatch.setenv("MEM0_DEFAULT_USER_ID", "tenant-env")
    deployed = tmp_path / "mem0-mcp-shim-deployed.py"
    deployed.write_text(SHIM_PATH.read_text(encoding="utf-8").replace(_SENTINEL, "someuser"), encoding="utf-8")
    mod = _load_native("shim_substituted_ut", deployed)
    calls = []
    monkeypatch.setattr(mod, "_request", lambda m, p, **kw: (calls.append(kw) or {"results": []}, "authority"))
    mod.memory_list()
    assert set(_user_ids(calls)) == {"someuser"}
