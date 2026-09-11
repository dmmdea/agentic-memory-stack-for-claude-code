"""maintenance_health.py — GET /health/maintenance (spec §9, P1-5).

Each chain step's last success, last run, duration and receipt id (from the receipts
ams-step.sh appends to ~/.mem0/maintenance/receipts.jsonl); the judge transport; pool usage
with the 85 % alarm; the box's boot ids for the last 7 days. Pure functions with injected
readers so the endpoint is testable without a chain, a pool or a journal; the route wires the
real readers. A health endpoint never raises on a reader: a failed reader reads as unknown."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Callable, Optional

POOL_ALARM_PCT = 85
STALE_AFTER_H = 48
MAX_RECEIPT_LINES = 2000


def _parse_ts(s: str) -> Optional[dt.datetime]:
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


def read_receipts(path: Path) -> list[dict]:
    """The last MAX_RECEIPT_LINES well-formed receipts (a line needs `step` and a parseable `ts`)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for ln in lines[-MAX_RECEIPT_LINES:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("step") and _parse_ts(o.get("ts", "")):
            out.append(o)
    return out


def parse_zfs_list(text: str) -> tuple[int, int]:
    """`zfs list -Hp -o used,avail <dataset>` -> (used_bytes, avail_bytes)."""
    used, avail = text.strip().split()[:2]
    return int(used), int(avail)


def zfs_pool_reader(dataset: str) -> Callable[[], tuple[int, int]]:
    def read() -> tuple[int, int]:
        cp = subprocess.run(["zfs", "list", "-Hp", "-o", "used,avail", dataset],
                            capture_output=True, text=True, timeout=5, check=True)
        return parse_zfs_list(cp.stdout)
    return read


def disk_usage_reader(path: str) -> Callable[[], tuple[int, int]]:
    import shutil

    def read() -> tuple[int, int]:
        u = shutil.disk_usage(path)
        return u.used, u.free
    return read


def boots_from_journal_json(text: str, now: dt.datetime) -> list[str]:
    """`journalctl --list-boots -o json` -> boot ids whose first entry is inside the last 7 days."""
    cutoff = (now - dt.timedelta(days=7)).timestamp() * 1e6
    rows = json.loads(text) if text.strip() else []
    return [r["boot_id"] for r in rows
            if isinstance(r, dict) and r.get("boot_id") and float(r.get("first_entry", 0)) >= cutoff]


def journal_boots_reader(now_fn=lambda: dt.datetime.now(dt.timezone.utc)) -> Callable[[], list[str]]:
    def read() -> list[str]:
        cp = subprocess.run(["journalctl", "--list-boots", "-o", "json"],
                            capture_output=True, text=True, timeout=5, check=True)
        return boots_from_journal_json(cp.stdout, now_fn())
    return read


def build(receipts_path: Path, now: dt.datetime, pool_reader: Callable[[], tuple[int, int]],
          boots_reader: Callable[[], list[str]], judge_transport: Callable[[], str]) -> dict:
    steps: dict[str, dict] = {}
    for r in read_receipts(Path(receipts_path)):
        s = steps.setdefault(r["step"], {"last_success": None, "last_run": None, "duration_ms": None,
                                         "receipt_id": None, "ok": False})
        ts = _parse_ts(r["ts"])
        if s["last_run"] is None or ts >= _parse_ts(s["last_run"]):
            s["last_run"] = r["ts"]
            s["ok"] = bool(r.get("ok"))
            s["duration_ms"] = r.get("duration_ms")
            s["receipt_id"] = r.get("receipt_id")
        if r.get("ok") and (s["last_success"] is None or ts >= _parse_ts(s["last_success"])):
            s["last_success"] = r["ts"]
    stale = sorted(n for n, s in steps.items()
                   if s["last_success"] is None
                   or (now - _parse_ts(s["last_success"])) > dt.timedelta(hours=STALE_AFTER_H))
    try:
        used, avail = pool_reader()
        pct = round(100.0 * used / (used + avail), 1) if (used + avail) > 0 else None
    except Exception:  # noqa: BLE001 — a health endpoint never raises on a reader
        pct = None
    pool = {"used_pct": pct, "alarm": bool(pct is not None and pct >= POOL_ALARM_PCT), "threshold_pct": POOL_ALARM_PCT}
    try:
        boots = list(boots_reader())
    except Exception:  # noqa: BLE001
        boots = []
    try:
        jt = str(judge_transport())
    except Exception:  # noqa: BLE001
        jt = "none"
    ok = not pool["alarm"] and not stale
    return {"ok": ok, "steps": steps, "stale_steps": stale, "judge_transport": jt, "pool": pool,
            "boots_7d": boots, "generated": now.isoformat()}
