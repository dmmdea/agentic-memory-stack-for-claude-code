"""Contract C2, producer to consumer: what maintenance_health.build() emits is what the SessionStart banner reads.

The banner (storage-cap-check.sh) parses /health/maintenance inside `python3 -c ... except BaseException: pass`,
so a field renamed on either side fails nothing: the banner just prints less, or nothing, on a red night. The
banner's own tests (test_storage_cap_replica_role.py) feed it a payload typed by hand from the contract. These
build the payload with the REAL builder, from receipts and readers, and run the REAL script on it, so a rename on
either side turns a test red instead of silencing the alarm.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

# The banner harness: the real script under bash, HOME at a fixture dir, curl replaced by a shim.
from test_storage_cap_replica_role import Box

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mem0-server"))
import maintenance_health as mh  # noqa: E402

NOW = dt.datetime(2026, 9, 29, 8, 5, tzinfo=dt.timezone.utc)   # a Tuesday


def _row(step, ts, ok=True, status="ok", note=""):
    return {"ts": ts, "step": step, "ok": ok, "status": status, "exit": 0 if ok else 1, "duration_ms": 1000,
            "receipt_id": f"{step}-{ts}", "note": note, "work": {}}


HEALTHY = [_row("dream", "2026-09-28T08:03:00Z"), _row("dream", "2026-09-29T08:03:00Z"),
           _row("wiki-index", "2026-09-29T08:03:00Z")]

# A red night: wiki-index failed after succeeding the night before, the dream exited 0 but posted nothing,
# l10-audit has not succeeded in four days.
RED = [_row("wiki-index", "2026-09-28T08:03:00Z"),
       _row("wiki-index", "2026-09-29T08:03:00Z", ok=False, status="failed", note="exit 1"),
       _row("dream", "2026-09-29T08:03:00Z", status="degraded", note="posted-0-of-3"),
       _row("l10-audit", "2026-09-25T08:03:00Z")]


def _payload(tmp_path, rows, *, health="ONLINE", used=500, avail=500, ack=None, drift=None, pool_reader=None):
    receipts = tmp_path / "receipts.jsonl"
    receipts.parent.mkdir(parents=True, exist_ok=True)
    receipts.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return mh.build(receipts, NOW, pool_reader or (lambda: (used, avail)), lambda: [], lambda: "native",
                    pool_health_reader=lambda: health, pool_ack_reader=lambda: ack,
                    drift_reader=(lambda: drift) if drift is not None else None)


def _banner(tmp_path, payload, role="replica"):
    box = Box(tmp_path / "box", role)
    box.authority(up=True, maintenance=payload, episodes=[])
    return [ln for ln in box.run().splitlines() if ln.startswith("[AMS]")]


def test_the_payload_carries_every_field_the_banner_reads(tmp_path):
    p = _payload(tmp_path, RED, health="DEGRADED", drift={"alarm": True, "before_retrievable": 5, "n_total": 7})
    assert p["ok"] is False
    assert [e["step"] for e in p["failed_steps"]] == ["wiki-index"]
    assert [e["step"] for e in p["degraded_steps"]] == ["dream"]
    assert p["stale_steps"] == ["l10-audit"]
    assert {"used_pct", "alarm", "health", "health_alarm"} <= set(p["pool"])
    assert p["drift"]["alarm"] is True


@pytest.mark.parametrize("role", ["brain", "replica"])
def test_a_red_night_reads_through_the_banner(tmp_path, role):
    p = _payload(tmp_path, RED, health="DEGRADED", used=847, avail=153,
                 drift={"alarm": True, "before_retrievable": 5, "n_total": 7})
    assert _banner(tmp_path, p, role) == [
        "[AMS] brain NOT OK — failed: wiki-index; degraded: dream; pool 84.7% DEGRADED; stale: l10-audit; drift alarm"]


def test_a_healthy_night_is_silent(tmp_path):
    p = _payload(tmp_path, HEALTHY, drift={"alarm": False, "before_retrievable": 7, "n_total": 7})
    assert p["ok"] is True
    assert _banner(tmp_path, p) == []


def test_an_acknowledged_pool_is_silent_and_a_lapsed_ack_is_not(tmp_path):
    """The operator's dated ack (MEM0_POOL_HEALTH_ACK) clears pool.health_alarm, so ok is true and the banner says
    nothing; once the date passes the same DEGRADED pool reads through the banner again."""
    acked = _payload(tmp_path / "acked", HEALTHY, health="DEGRADED", ack="DEGRADED:2026-10-06")
    assert acked["pool"]["health_alarm"] is False and acked["ok"] is True
    assert _banner(tmp_path / "acked", acked) == []
    lapsed = _payload(tmp_path / "lapsed", HEALTHY, health="DEGRADED", ack="DEGRADED:2026-09-28")
    assert lapsed["pool"]["health_alarm"] is True
    assert _banner(tmp_path / "lapsed", lapsed) == ["[AMS] brain NOT OK — pool 50.0% DEGRADED"]


def test_an_acknowledged_pool_does_not_hide_a_failed_step(tmp_path):
    p = _payload(tmp_path, RED[:2], health="DEGRADED", ack="DEGRADED:2026-10-06")
    assert _banner(tmp_path, p) == ["[AMS] brain NOT OK — failed: wiki-index"]


def _capacity_unreadable():
    raise RuntimeError("zfs list failed")


def test_a_sick_pool_whose_capacity_cannot_be_read_is_still_named(tmp_path):
    """On a FAULTED pool `zfs list` fails while `zpool list -o health` still answers, so the payload says
    used_pct null and health_alarm true. ok is false; the banner has to name the pool, not print a bare line."""
    p = _payload(tmp_path, HEALTHY, health="FAULTED", pool_reader=_capacity_unreadable)
    assert p["pool"]["used_pct"] is None and p["pool"]["health_alarm"] is True and p["ok"] is False
    assert _banner(tmp_path, p) == ["[AMS] brain NOT OK — pool FAULTED"]
