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


def test_pool_pct_is_the_pool_not_the_dataset_quota(tmp_path):
    """A quota-bearing dataset reports quota headroom as avail (2 %) while the pool stood at 78 %."""
    out = mh.build(tmp_path / "absent.jsonl", NOW, pool_reader=lambda: (78, 22, 2, 98), boots_reader=lambda: [],
                   judge_transport=lambda: "none", usage_reader=None)
    assert out["pool"]["used_pct"] == 78.0 and out["pool"]["alarm"] is False
    assert out["dataset"] == {"used_bytes": 2, "avail_bytes": 98, "used_pct": 2.0}
    out = mh.build(tmp_path / "absent.jsonl", NOW, pool_reader=lambda: (10, 90), boots_reader=lambda: [], judge_transport=lambda: "none")
    assert "dataset" not in out and out["usage"]["used_percent"] is None


def test_zfs_pool_reader_reads_pool_root_and_dataset(monkeypatch):
    import subprocess
    calls = []

    def run(cmd, **kw):
        calls.append((cmd[0], cmd[-1]))
        # zpool: allocated,size ; zfs: used,avail
        return subprocess.CompletedProcess(cmd, 0, stdout="76\t100\n" if cmd[0] == "zpool" else "2\t38\n", stderr="")
    monkeypatch.setattr(mh.subprocess, "run", run)
    assert mh.zfs_pool_reader("tank/apps/ams")() == (76, 24, 2, 38)
    assert calls == [("zpool", "tank"), ("zfs", "tank/apps/ams")], "the pool figure is zpool capacity, the dataset figure is zfs"
    assert mh.parse_zpool_list("278728622080\t362924736512\n") == (278728622080, 84196114432)


def test_usage_from_newest_window_probe(tmp_path):
    p = tmp_path / "codex-usage.jsonl"
    p.write_text('{"ts":"2026-09-11T01:00:00+00:00","component":"codex-window","used_percent":40,"resets_in_days":2.5,"note":""}\n'
                 '{"ts":"2026-09-11T02:00:00+00:00","component":"dream","tokens_used":5}\n', encoding="utf-8")
    out = mh.build(tmp_path / "absent.jsonl", NOW, pool_reader=lambda: (1, 99), boots_reader=lambda: [],
                   judge_transport=lambda: "none", usage_reader=mh.usage_window_reader(p))
    assert out["usage"]["used_percent"] == 40 and out["usage"]["resets_in_days"] == 2.5
    assert out["usage"]["probed_at"] == "2026-09-11T01:00:00+00:00"
    out = mh.build(tmp_path / "absent.jsonl", NOW, pool_reader=lambda: (1, 99), boots_reader=lambda: [],
                   judge_transport=lambda: "none", usage_reader=mh.usage_window_reader(tmp_path / "none.jsonl"))
    assert out["usage"]["used_percent"] is None and "no probe" in out["usage"]["note"]


def test_morning_summary_endpoint(tmp_path, monkeypatch):
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", classmethod(lambda cls: tmp_path))
    from fastapi.testclient import TestClient
    import app as appmod
    c = TestClient(appmod.app)
    assert c.get("/health/morning-summary").status_code == 404
    d = tmp_path / ".mem0" / "maintenance"
    d.mkdir(parents=True)
    (d / "morning-summary.md").write_text("## A\n1\n## B\n2\n## C\n3\n## D\n4\n", encoding="utf-8")
    r = c.get("/health/morning-summary")
    assert r.status_code == 200
    assert [s.splitlines()[0] for s in r.json()["sections"]] == ["## B", "## C", "## D"]
