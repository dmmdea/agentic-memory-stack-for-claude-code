"""test_ledger_audit.py — v0.20 Phase E (L5): --baseline must refuse to replace
an identity-bearing baseline when the Qdrant orphan scan did not complete.

v0.19's --baseline with Qdrant down printed a WARN but still wrote
orphan_ids=[] and exited 0 — silently erasing the orphan identities that
--accept-baseline subtracts BY identity (an attacker-friendly way to launder
orphans into 'new findings never surface'). The fix refuses (exit 1, NO file
write) unless --force is given, reusing the v0.18 E.1 scroll_complete flag.

The script filename has a hyphen, so it is loaded via importlib; LEDGER /
BASELINE / REPORT are repointed at tmp_path and QDRANT at a dead port
(127.0.0.1:9) so qdrant_reachable=False without touching the real Qdrant.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "wsl" / "ledger-audit.py"

LEDGER_LINE = json.dumps({
    "ts": "2026-06-01T00:00:00+00:00", "event": "add",
    "memory_id": "00000000-0000-0000-0000-0000000000aa",
    "actor": "rest-api", "tier": "evidence", "schema_version": "v17",
}) + "\n"

# Counts >= what the dead-Qdrant run produces (hard_findings=1 from
# qdrant_unreachable) so the v0.19 M8 ratchet guard does NOT fire and the
# run reaches the L5 incomplete-scan check.
PRIOR_WITH_IDS = {
    "ts": "2026-06-10T00:00:00+00:00",
    "parse_errors": 0, "monotonic_violations": 0, "schema_violations": 0,
    "orphan_count": 2, "hard_findings": 1,
    "orphan_ids": ["aaaaaaaa-0000-0000-0000-000000000001",
                   "aaaaaaaa-0000-0000-0000-000000000002"],
    "monotonic_keys": [],
}


def _run_baseline(tmp_path, prior, argv_extra=()):
    spec = importlib.util.spec_from_file_location("ledger_audit_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LEDGER = tmp_path / "tier-ledger.jsonl"
    mod.REPORT = tmp_path / "ledger-audit-report.jsonl"
    mod.BASELINE = tmp_path / "ledger-audit-baseline.json"
    mod.QDRANT = "http://127.0.0.1:9"  # dead port -> qdrant_reachable=False
    mod.LEDGER.write_text(LEDGER_LINE, encoding="utf-8")
    if prior is not None:
        mod.BASELINE.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    old_argv = sys.argv
    sys.argv = ["ledger-audit.py", "--baseline", *argv_extra]
    try:
        rc = mod.main()
    finally:
        sys.argv = old_argv
    return rc, mod


def test_baseline_refuses_incomplete_scan_with_identity_baseline(tmp_path, capsys):
    """Qdrant down + prior baseline carries orphan_ids -> exit 1, no write."""
    rc, mod = _run_baseline(tmp_path, PRIOR_WITH_IDS)
    out = capsys.readouterr().out
    assert rc == 1, f"--baseline must refuse (exit 1); got {rc}\n{out}"
    assert "REFUSED" in out and "did not complete" in out, out
    persisted = json.loads(mod.BASELINE.read_text(encoding="utf-8"))
    assert persisted["orphan_ids"] == PRIOR_WITH_IDS["orphan_ids"], (
        "baseline file must be untouched after the refusal"
    )
    assert persisted["ts"] == PRIOR_WITH_IDS["ts"]


def test_baseline_force_overrides_incomplete_scan(tmp_path):
    """--force keeps its existing escape-hatch semantics (explicit override)."""
    rc, mod = _run_baseline(tmp_path, PRIOR_WITH_IDS, argv_extra=("--force",))
    assert rc == 0
    persisted = json.loads(mod.BASELINE.read_text(encoding="utf-8"))
    assert persisted["orphan_ids"] == []  # explicit --force may downgrade


def test_baseline_proceeds_when_prior_has_no_identities(tmp_path, capsys):
    """Back-compat: a legacy count-only baseline (no orphan_ids) still
    re-baselines with the existing WARN — nothing identity-bearing to erase."""
    prior = {k: v for k, v in PRIOR_WITH_IDS.items()
             if k not in ("orphan_ids", "monotonic_keys")}
    rc, mod = _run_baseline(tmp_path, prior)
    out = capsys.readouterr().out
    assert rc == 0, f"legacy baseline must not trip the refusal; got {rc}\n{out}"
    assert "WARN" in out  # incomplete-scan warning still printed
    persisted = json.loads(mod.BASELINE.read_text(encoding="utf-8"))
    assert persisted["orphan_ids"] == []


# ---------------------------------------------------------------------------
# MEM-16 (2026-07-03): monthly segmentation — the auditor must read the legacy
# archive PLUS every tier-ledger-YYYY-MM.jsonl segment (and NOTHING else that
# happens to match tier-ledger-* — stack-restore.sh drops a
# tier-ledger-restore.jsonl inspection copy right next to them).
# ---------------------------------------------------------------------------

def _entry(ts: str, event: str = "add", mid: str = "00000000-0000-0000-0000-0000000000aa") -> str:
    return json.dumps({
        "ts": ts, "event": event, "memory_id": mid,
        "actor": "rest-api", "tier": "evidence", "schema_version": "v17",
    }) + "\n"


def _load_mod(tmp_path):
    spec = importlib.util.spec_from_file_location("ledger_audit_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LEDGER = tmp_path / "tier-ledger.jsonl"
    mod.REPORT = tmp_path / "ledger-audit-report.jsonl"
    mod.BASELINE = tmp_path / "ledger-audit-baseline.json"
    mod.QDRANT = "http://127.0.0.1:9"  # dead port -> orphan scan skipped
    return mod


def _run_main(mod, argv_extra=()):
    old_argv = sys.argv
    sys.argv = ["ledger-audit.py", *argv_extra]
    try:
        return mod.main()
    finally:
        sys.argv = old_argv


def test_ledger_files_walk_order_and_restore_exclusion(tmp_path):
    """legacy first, then segments sorted by month; tier-ledger-restore.jsonl
    (and a stray non-month sibling) never audited."""
    mod = _load_mod(tmp_path)
    (tmp_path / "tier-ledger.jsonl").write_text(_entry("2026-05-01T00:00:00+00:00"), encoding="utf-8")
    (tmp_path / "tier-ledger-2026-07.jsonl").write_text(_entry("2026-07-01T00:00:00+00:00"), encoding="utf-8")
    (tmp_path / "tier-ledger-2026-06.jsonl").write_text(_entry("2026-06-01T00:00:00+00:00"), encoding="utf-8")
    (tmp_path / "tier-ledger-restore.jsonl").write_text(_entry("1999-01-01T00:00:00+00:00"), encoding="utf-8")
    (tmp_path / "tier-ledger-20260701-030000.jsonl").write_text(_entry("1999-01-01T00:00:00+00:00"), encoding="utf-8")
    files = [p.name for p in mod.ledger_files()]
    assert files == ["tier-ledger.jsonl", "tier-ledger-2026-06.jsonl", "tier-ledger-2026-07.jsonl"]


def test_audit_counts_entries_across_legacy_and_segments(tmp_path, capsys):
    """3 valid entries in chronological walk order -> 0 violations, exit 0,
    report lists every audited file."""
    mod = _load_mod(tmp_path)
    (tmp_path / "tier-ledger.jsonl").write_text(_entry("2026-05-01T00:00:00+00:00"), encoding="utf-8")
    (tmp_path / "tier-ledger-2026-06.jsonl").write_text(_entry("2026-06-01T00:00:00+00:00"), encoding="utf-8")
    (tmp_path / "tier-ledger-2026-07.jsonl").write_text(_entry("2026-07-01T00:00:00+00:00"), encoding="utf-8")
    rc = _run_main(mod)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "3 entries across 3 file(s)" in out
    report = json.loads(mod.REPORT.read_text(encoding="utf-8"))
    assert report["total_lines"] == 3
    assert [Path(p).name for p in report["ledger_files"]] == [
        "tier-ledger.jsonl", "tier-ledger-2026-06.jsonl", "tier-ledger-2026-07.jsonl"]
    assert report["monotonic_violations"] == 0, "chronological walk must span the cutover cleanly"


def test_audit_flags_non_monotonic_across_segment_boundary(tmp_path, capsys):
    """A segment entry OLDER than the legacy tail is still a violation — the
    cross-file walk keeps the monotonic check meaningful, and the finding names
    the offending file."""
    mod = _load_mod(tmp_path)
    (tmp_path / "tier-ledger.jsonl").write_text(_entry("2026-06-15T00:00:00+00:00"), encoding="utf-8")
    (tmp_path / "tier-ledger-2026-06.jsonl").write_text(_entry("2026-06-01T00:00:00+00:00"), encoding="utf-8")
    rc = _run_main(mod)
    capsys.readouterr()
    assert rc == 1, "cross-boundary regression must exit nonzero"
    report = json.loads(mod.REPORT.read_text(encoding="utf-8"))
    assert report["monotonic_violations"] == 1
    finding = next(f for f in report["findings_sample"] if f["type"] == "non_monotonic")
    assert finding["file"] == "tier-ledger-2026-06.jsonl"


def test_audit_segments_only_no_legacy(tmp_path, capsys):
    """A fresh box (no legacy archive) audits segments alone; a box with NO
    ledger at all still exits 0 with the not-found message."""
    mod = _load_mod(tmp_path)
    rc = _run_main(mod)
    assert rc == 0
    assert "no ledger to audit" in capsys.readouterr().out
    (tmp_path / "tier-ledger-2026-07.jsonl").write_text(_entry("2026-07-01T00:00:00+00:00"), encoding="utf-8")
    rc = _run_main(mod)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "1 entries across 1 file(s)" in out
