"""test_stack_restore.py — v0.18 MED-18: coverage for scripts/wsl/stack-restore.sh --dry-run.

The restore drill was previously verified only by hand. This test runs the script in
--dry-run mode against the newest real snapshot in ~/.mem0/backups and asserts the
output structure that an operator relies on before a live restore.

NOTE (plan-vs-reality, verified by reading stack-restore.sh on 2026-06-11):
- The script has NO '--snapshot latest' alias — it requires a concrete TS
  (YYYYmmdd-HHMMSS), so we resolve the newest manifest ourselves.
- The plan's expected strings ("Qdrant points:", "Next steps:") were from memory.
  Actual dry-run output prints the manifest block ("qdrant_points  : N",
  "episodic: sessions=N episodes=N goals=N"), then "--- Intended actions ---",
  then "DRY RUN complete — no files written." and exits 0 BEFORE the live-restore
  section ("Next steps:" only prints on a live restore). Assertions below target
  the real load-bearing fields.
- Dry-run is non-mutating: the script exits at the DRY_RUN gate before any
  curl/cp/mv side effects (only reads the manifest + stats backup files).
"""

import glob
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "wsl" / "stack-restore.sh"
BACKUP_DIR = Path(os.path.expanduser("~/.mem0/backups"))


def _latest_snapshot_ts():
    """Newest snapshot TS by manifest filename (YYYYmmdd-HHMMSS sorts lexicographically)."""
    manifests = sorted(glob.glob(str(BACKUP_DIR / "manifest-*.json")))
    if not manifests:
        return None
    return Path(manifests[-1]).stem.replace("manifest-", "")


def test_stack_restore_dry_run_output_structure(tmp_path):
    """--dry-run exits 0 and prints manifest counts + intended actions, writing nothing.

    v0.20 Phase E (M10): DRILL_LOG points at a tmp file — suite-driven entries
    in the REAL ~/.mem0/restore-drill.jsonl were exactly the 'indefinite chain
    of dry-run drills' that R4 wrongly accepted as restore proof."""
    assert SCRIPT.exists(), f"stack-restore.sh not found at {SCRIPT}"
    ts = _latest_snapshot_ts()
    if ts is None:
        pytest.skip(f"no backup snapshots in {BACKUP_DIR} — run stack-backup.sh first")

    drill_log = tmp_path / "restore-drill.jsonl"
    r = subprocess.run(
        ["bash", str(SCRIPT), "--snapshot", ts, "--dry-run"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "DRILL_LOG": str(drill_log)},
    )
    assert r.returncode == 0, (
        f"dry-run exited {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    out = r.stdout

    # Header echoes the resolved snapshot + dry-run flag
    assert f"Snapshot   : {ts}" in out, f"missing snapshot header:\n{out}"
    assert "Dry run    : true" in out, f"missing dry-run flag echo:\n{out}"

    # Manifest counts (Qdrant points + episodic sessions/episodes/goals)
    assert re.search(r"qdrant_points\s*:\s*\d+", out), f"missing qdrant_points count:\n{out}"
    assert re.search(r"episodic: sessions=\d+ episodes=\d+ goals=\d+", out), (
        f"missing episodic counts line:\n{out}"
    )

    # Intended-actions plan (what a live restore would touch)
    assert "--- Intended actions ---" in out, f"missing intended-actions block:\n{out}"
    assert "Qdrant restore" in out, f"missing Qdrant restore action:\n{out}"
    assert "episodic.db" in out, f"missing episodic.db action:\n{out}"

    # Dry-run gate: must stop before the live-restore section
    assert "DRY RUN complete" in out, f"missing dry-run completion marker:\n{out}"
    assert "=== Starting live restore ===" not in out, (
        f"dry-run must not enter the live-restore section:\n{out}"
    )

    # Completed dry-run appends one mode=dry-run outcome=ok line (v0.19 M9)
    entries = [json.loads(line) for line in
               drill_log.read_text(encoding="utf-8").strip().splitlines()]
    assert entries and entries[-1]["mode"] == "dry-run" and entries[-1]["outcome"] == "ok", (
        f"completed dry-run must log mode=dry-run outcome=ok: {entries}"
    )


def test_stack_restore_unknown_snapshot_fails_loudly(tmp_path):
    """A bogus snapshot TS must exit non-zero with a clear manifest-not-found error
    (also non-mutating: fails at manifest validation before any restore action).

    v0.20 Phase E (M10): the failure is now LOGGED — the EXIT trap appends
    mode+outcome=failed to DRILL_LOG, so R4's outcome field is meaningful
    (a drill log where failures never appear cannot distinguish 'restores
    always work' from 'failures were invisible')."""
    drill_log = tmp_path / "restore-drill.jsonl"
    r = subprocess.run(
        ["bash", str(SCRIPT), "--snapshot", "19700101-000000", "--dry-run"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "DRILL_LOG": str(drill_log)},
    )
    assert r.returncode == 1, f"expected exit 1 for unknown snapshot, got {r.returncode}"
    assert "manifest not found" in (r.stdout + r.stderr), (
        f"missing manifest-not-found error:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    entries = [json.loads(line) for line in
               drill_log.read_text(encoding="utf-8").strip().splitlines()]
    assert len(entries) == 1, f"failed run must log exactly one entry: {entries}"
    assert entries[0]["mode"] == "dry-run" and entries[0]["outcome"] == "failed", (
        f"failed run must log mode=dry-run outcome=failed: {entries[0]}"
    )
    assert entries[0]["snapshot"] == "19700101-000000"
