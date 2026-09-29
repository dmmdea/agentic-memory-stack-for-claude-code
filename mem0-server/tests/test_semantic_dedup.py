"""scripts/wsl/semantic-dedup.py: the nightly near-duplicate sweep must WORK and PROVE it worked.

For weeks it compared nothing: every point's vector is a named-vector dict
({"": [dense...], "bm25": {"indices": [...], "values": [...]}}) and the old loop skipped anything
that was not a bare list, then reported `ok, deletions 0`. These tests pin the two things that
were missing: the extractor handles the real shape, and every run carries work counts a health
gate can evaluate. Fixtures are synthetic - no live Qdrant, no mem0 (httpx.MockTransport).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "semantic_dedup", REPO_ROOT / "scripts" / "wsl" / "semantic-dedup.py")
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)

DIM = 8


def _named(vec):
    """The shape Qdrant returns for with_vector=true on the live collection."""
    return {"": list(vec), "bm25": {"indices": [1, 5, 9], "values": [0.5, 0.25, 0.125]}}


def _unit(axis, wobble=0.0):
    v = np.zeros(DIM, dtype=np.float64)
    v[axis] = 1.0
    v[(axis + 1) % DIM] = wobble
    return v.tolist()


def _pt(pid, vec, tier="evidence", created="2026-08-01T00:00:00+00:00", source="l1a-extractor",
        user="u1", workspace="ws", project="p", raw=False):
    return {"id": pid, "vector": vec if raw else _named(vec),
            "payload": {"tier": tier, "created_at": created, "source": source, "user_id": user,
                        "workspace": workspace, "project": project, "data": f"text {pid}"}}


OLD, NEW = "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"


def test_dedup_named_vector_shape():
    """The finding itself: a near-identical evidence pair in the REAL named-vector shape must be
    compared and found. Under the old isinstance(list) guard scanned==2, compared==0."""
    pts = [_pt("a", _unit(0), created=OLD), _pt("b", _unit(0, 0.01), created=NEW)]
    decisions, stats = sd.plan_dedup(pts, max_deletions=50)
    assert stats["scanned"] == 2
    assert stats["skipped_no_vector"] == 0
    assert stats["compared_pairs"] == 1
    assert stats["candidates"] == 1
    assert [(d["deleted_id"], d["kept_id"]) for d in decisions] == [("b", "a")]  # keep the older


def test_dedup_counts_points_it_cannot_compare():
    """A point with no dense vector (sparse-only, missing, wrong dimension) is COUNTED, never a
    silent continue."""
    pts = [_pt("a", _unit(0)), _pt("b", _unit(0)),
           {"id": "sparse-only", "vector": {"bm25": {"indices": [1], "values": [1.0]}},
            "payload": {"tier": "evidence", "user_id": "u1"}},
           {"id": "no-vector", "payload": {"tier": "evidence", "user_id": "u1"}},
           _pt("short", [1.0, 0.0])]
    _, stats = sd.plan_dedup(pts, max_deletions=50)
    assert stats["scanned"] == 5
    assert stats["skipped_no_vector"] == 3
    assert stats["compared_pairs"] == 1


def test_dedup_cap_and_exemptions():
    """Review focus 4: the first working run must not delete more than its cap and must never
    delete a canonical, an operator-sourced insight or an automemory-sourced record."""
    pts = [
        # two evidence twin pairs on different axes: the only legitimate candidates
        _pt("e1-old", _unit(0), created=OLD), _pt("e1-new", _unit(0, 0.01), created=NEW),
        _pt("e2-old", _unit(2), created=OLD), _pt("e2-new", _unit(2, 0.01), created=NEW),
        # canonical twins: never deleted, either side
        _pt("c-old", _unit(4), tier="canonical", created=OLD, source="user-direct"),
        _pt("c-new", _unit(4, 0.01), tier="canonical", created=NEW, source="user-direct"),
        # automemory-migrated twins (both protected): the pair is left alone
        _pt("m-old", _unit(5), created=OLD, source="automemory:ws/a.md"),
        _pt("m-new", _unit(5, 0.01), created=NEW, source="automemory:ws/b.md"),
        # operator-sourced insight twins (not the consolidator's own output): left alone
        _pt("i-old", _unit(6), tier="insight", created=OLD, source="user-direct"),
        _pt("i-new", _unit(6, 0.01), tier="insight", created=NEW, source="user-direct"),
    ]
    decisions, stats = sd.plan_dedup(pts, max_deletions=50)
    assert {d["deleted_id"] for d in decisions} == {"e1-new", "e2-new"}
    assert stats["candidates"] == 2
    assert stats["deleted"] == 2
    # canonical never enters a comparison at all; the automemory and operator-insight pairs match
    # and are then left alone
    assert stats["protected_skips"] == 2
    assert stats["compared_pairs"] == 15 + 1   # 6 evidence-tier points + 2 insight-tier points

    capped, cstats = sd.plan_dedup(pts, max_deletions=1)
    assert cstats["candidates"] == 2       # the backlog is still reported ...
    assert cstats["deleted"] == 1          # ... but only one is acted on
    assert sum(1 for d in capped if d["within_cap"]) == 1
    assert cstats["capped"] is True
    protected = {"c-old", "c-new", "m-old", "m-new", "i-old", "i-new"}
    assert not protected & {d["deleted_id"] for d in capped if d["within_cap"]}


def test_consolidator_insights_still_dedup():
    """The exemption is for OPERATOR insights; the consolidator's own near-duplicate output is
    exactly what the dedup exists to drain."""
    pts = [_pt("i-old", _unit(1), tier="insight", created=OLD, source="c1-consolidator"),
           _pt("i-new", _unit(1, 0.01), tier="insight", created=NEW, source="c1-consolidator")]
    decisions, _ = sd.plan_dedup(pts, max_deletions=50)
    assert [d["deleted_id"] for d in decisions] == ["i-new"]


def test_dedup_respects_tier_threshold_and_partition():
    """Below the tier threshold, in another tier, or in another partition: not a duplicate."""
    far = _unit(0, 0.5)      # cosine ~0.894 vs axis 0: under every threshold
    pts = [_pt("a", _unit(0)), _pt("b", far),
           _pt("t", _unit(3), tier="stable"), _pt("t2", _unit(3, 0.01), tier="evidence"),
           _pt("p", _unit(7)), _pt("p2", _unit(7, 0.01), workspace="other-ws")]
    decisions, stats = sd.plan_dedup(pts, max_deletions=50)
    assert decisions == []
    assert stats["candidates"] == 0
    # only the (evidence, ws, p) group [a, b, t2, p] has more than one member: 4 choose 2 pairs
    assert stats["compared_pairs"] == 6


def test_chain_of_duplicates_deletes_each_record_once():
    """a~b~c all near-identical: the newest two go, the oldest survives, nothing is deleted twice."""
    pts = [_pt("a", _unit(0), created="2026-08-01T00:00:00+00:00"),
           _pt("b", _unit(0, 0.01), created="2026-08-02T00:00:00+00:00"),
           _pt("c", _unit(0, 0.02), created="2026-08-03T00:00:00+00:00")]
    decisions, stats = sd.plan_dedup(pts, max_deletions=50)
    ids = [d["deleted_id"] for d in decisions]
    assert sorted(ids) == ["b", "c"] and len(set(ids)) == 2
    assert stats["candidates"] == 2


def test_blocked_matmul_finds_the_same_pairs_as_a_single_block(monkeypatch):
    """The row-blocking is a memory bound, not a behaviour: a 1-row block gives the same answer."""
    rng = np.random.default_rng(7)
    base = rng.normal(size=(30, 64))   # 64-d: no accidental near-duplicates among random rows
    vecs = np.vstack([base, base[:5] + 0.001 * rng.normal(size=(5, 64))])
    pts = [_pt(f"p{i}", v.tolist(), created=f"2026-08-{(i % 27) + 1:02d}T00:00:00+00:00")
           for i, v in enumerate(vecs)]
    full, fstats = sd.plan_dedup(pts, max_deletions=0)
    monkeypatch.setattr(sd, "BLOCK_ROWS", 1)
    tiny, tstats = sd.plan_dedup(pts, max_deletions=0)
    assert fstats["compared_pairs"] == tstats["compared_pairs"] == 35 * 34 // 2
    assert {(d["deleted_id"], d["kept_id"]) for d in full} == {(d["deleted_id"], d["kept_id"]) for d in tiny}
    assert len(full) == 5


def test_outcome_says_degraded_when_it_compared_nothing_or_skipped_too_much():
    ok = {"scanned": 5000, "compared_pairs": 12_000_000, "skipped_no_vector": 0}
    assert sd.run_outcome(ok) == "ok"
    assert sd.run_outcome({"scanned": 5000, "compared_pairs": 0, "skipped_no_vector": 0}) == \
        "degraded:compared-0"
    assert sd.run_outcome({"scanned": 5000, "compared_pairs": 9, "skipped_no_vector": 51}) == \
        "degraded:skipped-no-vector"
    # exactly 1 % is tolerated, and a tiny corpus cannot be judged "compared nothing"
    assert sd.run_outcome({"scanned": 5000, "compared_pairs": 9, "skipped_no_vector": 50}) == "ok"
    assert sd.run_outcome({"scanned": 10, "compared_pairs": 0, "skipped_no_vector": 0}) == "ok"


# ---------------------------------------------------------------------------
# End to end through _run(): summary counts, outcome file, cap, only evidence deleted.
# ---------------------------------------------------------------------------

_FAILING: set = set()   # ids the fake mem0 refuses to delete (500), set per test


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    for name, fname in (("REPORT", "dedup-report.jsonl"), ("REPORT_DRY", "dedup-report.dryrun.jsonl"),
                        ("SUMMARY", "dedup-summary.jsonl"), ("DEDUP_LOCK", "dedup.lock")):
        monkeypatch.setattr(sd, name, tmp_path / fname)
    monkeypatch.setattr(sd, "LEDGER_DIR", tmp_path)
    outcome = tmp_path / "outcome.txt"
    monkeypatch.setenv("AMS_OUTCOME_FILE", str(outcome))
    deleted: list = []
    _FAILING.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            rid = request.url.path.rsplit("/", 1)[-1]
            if rid in _FAILING:
                return httpx.Response(500, json={})
            deleted.append(rid)
        return httpx.Response(200, json={})

    real = httpx.Client
    monkeypatch.setattr(sd.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(sd.httpx, "get", lambda *a, **k: httpx.Response(
        200, request=httpx.Request("GET", "http://x")))
    return tmp_path, outcome, deleted


def _twin_corpus():
    return [
        _pt("e1-old", _unit(0), created=OLD), _pt("e1-new", _unit(0, 0.01), created=NEW),
        _pt("e2-old", _unit(2), created=OLD), _pt("e2-new", _unit(2, 0.01), created=NEW),
        _pt("c-old", _unit(4), tier="canonical", created=OLD),
        _pt("c-new", _unit(4, 0.01), tier="canonical", created=NEW),
    ]


def test_run_deletes_only_within_cap_and_records_the_counts(rig, monkeypatch):
    tmp, outcome, deleted = rig
    monkeypatch.setattr(sd, "scroll_all_with_vectors", _twin_corpus)
    assert sd._run(dry_run=False, max_deletions=1) == 0
    assert len(deleted) == 1 and deleted[0] in {"e1-new", "e2-new"}
    row = json.loads((tmp / "dedup-summary.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["outcome"] == "ok"
    assert (row["scanned"], row["compared_pairs"], row["candidates"], row["deleted"]) == (6, 6, 2, 1)
    assert row["skipped_no_vector"] == 0 and row["max_deletions"] == 1 and row["dry_run"] is False
    assert row["deletions"] == 1   # the pre-existing field keeps its meaning
    status, _, body = outcome.read_text(encoding="utf-8").partition(" ")
    assert status == "ok"
    assert json.loads(body)["compared_pairs"] == 6
    # the restore record holds only what was really deleted
    report = [json.loads(x) for x in (tmp / "dedup-report.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["deleted_id"] for r in report] == deleted
    assert "deleted_full_payload" in report[0]


def test_dry_run_reports_every_candidate_and_deletes_nothing(rig, monkeypatch):
    tmp, outcome, deleted = rig
    monkeypatch.setattr(sd, "scroll_all_with_vectors", _twin_corpus)
    assert sd._run(dry_run=True, max_deletions=1) == 0
    assert deleted == []
    rows = [json.loads(x) for x in (tmp / "dedup-report.dryrun.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sorted(r["deleted_id"] for r in rows) == ["e1-new", "e2-new"]   # the whole backlog, for review
    assert not (tmp / "dedup-report.jsonl").exists()
    row = json.loads((tmp / "dedup-summary.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["dry_run"] is True and row["candidates"] == 2 and row["deleted"] == 1


def test_run_reports_degraded_when_nothing_was_comparable(rig, monkeypatch):
    """1500 points, none with a dense vector: the old job printed 'deletions=0' and exited ok."""
    tmp, outcome, deleted = rig
    blind = [{"id": f"x{i}", "vector": {"bm25": {"indices": [1], "values": [1.0]}},
              "payload": {"tier": "evidence", "user_id": "u1"}} for i in range(1500)]
    monkeypatch.setattr(sd, "scroll_all_with_vectors", lambda: blind)
    assert sd._run(dry_run=False, max_deletions=50) == 0
    row = json.loads((tmp / "dedup-summary.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["outcome"].startswith("degraded:")
    assert row["scanned"] == 1500 and row["compared_pairs"] == 0 and row["skipped_no_vector"] == 1500
    status = outcome.read_text(encoding="utf-8").split(" ", 1)[0]
    assert status.startswith("degraded:")
    assert deleted == []


def test_no_outcome_file_is_fine(rig, monkeypatch):
    """Run by hand (no ams-step wrapper) there is no AMS_OUTCOME_FILE: nothing to write, nothing raised."""
    tmp, outcome, deleted = rig
    monkeypatch.delenv("AMS_OUTCOME_FILE")
    monkeypatch.setattr(sd, "scroll_all_with_vectors", _twin_corpus)
    assert sd._run(dry_run=True, max_deletions=50) == 0
    assert not outcome.exists()


def test_failed_delete_is_not_counted_and_the_restore_record_is_written_first(rig, monkeypatch):
    """The payload goes to the restore report BEFORE the delete call; a delete the API refuses is
    marked in the report and is not counted as a deletion."""
    tmp, outcome, deleted = rig
    monkeypatch.setattr(sd, "scroll_all_with_vectors", _twin_corpus)
    _FAILING.add("e1-new")
    assert sd._run(dry_run=False, max_deletions=50) == 0
    assert deleted == ["e2-new"]
    lines = [json.loads(x) for x in (tmp / "dedup-report.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["deleted_id"] for r in lines if "deleted_full_payload" in r} == {"e1-new", "e2-new"}
    assert [r for r in lines if "delete_failed" in r] == [{"deleted_id": "e1-new", "delete_failed": 500}]
    row = json.loads((tmp / "dedup-summary.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["deleted"] == 1 and row["candidates"] == 2
