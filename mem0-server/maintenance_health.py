"""maintenance_health.py — GET /health/maintenance (spec §9, P1-5).

Each chain step's last success, last run, duration and receipt id (from the receipts
ams-step.sh appends to ~/.mem0/maintenance/receipts.jsonl); the steps whose LATEST run failed or
degraded (the receipt's `status`, outcome contract C1); the judge transport; pool usage with the
85 % alarm and the pool's HEALTH; the retrieval-drift and wiki-freshness readings; the box's boot
ids for the last 7 days. Pure functions with injected
readers so the endpoint is testable without a chain, a pool or a journal; the route wires the
real readers. A health endpoint never raises on a reader: a failed reader reads as unknown."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Callable, Optional

POOL_ALARM_PCT = 85
STALE_AFTER_H = 48             # a daily step with no success in two nights
WEEKLY_STALE_AFTER_H = 8 * 24  # a --weekly step: a week and a day of slack
WEEKLY_PROBE = 3               # receipts inspected to decide "this is a weekly step"
# ams-step.sh writes these notes for a run that did not run the job (`--weekly` off-day, boot guard).
NO_OP_NOTE_PREFIXES = ("weekly:", "guard:")
# The step that PRINTS this verdict exits non-zero on a bad one. The endpoint reads the previous
# night's receipt before tonight's stamp runs, so folding it back in (failed OR stale) would keep a night red.
VERDICT_STEPS = frozenset({"health-stamp"})
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


def parse_zpool_list(text: str) -> tuple[int, int]:
    """`zpool list -Hp -o allocated,size <pool>` -> (used_bytes, avail_bytes) = (alloc, size-alloc).
    The POOL figure is zpool's capacity, the number every receipt and the operator quote; the root
    dataset's used/avail subtracts slop space and reservations and read 85.9 % against a 76 % pool."""
    alloc, size = text.strip().split()[:2]
    return int(alloc), max(0, int(size) - int(alloc))


def zfs_pool_reader(dataset: str) -> Callable[[], tuple[int, int, int, int]]:
    """(pool_used, pool_avail, dataset_used, dataset_avail). The POOL is the alarm subject
    (spec §9: pool usage, alarm at 85 %); a quota-bearing dataset reports its quota headroom
    as avail, which read 2 % while the pool stood at 78 % (first staging night)."""
    pool = dataset.split("/", 1)[0]

    def read() -> tuple[int, int, int, int]:
        cp = subprocess.run(["zpool", "list", "-Hp", "-o", "allocated,size", pool],
                            capture_output=True, text=True, timeout=5, check=True)
        pu, pa = parse_zpool_list(cp.stdout)
        cp = subprocess.run(["zfs", "list", "-Hp", "-o", "used,avail", dataset],
                            capture_output=True, text=True, timeout=5, check=True)
        du, da = parse_zfs_list(cp.stdout)
        return pu, pa, du, da
    return read


def usage_window_reader(path: Path) -> Callable[[], dict]:
    """The newest `codex-window` row of the usage ledger (written by codex-usage-report.py
    --probe, which the dream step runs first): the Codex plan window spec §9 reports."""
    def read() -> dict:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return {"used_percent": None, "resets_in_days": None, "probed_at": None, "note": "no probe yet"}
        for ln in reversed(lines[-MAX_RECEIPT_LINES:]):
            try:
                o = json.loads(ln)
            except ValueError:
                continue
            if isinstance(o, dict) and o.get("component") == "codex-window":
                return {"used_percent": o.get("used_percent"), "resets_in_days": o.get("resets_in_days"),
                        "probed_at": o.get("ts"), "note": o.get("note", "")}
        return {"used_percent": None, "resets_in_days": None, "probed_at": None, "note": "no probe yet"}
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


def zpool_health_reader(dataset: str) -> Callable[[], str]:
    """`zpool list -H -o health <pool>` -> ONLINE | DEGRADED | FAULTED | ... for the pool that holds
    `dataset` (the capacity reader's pool). Raises on a missing zpool: build() reads that as unknown."""
    pool = dataset.split("/", 1)[0]

    def read() -> str:
        cp = subprocess.run(["zpool", "list", "-H", "-o", "health", pool],
                            capture_output=True, text=True, timeout=5, check=True)
        return cp.stdout.strip()
    return read


def _is_no_op(r: dict) -> bool:
    return str(r.get("note") or "").startswith(NO_OP_NOTE_PREFIXES)


def _status(r: dict) -> str:
    """The receipt's outcome: `status` (C1), else derived from `ok` for a pre-contract receipt."""
    st = str(r.get("status") or "")
    return st if st in ("ok", "degraded", "failed") else ("ok" if r.get("ok") else "failed")


def _is_weekly(recent: list[dict]) -> bool:
    """A `--weekly` step: it receipts a `weekly:` no-op on every off-day, or (no no-ops on file)
    its last few receipts all fall on a Sunday. A daily step never writes a `weekly:` note."""
    if any(str(r.get("note") or "").startswith("weekly:") for r in recent):
        return True
    tail = recent[-WEEKLY_PROBE:]
    return bool(tail) and all(_parse_ts(r["ts"]).weekday() == 6 for r in tail)


def _age_h(epoch_file: Path, now: dt.datetime) -> Optional[float]:
    try:
        return round((now.timestamp() - float(Path(epoch_file).read_text(encoding="utf-8").strip())) / 3600.0, 1)
    except (OSError, ValueError):
        return None


def _wiki(stamp_dir: Optional[Path], now: dt.datetime) -> dict:
    """Wiki-index freshness (C4): `last-pull` (existing) and `last-build` (any successful build) are
    epoch stamps beside the snapshot; `fresh_age_h` is the newer of the two. Reported, never folded
    into ok: the wiki-index step's own status carries that."""
    pull = built = None
    if stamp_dir is not None:
        pull, built = _age_h(Path(stamp_dir) / "last-pull", now), _age_h(Path(stamp_dir) / "last-build", now)
    known = [a for a in (pull, built) if a is not None]
    return {"last_pull_age_h": pull, "last_build_age_h": built, "fresh_age_h": min(known) if known else None}


def _drift(reader: Optional[Callable[[], dict]]) -> dict:
    out: dict = {"alarm": None, "before": None, "n_total": None}
    if reader is None:
        return out
    try:
        d = dict(reader())
        out = {"alarm": d.get("alarm"), "before": d.get("before_retrievable"), "n_total": d.get("n_total")}
    except Exception:  # noqa: BLE001 — a health endpoint never raises on a reader
        pass
    return out


def build(receipts_path: Path, now: dt.datetime, pool_reader: Callable[[], tuple],
          boots_reader: Callable[[], list[str]], judge_transport: Callable[[], str],
          usage_reader: Optional[Callable[[], dict]] = None,
          pool_health_reader: Optional[Callable[[], str]] = None,
          wiki_stamp_dir: Optional[Path] = None,
          drift_reader: Optional[Callable[[], dict]] = None) -> dict:
    by_step: dict[str, list[dict]] = {}
    for r in read_receipts(Path(receipts_path)):
        by_step.setdefault(r["step"], []).append(r)
    steps: dict[str, dict] = {}
    stale: list[str] = []
    failed_steps: list[dict] = []
    degraded_steps: list[dict] = []
    for name, rows in by_step.items():
        s = steps[name] = {"last_success": None, "last_run": None, "duration_ms": None,
                           "receipt_id": None, "ok": False, "status": "failed"}
        real_success: Optional[dt.datetime] = None   # latest ok receipt that actually ran the job
        latest_run: Optional[dict] = None            # latest receipt that actually ran the job
        for r in rows:
            ts = _parse_ts(r["ts"])
            if s["last_run"] is None or ts >= _parse_ts(s["last_run"]):
                s["last_run"] = r["ts"]
                s["ok"] = bool(r.get("ok"))
                s["status"] = _status(r)
                s["duration_ms"] = r.get("duration_ms")
                s["receipt_id"] = r.get("receipt_id")
            if r.get("ok") and (s["last_success"] is None or ts >= _parse_ts(s["last_success"])):
                s["last_success"] = r["ts"]
            if _is_no_op(r):
                continue
            if r.get("ok") and (real_success is None or ts > real_success):
                real_success = ts
            if latest_run is None or ts >= _parse_ts(latest_run["ts"]):
                latest_run = r
        # A step is judged on its LATEST real run (a later ok run clears it). The weekly / guard
        # no-ops are ok:true rows but not runs: Monday's off-day receipt must not erase Sunday's failure.
        if latest_run is not None and name not in VERDICT_STEPS:
            st = _status(latest_run)
            entry = {"step": name, "ts": latest_run["ts"], "note": str(latest_run.get("note") or "")}
            if st == "failed":
                failed_steps.append(entry)
            elif st == "degraded":
                degraded_steps.append(entry)
        # Staleness: 48 h for a daily step; a weekly step is judged on its real runs against 8 days
        # (its off-day no-ops would otherwise read "alive" for a Sunday run that never happened).
        if _is_weekly(rows[-10:]):
            ref = real_success or (_parse_ts(s["last_success"]) if s["last_success"] else None)
            limit_h = WEEKLY_STALE_AFTER_H
        else:
            ref = _parse_ts(s["last_success"]) if s["last_success"] else None
            limit_h = STALE_AFTER_H
        if name not in VERDICT_STEPS and (ref is None or (now - ref) > dt.timedelta(hours=limit_h)):
            stale.append(name)
    stale.sort()
    failed_steps.sort(key=lambda e: e["step"])
    degraded_steps.sort(key=lambda e: e["step"])
    dataset: Optional[dict] = None
    try:
        used, avail, *ds = pool_reader()   # 2-tuple (disk usage) or 4-tuple (pool + dataset)
        pct = round(100.0 * used / (used + avail), 1) if (used + avail) > 0 else None
        if len(ds) == 2:
            du, da = ds
            dataset = {"used_bytes": du, "avail_bytes": da,
                       "used_pct": round(100.0 * du / (du + da), 1) if (du + da) > 0 else None}
    except Exception:  # noqa: BLE001 — a health endpoint never raises on a reader
        pct = None
    # Pool HEALTH is a different fact from pool CAPACITY: a mirror with a leg offline is 40 % full and
    # DEGRADED. Anything but ONLINE alarms; an unreadable pool reads "unknown" and does not (fail-open on
    # the reader, loud in the value).
    health = "unknown"
    if pool_health_reader is not None:
        try:
            health = str(pool_health_reader()).strip() or "unknown"
        except Exception:  # noqa: BLE001
            health = "unknown"
    pool = {"used_pct": pct, "alarm": bool(pct is not None and pct >= POOL_ALARM_PCT), "threshold_pct": POOL_ALARM_PCT,
            "health": health, "health_alarm": health != "unknown" and health.upper() != "ONLINE"}
    usage: dict = {"used_percent": None, "resets_in_days": None, "probed_at": None, "note": "no probe yet"}
    if usage_reader is not None:
        try:
            usage = dict(usage_reader())
        except Exception:  # noqa: BLE001
            usage["note"] = "usage reader failed"
    try:
        boots = list(boots_reader())
    except Exception:  # noqa: BLE001
        boots = []
    try:
        jt = str(judge_transport())
    except Exception:  # noqa: BLE001
        jt = "none"
    ok = (not pool["alarm"] and not pool["health_alarm"] and not stale
          and not failed_steps and not degraded_steps)
    out = {"ok": ok, "steps": steps, "stale_steps": stale, "failed_steps": failed_steps,
           "degraded_steps": degraded_steps, "judge_transport": jt, "pool": pool,
           "drift": _drift(drift_reader), "wiki": _wiki(wiki_stamp_dir, now),
           "usage": usage, "boots_7d": boots, "generated": now.isoformat()}
    if dataset is not None:
        out["dataset"] = dataset
    return out
