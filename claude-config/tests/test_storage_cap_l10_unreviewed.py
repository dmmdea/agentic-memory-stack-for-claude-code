"""MEM-11 (2026-07-03): the L10 audit-flags banner counts UNREVIEWED, not raw lines.

The old computation was `wc -l audit-flags.jsonl` minus a baseline file holding
0 — i.e. every flag ever written (328 live), 4.6x the real backlog, and
unfixable by triage because the flags file is append-only (triage marks
reviewed_keys in l10-state.json; it never shrinks the file). The banner now
mirrors SLOWDRIP / audit-flags-triage.py --summary: flags whose
"<memory_id>:<flag_type>" key is NOT in l10-state.json["reviewed_keys"].

These tests run the REAL script under bash with HOME pointed at a fixture dir
(every path in the script is $HOME-derived, so a fake HOME isolates it fully;
sections needing api-key / episodic.db silently no-op). Requires bash — the
suite runs in the WSL gate env.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "storage-cap-check.sh"


def _run_with_fake_home(home: Path) -> str:
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "CLAUDE_CWD": "/tmp"},
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, f"script must always exit 0: {res.stderr}"
    return res.stdout


def _write_flags(home: Path, n_flags: int, n_reviewed: int) -> None:
    mem0 = home / ".mem0"
    mem0.mkdir(parents=True, exist_ok=True)
    with (mem0 / "audit-flags.jsonl").open("w", encoding="utf-8") as f:
        for i in range(n_flags):
            f.write(json.dumps({"audited_at": 1751500000, "memory_id": f"m{i}",
                                "flag_type": "oversize"}) + "\n")
    (mem0 / "l10-state.json").write_text(json.dumps(
        {"reviewed_keys": [f"m{i}:oversize" for i in range(n_reviewed)]}),
        encoding="utf-8")


def test_banner_shows_unreviewed_not_total(tmp_path):
    """25 flags, 4 reviewed -> banner says 21 unreviewed (NOT 25)."""
    _write_flags(tmp_path, n_flags=25, n_reviewed=4)
    out = _run_with_fake_home(tmp_path)
    assert "L10 audit-flags: 21 unreviewed (total 25)" in out
    assert "NEW since baseline" not in out, "old inflated wording must be gone"


def test_banner_silent_when_backlog_triaged(tmp_path):
    """Same 25 flags but 24 reviewed -> 1 unreviewed <= threshold 20 -> no banner.
    THE MEM-11 point: triage now actually clears the alarm (the old line-count
    delta could never go down)."""
    _write_flags(tmp_path, n_flags=25, n_reviewed=24)
    out = _run_with_fake_home(tmp_path)
    assert "L10 audit-flags" not in out


def test_missing_state_counts_all_unreviewed(tmp_path):
    """No l10-state.json -> conservative: every flag unreviewed (mirrors
    SLOWDRIP's fresh-install semantics)."""
    _write_flags(tmp_path, n_flags=30, n_reviewed=0)
    ((tmp_path / ".mem0") / "l10-state.json").unlink()
    out = _run_with_fake_home(tmp_path)
    assert "L10 audit-flags: 30 unreviewed (total 30)" in out


def test_no_flags_file_no_banner(tmp_path):
    (tmp_path / ".mem0").mkdir(parents=True, exist_ok=True)
    out = _run_with_fake_home(tmp_path)
    assert "L10 audit-flags" not in out


def test_bash_syntax():
    res = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


# ---------------------------------------------------------------------------
# MEM-13 (2026-07-03): contradiction review queue — own visible line.
# ---------------------------------------------------------------------------

def test_contradiction_review_queue_prints_own_line(tmp_path):
    """A populated queue prints 'N contradiction verdict(s) await review' as
    its OWN line (not inside the [storage-cap] blob), so it shows even when
    nothing is over cap."""
    mem0 = tmp_path / ".mem0"
    mem0.mkdir(parents=True, exist_ok=True)
    (mem0 / "contradiction-promote-review.jsonl").write_text(
        '{"memory_id":"a"}\n{"memory_id":"b"}\n', encoding="utf-8")
    out = _run_with_fake_home(tmp_path)
    assert "2 contradiction verdict(s) await review" in out
    assert "[storage-cap] 2 contradiction" not in out


def test_contradiction_review_queue_silent_when_empty(tmp_path):
    mem0 = tmp_path / ".mem0"
    mem0.mkdir(parents=True, exist_ok=True)
    (mem0 / "contradiction-promote-review.jsonl").write_text("", encoding="utf-8")
    out = _run_with_fake_home(tmp_path)
    assert "await review" not in out
