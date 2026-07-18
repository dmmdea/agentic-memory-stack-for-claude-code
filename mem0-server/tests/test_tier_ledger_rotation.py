"""MEM-16 (2026-07-03): tier-ledger monthly segmentation — writer side.

The single tier-ledger.jsonl grew unbounded (9.8MB) because app.py and every
maintenance script appended to one file forever. app._append_ledger now writes
to tier-ledger-YYYY-MM.jsonl (UTC month) and the legacy file is a FROZEN
historical archive. These tests pin:
  * the pure segment-path naming (app._ledger_segment_path),
  * that _append_ledger actually lands in the current-month segment and never
    touches the legacy file,
  * writer parity — every OTHER ledger writer in the repo (decay-scan /
    goals-stale-sweep / semantic-dedup / stack-promote.sh) moved to the same
    monthly naming in the same change, so ledger-audit.py's chronological walk
    (legacy first, then segments) stays monotonic across the cutover,
  * stack-backup.sh backs up legacy + segments (a segment-only entry must not
    silently drop out of the weekly snapshot).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402  (heavy import; mem0 init runs once, shared with test_raw_fallback)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_segment_path_naming_pure():
    fixed = dt.datetime(2026, 7, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    p = app._ledger_segment_path(Path("/tmp/mem0"), now=fixed)
    assert p == Path("/tmp/mem0/tier-ledger-2026-07.jsonl")
    # month boundary: December stays zero-padded two-digit
    dec = app._ledger_segment_path(Path("/x"), now=dt.datetime(2025, 12, 31, tzinfo=dt.timezone.utc))
    assert dec.name == "tier-ledger-2025-12.jsonl"


def test_append_ledger_writes_current_month_segment_not_legacy(tmp_path, monkeypatch):
    """_append_ledger lands in tier-ledger-YYYY-MM.jsonl; the legacy archive is
    never created/touched by new writes."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    app._append_ledger({"event": "tier-change", "memory_id": "test-mem-16",
                        "tier": "stable", "actor": "test-rotation"})
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    seg = tmp_path / ".mem0" / f"tier-ledger-{month}.jsonl"
    legacy = tmp_path / ".mem0" / "tier-ledger.jsonl"
    assert seg.exists(), "entry must land in the current-month segment"
    assert not legacy.exists(), "legacy archive must stay frozen (no new writes)"
    rec = json.loads(seg.read_text(encoding="utf-8").strip())
    assert rec["event"] == "tier-change"
    assert rec["schema_version"] == "v17"   # v0.17 F.4.4 auto-stamp preserved
    assert rec["ts"], "ts auto-stamp preserved"


_MONTHLY_WRITERS_PY = (
    "scripts/wsl/decay-scan.py",
    "scripts/wsl/goals-stale-sweep.py",
    "scripts/wsl/semantic-dedup.py",
)


def test_every_python_writer_uses_monthly_segment_naming():
    """Writer parity: a straggler still appending to the legacy file would
    interleave post-cutover timestamps into the frozen archive and trip the
    auditor's cross-file monotonic walk."""
    for rel in _MONTHLY_WRITERS_PY:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "tier-ledger-{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m')}.jsonl" in text, \
            f"{rel}: must append to the monthly segment (MEM-16)"
        assert 'LEDGER = Path.home() / ".mem0" / "tier-ledger.jsonl"' not in text, \
            f"{rel}: legacy tier-ledger.jsonl constant must be gone"


def test_stack_promote_sh_uses_monthly_segment():
    text = (REPO_ROOT / "scripts/wsl/stack-promote.sh").read_text(encoding="utf-8")
    assert 'tier-ledger-$(date -u +%Y-%m).jsonl' in text, \
        "stack-promote.sh must append its production-restore event to the monthly segment"


def test_stack_backup_covers_segments():
    """stack-backup.sh must concatenate legacy + monthly segments into the
    dated snapshot — otherwise every post-cutover ledger entry silently drops
    out of the weekly backup."""
    text = (REPO_ROOT / "scripts/wsl/stack-backup.sh").read_text(encoding="utf-8")
    assert "tier-ledger-[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl" in text
    assert re.search(r"cat\s+\"\$\{ledger_parts\[@\]\}\"", text), \
        "backup must concatenate legacy + segments into one dated file"


def test_app_ledger_writer_has_no_legacy_path_left():
    """app.py must not retain any append path to the legacy file name."""
    text = (REPO_ROOT / "mem0-server/app.py").read_text(encoding="utf-8")
    assert 'Path.home() / ".mem0" / "tier-ledger.jsonl"' not in text
    assert "_ledger_segment_path" in text
