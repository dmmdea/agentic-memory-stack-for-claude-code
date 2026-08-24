"""Tests for scripts/wsl/audit-flags-triage.py (2026-08-24: --only-types + atomic state).

The triage tool writes the operator's review state (l10-state.json reviewed_keys) —
the file the daily backup now protects. Two properties are load-bearing:
(1) --only-types resolves EXACTLY the named class (an en-masse resolve that
    accidentally swallows a security class is the failure the flag prevents);
(2) the state write is atomic (the l10 timer floats and can coincide with the
    03:30 backup; a torn state restored later wipes reviewed_keys).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "audit_flags_triage", REPO_ROOT / "scripts" / "wsl" / "audit-flags-triage.py")
tri = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tri)


@pytest.fixture(autouse=True)
def _tmp_state(monkeypatch, tmp_path):
    monkeypatch.setattr(tri, "STATE_FILE", tmp_path / "l10-state.json")
    monkeypatch.setattr(tri, "FLAGS_FILE", tmp_path / "audit-flags.jsonl")


def _rows():
    return [
        {"memory_id": "m1", "flag_type": "oversize"},
        {"memory_id": "m2", "flag_type": "oversize"},
        {"memory_id": "m3", "flag_type": "possible-credential"},
        {"memory_id": "m4", "flag_type": "missing-provenance"},
    ]


def _reviewed():
    return set(json.loads(tri.STATE_FILE.read_text(encoding="utf-8"))["reviewed_keys"])


def test_only_types_resolves_exactly_the_named_class():
    tri.resolve(_rows(), {}, "burn the oversize backlog", set(), only_types={"oversize"})
    reviewed = _reviewed()
    assert len(reviewed) == 2
    assert all("m1" in k or "m2" in k for k in reviewed), \
        "only the oversize rows may be marked; credential + provenance stay open"


def test_keep_types_still_wins_over_only_types():
    tri.resolve(_rows(), {}, "r", {"oversize"}, only_types={"oversize"})
    assert tri.STATE_FILE.exists() and _reviewed() == set(), \
        "keep-types is the operator's hard protection - it must beat only-types"


def test_no_only_types_keeps_the_old_resolve_all_behavior():
    tri.resolve(_rows(), {}, "r", {"possible-credential"})
    assert len(_reviewed()) == 3   # everything except the kept security class


def test_review_log_records_the_class_scope():
    tri.resolve(_rows(), {}, "scoped burn", set(), only_types={"oversize"})
    state = json.loads(tri.STATE_FILE.read_text(encoding="utf-8"))
    entry = state["review_log"][-1]
    assert entry["only_types"] == ["oversize"] and entry["marked"] == 2


def test_state_write_is_atomic_no_tmp_left_behind():
    tri.resolve(_rows(), {}, "r", set(), only_types={"oversize"})
    leftovers = [p.name for p in tri.STATE_FILE.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    json.loads(tri.STATE_FILE.read_text(encoding="utf-8"))  # parses whole


def test_still_open_report_counts_every_unreviewed_flag(capsys):
    """Review fix: the confirmation line used to count only KEPT types, so a scoped
    --only-types run printed "still-open: 0" while the security classes it protects
    were still open - the operator's only confirmation line reported the safety
    property backwards."""
    tri.resolve(_rows(), {}, "burn", set(), only_types={"oversize"})
    out = capsys.readouterr().out
    assert "still-open: 2" in out
    assert "possible-credential" in out and "missing-provenance" in out


def test_empty_only_types_is_refused_not_resolve_all(monkeypatch):
    """An unset shell variable (--only-types "$TYPES") must never invert the
    narrowest scope into the widest."""
    tri.FLAGS_FILE.write_text("\n".join(json.dumps(r) for r in _rows()) + "\n", encoding="utf-8")
    for bad in ("", "  ", ","):
        monkeypatch.setattr(tri.sys, "argv", ["triage", "--resolve", "--only-types", bad])
        with pytest.raises(SystemExit, match="given but empty"):
            tri.main()
    assert not tri.STATE_FILE.exists(), "nothing may be resolved on a refused invocation"


def test_empty_keep_types_is_refused_not_drop_protection(monkeypatch):
    """Review R2: the twin hazard - --keep-types "$SEC" with $SEC unset silently
    dropped the operator's hard protection set and resolved the security classes;
    the docs teach exactly that invocation."""
    tri.FLAGS_FILE.write_text("\n".join(json.dumps(r) for r in _rows()) + "\n", encoding="utf-8")
    for bad in ("", " , "):
        monkeypatch.setattr(tri.sys, "argv", ["triage", "--resolve", "--keep-types", bad])
        with pytest.raises(SystemExit, match="keep-types was given but empty"):
            tri.main()
    assert not tri.STATE_FILE.exists()
    # omitting the flag entirely keeps the legacy resolve-all behavior — assert the
    # COUNT, not mere file existence (resolve() writes state even when it marks zero;
    # only a count kills the resolve-all -> resolve-nothing mutant, review R3)
    monkeypatch.setattr(tri.sys, "argv", ["triage", "--resolve", "--reason", "r"])
    assert tri.main() == 0 and len(_reviewed()) == 4


def test_corrupt_or_quarantined_state_is_refused_never_defaulted(monkeypatch):
    """Parity with l10-audit's gate: a corrupt state file, or an unresolved
    quarantine beside a valid one, must refuse - defaulting to {} and then writing
    would erase reviewed_keys."""
    tri.STATE_FILE.write_text('{"reviewed_keys": ["a:b"], TRUNCATED', encoding="utf-8")
    with pytest.raises(SystemExit, match="corrupt"):
        tri._load_state()
    tri.STATE_FILE.write_text('{"reviewed_keys": ["a:b"]}', encoding="utf-8")
    (tri.STATE_FILE.parent / "l10-state.json.corrupt-20260824000000").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="unresolved l10-state quarantine"):
        tri._load_state()
