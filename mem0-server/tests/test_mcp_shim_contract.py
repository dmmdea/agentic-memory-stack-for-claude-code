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
    # status_code is part of the httpx.Response surface the shim reads: since
    # 2026-07-26 it checks for a retryable 503 before trusting the body. A stand-in
    # missing a field the real object always carries is a defect in the stand-in.
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {"ok": True, "results": []}
        self.status_code = status_code

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


def test_memory_health_probes_the_deep_endpoint_that_actually_embeds(shim, monkeypatch):
    """A health tool that cannot see a broken write path is worse than none: it
    actively certifies the outage. Shallow /health returns a STATIC dict — no
    Qdrant call, no embed — so it answered ok=True through a live 429 burst that
    had memory_add returning 500. Only /health/deep embeds (asserting the 768-dim
    vector) and touches the store, so that is what this tool must probe."""
    seen = []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        seen.append((method, url))
        return _FakeResp({"ok": True, "checks": {"embedder": {"ok": True, "dim": 768}}})

    monkeypatch.setattr(shim.httpx, "request", fake_request)
    _tool_fn(shim.memory_health)()
    assert len(seen) == 1
    method, url = seen[0]
    assert method == "GET"
    assert url.endswith("/health/deep"), (
        f"memory_health probed {url!r}; the shallow /health never embeds and would "
        "report ok=True while the write path is broken")


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
# 2026-08-24: oversize ADVISORY on direct saves (never a split, never a reject —
# the server deliberately accepts up to 4000; the writer knows the semantics).
# ---------------------------------------------------------------------------

def _ok_add(monkeypatch, shim):
    posts = []
    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        posts.append((url, json))
        return _FakeResp({"results": [{"id": "x"}]})
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    return posts


def test_oversize_direct_save_is_stored_intact_with_advisory(shim, monkeypatch):
    posts = _ok_add(monkeypatch, shim)
    text = "y" * (shim.OVERSIZE_ADVISORY_CHARS + 100)
    out = _tool_fn(shim.memory_add)(text, infer=False)
    assert posts[0][1]["messages"] == text, "the record must reach the server UNSPLIT"
    assert "stored INTACT" in out["note"] and f"{len(text)} chars" in out["note"]
    assert "advisory" in out["note"]


def test_oversize_advisory_not_emitted_at_or_below_the_line(shim, monkeypatch):
    _ok_add(monkeypatch, shim)
    out = _tool_fn(shim.memory_add)("y" * shim.OVERSIZE_ADVISORY_CHARS, infer=False)
    assert "note" not in out


def test_oversize_advisory_skipped_for_infer_true(shim, monkeypatch):
    """infer=True hands the text to the server's LLM extraction, which reshapes it
    into atomic facts itself — advising the writer to split would be noise."""
    _ok_add(monkeypatch, shim)
    out = _tool_fn(shim.memory_add)("y" * 3000, infer=True)
    assert "note" not in out


def test_oversize_advisory_composes_with_tier_downgrade_note(shim, monkeypatch):
    _ok_add(monkeypatch, shim)
    out = _tool_fn(shim.memory_add)("y" * 2000, infer=False, metadata={"tier": "canonical"})
    assert "auto-downgraded" in out["note"] and "stored INTACT" in out["note"]
    assert " | " in out["note"], "the documented join separator (api-contracts.md)"


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


# ---------------------------------------------------------------------------
# W5 T1.2/T2.2 (ADOPT-2/3): degradation + withheld notes, age summary,
# memory_diagnose.
# ---------------------------------------------------------------------------

def test_memory_search_rerank_note_on_failed_fallback(shim, monkeypatch):
    monkeypatch.setattr(shim.httpx, "request", lambda method, url, json=None, params=None, headers=None, timeout=None:
                        _FakeResp({"results": [], "rerank_status": "failed_fallback_dense"}))
    out = _tool_fn(shim.memory_search)("anything", rerank=True)
    assert "rerank_note" in out and "dense-only" in out["rerank_note"]
    # ran / skipped / absent -> no note
    monkeypatch.setattr(shim.httpx, "request", lambda method, url, json=None, params=None, headers=None, timeout=None:
                        _FakeResp({"results": [], "rerank_status": "ran"}))
    assert "rerank_note" not in _tool_fn(shim.memory_search)("anything", rerank=True)
    monkeypatch.setattr(shim.httpx, "request", lambda method, url, json=None, params=None, headers=None, timeout=None:
                        _FakeResp({"results": []}))
    assert "rerank_note" not in _tool_fn(shim.memory_search)("anything")


def test_memory_search_withheld_note(shim, monkeypatch):
    monkeypatch.setattr(shim.httpx, "request", lambda method, url, json=None, params=None, headers=None, timeout=None:
                        _FakeResp({"results": [], "rejected_superseded": 2,
                                   "rejected_contradicted": 1}))
    out = _tool_fn(shim.memory_search)("anything")
    assert "withheld_note" in out
    assert "2 superseded" in out["withheld_note"]
    assert "1 contradicting-canonical" in out["withheld_note"]
    # the byte-pinned brand hint is untouched by the new key
    monkeypatch.setattr(shim.httpx, "request", lambda method, url, json=None, params=None, headers=None, timeout=None:
                        _FakeResp({"results": [], "rejected_superseded": 0,
                                   "rejected_contradicted": 0}))
    assert "withheld_note" not in _tool_fn(shim.memory_search)("anything")


def test_memory_recall_withheld_note_and_age_summary(shim, monkeypatch):
    import datetime as dt
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=100)).isoformat()
    fresh = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        if url.endswith("/bundle"):
            return _FakeResp({"memories": [
                {"memory": "a", "created_at": old},
                {"memory": "b", "metadata": {"created_at": fresh}},
                {"memory": "c"},                       # no created_at -> skipped
            ], "goals": [], "open_questions": [],
                "rejected_superseded": 1, "rejected_contradicted": 0})
        return _FakeResp({"results": []})

    monkeypatch.setattr(shim.httpx, "request", fake_request)
    out = _tool_fn(shim.memory_recall)("what changed")
    assert "withheld_note" in out and "1 superseded" in out["withheld_note"]
    assert out["age_summary"]["newest_days"] == pytest.approx(2, abs=1)
    assert out["age_summary"]["oldest_days"] == pytest.approx(100, abs=1)


def test_memory_recall_no_age_summary_without_created_at(shim, monkeypatch):
    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        if url.endswith("/bundle"):
            return _FakeResp({"memories": [{"memory": "a"}], "goals": [],
                              "open_questions": []})
        return _FakeResp({"results": []})
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    out = _tool_fn(shim.memory_recall)("anything")
    assert "age_summary" not in out and "withheld_note" not in out


def test_memory_diagnose_posts_diagnose_contract(shim, monkeypatch):
    posts = []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        posts.append((method, url, json))
        return _FakeResp({"verdict": "returned"})

    monkeypatch.setattr(shim.httpx, "request", fake_request)
    out = _tool_fn(shim.memory_diagnose)(
        "why missing", "mid-123", threshold=0.55, limit=5, rerank=False)
    assert out["verdict"] == "returned"
    method, url, payload = posts[0]
    assert method == "POST" and url.endswith("/v1/memories/diagnose")
    assert payload["target_id"] == "mid-123"
    assert payload["threshold"] == 0.55 and payload["limit"] == 5
    assert payload["rerank"] is False
