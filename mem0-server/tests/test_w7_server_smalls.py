"""W7 server smalls — headless pins for AMS-21/22/39/40/41/42.

Each test is written to go RED if its fix is reverted (the W1 vacuous-guard
class is the enemy): the fail-open paths are exercised through the real code,
not asserted about prose.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "mem0-server"))

import security_invariants as si  # noqa: E402


def _app_module():
    """app.py imports the `mem0` package at module scope, which the headless
    CI runner does not install — so the two RUNTIME tests below (AMS-39/42)
    skip there LOUDLY and gate in the live-stack suite instead. Everything
    else in this file is pure or source-static and gates in CI."""
    try:
        import app
    except Exception as e:  # noqa: BLE001 — any import failure is a skip
        pytest.skip(f"needs the live-stack venv (app import failed: "
                    f"{type(e).__name__}: {str(e)[:60]}) — runs in the "
                    "live suite")
    return app


# ----------------------------------------------------------------------
# AMS-21: the canonical/insight mutation gate must FAIL CLOSED on a
# transient store error (it returned None -> the mutation proceeded ungated)
# ----------------------------------------------------------------------

class _BoomClient:
    def retrieve(self, *a, **kw):
        raise ConnectionError("qdrant blip")


class _EmptyClient:
    def retrieve(self, *a, **kw):
        return []


class _CanonicalClient:
    class _Rec:
        payload = {"tier": "canonical"}

    def retrieve(self, *a, **kw):
        return [self._Rec()]


def test_ams21_store_error_refuses_the_mutation():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        si.fetch_current_tier(_BoomClient(), "coll", "m1")
    assert ei.value.status_code == 503
    assert "mutation rejected" in str(ei.value.detail)


def test_ams21_genuine_not_found_still_passes_through():
    """The fix must not turn a legitimately absent point into a 503 — the
    caller still needs _NOT_FOUND so the underlying op returns its own 404."""
    assert si.fetch_current_tier(_EmptyClient(), "coll", "m1") is si._NOT_FOUND


def test_ams21_normal_path_unchanged():
    assert si.fetch_current_tier(_CanonicalClient(), "coll", "m1") == "canonical"


def test_ams21_assert_writable_propagates_the_503(monkeypatch):
    """End-to-end through the gate the endpoints actually call: a store blip
    on a canonical record must 503, never silently permit."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        si.assert_writable(_BoomClient(), "coll", "m1", "patch_metadata",
                           None, None, actor="test", reason="test")
    assert ei.value.status_code == 503


# ----------------------------------------------------------------------
# AMS-41: l10-audit dedup keys must truncate OLDEST-first, deterministically
# ----------------------------------------------------------------------

_L10 = REPO_ROOT / "scripts" / "wsl" / "l10-audit.py"
_spec = importlib.util.spec_from_file_location("l10_audit", _L10)
l10 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(l10)


def test_ams41_truncation_drops_the_oldest_keys_not_random_ones(tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(l10, "STATE_FILE", tmp_path / "l10-state.json")
    keys = [f"m{i:05d}:oversize" for i in range(5100)]
    l10.save_state({"last_audit_ts": 0, "audited_keys": list(keys)})
    kept = json.loads((tmp_path / "l10-state.json").read_text())["audited_keys"]
    assert len(kept) == 5000
    # OLDEST 100 dropped, NEWEST kept, ORDER preserved — with the pre-fix
    # hash-ordered set this held only by luck (and usually not at all).
    assert kept[0] == "m00100:oversize"
    assert kept[-1] == "m05099:oversize"
    assert kept == keys[100:]


def test_ams41_insertion_order_survives_a_load_save_cycle(tmp_path, monkeypatch):
    """The ordered structure must round-trip: load -> add -> save keeps the
    original order with new keys appended (a set() would scramble it)."""
    monkeypatch.setattr(l10, "STATE_FILE", tmp_path / "l10-state.json")
    l10.save_state({"last_audit_ts": 0,
                    "audited_keys": ["a:x", "b:x", "c:x"]})
    state = l10.load_state()
    ordered = dict.fromkeys(state["audited_keys"])
    ordered["d:x"] = None
    l10.save_state({"last_audit_ts": 1, "audited_keys": list(ordered)})
    kept = json.loads((tmp_path / "l10-state.json").read_text())["audited_keys"]
    assert kept == ["a:x", "b:x", "c:x", "d:x"]


# ----------------------------------------------------------------------
# AMS-42: concurrent ledger appends must not tear a >8KB line
# ----------------------------------------------------------------------

def test_ams42_concurrent_large_appends_never_tear(tmp_path, monkeypatch):
    """Without the lock, CPython's buffered writer splits a >8KB line into
    several raw writes and two threads interleave chunks — readers then skip
    the unparseable line silently, i.e. a LOST audit entry."""
    app_mod = _app_module()

    ledger = tmp_path / "tier-ledger-2026-08.jsonl"
    monkeypatch.setattr(app_mod, "_ledger_segment_path",
                        lambda *a, **kw: ledger)
    big = "x" * 12000          # forces a multi-chunk buffered write
    n_threads, per_thread = 8, 12

    def writer(tag):
        for i in range(per_thread):
            app_mod._append_ledger({
                "event": "decay-delete", "memory_id": f"{tag}-{i}",
                "actor": "test", "reason": big,
            })

    threads = [threading.Thread(target=writer, args=(t,))
               for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * per_thread
    for line in lines:
        json.loads(line)       # every line parses => no torn writes


# ----------------------------------------------------------------------
# AMS-39: the raw-trace fallback must leave a receipt on BOTH outcomes
# ----------------------------------------------------------------------

def test_ams39_counter_records_fire_abstain_and_error():
    app_mod = _app_module()

    app_mod._raw_fallback_today.update(
        {"date": None, "fired": 0, "abstained": 0, "errors": 0})
    app_mod._raw_fallback_bump(fired=1)
    app_mod._raw_fallback_bump(abstained=2)
    app_mod._raw_fallback_bump(errors=1)
    snap = dict(app_mod._raw_fallback_today)
    assert (snap["fired"], snap["abstained"], snap["errors"]) == (1, 2, 1)
    assert snap["date"] is not None


def test_ams39_bundle_wires_both_outcomes_to_the_counter():
    """Pin the CALL SITES: a counter nothing bumps is the defect itself
    (designed-but-dead). Both branches of the fallback must bump."""
    src = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    i = src.find("if RAW_FALLBACK_ENABLED and not out.get(\"memories\")")
    assert i != -1
    block = src[i:i + 900]
    assert "_raw_fallback_bump(fired=1)" in block
    assert "_raw_fallback_bump(abstained=1)" in block
    assert '"raw_fallback_today"' in src


# ----------------------------------------------------------------------
# AMS-40: the PUT carry-over restore failure must 500 for ANY truthy tier
# ----------------------------------------------------------------------

def test_ams40_restore_failure_raises_for_any_tier_not_just_canonical():
    src = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    i = src.find("_put_carryover_bump(lost=len(_missing))")
    assert i != -1
    block = src[i:i + 700]
    code = "\n".join(ln for ln in block.splitlines()
                     if not ln.strip().startswith("#"))
    assert "if _tier_now:" in code, \
        "the 500 is still gated on a tier allowlist — 'stable' can lose its tier silently"
    assert '{"canonical", "insight"}' not in code


# ----------------------------------------------------------------------
# AMS-22: write-ahead audit — the intent line precedes the mutation
# ----------------------------------------------------------------------

def test_ams22_delete_and_tier_change_write_intent_before_mutating():
    src = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    # DELETE: intent append must precede the client delete call.
    d = src.find("def delete(")
    body = src[d:src.find("\n@app.", d + 10)]
    i_intent = body.find('"delete-intent"')
    i_delete = body.find("mem.delete(")
    assert i_intent != -1 and i_delete != -1
    assert i_intent < i_delete, "the audit intent no longer precedes the deletion"
    assert "503" in body[i_intent:i_delete], \
        "an intent-append failure must REFUSE the deletion (503), not proceed"
    # PATCH /tier: same shape.
    t = src.find("def update_tier(")
    tbody = src[t:src.find("\n@app.", t + 10)]
    i_ti = tbody.find('"tier-change-intent"')
    i_set = tbody.find("set_payload(")
    assert i_ti != -1 and i_set != -1 and i_ti < i_set


def test_ams22_intent_events_are_registered_in_the_ledger_schema():
    """An unregistered event type makes ledger-audit report every intent line
    as a schema violation — the audit tool must know them."""
    la = REPO_ROOT / "scripts" / "wsl" / "ledger-audit.py"
    spec = importlib.util.spec_from_file_location("ledger_audit", la)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "delete-intent" in mod.SCHEMA
    assert "tier-change-intent" in mod.SCHEMA
    assert "status" in mod.SCHEMA["delete"]["optional"]


# ----------------------------------------------------------------------
# GET /v1/memories/{mid}: an id that is not a UUID is a 404, not a 500
# ----------------------------------------------------------------------
# Qdrant answers 400 ("Can not recognize ... as point id") to a short id, and the handler's blanket
# `except Exception -> _upstream_error` relayed it as HTTP 500 (live 2026-09-19..10-08: seven GETs by an 8-character
# id). `import app` needs the live stack, so the handler is loaded from its source with stubbed collaborators.

def _handler_namespace(retrieve):
    import ast
    import logging
    import typing
    import uuid

    from fastapi import Header, HTTPException

    src = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {"_is_point_id", "_mid_lock_key", "get_memory_by_id", "diagnose_memory"}
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in wanted:
            n.decorator_list = []                              # @app.get(...) needs the app
            nodes.append(n)
    assert any(n.name == "get_memory_by_id" for n in nodes)

    class _Client:
        def retrieve(self, *a, **kw):
            return retrieve(*a, **kw)

    class _Mem:
        class vector_store:                                    # noqa: N801 — mirrors mem.vector_store
            client = _Client()
            collection_name = "c"

    ns = {"Optional": typing.Optional, "Header": Header, "HTTPException": HTTPException, "_uuid": uuid,
          "DiagnoseIn": object,                              # annotation only: the handler signature is evaluated at def time
          "auth": lambda key: None, "mem": _Mem(), "_INTENT_KEYS": frozenset(),
          "log": logging.getLogger("t"), "_upstream_error": lambda e: HTTPException(500, str(e))}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), ns)
    return ns


def test_a_short_memory_id_is_a_404_and_never_reaches_qdrant():
    from fastapi import HTTPException
    asked = []

    def retrieve(**kw):
        asked.append(kw["ids"])
        raise RuntimeError("Unexpected Response: 400 (Bad Request) Can not recognize \"1a2b3c4d\" as point id")
    get = _handler_namespace(retrieve)["get_memory_by_id"]
    for short in ("1a2b3c4d", "5e6f7a8b", "", "not-an-id"):
        with pytest.raises(HTTPException) as ei:
            get(short)
        assert ei.value.status_code == 404, f"{short!r} -> {ei.value.status_code}: a non-UUID cannot exist"
    assert asked == [], "the id is refused before Qdrant is asked"


def test_every_uuid_spelling_still_reaches_qdrant_and_an_absent_one_is_still_404():
    from fastapi import HTTPException
    asked = []

    def retrieve(**kw):
        asked.append(kw["ids"][0])
        return []
    ns = _handler_namespace(retrieve)
    u = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    spellings = (u, u.upper(), u.replace("-", ""), "{" + u + "}", "urn:uuid:" + u,
                 "  " + u + " ", "uuid:" + u, "{" + u)      # uuid.UUID takes these too, Qdrant would 400 on them
    for spelling in spellings:
        with pytest.raises(HTTPException) as ei:
            ns["get_memory_by_id"](spelling)
        assert ei.value.status_code == 404
    assert asked == [u] * len(spellings), "every spelling reaches Qdrant as the one canonical id"


def test_a_genuine_qdrant_failure_on_a_valid_id_is_still_a_500():
    """Scope guard: only the unknowable id changed. A store fault must stay loud."""
    from fastapi import HTTPException

    def retrieve(**kw):
        raise RuntimeError("qdrant unreachable")
    get = _handler_namespace(retrieve)["get_memory_by_id"]
    with pytest.raises(HTTPException) as ei:
        get("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
    assert ei.value.status_code == 500


def test_diagnose_of_a_short_target_id_is_a_404_not_a_500():
    """POST /v1/memories/diagnose documents 404 for an absent target; a short id used to reach Qdrant and come back 500."""
    import types
    from fastapi import HTTPException
    asked = []

    def retrieve(**kw):
        asked.append(kw["ids"])
        raise RuntimeError("Unexpected Response: 400 (Bad Request)")
    diagnose = _handler_namespace(retrieve)["diagnose_memory"]
    with pytest.raises(HTTPException) as ei:
        diagnose(types.SimpleNamespace(target_id="1a2b3c4d"))
    assert ei.value.status_code == 404 and asked == []


def test_diagnose_sends_qdrant_the_canonical_spelling():
    """The probes compare the target with the ids search returns (canonical), and Qdrant refuses some spellings
    uuid.UUID accepts: diagnose normalizes the target once, before anything reads it."""
    import types
    from fastapi import HTTPException
    asked = []

    def retrieve(**kw):
        asked.append(kw["ids"][0])
        return []
    diagnose = _handler_namespace(retrieve)["diagnose_memory"]
    u = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    with pytest.raises(HTTPException) as ei:
        diagnose(types.SimpleNamespace(target_id="{" + u.upper() + "}"))
    assert ei.value.status_code == 404 and asked == [u]
