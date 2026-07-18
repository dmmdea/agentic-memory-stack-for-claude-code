"""_ledger_paths.py — MEM-16 (2026-07-03) test-side tier-ledger reader.

The tier-ledger is now the legacy ~/.mem0/tier-ledger.jsonl (frozen historical
archive) PLUS monthly segments tier-ledger-YYYY-MM.jsonl (what app.py
_append_ledger and the maintenance scripts append to). Tests that assert "the
endpoint wrote a ledger entry" must look at the UNION: pre-deploy the live
server still appends to legacy, post-deploy to the current-month segment —
these helpers keep the assertions valid on both sides of the cutover.

Walk order mirrors scripts/wsl/ledger-audit.py: legacy first, then segments
sorted by name (== chronological), so ledger_last_line() is the newest entry.
The strict YYYY-MM regex excludes tier-ledger-restore.jsonl (stack-restore.sh's
inspection copy).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_SEGMENT_RE = re.compile(r"^tier-ledger-\d{4}-\d{2}\.jsonl$")


def ledger_files() -> list[Path]:
    """Every live ledger file: legacy archive first, then monthly segments."""
    mem0 = Path.home() / ".mem0"
    files: list[Path] = []
    legacy = mem0 / "tier-ledger.jsonl"
    if legacy.exists():
        files.append(legacy)
    if mem0.exists():
        files.extend(sorted(
            p for p in mem0.glob("tier-ledger-*.jsonl") if _SEGMENT_RE.match(p.name)
        ))
    return files


def ledger_line_count() -> int:
    """Total line count across legacy + segments (the before/after delta the
    ledger-writes-exactly-N assertions compare)."""
    total = 0
    for p in ledger_files():
        with p.open(encoding="utf-8") as f:
            total += sum(1 for _ in f)
    return total


def ledger_lines() -> list[str]:
    """All non-blank ledger lines in walk order (oldest file first)."""
    out: list[str] = []
    for p in ledger_files():
        out.extend(ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
    return out


def ledger_last_line() -> Optional[str]:
    """The newest ledger line across all files (None when no ledger exists)."""
    lines = ledger_lines()
    return lines[-1] if lines else None
