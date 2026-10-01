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
              hist_deleted=None, led_deleted=None, evidence_raises=False,
              evidence_raises_history=False, hist_delete_total=1):
    summaries = []
    monkeypatch.setattr(recon, "_append_summary", lambda rec: summaries.append(rec))
    monkeypatch.setattr(recon, "LEDGER_PARSE_ERRORS", {})
    # 2026-08-24: main() consults the deletion-evidence sources; a unit run must
    # never touch the live ~/.mem0 history.db / tier-ledger (and CI has neither).
    def fake_hist(ids, db_path=None):
        if evidence_raises or evidence_raises_history:
            raise sqlite3.OperationalError("unable to open database file")
        return set(hist_deleted or [])
    def fake_hist_total(db_path=None):
        if evidence_raises or evidence_raises_history:
            raise sqlite3.OperationalError("unable to open database file")
        return hist_delete_total
    def fake_led(ids, ledger_dir=None):
        if evidence_raises:
            raise FileNotFoundError("no tier-ledger files")
        return dict(led_deleted or {})
    monkeypatch.setattr(recon, "history_deleted_ids", fake_hist)
    monkeypatch.setattr(recon, "history_delete_row_count", fake_hist_total)
    monkeypatch.setattr(recon, "ledger_deleted", fake_led)
    # WP-4: the weekly run also abandons stale checkpoints and backfills episode embeddings; these
    # pre-existing orphan-classification flows must never reach the network or the embedder
    monkeypatch.setattr(recon, "run_embedding_backfill", lambda limit, db_path: {"not_run": "test"})
    monkeypatch.delenv("AMS_OUTCOME_FILE", raising=False)

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


# --- 2026-08-24 round 2: partial-evidence, rotation, parse-error surfacing ---------

def test_history_delete_row_count_reads_delete_rows(tmp_path):
    db = tmp_path / "history.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE history (memory_id TEXT, event TEXT)")
    conn.executemany("INSERT INTO history (memory_id, event) VALUES (?, ?)",
                     [("a", "ADD"), ("a", "DELETE"), ("b", "delete"), ("c", "UPDATE")])
    conn.commit(); conn.close()
    assert recon.history_delete_row_count(db_path=db) == 2   # DELETE + delete (case-tolerant)


def test_rotated_history_table_is_evidence_loss_not_no_deletions(monkeypatch, tmp_path):
    """A rebuilt history table (0 DELETE rows) beside live orphans must NOT read as
    'no deletions' - it is evidence loss (review R1 HIGH). It surfaces as a partial
    outcome, not a clean unexplained accusation."""
    db = _ledger_with(tmp_path, [(1, 10, "produced_evidence", "mem0", "gone")])
    rc, s = _run_main(monkeypatch, db, present=set(),
                      hist_delete_total=0, led_deleted={})
    assert rc == 1 and s["outcome"].startswith("degraded:orphan-evidence-partial:")
    assert "history.db" in s["orphan_evidence_errors"]
    assert s["history_delete_rows_total"] == 0


def test_single_source_failure_is_partial_not_full_accusation(monkeypatch, tmp_path):
    """history.db unreadable but the ledger explains the orphan: the split still runs,
    the outcome is partial (names the dead source), NOT a data-loss accusation."""
    db = _ledger_with(tmp_path, [(1, 10, "produced_evidence", "mem0", "gone")])
    rc, s = _run_main(monkeypatch, db, present=set(), evidence_raises_history=True,
                      led_deleted={"gone": {"actor": "semantic-dedup", "reason": "dup",
                                            "event": "decay-delete"}})
    assert rc == 1 and s["outcome"].startswith("degraded:orphan-evidence-partial:history.db")
    assert s["orphaned_explained_count"] == 1 and s["orphaned_unexplained_count"] == 0


def test_ledger_non_dict_line_does_not_crash(tmp_path):
    # a JSON ARRAY containing the quoted "memory_id" token passes the substring
    # prefilter and parses, but is not a dict -> counted bad, never .get-crashed
    lines = ['["memory_id", "not-a-dict"]',
             '{"event": "delete", "memory_id": "real", "actor": "x"}']
    (tmp_path / "tier-ledger-2026-08.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = recon.ledger_deleted(["real", "never"], ledger_dir=tmp_path)
    assert set(out) == {"real"}
    assert recon.LEDGER_PARSE_ERRORS.get("tier-ledger-2026-08.jsonl") == 1


def test_wholly_unparseable_ledger_segment_raises(tmp_path):
    # lines carry the quoted "memory_id" token (so the prefilter admits them) but are
    # not valid JSON -> 0 parseable, all bad -> the segment is unreadable, raise
    lines = ['{"memory_id": TRUNCATED', '{"memory_id": also broken']
    (tmp_path / "tier-ledger-2026-08.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(OSError, match="unreadable"):
        recon.ledger_deleted(["x"], ledger_dir=tmp_path)



# ---------------------------------------------------------------------------
# WP-4: orphan in_progress episodes are abandoned; missing embeddings are backfilled (bounded)
# ---------------------------------------------------------------------------
#
# Every prompt opens an in_progress checkpoint episode that only a later extraction finalizes; sessions
# that produced nothing stayed in_progress forever (1,352 of them, 25 % of all episodes), and the
# documented stale-sweep to 'abandoned' was never written. Separately the semantic layer covered 14 %
# of eligible episodes because the backfill had no trigger and the reconcile only measured the gap.

import datetime as _dtmod
import importlib.util as _ilu
import json as _json

_SERVER = str(REPO_ROOT / "mem0-server")
if _SERVER not in _sys.path:
    _sys.path.insert(0, _SERVER)

_NOW = _dtmod.datetime(2026, 9, 29, 12, 0, tzinfo=_dtmod.timezone.utc)


def _real_ledger(tmp_path, rows):
    """A ledger with the REAL schema (episodic.init_schema), rows = [(state, ended_at)]."""
    from episodic import _connect_to, init_schema
    db = tmp_path / "episodic.db"
    conn = _connect_to(db)
    init_schema(conn)
    for i, (state, ended) in enumerate(rows, start=1):
        # one session per episode: the schema allows at most ONE in_progress episode per session
        conn.execute("INSERT INTO sessions (session_id, started_at) VALUES (?, '2026-01-01T00:00:00+00:00')",
                     (f"s{i}",))
        conn.execute(
            "INSERT INTO episodes (id, session_id, started_at, ended_at, goal_text, summary_text, state) "
            "VALUES (?, ?, ?, ?, '', ?, ?)", (i, f"s{i}", ended, ended, "x" * 80, state))
    conn.commit()
    conn.close()
    return db


def _states(db):
    c = sqlite3.connect(db)
    try:
        return dict(c.execute("SELECT id, state FROM episodes").fetchall())
    finally:
        c.close()


def test_abandon_stale_in_progress_only_touches_old_checkpoints(tmp_path):
    day = _dtmod.timedelta(days=1)
    iso = lambda d: (_NOW - d).isoformat()      # noqa: E731
    db = _real_ledger(tmp_path, [
        ("in_progress", iso(10 * day)),          # 1: orphaned checkpoint -> abandoned
        ("in_progress", iso(1 * day)),           # 2: a live session's checkpoint -> untouched
        ("complete", iso(30 * day)),             # 3: finished, old -> untouched
        ("abandoned", iso(30 * day)),            # 4: already abandoned -> untouched
        ("in_progress", iso(8 * day)),           # 5: past the window -> abandoned
        ("in_progress", "not-a-timestamp"),      # 6: unparseable age -> left alone, never guessed
    ])
    n = recon.abandon_stale_in_progress(db, days=7, now=_NOW)
    assert n == 2
    assert _states(db) == {1: "abandoned", 2: "in_progress", 3: "complete", 4: "abandoned",
                           5: "abandoned", 6: "in_progress"}
    # idempotent: a second sweep finds nothing left to abandon
    assert recon.abandon_stale_in_progress(db, days=7, now=_NOW) == 0


def test_abandon_is_fail_soft_on_a_ledger_it_cannot_write(tmp_path):
    """The reconcile must still produce its receipt when the sweep cannot run (no state column,
    unreadable db): 0 abandoned and an error string, never an exception."""
    db = _ledger_with(tmp_path, [])            # the minimal test ledger has no `state` column
    n, err = recon.try_abandon_stale(db, days=7, now=_NOW)
    assert n == 0 and err and "state" in err


def test_coverage_outcome_degrades_below_ninety_percent():
    assert recon.coverage_outcome({"eligible": 3628, "embedded": 519, "missing": 3109}) == \
        "degraded:embedding-coverage-14"
    assert recon.coverage_outcome({"eligible": 100, "embedded": 90, "missing": 10}) is None
    assert recon.coverage_outcome({"eligible": 100, "embedded": 89, "missing": 11}) == \
        "degraded:embedding-coverage-89"
    # more points than eligible episodes (stale points for retired episodes) is full coverage, not >100 %
    assert recon.coverage_outcome({"eligible": 100, "embedded": 140, "missing": 0}) is None
    # nothing eligible, or the probe itself failed: no verdict from coverage
    assert recon.coverage_outcome({"eligible": 0, "embedded": 0, "missing": 0}) is None
    assert recon.coverage_outcome({"eligible": None, "embedded": None, "missing": None, "error": "x"}) is None


class _FakeBackfill:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result or {"embedded": 7, "skipped": 0, "errors": 0,
                                                         "remaining": 0, "total_complete": 7}, error, []

    def run(self, limit=None, db_path=None, **kw):
        self.calls.append({"limit": limit, "db_path": db_path, **kw})
        if self.error:
            raise self.error
        return dict(self.result)


def test_backfill_is_bounded_and_skipped_when_the_embedder_is_down(monkeypatch, tmp_path):
    fake = _FakeBackfill()
    monkeypatch.setattr(recon, "_load_backfill", lambda: fake)
    monkeypatch.setattr(recon.httpx, "get", lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("down")))
    sleeps = []
    out = recon.run_embedding_backfill(500, tmp_path / "e.db", wait_s=30, step_s=15, sleep=sleeps.append)
    assert out["not_run"] == "embedder-down" and fake.calls == []
    assert sleeps == [15, 15], "it polled the whole window before giving up"

    monkeypatch.setattr(recon.httpx, "get", lambda url, **kw: _types.SimpleNamespace(raise_for_status=lambda: None))
    out = recon.run_embedding_backfill(500, tmp_path / "e.db")
    assert fake.calls == [{"limit": 500, "db_path": tmp_path / "e.db"}]
    assert out["embedded"] == 7 and "not_run" not in out


def test_a_cold_embedder_is_waited_for_not_skipped(monkeypatch, tmp_path):
    """The first GET of a job lands on an unloaded seat and answers 503 while it loads: a one-shot
    probe read that as 'down' and skipped the whole run (the weekly backfill, and the backfill the
    daily upkeep makes). It polls /health/embedder through the shared ams_env.wait_for_embedder."""
    fake = _FakeBackfill()
    monkeypatch.setattr(recon, "_load_backfill", lambda: fake)
    answers, sleeps, urls = [503, 503, 200], [], []

    def get(url, **kw):
        urls.append(url)
        return httpx.Response(answers.pop(0), request=httpx.Request("GET", url))

    monkeypatch.setattr(recon.httpx, "get", get)
    out = recon.run_embedding_backfill(500, tmp_path / "e.db", wait_s=120, step_s=15, sleep=sleeps.append)
    assert sleeps == [15, 15] and len(urls) == 3 and all(u.endswith("/health/embedder") for u in urls)
    assert out["embedded"] == 7 and "not_run" not in out
    assert fake.calls == [{"limit": 500, "db_path": tmp_path / "e.db"}]


def test_the_reconcile_polls_for_two_minutes_every_fifteen_seconds_by_default():
    assert (recon.EMBEDDER_WAIT_S, recon.EMBEDDER_STEP_S) == (120, 15)


def test_backfill_failure_is_fail_soft(monkeypatch, tmp_path):
    monkeypatch.setattr(recon.httpx, "get", lambda url, **kw: _types.SimpleNamespace(raise_for_status=lambda: None))
    monkeypatch.setattr(recon, "_load_backfill", lambda: _FakeBackfill(error=ImportError("no qdrant_client")))
    out = recon.run_embedding_backfill(500, tmp_path / "e.db")
    assert out["error"].startswith("ImportError") and out["embedded"] == 0


def _main_with_episodes(monkeypatch, tmp_path, coverage, args=(), backfill=None):
    """main() over a real-schema ledger with a stubbed coverage probe and backfill."""
    db = _real_ledger(tmp_path, [("in_progress", (_NOW - _dtmod.timedelta(days=20)).isoformat())])
    monkeypatch.setattr(recon, "embedding_coverage", lambda conn, http: dict(coverage))
    fake = backfill or _FakeBackfill()
    monkeypatch.setattr(recon, "_load_backfill", lambda: fake)
    outcome_file = tmp_path / "outcome.txt"
    monkeypatch.setenv("AMS_OUTCOME_FILE", str(outcome_file))
    summaries = []
    monkeypatch.setattr(recon, "_append_summary", lambda rec: summaries.append(rec))
    monkeypatch.setattr(recon, "history_deleted_ids", lambda ids, db_path=None: set())
    monkeypatch.setattr(recon, "history_delete_row_count", lambda db_path=None: 1)
    monkeypatch.setattr(recon, "ledger_deleted", lambda ids, ledger_dir=None: {})
    monkeypatch.setattr(recon.httpx, "get", lambda url, **kw: _types.SimpleNamespace(raise_for_status=lambda: None))
    monkeypatch.setattr(recon, "qdrant_present_ids", lambda http, ids: set())
    monkeypatch.setattr(_sys, "argv", ["episodic-reconcile.py", "--db", str(db), *args])
    rc = recon.main()
    return rc, summaries[-1], fake, outcome_file, db


def test_main_abandons_orphans_backfills_and_degrades_on_low_coverage(monkeypatch, tmp_path):
    rc, s, fake, outcome_file, db = _main_with_episodes(
        monkeypatch, tmp_path, {"eligible": 1000, "embedded": 400, "missing": 600})
    assert s["abandoned_stale_in_progress"] == 1 and _states(db) == {1: "abandoned"}
    assert fake.calls and fake.calls[0]["limit"] == 500
    assert s["embedding_backfill"]["embedded"] == 7
    assert s["outcome"] == "degraded:embedding-coverage-40"
    assert rc == 0, "a catching-up coverage gap is reported, it does not fail the unit"
    status, _, body = outcome_file.read_text(encoding="utf-8").partition(" ")
    assert status == "degraded:embedding-coverage-40"
    counts = _json.loads(body)
    assert counts["abandoned"] == 1 and counts["embedded"] == 7 and counts["coverage_pct"] == 40


def test_main_reads_ok_when_coverage_is_healthy(monkeypatch, tmp_path):
    rc, s, fake, outcome_file, db = _main_with_episodes(
        monkeypatch, tmp_path, {"eligible": 1000, "embedded": 950, "missing": 50})
    assert rc == 0 and s["outcome"] == "ok"
    assert outcome_file.read_text(encoding="utf-8").startswith("ok ")


def test_low_coverage_never_masks_a_worse_outcome(monkeypatch, tmp_path):
    """Precedence is unchanged: an infrastructure or orphan verdict stays the headline."""
    monkeypatch.setattr(recon, "qdrant_present_ids", lambda http, ids: set())
    db = _real_ledger(tmp_path, [("complete", _NOW.isoformat())])
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE IF NOT EXISTS episode_links (id INTEGER PRIMARY KEY, episode_id INTEGER, "
              "link_type TEXT, target_kind TEXT, target_id TEXT)")
    c.execute("INSERT INTO episode_links (episode_id, link_type, target_kind, target_id) "
              "VALUES (1, 'produced_evidence', 'mem0', 'vanished')")
    c.commit()
    c.close()
    monkeypatch.setattr(recon, "embedding_coverage", lambda conn, http: {"eligible": 10, "embedded": 1, "missing": 9})
    monkeypatch.setattr(recon, "_load_backfill", lambda: _FakeBackfill())
    monkeypatch.setattr(recon, "_append_summary", lambda rec: None)
    monkeypatch.setattr(recon, "history_deleted_ids", lambda ids, db_path=None: set())
    monkeypatch.setattr(recon, "history_delete_row_count", lambda db_path=None: 1)
    monkeypatch.setattr(recon, "ledger_deleted", lambda ids, ledger_dir=None: {})
    monkeypatch.setattr(recon.httpx, "get", lambda url, **kw: _types.SimpleNamespace(raise_for_status=lambda: None))
    monkeypatch.delenv("AMS_OUTCOME_FILE", raising=False)
    monkeypatch.setattr(_sys, "argv", ["episodic-reconcile.py", "--db", str(db)])
    assert recon.main() == 1


def test_backfill_flag_zero_disables_it(monkeypatch, tmp_path):
    rc, s, fake, _, _ = _main_with_episodes(
        monkeypatch, tmp_path, {"eligible": 10, "embedded": 10, "missing": 0}, args=["--backfill-limit", "0"])
    assert fake.calls == [] and s["embedding_backfill"] == {"not_run": "disabled"}


# --- the backfill script itself: bounded, newest first, fail-open per row, aborts on a dead embedder ---

def _load_backfill_script():
    spec = _ilu.spec_from_file_location("episode_embed_backfill", REPO_ROOT / "scripts" / "wsl" / "episode-embed-backfill.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Emb:
    def __init__(self, fail_from=None):
        self.n, self.fail_from = 0, fail_from

    def embed(self, text, memory_action=None):
        self.n += 1
        if self.fail_from is not None and self.n >= self.fail_from:
            raise RuntimeError("embedder down")
        return [0.1, 0.2, 0.3]


def _backfill_db(tmp_path, n=8):
    from episodic import _connect_to, init_schema
    conn = _connect_to(tmp_path / "b.db")
    init_schema(conn)
    conn.execute("INSERT INTO sessions (session_id, started_at, brand) VALUES ('s1', 'a', 'brand-x')")
    for i in range(1, n + 1):
        conn.execute("INSERT INTO episodes (id, session_id, started_at, ended_at, goal_text, summary_text, state) "
                     "VALUES (?, 's1', 'a', 'b', 'g', ?, 'complete')", (i, "summary text " * 8))
    conn.execute("INSERT INTO episodes (id, session_id, started_at, ended_at, goal_text, summary_text, state) "
                 "VALUES (99, 's1', 'a', 'b', '', ?, 'in_progress')", ("checkpoint " * 12,))
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def test_backfill_script_respects_limit_newest_first_and_reports_remaining(tmp_path):
    mod = _load_backfill_script()
    conn = _backfill_db(tmp_path)
    seen = []
    out = mod.backfill(conn, {8}, _Emb(), lambda ep, vec, payload: seen.append(ep), limit=3)
    conn.close()
    assert seen == [7, 6, 5], "newest first, existing (8) skipped, capped at the limit"
    assert out["embedded"] == 3 and out["remaining"] == 4 and out["errors"] == 0
    assert 99 not in seen, "an in_progress checkpoint is never indexed"


def test_backfill_script_without_a_limit_embeds_everything_oldest_first(tmp_path):
    mod = _load_backfill_script()
    conn = _backfill_db(tmp_path, n=4)
    seen = []
    out = mod.backfill(conn, set(), _Emb(), lambda ep, vec, payload: seen.append((ep, payload["brand"])))
    conn.close()
    assert seen == [(1, "brand-x"), (2, "brand-x"), (3, "brand-x"), (4, "brand-x")]
    assert out["embedded"] == 4 and out["remaining"] == 0


def test_backfill_script_stops_after_consecutive_embed_failures(tmp_path):
    mod = _load_backfill_script()
    conn = _backfill_db(tmp_path, n=20)
    out = mod.backfill(conn, set(), _Emb(fail_from=3), lambda ep, vec, payload: None, limit=500)
    conn.close()
    assert out["embedded"] == 2 and out["errors"] == mod.MAX_CONSECUTIVE_ERRORS
    assert out["aborted"] and out["remaining"] == 18


# --- 1.32.4: the backfill rides out a cold start, diffs first, and reports a verdict ---------------------
#
# Its first run after an embedder restart left four vectors missing (500 'exited prematurely' while the seat
# started; the second run embedded them), and nothing told the operator. Cold-shaped failures are retried per
# row inside a RUN budget (episode_embeddings.embed_with_cold_retry); a seat that does not come up aborts the
# run as 'embedder-down'; the SQL-eligible minus Qdrant-ids diff comes first so a run with nothing missing
# makes no embedder call at all; and under ams-step it writes the one outcome line the chain reads.

COLD_START_BODY = {"error": {"message": "unspecific error: upstream command exited prematurely",
                             "type": "server_error"}}
CTX_OVERFLOW_BODY = {"error": {"message": "input is too large to process", "type": "server_error"}}
_REQ = httpx.Request("POST", "http://embedder.invalid/v1/embeddings")


def _http_error(status, body):
    return httpx.HTTPStatusError(f"HTTP {status}", request=_REQ,
                                 response=httpx.Response(status, json=body, request=_REQ))


class _ScriptedEmb:
    """embed() raises each queued exception in turn (None = succeed), then succeeds forever."""

    def __init__(self, *script):
        self.script, self.n = list(script), 0

    def embed(self, text, memory_action=None):
        self.n += 1
        err = self.script.pop(0) if self.script else None
        if err is not None:
            raise err
        return [0.1, 0.2, 0.3]


class _FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def _run_backfill(tmp_path, emb, n=1, existing=None, limit=None, **kw):
    mod, clock, written = _load_backfill_script(), _FakeClock(), []
    conn = _backfill_db(tmp_path, n=n)
    out = mod.backfill(conn, set(existing or ()), emb, lambda ep, vec, payload: written.append(ep), limit=limit,
                       sleep=clock.sleep, clock=clock.monotonic, **kw)
    conn.close()
    return out, clock, written


@pytest.mark.parametrize("cold", [
    lambda: _http_error(500, COLD_START_BODY),
    lambda: _http_error(503, {"error": "loading"}),
    lambda: httpx.ConnectError("refused"),
], ids=["500-exited-prematurely", "503", "connect-error"])
def test_backfill_retries_one_cold_failure_and_embeds_the_row(tmp_path, cold):
    emb = _ScriptedEmb(cold())
    out, clock, written = _run_backfill(tmp_path, emb, n=1)
    assert emb.n == 2 and out["embedded"] == 1 and out["errors"] == 0 and out["aborted"] is None
    assert written == [1] and len(clock.sleeps) == 1 and clock.sleeps[0] >= 10


def test_backfill_does_not_retry_a_context_overflow_500(tmp_path):
    emb = _ScriptedEmb(_http_error(500, CTX_OVERFLOW_BODY))
    out, clock, written = _run_backfill(tmp_path, emb, n=3)
    assert emb.n == 3 and clock.sleeps == [], "a request that can never fit is not replayed"
    assert out["errors"] == 1 and out["embedded"] == 2 and out["aborted"] is None


def test_backfill_aborts_as_embedder_down_when_the_seat_never_comes_up_without_burning_the_budget(tmp_path):
    emb = _ScriptedEmb(*[httpx.ConnectError("refused")] * 50)
    out, clock, written = _run_backfill(tmp_path, emb, n=20)
    assert out["aborted"] == "embedder-down" and out["errors"] >= 1 and out["embedded"] == 0
    assert emb.n == 3 and clock.sleeps == [10, 20], "one row's retries, then stop: not five rows of them"
    assert out["remaining"] == 20 and written == []


def test_backfill_run_budget_is_shared_across_rows(tmp_path):
    """Each cold row spends from ONE run budget: a flapping seat cannot cost budget-per-row."""
    cold = httpx.ConnectError("refused")
    emb = _ScriptedEmb(cold, None, cold, cold, cold)       # row 1 recovers after 10 s; row 2 never does
    out, clock, written = _run_backfill(tmp_path, emb, n=6, retry_budget_s=15)
    assert clock.sleeps == [10], "row 1 spent 10 of the 15 s: row 2 cannot afford its own 10 s wait"
    assert emb.n == 3 and out["embedded"] == 1 and out["aborted"] == "embedder-down" and out["remaining"] == 5


def test_backfill_still_stops_after_consecutive_plain_failures(tmp_path):
    out, clock, _ = _run_backfill(tmp_path, _Emb(fail_from=3), n=20, limit=500)
    assert out["embedded"] == 2 and out["aborted"] and out["aborted"] != "embedder-down"
    assert out["errors"] == _load_backfill_script().MAX_CONSECUTIVE_ERRORS and clock.sleeps == []


def test_backfill_with_nothing_missing_makes_zero_embedder_calls(tmp_path):
    emb = _ScriptedEmb()
    out, clock, written = _run_backfill(tmp_path, emb, n=4, existing={1, 2, 3, 4})
    assert emb.n == 0 and written == [] and clock.sleeps == []
    assert out["embedded"] == 0 and out["missing"] == 0 and out["remaining"] == 0 and out["missing_ids"] == []


def test_backfill_reports_the_exact_per_id_gap(tmp_path):
    """The coverage probe is a count diff (SQL TRIM vs Python strip, stale points masking real gaps); the
    backfill's own diff is per id, so a gap of 4 reads as 4 ids, not '99 %'."""
    out, _, written = _run_backfill(tmp_path, _ScriptedEmb(), n=8, existing={8}, limit=3)
    assert written == [7, 6, 5]
    assert out["missing"] == 7 and out["missing_ids"] == [7, 6, 5, 4, 3, 2, 1]
    assert out["remaining"] == 4 and out["remaining_ids"] == [4, 3, 2, 1]


def test_a_whitespace_padded_short_summary_is_not_counted_missing(tmp_path):
    """Eligibility is Python strip() >= 64 chars: a row SQLite's TRIM would count (tabs and newlines survive
    TRIM) but the indexer never embeds must not show up as a missing vector."""
    mod = _load_backfill_script()
    conn = _backfill_db(tmp_path, n=2)
    conn.execute("UPDATE episodes SET summary_text = ? WHERE id = 1", ("\t\n" * 40 + "short",))
    conn.commit()
    out = mod.backfill(conn, set(), _ScriptedEmb(), lambda *a: None)
    conn.close()
    assert out["missing_ids"] == [2] and out["embedded"] == 1


def _core(tmp_path, n=4, existing=(), embedder=None, wait_result=True, **kw):
    mod, clock = _load_backfill_script(), _FakeClock()
    conn = _backfill_db(tmp_path, n=n)
    calls = {"embedder": 0, "wait": []}
    written = []

    def make_embedder():
        calls["embedder"] += 1
        return embedder or _ScriptedEmb()

    def wait(total_s):
        calls["wait"].append(total_s)
        return wait_result

    out = mod._run_core(conn, set(existing), make_embedder, lambda ep, vec, payload: written.append(ep),
                        wait=wait, sleep=clock.sleep, clock=clock.monotonic, **kw)
    conn.close()
    return out, calls, written


def test_run_with_nothing_missing_builds_no_embedder_and_does_not_wait(tmp_path):
    out, calls, written = _core(tmp_path, n=3, existing={1, 2, 3}, wait_embedder_s=120)
    assert calls == {"embedder": 0, "wait": []} and written == []
    assert out["missing"] == 0 and out["embedded"] == 0 and out["remaining"] == 0


def test_run_dry_run_reports_the_gap_and_touches_nothing(tmp_path):
    out, calls, written = _core(tmp_path, n=4, existing={4}, dry_run=True, wait_embedder_s=120)
    assert calls == {"embedder": 0, "wait": []} and written == []
    assert out["missing"] == 3 and out["missing_ids"] == [1, 2, 3] and out["embedded"] == 0
    assert out["remaining"] == 3 and out["dry_run"] is True


def test_run_waits_for_the_embedder_only_when_something_is_missing(tmp_path):
    out, calls, written = _core(tmp_path, n=3, existing={1}, wait_embedder_s=120)
    assert calls["wait"] == [120] and calls["embedder"] == 1 and written == [2, 3] and out["embedded"] == 2


def test_run_that_cannot_reach_the_embedder_aborts_without_building_it(tmp_path):
    out, calls, written = _core(tmp_path, n=3, wait_embedder_s=120, wait_result=False)
    assert calls["embedder"] == 0 and written == []
    assert out["aborted"] == "embedder-down" and out["remaining"] == 3 and out["embedded"] == 0


def test_run_without_a_wait_window_goes_straight_to_the_embedder(tmp_path):
    out, calls, written = _core(tmp_path, n=2)
    assert calls == {"embedder": 1, "wait": []} and written == [1, 2]


@pytest.mark.parametrize("res, expected", [
    ({"embedded": 5, "remaining": 0, "errors": 0}, "ok"),
    ({"embedded": 0, "remaining": 0, "errors": 0, "missing": 0}, "ok"),
    ({"embedded": 0, "remaining": 4, "errors": 0, "aborted": "embedder-down"}, "degraded:embedder-down"),
    ({"embedded": 2, "remaining": 18, "errors": 5, "aborted": "5 consecutive embed failures (last: x)"},
     "degraded:embed-errors-5"),
    ({"embedded": 9, "remaining": 1, "errors": 1}, "degraded:embed-errors-1"),
    ({"embedded": 200, "remaining": 800, "errors": 0}, "degraded:remaining-800"),
    ({"embedded": 0, "error": "ConnectError: qdrant"}, "degraded:backfill-failed"),
])
def test_backfill_outcome_verdicts(res, expected):
    assert _load_backfill_script().outcome_for(res) == expected


def _main_script(monkeypatch, tmp_path, res, argv=(), outcome=True):
    mod = _load_backfill_script()
    seen = {}

    def fake_run(**kw):
        seen.update(kw)
        return dict(res)

    monkeypatch.setattr(mod, "run", fake_run)
    out_file = tmp_path / "outcome.txt"
    if outcome:
        monkeypatch.setenv("AMS_OUTCOME_FILE", str(out_file))
    else:
        monkeypatch.delenv("AMS_OUTCOME_FILE", raising=False)
    return mod, mod.main(list(argv)), seen, out_file


def test_script_cli_flags_reach_run(monkeypatch, tmp_path):
    ok = {"embedded": 0, "skipped": 0, "errors": 0, "remaining": 0, "total_complete": 0, "missing": 0}
    _, rc, seen, _ = _main_script(monkeypatch, tmp_path, ok, [
        "--limit", "7", "--db", str(tmp_path / "x.db"), "--wait-embedder", "90", "--retry-budget-s", "45"])
    assert rc == 0
    assert seen == {"limit": 7, "db_path": str(tmp_path / "x.db"), "wait_embedder_s": 90.0,
                    "retry_budget_s": 45.0, "dry_run": False}


def test_script_with_no_arguments_keeps_the_hand_run_behaviour(monkeypatch, tmp_path):
    ok = {"embedded": 3, "skipped": 0, "errors": 0, "remaining": 0, "total_complete": 3, "missing": 3}
    _, rc, seen, _ = _main_script(monkeypatch, tmp_path, ok, outcome=False)
    assert rc == 0 and seen["limit"] is None, "all rows, oldest first"
    assert seen["wait_embedder_s"] == 0


def test_script_rejects_a_zero_limit(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _main_script(monkeypatch, tmp_path, {}, ["--limit", "0"])


@pytest.mark.parametrize("res, status", [
    ({"embedded": 4, "skipped": 0, "errors": 0, "remaining": 0, "total_complete": 4, "missing": 4}, "ok"),
    ({"embedded": 0, "skipped": 0, "errors": 1, "remaining": 4, "total_complete": 4, "missing": 4,
      "aborted": "embedder-down"}, "degraded:embedder-down"),
    ({"embedded": 1, "skipped": 0, "errors": 2, "remaining": 3, "total_complete": 4, "missing": 4},
     "degraded:embed-errors-2"),
    ({"embedded": 2, "skipped": 0, "errors": 0, "remaining": 6, "total_complete": 8, "missing": 8},
     "degraded:remaining-6"),
])
def test_script_writes_the_ams_step_outcome_line_and_exits_zero_for_a_reported_state(
        monkeypatch, tmp_path, res, status):
    _, rc, _, out_file = _main_script(monkeypatch, tmp_path, res)
    assert rc == 0, "a degraded state under ams-step is reported through the line, not the exit code"
    got, _, body = out_file.read_text(encoding="utf-8").strip().partition(" ")
    assert got == status
    counts = _json.loads(body)
    assert counts["embedded"] == res["embedded"] and counts["remaining"] == res["remaining"]
    assert counts["errors"] == res["errors"] and counts["missing"] == res["missing"]


def test_script_without_the_outcome_file_keeps_the_legacy_exit_one_on_errors(monkeypatch, tmp_path):
    bad = {"embedded": 1, "skipped": 0, "errors": 2, "remaining": 3, "total_complete": 4, "missing": 4}
    _, rc, _, out_file = _main_script(monkeypatch, tmp_path, bad, outcome=False)
    assert rc == 1 and not out_file.exists()
    clean = {"embedded": 4, "skipped": 0, "errors": 0, "remaining": 0, "total_complete": 4, "missing": 4}
    assert _main_script(monkeypatch, tmp_path, clean, outcome=False)[1] == 0


def test_dry_run_with_a_limit_lists_the_ids_in_the_order_the_real_run_would_take_them(tmp_path):
    out, calls, _ = _core(tmp_path, n=6, existing={6}, dry_run=True, limit=2)
    assert out["missing_ids"] == [5, 4, 3, 2, 1] and calls["embedder"] == 0


def test_the_embedder_is_built_only_inside_the_gap_gate():
    """build_embedder imports mem0 and wires llama-swap: it must be reachable only through the factory that
    _run_core calls once something is missing, never at import time or on a nothing-missing run."""
    import ast
    src = (REPO_ROOT / "scripts" / "wsl" / "episode-embed-backfill.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    holders = [fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
               for n in ast.walk(fn) if isinstance(n, ast.ImportFrom) and n.module == "config"]
    assert holders and set(holders) <= {"make_embedder", "run"}, holders
    assert not [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
                and getattr(n, "module", None) == "config"]


# --- 1.32.4: the daily upkeep closes stale checkpoints and retries missed vectors ----------------------
#
# `abandon_stale_in_progress` shipped in 1.32.0 but only runs in the Sunday chain step, after the Qdrant
# readiness gate, with no dry-run and a bare count for a receipt: 1,063 episodes sat `in_progress` (1,009 of
# them older than 7 days) and nobody could see what the first run would touch. It now selects ids and closes
# them under one write lock, says which, runs before the Qdrant gate (it is SQLite-only), and a new
# `--upkeep` mode (the daily chain step) does that plus the bounded vector backfill and nothing else.

_DAY = _dtmod.timedelta(days=1)


def _iso_ago(days):
    return (_NOW - days * _DAY).isoformat()


def _stale_ledger(tmp_path):
    return _real_ledger(tmp_path, [
        ("in_progress", _iso_ago(30)),      # 1: the oldest orphan
        ("in_progress", _iso_ago(10)),      # 2
        ("in_progress", _iso_ago(1)),       # 3: a live session's checkpoint
        ("complete", _iso_ago(40)),         # 4: finished
        ("in_progress", _iso_ago(8)),       # 5
    ])


def _ledger_snapshot(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT id, session_id, started_at, ended_at, goal_text, summary_text FROM episodes "
                         "ORDER BY id").fetchall()
    finally:
        c.close()


def test_sweep_dry_run_reports_what_it_would_close_and_writes_nothing(tmp_path):
    db = _stale_ledger(tmp_path)
    before = db.read_bytes()
    info = recon.sweep_stale_in_progress(db, days=7, now=_NOW, dry_run=True)
    assert info["would_abandon"] == 3 and info["abandoned"] == 0 and info["dry_run"] is True
    assert [s["id"] for s in info["abandoned_sample"]] == [1, 2, 5]
    assert info["in_progress_remaining"] == 4
    assert db.read_bytes() == before, "not one byte of the ledger changed"
    assert _states(db) == {1: "in_progress", 2: "in_progress", 3: "in_progress", 4: "complete", 5: "in_progress"}


def test_sweep_receipt_names_the_rows_it_closed(tmp_path):
    db = _stale_ledger(tmp_path)
    info = recon.sweep_stale_in_progress(db, days=7, now=_NOW, sample_cap=2)
    assert info["abandoned"] == 3 and info["would_abandon"] == 3 and info["dry_run"] is False
    assert info["abandoned_sample"] == [{"id": 1, "session_id": "s1", "ended_at": _iso_ago(30)},
                                        {"id": 2, "session_id": "s2", "ended_at": _iso_ago(10)}], \
        "oldest first, capped by sample_cap"
    assert info["abandoned_oldest_ended_at"] == _iso_ago(30)
    assert info["in_progress_remaining"] == 1, "only the live session's checkpoint is left"
    assert _states(db) == {1: "abandoned", 2: "abandoned", 3: "in_progress", 4: "complete", 5: "abandoned"}
    again = recon.sweep_stale_in_progress(db, days=7, now=_NOW)
    assert again["abandoned"] == 0 and again["abandoned_oldest_ended_at"] is None, "idempotent"


def test_sweep_never_deletes_or_rewrites_a_row(tmp_path):
    db = _stale_ledger(tmp_path)
    before = _ledger_snapshot(db)
    recon.sweep_stale_in_progress(db, days=7, now=_NOW)
    assert _ledger_snapshot(db) == before, "only the state column moves: no row deleted, no text rewritten"


def test_a_row_that_stopped_being_stale_after_the_select_is_not_clobbered(tmp_path):
    db = _stale_ledger(tmp_path)
    cutoff = _iso_ago(7)
    conn = sqlite3.connect(db)
    ids = [r[0] for r in recon.select_stale_in_progress(conn, cutoff)]
    assert ids == [1, 2, 5]
    # between the SELECT and the UPDATE: episode 1 is finalized, episode 2 gets a fresh checkpoint
    conn.execute("UPDATE episodes SET state = 'complete' WHERE id = 1")
    conn.execute("UPDATE episodes SET ended_at = ? WHERE id = 2", (_NOW.isoformat(),))
    conn.commit()
    assert recon.abandon_ids(conn, ids, cutoff) == 1
    conn.commit()
    conn.close()
    assert _states(db) == {1: "complete", 2: "in_progress", 3: "in_progress", 4: "complete", 5: "abandoned"}


def test_a_competing_writer_is_locked_out_between_the_select_and_the_update(tmp_path, monkeypatch):
    """The checkpoint hook writes to the same ledger. The sweep takes the write lock BEFORE it selects
    (BEGIN IMMEDIATE), so a checkpoint waits for the commit and then opens a fresh row instead of
    bumping one the sweep is about to close."""
    db = _stale_ledger(tmp_path)
    real_select = recon.select_stale_in_progress
    seen = {}

    def spying_select(conn, cutoff):
        rows = real_select(conn, cutoff)
        other = sqlite3.connect(db, timeout=0.1)
        try:
            other.execute("UPDATE episodes SET ended_at = ? WHERE id = 2", (_NOW.isoformat(),))
            other.commit()
            seen["locked"] = False
        except sqlite3.OperationalError as e:
            seen["locked"] = "locked" in str(e)
        finally:
            other.close()
        return rows

    monkeypatch.setattr(recon, "select_stale_in_progress", spying_select)
    info = recon.sweep_stale_in_progress(db, days=7, now=_NOW)
    assert seen == {"locked": True} and info["abandoned"] == 3


def test_the_sweep_rolls_back_when_it_fails_midway(tmp_path, monkeypatch):
    db = _stale_ledger(tmp_path)

    def boom(conn, ids, cutoff):
        conn.execute("UPDATE episodes SET state = 'abandoned' WHERE id = 1")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(recon, "abandon_ids", boom)
    with pytest.raises(sqlite3.OperationalError):
        recon.sweep_stale_in_progress(db, days=7, now=_NOW)
    assert _states(db)[1] == "in_progress", "a half-done sweep leaves the ledger as it was"
    info, err = recon.try_sweep_stale(db, days=7, now=_NOW)
    assert info["abandoned"] == 0 and "disk I/O error" in err


def test_the_default_staleness_window_is_still_seven_days(monkeypatch, tmp_path):
    assert recon.STALE_IN_PROGRESS_DAYS == 7
    db = _real_ledger(tmp_path, [("in_progress", (_dtmod.datetime.now(_dtmod.timezone.utc) - 6 * _DAY).isoformat()),
                                 ("in_progress", (_dtmod.datetime.now(_dtmod.timezone.utc) - 8 * _DAY).isoformat())])
    r = _main_run(monkeypatch, tmp_path, ["--upkeep"], db=db)
    assert _states(db) == {1: "in_progress", 2: "abandoned"} and r.rc == 0


class _Boom:
    def __init__(self, name):
        self.name = name

    def __call__(self, *a, **kw):
        raise AssertionError(f"{self.name} must not run in this mode")


def _main_run(monkeypatch, tmp_path, args=(), rows=None, backfill=None, coverage=None, http_get=None,
              outcome=True, db=None):
    """main() over a real-schema ledger. --upkeep makes every orphan / drift / coverage / Qdrant seam raise."""
    db = db or _real_ledger(tmp_path, rows if rows is not None else [("in_progress", _iso_ago(20))])
    fake = backfill or _FakeBackfill()
    monkeypatch.setattr(recon, "_load_backfill", lambda: fake)
    summaries = []
    monkeypatch.setattr(recon, "_append_summary", lambda rec: summaries.append(rec))
    out_file = tmp_path / "outcome.txt"
    if outcome:
        monkeypatch.setenv("AMS_OUTCOME_FILE", str(out_file))
    else:
        monkeypatch.delenv("AMS_OUTCOME_FILE", raising=False)
    healthy = _types.SimpleNamespace(raise_for_status=lambda: None)
    if "--upkeep" in args:
        for name in ("read_episode_links", "existing_episode_ids", "embedding_coverage", "qdrant_present_ids",
                     "history_deleted_ids", "history_delete_row_count", "ledger_deleted"):
            monkeypatch.setattr(recon, name, _Boom(name))
        monkeypatch.setattr(recon.httpx, "get", http_get or _Boom("httpx.get (the Qdrant gate / embedder probe)"))
    else:
        monkeypatch.setattr(recon, "embedding_coverage",
                            lambda conn, http: dict(coverage or {"eligible": 10, "embedded": 10, "missing": 0}))
        monkeypatch.setattr(recon, "history_deleted_ids", lambda ids, db_path=None: set())
        monkeypatch.setattr(recon, "history_delete_row_count", lambda db_path=None: 1)
        monkeypatch.setattr(recon, "ledger_deleted", lambda ids, ledger_dir=None: {})
        monkeypatch.setattr(recon, "qdrant_present_ids", lambda http, ids: set())
        monkeypatch.setattr(recon.httpx, "get", http_get or (lambda url, **kw: healthy))
    monkeypatch.setattr(_sys, "argv", ["episodic-reconcile.py", "--db", str(db), *args])
    rc = recon.main()
    return _types.SimpleNamespace(rc=rc, summaries=summaries, fake=fake, outcome_file=out_file, db=db)


def _outcome(r):
    status, _, body = r.outcome_file.read_text(encoding="utf-8").strip().partition(" ")
    return status, _json.loads(body)


def test_the_weekly_sweep_runs_before_the_qdrant_gate(monkeypatch, tmp_path):
    """The sweep is SQLite-only: a Qdrant outage must not stop it (it used to sit after the readiness gate)."""
    def qdrant_down(url, **kw):
        raise httpx.ConnectError("qdrant down")

    r = _main_run(monkeypatch, tmp_path, http_get=qdrant_down, outcome=False)
    assert r.rc == 1 and _states(r.db) == {1: "abandoned"}
    s = r.summaries[-1]
    assert s["outcome"] == "degraded:qdrant-unreachable" and s["abandoned_stale_in_progress"] == 1
    assert s["abandoned_sample"][0]["id"] == 1 and r.fake.calls == []


def test_the_weekly_receipt_carries_the_sweep_fields(monkeypatch, tmp_path):
    r = _main_run(monkeypatch, tmp_path, ["--limit-sample", "1"], rows=[
        ("in_progress", _iso_ago(30)), ("in_progress", _iso_ago(20))])
    s = r.summaries[-1]
    assert s["abandoned_stale_in_progress"] == 2 and s["would_abandon"] == 2 and s["dry_run"] is False
    assert [x["id"] for x in s["abandoned_sample"]] == [1], "capped by --limit-sample"
    assert s["abandoned_oldest_ended_at"] == _iso_ago(30) and s["in_progress_remaining"] == 0


def test_main_dry_run_changes_nothing_and_appends_no_receipt(monkeypatch, tmp_path, capsys):
    r = _main_run(monkeypatch, tmp_path, ["--dry-run"])
    assert _states(r.db) == {1: "in_progress"}, "nothing abandoned"
    assert r.summaries == [] and r.fake.calls == [] and not r.outcome_file.exists()
    out = capsys.readouterr().out
    assert '"would_abandon": 1' in out and '"dry_run": true' in out


def test_upkeep_closes_stale_rows_and_backfills_and_skips_every_other_pass(monkeypatch, tmp_path):
    bf = _FakeBackfill({"embedded": 3, "skipped": 0, "errors": 0, "remaining": 0, "total_complete": 3,
                        "missing": 3, "missing_ids": [4, 5, 6], "remaining_ids": []})
    r = _main_run(monkeypatch, tmp_path, ["--upkeep"], backfill=bf)
    assert r.rc == 0 and _states(r.db) == {1: "abandoned"}
    assert bf.calls == [{"limit": 200, "db_path": r.db, "dry_run": False, "wait_embedder_s": recon.EMBEDDER_WAIT_S}]
    s = r.summaries[-1]
    assert s["mode"] == "upkeep" and s["outcome"] == "ok" and s["abandoned_stale_in_progress"] == 1
    assert not {"orphaned_count", "dangling_count", "embedding_coverage", "total_links"} & set(s)
    status, counts = _outcome(r)
    assert status == "ok" and counts["abandoned"] == 1 and counts["embedded"] == 3 and counts["missing"] == 3
    assert counts["remaining"] == 0 and counts["in_progress_remaining"] == 0


def test_upkeep_backfill_limit_defaults_to_200_and_zero_disables_it(monkeypatch, tmp_path):
    assert recon.UPKEEP_BACKFILL_PER_RUN == 200 and recon.BACKFILL_PER_RUN == 500
    r = _main_run(monkeypatch, tmp_path, ["--upkeep", "--backfill-limit", "7"])
    assert r.fake.calls[0]["limit"] == 7
    zero = tmp_path / "zero"
    zero.mkdir()
    r = _main_run(monkeypatch, zero, ["--upkeep", "--backfill-limit", "0"])
    assert r.fake.calls == [] and r.summaries[-1]["embedding_backfill"] == {"not_run": "disabled"}
    assert r.summaries[-1]["outcome"] == "ok"


def test_the_weekly_mode_keeps_its_500_embed_cap(monkeypatch, tmp_path):
    r = _main_run(monkeypatch, tmp_path)
    assert r.fake.calls == [{"limit": 500, "db_path": r.db}]


@pytest.mark.parametrize("backfill, status", [
    (_FakeBackfill({"embedded": 0, "skipped": 0, "errors": 1, "remaining": 4, "total_complete": 4, "missing": 4,
                    "aborted": "embedder-down"}), "degraded:embedder-down"),
    (_FakeBackfill({"embedded": 1, "skipped": 0, "errors": 2, "remaining": 3, "total_complete": 4, "missing": 4}),
     "degraded:embed-errors-2"),
    (_FakeBackfill({"embedded": 200, "skipped": 0, "errors": 0, "remaining": 800, "total_complete": 1000,
                    "missing": 1000}), "degraded:remaining-800"),
    (_FakeBackfill(error=httpx.ConnectError("qdrant down")), "degraded:backfill-failed"),
], ids=["embedder-down", "embed-errors", "remaining", "backfill-failed"])
def test_upkeep_reports_a_degraded_state_through_the_outcome_line_and_exits_zero(monkeypatch, tmp_path, backfill, status):
    r = _main_run(monkeypatch, tmp_path, ["--upkeep"], backfill=backfill)
    assert r.rc == 0, "reported, not failed: the chain's next steps still run"
    assert _outcome(r)[0] == status and r.summaries[-1]["outcome"] == status
    assert _states(r.db) == {1: "abandoned"}, "the SQLite sweep still ran: a Qdrant or embedder outage never stops it"


def test_upkeep_names_every_reason_when_the_sweep_and_the_backfill_both_fail(monkeypatch, tmp_path):
    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recon, "sweep_stale_in_progress", locked)
    bf = _FakeBackfill({"embedded": 0, "skipped": 0, "errors": 0, "remaining": 2, "total_complete": 2, "missing": 2})
    r = _main_run(monkeypatch, tmp_path, ["--upkeep"], backfill=bf)
    assert r.rc == 0 and _outcome(r)[0] == "degraded:abandon-failed,remaining-2"
    assert "database is locked" in r.summaries[-1]["abandon_error"]


def test_upkeep_receipt_reports_the_exact_per_id_gap(monkeypatch, tmp_path):
    """A coverage percentage reads '99 % ok' with 4 vectors missing; the receipt lists the ids."""
    bf = _FakeBackfill({"embedded": 0, "skipped": 0, "errors": 0, "remaining": 4, "total_complete": 3583,
                        "missing": 4, "missing_ids": [101, 102, 103, 104], "remaining_ids": [101, 102, 103, 104]})
    r = _main_run(monkeypatch, tmp_path, ["--upkeep", "--limit-sample", "2"], backfill=bf)
    s = r.summaries[-1]
    assert s["missing"] == 4 and s["remaining"] == 4
    assert s["missing_ids"] == [101, 102] and s["remaining_ids"] == [101, 102], "ids capped by --limit-sample"
    assert "missing_ids" not in s["embedding_backfill"] and s["embedding_backfill"]["missing"] == 4
    assert _outcome(r)[0] == "degraded:remaining-4"


def test_upkeep_dry_run_reports_and_writes_nothing(monkeypatch, tmp_path, capsys):
    bf = _FakeBackfill({"embedded": 0, "skipped": 0, "errors": 0, "remaining": 4, "total_complete": 10,
                        "missing": 4, "missing_ids": [7, 8, 9, 10], "remaining_ids": [7, 8, 9, 10], "dry_run": True})
    r = _main_run(monkeypatch, tmp_path, ["--upkeep", "--dry-run"], backfill=bf)
    assert r.rc == 0 and _states(r.db) == {1: "in_progress"}
    assert bf.calls == [{"limit": 200, "db_path": r.db, "dry_run": True, "wait_embedder_s": recon.EMBEDDER_WAIT_S}]
    assert r.summaries == [] and not r.outcome_file.exists()
    out = capsys.readouterr().out
    assert '"would_abandon": 1' in out and '"missing": 4' in out and '"mode": "upkeep"' in out


def test_upkeep_without_a_ledger_is_a_failed_run(monkeypatch, tmp_path):
    summaries = []
    monkeypatch.setattr(recon, "_append_summary", lambda rec: summaries.append(rec))
    monkeypatch.setattr(_sys, "argv", ["episodic-reconcile.py", "--upkeep", "--db", str(tmp_path / "absent.db")])
    assert recon.main() == 1
    assert summaries[-1]["outcome"] == "degraded:no-episodic-db" and summaries[-1]["mode"] == "upkeep"


@pytest.mark.parametrize("res", [
    {"embedded": 5, "remaining": 0, "errors": 0},
    {"embedded": 0, "remaining": 4, "errors": 1, "aborted": "embedder-down"},
    {"embedded": 2, "remaining": 18, "errors": 5, "aborted": "5 consecutive embed failures (last: x)"},
    {"embedded": 9, "remaining": 1, "errors": 1},
    {"embedded": 200, "remaining": 800, "errors": 0},
    {"embedded": 0, "error": "ConnectError: qdrant"},
])
def test_the_reconcile_and_the_backfill_script_read_a_result_the_same_way(res):
    reason = recon.backfill_reason(res)
    assert ("ok" if reason is None else f"degraded:{reason}") == _load_backfill_script().outcome_for(res)


def test_the_weekly_step_unit_and_the_wsl_timer_keep_their_schedule():
    """The Sunday episodic-reconcile step stays as it is: the daily work is its own step."""
    unit = (REPO_ROOT / "systemd" / "ams-step-episodic-reconcile.service").read_text(encoding="utf-8")
    assert "--guarded --weekly Sun episodic-reconcile" in unit and "--upkeep" not in unit
