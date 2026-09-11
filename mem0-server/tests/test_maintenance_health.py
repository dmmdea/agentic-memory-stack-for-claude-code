# mem0-server/tests/test_maintenance_health.py
"""GET /health/maintenance (spec §9): each chain step's last success, duration and receipt id;
the judge transport; pool usage with the 85 % alarm; the box's boot ids for the last 7 days."""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import maintenance_health as mh  # noqa: E402

NOW = dt.datetime(2026, 9, 12, 8, 0, tzinfo=dt.timezone.utc)


def _receipts(tmp_path, rows):
    p = tmp_path / "receipts.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""), encoding="utf-8")
    return p


def test_steps_report_last_success_and_stale(tmp_path):
    p = _receipts(tmp_path, [
        {"ts": "2026-09-11T03:05:00Z", "step": "stack-backup", "ok": True, "duration_ms": 4000, "receipt_id": "r1"},
        {"ts": "2026-09-12T03:04:00Z", "step": "stack-backup", "ok": False, "duration_ms": 10, "receipt_id": "r2", "note": "rsync 23"},
        {"ts": "2026-09-09T03:00:00Z", "step": "rtcwake", "ok": True, "duration_ms": 50, "receipt_id": "r0"},
    ])
    out = mh.build(p, NOW, pool_reader=lambda: (10, 90), boots_reader=lambda: ["b1"], judge_transport=lambda: "native")
    assert out["steps"]["stack-backup"]["last_success"] == "2026-09-11T03:05:00Z"
    assert out["steps"]["stack-backup"]["last_run"] == "2026-09-12T03:04:00Z"
    assert out["steps"]["stack-backup"]["ok"] is False
    assert out["steps"]["stack-backup"]["receipt_id"] == "r2"
    assert out["steps"]["rtcwake"]["last_success"] == "2026-09-09T03:00:00Z"
    assert out["stale_steps"] == ["rtcwake"]  # 3 days old > 48 h
    assert out["judge_transport"] == "native"
    assert out["ok"] is False


def test_pool_alarm_at_85_pct(tmp_path):
    p = _receipts(tmp_path, [])
    out = mh.build(p, NOW, pool_reader=lambda: (85, 15), boots_reader=lambda: [], judge_transport=lambda: "none")
    assert out["pool"] == {"used_pct": 85.0, "alarm": True, "threshold_pct": 85}
    assert out["ok"] is False
    out = mh.build(p, NOW, pool_reader=lambda: (84, 16), boots_reader=lambda: [], judge_transport=lambda: "none")
    assert out["pool"]["alarm"] is False and out["ok"] is True


def test_missing_receipts_file_is_empty_not_error(tmp_path):
    out = mh.build(tmp_path / "absent.jsonl", NOW, pool_reader=lambda: (1, 99), boots_reader=lambda: ["b"], judge_transport=lambda: "shim")
    assert out["steps"] == {} and out["ok"] is True and out["boots_7d"] == ["b"]


def test_readers_fail_soft(tmp_path):
    def boom():
        raise RuntimeError("no zfs")
    out = mh.build(tmp_path / "absent.jsonl", NOW, pool_reader=boom, boots_reader=boom, judge_transport=boom)
    assert out["pool"]["used_pct"] is None and out["pool"]["alarm"] is False
    assert out["boots_7d"] == [] and out["judge_transport"] == "none" and out["ok"] is True


def test_malformed_receipt_lines_are_skipped(tmp_path):
    p = tmp_path / "receipts.jsonl"
    p.write_text('not json\n{"step":"x"}\n{"ts":"2026-09-12T03:00:00Z","step":"y","ok":true}\n', encoding="utf-8")
    out = mh.build(p, NOW, pool_reader=lambda: (1, 99), boots_reader=lambda: [], judge_transport=lambda: "none")
    assert list(out["steps"]) == ["y"]


def test_zfs_pool_reader_parses_used_avail():
    assert mh.parse_zfs_list("30973952\t42918699008\n") == (30973952, 42918699008)


def test_boots_reader_keeps_last_7_days():
    rows = [{"boot_id": "old", "first_entry": int((NOW - dt.timedelta(days=9)).timestamp() * 1e6)},
            {"boot_id": "new", "first_entry": int((NOW - dt.timedelta(days=1)).timestamp() * 1e6)}]
    assert mh.boots_from_journal_json(json.dumps(rows), NOW) == ["new"]
    assert mh.boots_from_journal_json("", NOW) == []
