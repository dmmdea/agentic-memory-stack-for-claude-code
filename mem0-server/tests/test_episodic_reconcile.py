"""Unit tests for scripts/wsl/episodic-reconcile.py (v0.27.4 R5).

Pure classify_links + the read helpers (against a temp SQLite ledger) + qdrant_present_ids
(httpx.MockTransport). No live Qdrant / mem0. Asserts the read-only + drift-detection contract.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "wsl" / "episodic-reconcile.py"
_spec = importlib.util.spec_from_file_location("episodic_reconcile", SCRIPT)
recon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recon)


# --- classify_links (pure) ---

def _link(lid, ep, kind, tid, lt="produced_evidence"):
    return {"id": lid, "episode_id": ep, "link_type": lt, "target_kind": kind, "target_id": tid}


def test_classify_clean_store():
    links = [_link(1, 10, "mem0", "m1"), _link(2, 10, "mem0", "m2")]
    out = recon.classify_links(links, existing_episode_ids={10}, present_memory_ids={"m1", "m2"})
    assert out["orphaned_link"] == [] and out["dangling"] == []
    assert out["memory_links"] == 2 and out["ok"] == 2


def test_classify_orphaned_memory():
    links = [_link(1, 10, "mem0", "m1"), _link(2, 10, "mem0", "gone")]
    out = recon.classify_links(links, {10}, {"m1"})
    assert [o["target_id"] for o in out["orphaned_link"]] == ["gone"]
    assert out["ok"] == 1 and out["memory_links"] == 2


def test_classify_dangling_episode():
    links = [_link(1, 999, "mem0", "m1")]  # episode 999 absent
    out = recon.classify_links(links, existing_episode_ids={10}, present_memory_ids={"m1"})
    assert len(out["dangling"]) == 1 and out["dangling"][0]["episode_id"] == 999
    # a dangling link is NOT also counted as orphaned/memory_link
    assert out["orphaned_link"] == [] and out["memory_links"] == 0


def test_classify_recognizes_both_mem0_and_memory_kinds():
    # live uses 'mem0'; 'memory' is accepted defensively (MEMORY_TARGET_KINDS)
    links = [_link(1, 10, "mem0", "a"), _link(2, 10, "memory", "b")]
    out = recon.classify_links(links, {10}, {"a", "b"})
    assert out["memory_links"] == 2 and out["ok"] == 2


def test_classify_ignores_non_memory_links():
    links = [_link(1, 10, "goal", "g1", lt="advanced_goal")]
    out = recon.classify_links(links, {10}, set())
    assert out["memory_links"] == 0 and out["orphaned_link"] == [] and out["dangling"] == []


# --- read helpers against a temp SQLite ledger (verifies READ-ONLY open + queries) ---

def _make_ledger(tmp_path) -> Path:
    db = tmp_path / "episodic.db"
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE episodes (id INTEGER PRIMARY KEY, session_id TEXT, started_at TEXT, ended_at TEXT);"
        "CREATE TABLE episode_links (id INTEGER PRIMARY KEY, episode_id INTEGER, link_type TEXT, target_kind TEXT, target_id TEXT);"
    )
    c.execute("INSERT INTO episodes (id, session_id, started_at, ended_at) VALUES (10,'s','a','b')")
    c.executemany("INSERT INTO episode_links (id, episode_id, link_type, target_kind, target_id) VALUES (?,?,?,?,?)",
                  [(1, 10, "produced_evidence", "mem0", "m1"),   # live target_kind is 'mem0', not 'memory'
                   (2, 10, "produced_evidence", "mem0", "m2"),
                   (3, 10, "advanced_goal", "goal", "g1")])
    c.commit(); c.close()
    return db


def test_read_episode_links_and_ids(tmp_path):
    db = _make_ledger(tmp_path)
    conn = recon.open_ledger_ro(db)
    try:
        links = recon.read_episode_links(conn)
        eids = recon.existing_episode_ids(conn)
    finally:
        conn.close()
    assert len(links) == 3
    assert eids == {10}
    assert {l["target_id"] for l in links if l["target_kind"] == "mem0"} == {"m1", "m2"}


def test_open_ledger_ro_is_read_only(tmp_path):
    db = _make_ledger(tmp_path)
    conn = recon.open_ledger_ro(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO episodes (id, session_id, started_at, ended_at) VALUES (99,'x','a','b')")
    finally:
        conn.close()


# --- qdrant_present_ids (MockTransport) ---

def test_qdrant_present_ids_returns_subset():
    def handler(request):
        import json
        ids = json.loads(request.content)["ids"]
        # only m1 + m3 exist
        present = [{"id": x} for x in ids if x in ("m1", "m3")]
        return httpx.Response(200, json={"result": present})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        present = recon.qdrant_present_ids(c, ["m1", "m2", "m3"])
    assert present == {"m1", "m3"}


def test_qdrant_present_ids_raises_on_transport_error():
    def handler(request):
        raise httpx.ConnectError("qdrant down")
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPError):
            recon.qdrant_present_ids(c, ["m1"])


def test_outcome_and_exit_code():
    assert recon.reconcile_outcome(True, True) == "ok"
    assert recon.reconcile_outcome(False, True).startswith("degraded")
    assert recon.reconcile_outcome(True, False).startswith("degraded")
    assert recon.exit_code_for("ok") == 0
    assert recon.exit_code_for("degraded:x") == 1


# --- v0.27.4 audit fixes: qdrant_present_ids malformed-200 + multi-batch, main() degrade/happy ---

import sys as _sys
import types as _types


def test_qdrant_present_ids_malformed_200_raises():
    # a 200 whose body lacks a list 'result' must RAISE (never read as 'all absent' -> false orphans)
    for body in ({"result": None}, {}, {"status": "error"}):
        with httpx.Client(transport=httpx.MockTransport(lambda r, b=body: httpx.Response(200, json=b))) as c:
            with pytest.raises(ValueError):
                recon.qdrant_present_ids(c, ["m1"])


def test_qdrant_present_ids_multi_batch(monkeypatch):
    monkeypatch.setattr(recon, "QDRANT_BATCH", 2)
    seen_batches = []
    def handler(request):
        import json
        ids = json.loads(request.content)["ids"]
        seen_batches.append(tuple(ids))
        return httpx.Response(200, json={"result": [{"id": x} for x in ids if x != "gone"]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        present = recon.qdrant_present_ids(c, ["a", "b", "c", "gone"])
    assert present == {"a", "b", "c"}
    assert seen_batches == [("a", "b"), ("c", "gone")]  # batched at size 2


def test_qdrant_present_ids_partial_batch_failure_raises(monkeypatch):
    monkeypatch.setattr(recon, "QDRANT_BATCH", 2)
    def handler(request):
        import json
        ids = json.loads(request.content)["ids"]
        if "c" in ids:
            raise httpx.ConnectError("blip on 2nd batch")
        return httpx.Response(200, json={"result": [{"id": x} for x in ids]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPError):
            recon.qdrant_present_ids(c, ["a", "b", "c", "d"])


def _ledger_with(tmp_path, links):
    db = tmp_path / "episodic.db"
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE episodes (id INTEGER PRIMARY KEY, session_id TEXT, started_at TEXT, ended_at TEXT);"
        "CREATE TABLE episode_links (id INTEGER PRIMARY KEY, episode_id INTEGER, link_type TEXT, target_kind TEXT, target_id TEXT);"
    )
    c.execute("INSERT INTO episodes (id, session_id, started_at, ended_at) VALUES (10,'s','a','b')")
    c.executemany("INSERT INTO episode_links (id, episode_id, link_type, target_kind, target_id) VALUES (?,?,?,?,?)", links)
    c.commit(); c.close()
    return db


def _run_main(monkeypatch, db, *, readyz_ok=True, present=None, present_raises=None):
    summaries = []
    monkeypatch.setattr(recon, "_append_summary", lambda rec: summaries.append(rec))

    def fake_get(url, **kw):
        if not readyz_ok:
            raise httpx.ConnectError("qdrant down")
        return _types.SimpleNamespace(raise_for_status=lambda: None)
    monkeypatch.setattr(recon.httpx, "get", fake_get)

    def fake_present(http, ids):
        if present_raises is not None:
            raise present_raises
        return set(present or [])
    monkeypatch.setattr(recon, "qdrant_present_ids", fake_present)
    monkeypatch.setattr(_sys, "argv", ["episodic-reconcile.py", "--db", str(db)])
    rc = recon.main()
    return rc, (summaries[-1] if summaries else None)


def test_main_degrades_on_fetch_failure_no_spurious_orphans(monkeypatch, tmp_path):
    # the HIGH: a transient point-fetch failure must exit 1 + degraded + report ZERO orphans
    db = _ledger_with(tmp_path, [(1, 10, "produced_evidence", "mem0", "m1"),
                                 (2, 10, "produced_evidence", "mem0", "m2")])
    rc, s = _run_main(monkeypatch, db, present_raises=httpx.ConnectError("blip"))
    assert rc == 1
    assert s["outcome"] == "degraded:qdrant-fetch-failed"
    assert "orphaned_count" not in s  # classify_links never reached -> no false orphans


def test_main_degrades_on_readyz_unreachable(monkeypatch, tmp_path):
    db = _ledger_with(tmp_path, [(1, 10, "produced_evidence", "mem0", "m1")])
    rc, s = _run_main(monkeypatch, db, readyz_ok=False)
    assert rc == 1
    assert s["outcome"] == "degraded:qdrant-unreachable"


def test_main_degrades_on_missing_db(monkeypatch, tmp_path):
    rc, s = _run_main(monkeypatch, tmp_path / "nope.db")
    assert rc == 1
    assert s["outcome"] == "degraded:no-episodic-db"


def test_main_happy_path_reports_orphaned_and_dangling(monkeypatch, tmp_path):
    db = _ledger_with(tmp_path, [
        (1, 10, "produced_evidence", "mem0", "present-mem"),   # ok
        (2, 10, "produced_evidence", "mem0", "gone-mem"),      # orphaned (absent from Qdrant)
        (3, 999, "produced_evidence", "mem0", "x"),            # dangling (episode 999 missing)
    ])
    rc, s = _run_main(monkeypatch, db, present={"present-mem"})
    assert rc == 0 and s["outcome"] == "ok"
    assert s["orphaned_count"] == 1 and s["dangling_count"] == 1
    assert s["ok_memory_links"] == 1  # present-mem (the dangling one isn't counted as a memory link)
