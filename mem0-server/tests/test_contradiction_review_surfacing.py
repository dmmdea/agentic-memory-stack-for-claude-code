"""MEM-13 (2026-07-03): /health/deep surfaces the contradiction review queue.

The SAFE resolver (contradiction-sweep.py --rejudge-stamped, no_auto_promote)
QUEUES genuine contradictions in ~/.mem0/contradiction-promote-review.jsonl for
human review instead of auto-hiding — so an unwatched queue silently
accumulates verdicts nobody promotes. /health/deep now reports
checks.pending_contradiction_reviews (0 when the file is absent), and
storage-cap-check.sh prints its own visible line when >0 (tested in
claude-config/tests).

Direct app-function calls — the live :18791 server predates the field until
the orchestrator deploys.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402  (heavy import; mem0 init runs once, shared across the suite)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pending_reviews_zero_when_queue_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    d = app.health_deep()
    assert d["checks"]["pending_contradiction_reviews"] == 0


def test_pending_reviews_counts_nonblank_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    mem0 = tmp_path / ".mem0"
    mem0.mkdir()
    (mem0 / "contradiction-promote-review.jsonl").write_text(
        '{"memory_id":"a","canonical_id":"c1"}\n'
        '\n'   # blank line must not count as a pending verdict
        '{"memory_id":"b","canonical_id":"c2"}\n'
        '{"memory_id":"c","canonical_id":"c3"}\n',
        encoding="utf-8",
    )
    d = app.health_deep()
    assert d["checks"]["pending_contradiction_reviews"] == 3


def test_pending_reviews_never_flips_ok(tmp_path, monkeypatch):
    """Queue depth is a review chore, not a liveness failure."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    mem0 = tmp_path / ".mem0"
    mem0.mkdir()
    (mem0 / "contradiction-promote-review.jsonl").write_text(
        '{"memory_id":"x"}\n' * 50, encoding="utf-8")
    d = app.health_deep()
    checks_ok = [v.get("ok") for k, v in d["checks"].items()
                 if isinstance(v, dict) and "ok" in v]
    assert d["ok"] == all(checks_ok), "queue depth must not affect ok"


def test_storage_cap_prints_own_review_line():
    """MEM-13 companion: the SessionStart banner line moved OUT of the
    [storage-cap] warnings blob (visible even when nothing is over cap) and
    uses the 'contradiction verdict(s) await review' wording."""
    text = (REPO_ROOT / "claude-config" / "storage-cap-check.sh").read_text(encoding="utf-8")
    assert 'contradiction verdict(s) await review' in text
    assert 'warnings+="${nrev} contradiction' not in text, \
        "the queue count must no longer ride the warnings blob"
