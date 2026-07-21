# mem0-server/tests/test_shim_offline.py
from __future__ import annotations
import importlib.util
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM_PATH = REPO_ROOT / "scripts" / "wsl" / "mem0-mcp-shim.py"

@pytest.fixture()
def shim(monkeypatch, tmp_path):
    monkeypatch.setenv("MEM0_URL", "http://authority.invalid:18791")
    # api-key file is required at import; point HOME at a tmp dir with one
    monkeypatch.setenv("HOME", str(tmp_path))
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
    monkeypatch.setenv("HOME", str(tmp_path))
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
