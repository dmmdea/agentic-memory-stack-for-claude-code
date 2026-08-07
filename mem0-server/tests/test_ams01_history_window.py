"""Headless pin for ams01-repair's history.db restore-window derivation.

Pins the mem0 add_history column semantics that shipped a live defect on
2026-08-07: every history row's created_at carries the RECORD's creation
time (an UPDATE row's created_at equals the ADD row's); the event's own
timestamp is in updated_at. A reader that takes created_at for UPDATE rows
computes an empty restore window [ADD, first UPDATE) for every record and
silently degrades every snapshot-RESTORE disposition to a bare MARK.
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wsl" / "ams01-repair.py"


def _load_module(monkeypatch):
    # The script guards its env at import (temp-collection identity) — defaults
    # are safe; make that explicit so the test cannot depend on ambient env.
    monkeypatch.delenv("MEM0_AMS01_TMP_COLLECTION", raising=False)
    spec = importlib.util.spec_from_file_location("ams01_repair", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ams01_repair"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_update_window_uses_event_time_not_record_birth_time(tmp_path, monkeypatch):
    mod = _load_module(monkeypatch)
    db = tmp_path / "history.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE history (id TEXT, memory_id TEXT, old_memory TEXT, "
        "new_memory TEXT, event TEXT, created_at TEXT, updated_at TEXT, "
        "is_deleted INTEGER, actor_id TEXT, role TEXT)"
    )
    birth = "2026-07-16T12:41:42.817139+00:00"
    updated = "2026-07-26T17:49:52.202580+00:00"
    # Exactly mem0's live shape: the UPDATE row repeats the record's
    # created_at and carries the event time only in updated_at.
    con.execute("INSERT INTO history (memory_id, event, created_at, updated_at) "
                "VALUES ('m1', 'ADD', ?, ?)", (birth, birth))
    con.execute("INSERT INTO history (memory_id, event, created_at, updated_at) "
                "VALUES ('m1', 'UPDATE', ?, ?)", (birth, updated))
    con.commit()
    con.close()

    idx = mod.load_history_index(db)
    rec = idx["m1"]
    assert rec["update_rows"] == 1
    assert rec["add_ts"] == mod.parse_ts(birth)
    assert rec["first_update_ts"] == mod.parse_ts(updated), (
        "UPDATE rows must be timestamped from updated_at — created_at is the "
        "record's birth time, and using it collapses every restore window"
    )
    assert rec["first_update_ts"] > rec["add_ts"]  # a real, non-empty window


def test_multiple_updates_take_the_earliest_event(tmp_path, monkeypatch):
    mod = _load_module(monkeypatch)
    db = tmp_path / "history.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE history (id TEXT, memory_id TEXT, old_memory TEXT, "
        "new_memory TEXT, event TEXT, created_at TEXT, updated_at TEXT, "
        "is_deleted INTEGER, actor_id TEXT, role TEXT)"
    )
    birth = "2026-07-01T00:00:00+00:00"
    con.execute("INSERT INTO history (memory_id, event, created_at, updated_at) "
                "VALUES ('m2', 'ADD', ?, ?)", (birth, birth))
    for ev_ts in ("2026-07-20T10:00:00+00:00", "2026-07-05T10:00:00+00:00",
                  "2026-07-30T10:00:00+00:00"):
        con.execute("INSERT INTO history (memory_id, event, created_at, updated_at) "
                    "VALUES ('m2', 'UPDATE', ?, ?)", (birth, ev_ts))
    con.commit()
    con.close()

    rec = mod.load_history_index(db)["m2"]
    assert rec["update_rows"] == 3
    assert rec["first_update_ts"] == mod.parse_ts("2026-07-05T10:00:00+00:00")
