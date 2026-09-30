"""Tests for the SAFE resolution policy (2026-06-30): the Codex rejudge must NEVER auto-HIDE
(promote), because Codex over-promotes — a live run promoted 3/4 CONSISTENT facts into hidden.
Auto-CLEAR stays (17/17 correct); a YES on an advisory-pending record is routed to a human-review
queue instead of being enforced. Pure decision + queue helpers, unit-tested with no live store.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "wsl" / "contradiction-sweep.py"
_spec = importlib.util.spec_from_file_location("contradiction_sweep", SCRIPT)
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)


# --- resolve_action: the decision matrix (verdict, was_pending, no_auto_promote) ---

def test_no_verdict_skips():
    assert sweep.resolve_action(None, True, True) == "skip"
    assert sweep.resolve_action(None, False, False) == "skip"


def test_no_clears_any_stamped():
    # a NO verdict on a stamped record = false positive -> clear (safe, the 17/17-correct action)
    assert sweep.resolve_action(False, True, False) == "clear"
    assert sweep.resolve_action(False, False, False) == "clear"
    assert sweep.resolve_action(False, True, True) == "clear"


def test_yes_pending_auto_promotes_only_when_allowed():
    # legacy behavior (no_auto_promote=False): YES on advisory pending -> promote (enforce/hide)
    assert sweep.resolve_action(True, True, False) == "promote"


def test_yes_pending_queues_for_review_when_safe():
    # SAFE default: YES on advisory pending -> queue for human review, never auto-hide
    assert sweep.resolve_action(True, True, True) == "queue-review"


def test_yes_confirmed_keeps_regardless():
    # already-enforced record re-validated YES -> keep (no change), under either policy
    assert sweep.resolve_action(True, False, True) == "keep"
    assert sweep.resolve_action(True, False, False) == "keep"


# --- append_review_queue: route a YES-promote candidate to the human-review file ---

def test_append_review_queue_writes_jsonl(tmp_path):
    q = tmp_path / "sub" / "promote-review.jsonl"
    rec = {"memory_id": "m1", "canonical_id": "c1", "justification": "YES they conflict"}
    assert sweep.append_review_queue(str(q), rec) is True
    assert sweep.append_review_queue(str(q), {"memory_id": "m2", "canonical_id": "c2"}) is True
    lines = q.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["memory_id"] == "m1"
    assert json.loads(lines[1])["memory_id"] == "m2"


def test_append_review_queue_failure_is_nonfatal(tmp_path):
    # a directory path (not a file) makes the write fail -> returns False, never raises
    d = tmp_path / "adir"
    d.mkdir()
    assert sweep.append_review_queue(str(d), {"memory_id": "x"}) is False


def test_append_review_queue_is_idempotent_by_memory_id(tmp_path):
    q = tmp_path / "promote-review.jsonl"
    assert sweep.append_review_queue(str(q), {"memory_id": "dup", "canonical_id": "c1"}) is True
    assert sweep.append_review_queue(str(q), {"memory_id": "dup", "canonical_id": "c1"}) is True
    lines = [ln for ln in q.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1  # a re-flagged candidate is not duplicated across runs


def test_acquire_lock_is_exclusive(tmp_path):
    lk = tmp_path / ".lock"
    assert sweep._acquire_lock(lk) is True     # first acquire
    assert sweep._acquire_lock(lk) is False    # held -> a concurrent run is denied
    lk.rmdir()                                 # release
    assert sweep._acquire_lock(lk) is True     # re-acquire after release
    lk.rmdir()


def test_acquire_lock_reclaims_stale(tmp_path):
    lk = tmp_path / ".lock"
    lk.mkdir()  # a pre-existing lock (simulating a crashed prior run)
    assert sweep._acquire_lock(lk, stale_s=-1) is True  # any age > -1 -> stale -> reclaimed
    lk.rmdir()


def test_remove_from_review_queue(tmp_path):
    q = tmp_path / "q.jsonl"
    sweep.append_review_queue(str(q), {"memory_id": "a", "canonical_id": "c"})
    sweep.append_review_queue(str(q), {"memory_id": "b", "canonical_id": "c"})
    assert sweep.remove_from_review_queue(str(q), "a") == 1
    lines = [ln for ln in q.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1 and json.loads(lines[0])["memory_id"] == "b"
    assert sweep.remove_from_review_queue(str(q), "nope") == 0  # absent -> 0
    assert sweep.remove_from_review_queue(str(tmp_path / "missing.jsonl"), "a") == 0  # no file -> 0


# --- canonical-possibly-stale entries: keyed by (memory_id, kind, stale canonical), removable, pruned ---

STALE = "canonical-possibly-stale"


def _stale(mid, can):
    return {"memory_id": mid, "stale_canonical_id": can, "kind": STALE}


def _lines(q):
    return [json.loads(ln) for ln in q.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_a_queued_promote_record_does_not_swallow_the_stale_record_for_the_same_memory(tmp_path):
    """append_review_queue was idempotent by memory_id alone, so a candidate already queued for
    promote silently dropped the canonical-possibly-stale record the sweep then tried to add."""
    q = tmp_path / "q.jsonl"
    assert sweep.append_review_queue(str(q), {"memory_id": "m1", "canonical_id": "c1"}) is True
    assert sweep.append_review_queue(str(q), _stale("m1", "c2")) is True
    assert sweep.append_review_queue(str(q), _stale("m1", "c2")) is True      # still idempotent
    assert sweep.append_review_queue(str(q), _stale("m1", "c3")) is True      # another stale canonical
    assert [(r.get("kind"), r.get("stale_canonical_id")) for r in _lines(q)] == [
        (None, None), (STALE, "c2"), (STALE, "c3")]


def test_promote_removal_leaves_the_stale_record_alone(tmp_path):
    """--promote acts on canonical_id records only; the stale record is a different question (is
    the canonical stale?) and stays until it is dismissed or its canonical is demoted."""
    q = tmp_path / "q.jsonl"
    sweep.append_review_queue(str(q), {"memory_id": "m1", "canonical_id": "c1"})
    sweep.append_review_queue(str(q), _stale("m1", "c2"))
    assert sweep.remove_from_review_queue(str(q), "m1", exclude_kinds=(STALE,)) == 1
    assert [r["kind"] for r in _lines(q)] == [STALE]


def test_run_promote_keeps_the_stale_record_of_the_same_memory(tmp_path, monkeypatch):
    q = tmp_path / "q.jsonl"
    monkeypatch.setattr(sweep, "REVIEW_QUEUE", q)
    monkeypatch.setattr(sweep, "stamp_candidate", lambda *a, **k: True)
    sweep.append_review_queue(str(q), {"memory_id": "m1", "canonical_id": "c1"})
    sweep.append_review_queue(str(q), _stale("m1", "c2"))
    assert sweep.run_promote(None, "m1") == 0
    assert [r.get("kind") for r in _lines(q)] == [STALE]


def test_dismiss_removes_every_queue_line_for_the_memory(tmp_path, monkeypatch):
    q = tmp_path / "q.jsonl"
    monkeypatch.setattr(sweep, "REVIEW_QUEUE", q)
    sweep.append_review_queue(str(q), {"memory_id": "m1", "canonical_id": "c1"})
    sweep.append_review_queue(str(q), _stale("m1", "c2"))
    sweep.append_review_queue(str(q), _stale("m2", "c2"))
    assert sweep.run_dismiss("m1") == 0
    assert [r["memory_id"] for r in _lines(q)] == ["m2"]
    assert sweep.run_dismiss("m1") == 1          # nothing left to dismiss: not silent success


def test_main_dismiss_needs_no_backend(tmp_path, monkeypatch):
    """--dismiss is queue-file only. It must run on a box where the Codex judge is unavailable
    (no bridge import, no install receipt), where the judge preflight would otherwise answer a
    no-op with exit 0 and leave the line in the queue: success reported for work not done."""
    q = tmp_path / "q.jsonl"
    monkeypatch.setattr(sweep, "REVIEW_QUEUE", q)
    monkeypatch.setattr(sweep, "_codex", None)
    monkeypatch.setattr(sweep, "_install_is_provisioned", lambda: False)
    monkeypatch.setattr(sweep, "_append_summary", lambda rec: None)
    sweep.append_review_queue(str(q), _stale("m1", "c2"))
    assert sweep.main(["--dismiss", "m1"]) == 0
    assert _lines(q) == []


def _info_client(tiers):
    """Fake Qdrant points lookup: id -> tier (None = absent; 'ERR' = server error)."""
    def handler(request: httpx.Request) -> httpx.Response:
        pid = json.loads(request.content)["ids"][0]
        t = tiers.get(pid)
        if t == "ERR":
            return httpx.Response(500, json={})
        if t is None:
            return httpx.Response(200, json={"result": []})
        return httpx.Response(200, json={"result": [{"id": pid, "payload": {"data": "x", "tier": t}}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_prune_drops_stale_records_whose_canonical_is_no_longer_canonical(tmp_path):
    """Without this the queue only grew: nothing removed a stale line, the depth count degraded the
    review-queue capability above 50, and it stayed degraded for good."""
    q = tmp_path / "q.jsonl"
    sweep.append_review_queue(str(q), {"memory_id": "p1", "canonical_id": "c9"})   # a promote record
    for mid, can in (("m-demoted", "c-stable"), ("m-gone", "c-gone"), ("m-live", "c-live"),
                     ("m-blip", "c-blip")):
        sweep.append_review_queue(str(q), _stale(mid, can))
    http = _info_client({"c-stable": "stable", "c-gone": None, "c-live": "canonical", "c-blip": "ERR"})
    assert sweep.prune_stale_review_entries(http, str(q)) == 2
    assert sorted(r["memory_id"] for r in _lines(q)) == ["m-blip", "m-live", "p1"]   # an error is never absence


# --- evidence-vs-evidence detection: recency (the NEWER fact wins; only an OLDER neighbor loses) ---

def test_parse_created_and_is_older():
    a = {"payload": {"created_at": "2026-06-20T00:00:00+00:00"}}       # anchor (newer)
    b_old = {"payload": {"created_at": "2026-06-10T00:00:00+00:00"}}
    b_new = {"payload": {"created_at": "2026-06-25T00:00:00+00:00"}}
    assert sweep.is_older(b_old, a) is True
    assert sweep.is_older(b_new, a) is False
    assert sweep.is_older({"payload": {}}, a) is False          # unparseable neighbor -> conservative
    assert sweep.is_older(b_old, {"payload": {}}) is False      # unparseable anchor -> conservative
    assert sweep.parse_created({"payload": {"metadata": {"created_at": "2026-06-01T00:00:00Z"}}}) is not None
    assert sweep.parse_created({"payload": {}}) is None
