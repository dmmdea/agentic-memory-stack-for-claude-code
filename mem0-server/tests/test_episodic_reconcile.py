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


# --- W7 AMS-20/19: the verdict must reflect the count; coverage measured ---

def test_ams20_orphan_backlog_degrades_the_outcome():
    """The finding itself: the run COUNTED dozens of orphans and still wrote
    outcome='ok', so nothing ever escalated. Past the threshold the verdict
    must degrade (TMS + the heartbeat key off outcome)."""
    assert recon.reconcile_outcome(True, True, orphaned_count=0) == "ok"
    assert recon.reconcile_outcome(
        True, True, orphaned_count=recon.ORPHAN_DEGRADE_THRESHOLD) == "ok"
    out = recon.reconcile_outcome(True, True, orphaned_count=59)
    assert out.startswith("degraded:orphaned-links:59")
    assert recon.exit_code_for(out) == 1


def test_ams20_infra_degradations_still_win_over_the_orphan_verdict():
    assert recon.reconcile_outcome(False, True, orphaned_count=99) == \
        "degraded:no-episodic-db"
    assert recon.reconcile_outcome(True, False, orphaned_count=99) == \
        "degraded:qdrant-unreachable"


def test_ams19_embedding_coverage_measured(tmp_path):
    """Schema-accurate AND state-accurate. The real ledger column is
    `summary_text`, the embedder's own rule is >= 64 chars after stripping
    (MIN_SUMMARY_CHARS), and only state='complete' episodes are ever indexed —
    in_progress checkpoint summaries are excluded on purpose. Both halves were
    learned by running against the live ledger: the first probe cut guessed a
    `summary` column and fail-softed; the second counted checkpoints and
    reported 539 missing where the true complete-state gap was ~15."""
    db = tmp_path / "e.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, "
                 "summary_text TEXT, state TEXT)")
    long_enough = "x" * 70
    conn.executemany("INSERT INTO episodes (summary_text, state) VALUES (?, ?)",
                     [(long_enough, "complete"), (long_enough, "complete"),
                      ("too short", "complete"),
                      ("", "complete"), (None, "complete"),
                      # the state-filter killer: long summary, but a checkpoint
                      # — the indexer never touches it, so neither may the probe
                      (long_enough, "in_progress")])
    conn.commit()

    def handler(request):
        return httpx.Response(200, json={"result": {"count": 1}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    out = recon.embedding_coverage(conn, http)
    conn.close()
    # 2 eligible (complete + >=64 chars), 1 embedded -> 1 missing; the short/
    # empty/null rows AND the long in_progress checkpoint are NOT eligible,
    # exactly as the indexing path treats them
    assert out == {"eligible": 2, "embedded": 1, "missing": 1}


def test_ams19_coverage_query_matches_the_real_ledger_schema(tmp_path):
    """A probe that silently measures nothing is worth nothing: pin the
    column name against the shape the live ledger actually has."""
    db = tmp_path / "e.db"
    conn = sqlite3.connect(db)
    # the real episodes table (subset), from the live ledger
    conn.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, session_id "
                 "TEXT, goal_text TEXT, summary_text TEXT, state TEXT)")
    conn.commit()

    def handler(request):
        return httpx.Response(200, json={"result": {"count": 0}})

    out = recon.embedding_coverage(conn, httpx.Client(
        transport=httpx.MockTransport(handler)))
    conn.close()
    assert "error" not in out, f"coverage probe broke on the real schema: {out}"
    assert out["eligible"] == 0 and out["missing"] == 0


def test_ams19_coverage_probe_never_raises(tmp_path):
    """A coverage probe must never fail the reconciliation run."""
    db = tmp_path / "e.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, summary TEXT)")
    conn.commit()

    def boom(request):
        raise httpx.ConnectError("down")

    http = httpx.Client(transport=httpx.MockTransport(boom))
    out = recon.embedding_coverage(conn, http)
    conn.close()
    assert out["missing"] is None and "error" in out


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


def _run_main(monkeypatch, db, *, readyz_ok=True, present=None, present_raises=None,
              hist_deleted=None, led_deleted=None, evidence_raises=False):
    summaries = []
    monkeypatch.setattr(recon, "_append_summary", lambda rec: summaries.append(rec))
    # 2026-08-24: main() consults the deletion-evidence sources; a unit run must
    # never touch the live ~/.mem0 history.db / tier-ledger (and CI has neither).
    def fake_hist(ids, db_path=None):
        if evidence_raises:
            raise sqlite3.OperationalError("unable to open database file")
        return set(hist_deleted or [])
    def fake_led(ids, ledger_dir=None):
        if evidence_raises:
            raise FileNotFoundError("no tier-ledger files")
        return dict(led_deleted or {})
    monkeypatch.setattr(recon, "history_deleted_ids", fake_hist)
    monkeypatch.setattr(recon, "ledger_deleted", fake_led)

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
    # the orphan has a DELETE on record -> explained -> ok (the live 63/63 shape)
    rc, s = _run_main(monkeypatch, db, present={"present-mem"}, hist_deleted={"gone-mem"})
    assert rc == 0 and s["outcome"] == "ok"
    assert s["orphaned_count"] == 1 and s["dangling_count"] == 1
    assert s["orphaned_explained_count"] == 1 and s["orphaned_unexplained_count"] == 0
    assert s["orphaned_explained_sample"][0]["evidence"] == ["history.db"]
    assert s["ok_memory_links"] == 1  # present-mem (the dangling one isn't counted as a memory link)


def test_main_unexplained_orphan_degrades_at_zero(monkeypatch, tmp_path):
    """A memory gone from Qdrant with NO deletion trace is possible data loss - one is enough."""
    db = _ledger_with(tmp_path, [(1, 10, "produced_evidence", "mem0", "vanished")])
    rc, s = _run_main(monkeypatch, db, present=set())
    assert rc == 1 and s["outcome"] == "degraded:orphaned-links-unexplained:1"
    assert s["orphaned_unexplained_sample"][0]["target_id"] == "vanished"


def test_main_abstains_when_both_evidence_sources_unreadable(monkeypatch, tmp_path):
    """Missing evidence is not 'no deletions': accusing every orphan would be the
    false-positive storm; refusing to split, loudly, is the honest verdict."""
    db = _ledger_with(tmp_path, [(1, 10, "produced_evidence", "mem0", "gone")])
    rc, s = _run_main(monkeypatch, db, present=set(), evidence_raises=True)
    assert rc == 1 and s["outcome"] == "degraded:orphan-evidence-unavailable:1"
    assert s["orphaned_explained_count"] is None
    assert set(s["orphan_evidence_errors"]) == {"history.db", "tier-ledger"}


# --- 2026-08-24: orphans split by deletion evidence ------------------------------
# Every one of the 63 live orphans had a DELETE on record (dedup/decay purges), yet
# the count WARNed forever. Explained (traced deletion) vs unexplained (vanished
# with no trace) — only the latter is drift worth a page, at threshold ZERO.

def _orphans(*ids):
    return [{"link_id": i, "episode_id": 10, "link_type": "produced_evidence",
             "target_id": t} for i, t in enumerate(ids)]


def test_explain_orphans_splits_by_either_evidence_source():
    out = recon.explain_orphans(
        _orphans("h-only", "l-only", "both", "none"),
        history_deleted={"h-only", "both"},
        ledger_deleted={"l-only": {"actor": "semantic-dedup", "reason": "dup of x",
                                   "event": "decay-delete"},
                        "both": {"actor": "rest-api", "reason": "DELETE", "event": "delete"}})
    ex = {e["target_id"]: e for e in out["explained"]}
    assert set(ex) == {"h-only", "l-only", "both"}
    assert ex["h-only"]["evidence"] == ["history.db"]
    assert ex["l-only"]["evidence"] == ["tier-ledger"] and ex["l-only"]["actor"] == "semantic-dedup"
    assert ex["both"]["evidence"] == ["history.db", "tier-ledger"]
    assert [u["target_id"] for u in out["unexplained"]] == ["none"]


def test_unexplained_orphans_degrade_at_zero_explained_never_do():
    # 63 explained, 0 unexplained -> ok (the live state this shipped against)
    assert recon.reconcile_outcome(True, True, orphaned_count=63, unexplained_count=0) == "ok"
    # ONE traceless orphan is store-level integrity loss
    out = recon.reconcile_outcome(True, True, orphaned_count=1, unexplained_count=1)
    assert out == "degraded:orphaned-links-unexplained:1" and recon.exit_code_for(out) == 1
    # legacy path (no split supplied) keeps the old threshold semantics
    assert recon.reconcile_outcome(True, True, orphaned_count=11) == "degraded:orphaned-links:11"


def test_history_deleted_ids_reads_only_delete_rows(tmp_path):
    db = tmp_path / "history.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE history (id TEXT, memory_id TEXT, old_memory TEXT, "
                 "new_memory TEXT, event TEXT, created_at TEXT, updated_at TEXT, "
                 "is_deleted INTEGER, actor_id TEXT, role TEXT)")
    conn.executemany("INSERT INTO history (memory_id, event) VALUES (?, ?)",
                     [("m-del", "ADD"), ("m-del", "DELETE"), ("m-upd", "ADD"), ("m-upd", "UPDATE")])
    conn.commit()
    conn.close()
    assert recon.history_deleted_ids(["m-del", "m-upd", "m-unknown"], db_path=db) == {"m-del"}
    assert recon.history_deleted_ids([], db_path=db) == set()


def test_history_deleted_ids_raises_when_db_missing(tmp_path):
    """Missing evidence must RAISE (the caller abstains), never read as 'no deletions'."""
    with pytest.raises(sqlite3.Error):
        recon.history_deleted_ids(["x"], db_path=tmp_path / "absent.db")


def test_ledger_deleted_scans_legacy_and_monthly_segments(tmp_path):
    (tmp_path / "tier-ledger.jsonl").write_text(
        '{"event": "delete", "memory_id": "old", "actor": "rest-api", "reason": "DELETE /v1"}\n'
        '{"event": "promote", "memory_id": "kept", "actor": "user-direct"}\n', encoding="utf-8")
    (tmp_path / "tier-ledger-2026-08.jsonl").write_text(
        'garbage line\n'
        '{"event": "decay-delete", "memory_id": "purged", "actor": "semantic-dedup", "reason": "dup"}\n',
        encoding="utf-8")
    out = recon.ledger_deleted(["old", "kept", "purged", "never"], ledger_dir=tmp_path)
    assert set(out) == {"old", "purged"}
    assert out["purged"]["actor"] == "semantic-dedup" and out["purged"]["event"] == "decay-delete"


def test_ledger_deleted_raises_when_no_ledger_files(tmp_path):
    with pytest.raises(OSError):
        recon.ledger_deleted(["x"], ledger_dir=tmp_path)
