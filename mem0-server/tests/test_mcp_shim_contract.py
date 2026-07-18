"""MEM-19 (2026-07-03): the MCP shim stamps hook_contract_version.

The shim (scripts/wsl/mem0-mcp-shim.py) was the last field-less high-traffic
caller of /v1/memories/search and /v1/context/bundle — every MCP search/recall
incremented /health/deep checks.hook_contract.missing. It now stamps:
  '17.0' (search wire contract — same as pre-tool-check.ps1) on search POSTs,
  '20.0' (batched bundle contract — same as user-prompt-extract.ps1) on the
  recall bundle POST, and forward-stamps the add POST.

Two layers:
  * parity — the shim's constants MUST be members of hook_contract.py's
    KNOWN_HOOK_CONTRACT_VERSIONS (an unknown version would flip the WARN
    counter this change exists to zero; the KNOWN set is only ever extended in
    the same commit that bumps a wire contract, v0.19 M15 rule).
  * behavior — the underlying tool functions actually send the field, proven
    by intercepting the shim module's httpx.request (all reads and mutations
    route through it since offline-first C1).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from hook_contract import KNOWN_HOOK_CONTRACT_VERSIONS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM_PATH = REPO_ROOT / "scripts" / "wsl" / "mem0-mcp-shim.py"


@pytest.fixture(scope="module")
def shim():
    """Load the shim module (hyphenated filename -> importlib). Import needs
    fastmcp + ~/.mem0/api-key — both present in the WSL gate env; skip
    gracefully anywhere else so the suite stays runnable on a bare checkout."""
    try:
        spec = importlib.util.spec_from_file_location("mem0_mcp_shim_under_test", SHIM_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except (ImportError, SystemExit) as e:
        pytest.skip(f"shim not importable here: {e}")
    return mod


def _tool_fn(tool):
    """fastmcp 3.x @mcp.tool wraps the function in a FunctionTool; unwrap."""
    return getattr(tool, "fn", tool)


class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {"ok": True, "results": []}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_shim_versions_are_known_to_the_server(shim):
    """Parity pin: a shim stamp outside the KNOWN set would log WARN drift on
    every MCP call — the exact noise MED-17 reserves for real skew."""
    assert shim.SEARCH_HOOK_CONTRACT_VERSION in KNOWN_HOOK_CONTRACT_VERSIONS
    assert shim.BUNDLE_HOOK_CONTRACT_VERSION in KNOWN_HOOK_CONTRACT_VERSIONS
    # the documented pairing: search wire == pre-tool-check's '17.0',
    # bundle wire == user-prompt-extract's '20.0'
    assert shim.SEARCH_HOOK_CONTRACT_VERSION == "17.0"
    assert shim.BUNDLE_HOOK_CONTRACT_VERSION == "20.0"


def test_memory_search_posts_search_contract_version(shim, monkeypatch):
    posts = []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        posts.append((url, json))
        return _FakeResp()

    # Reads route through _request -> httpx.request (multi-method entry point);
    # patch that, not httpx.post (offline-first C1).
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    _tool_fn(shim.memory_search)("where is the token", limit=2)
    assert len(posts) == 1
    url, payload = posts[0]
    assert url.endswith("/v1/memories/search")
    assert payload["hook_contract_version"] == shim.SEARCH_HOOK_CONTRACT_VERSION


def test_memory_recall_posts_bundle_and_search_contract_versions(shim, monkeypatch):
    posts = []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        posts.append((url, json))
        return _FakeResp({"memories": [], "goals": [], "open_questions": [], "results": []})

    # recall's bundle + canonical-search reads route through _request -> httpx.request.
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    out = _tool_fn(shim.memory_recall)("what ports are locked")
    assert out["ok"] is True
    by_url = {url.rsplit("/", 1)[-1]: payload for url, payload in posts}
    assert by_url["bundle"]["hook_contract_version"] == shim.BUNDLE_HOOK_CONTRACT_VERSION
    assert by_url["search"]["hook_contract_version"] == shim.SEARCH_HOOK_CONTRACT_VERSION
    # recall stays side-effect-free: checkpoint suppressed on the bundle POST
    assert by_url["bundle"]["checkpoint"] is False


def test_memory_add_forward_stamps_contract_version(shim, monkeypatch):
    posts = []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        posts.append((url, json))
        return _FakeResp({"results": [{"id": "x"}]})

    # Mutations route through _authority_only -> httpx.request (offline-first C1 Task 2);
    # patch that, not httpx.post.
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    _tool_fn(shim.memory_add)("a fact", infer=False)
    url, payload = posts[0]
    assert url.endswith("/v1/memories")
    # AddIn ignores extras today (pydantic) — the stamp is forward-compliance
    assert payload["hook_contract_version"] == shim.SEARCH_HOOK_CONTRACT_VERSION


# ---------------------------------------------------------------------------
# MEM-8 (2026-07-03): shim ergonomics — brandless fail-closed hides get a hint.
# ---------------------------------------------------------------------------

def test_memory_search_hints_when_brand_scoped_records_hidden(shim, monkeypatch):
    monkeypatch.setattr(shim.httpx, "request", lambda method, url, json=None, params=None, headers=None, timeout=None:
                        _FakeResp({"results": [], "rejected_brand_scoped": 3}))
    out = _tool_fn(shim.memory_search)("brand-a pen specs")
    assert out["hint"] == "3 brand-scoped records were hidden — pass brand= or use memory_recall"


def test_memory_search_no_hint_when_nothing_hidden(shim, monkeypatch):
    """0 hides -> no hint key (don't add noise to every clean search); a
    pre-remediation server response without the field behaves the same."""
    monkeypatch.setattr(shim.httpx, "request", lambda method, url, json=None, params=None, headers=None, timeout=None:
                        _FakeResp({"results": [], "rejected_brand_scoped": 0}))
    assert "hint" not in _tool_fn(shim.memory_search)("anything")
    monkeypatch.setattr(shim.httpx, "request", lambda method, url, json=None, params=None, headers=None, timeout=None:
                        _FakeResp({"results": []}))   # old server: field absent
    assert "hint" not in _tool_fn(shim.memory_search)("anything")
