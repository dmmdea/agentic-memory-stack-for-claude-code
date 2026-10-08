"""maintenance_health.py — GET /health/maintenance (spec §9, P1-5).

Each chain step's last success, last run, duration and receipt id (from the receipts
ams-step.sh appends to ~/.mem0/maintenance/receipts.jsonl); the steps whose LATEST run failed or
degraded (the receipt's `status`, outcome contract C1), and `critical_failed_steps`, the failed ones
minus the PC-dependent steps (PC_DEPENDENT_STEPS); the judge transport; pool usage with the
85 % alarm and the pool's HEALTH; the retrieval-drift and wiki-freshness readings; the box's boot
ids for the last 7 days; the write path (write_path.py), whose latest failed write turns
`ok` false; and the capture liveness of the PC-side L1a extractor (`capture`, informational: it
never turns `ok` false). Pure functions with injected
readers so the endpoint is testable without a chain, a pool or a journal; the route wires the
real readers. A health endpoint never raises on a reader: a failed reader reads as unknown."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

from job_liveness import read_stack_env

POOL_ALARM_PCT = 85
STALE_AFTER_H = 48             # a daily step with no success in two nights
WEEKLY_STALE_AFTER_H = 8 * 24  # a --weekly step: a week and a day of slack
WEEKLY_PROBE = 3               # receipts inspected to decide "this is a weekly step"
# ams-step.sh writes these notes for a run that did not run the job (`--weekly` off-day, boot guard).
NO_OP_NOTE_PREFIXES = ("weekly:", "guard:")
# The step that PRINTS this verdict exits non-zero on a bad one. The endpoint reads the previous
# night's receipt before tonight's stamp runs, so folding it back in (failed OR stale) would keep a night red.
VERDICT_STEPS = frozenset({"health-stamp"})
# Steps whose failure is not actionable at night, because they depend on a PC being switched on. `critical_failed_steps`
# is `failed_steps` minus this set: an external monitor pages on it urgently, even through quiet hours, while
# `failed_steps` keeps paging at the normal level. Today that is `wiki-index` alone: its nightly pull needs a PC that
# mounts the vault, so with every PC off it fails by design once the index is more than 72 h old
# (scripts/wsl/wiki-index-nightly.sh, docs/systems/wiki-index.md). The unit is the step: its other failure (a pull whose
# build failed) is real, and still reaches the normal page through `failed_steps`.
PC_DEPENDENT_STEPS = frozenset({"wiki-index"})
MAX_RECEIPT_LINES = 2000
# Capture liveness (audit CRIT-01). The two thresholds are capabilities.py's own FRESH_H and L1A_CONVICT_H, so the
# authority and the capability manifest mean the same by "quiet" and "convict".
CAPTURE_QUIET_H = 48.0
CAPTURE_STALLED_H = 96.0
# How long the sessions since the last success must have been going before a stale success convicts: L1a runs at most
# every 10 minutes (its throttle), so an hour of sessions without a finished run is past its first chance. Without it,
# the first prompt after a trip of more than 96 h would read `stalled` until L1a's first run.
CAPTURE_GRACE_H = 1.0
# A stamp further ahead of this server's clock than this is a PC clock error, not a reading: it would otherwise hold
# the verdict at `ok` until real time caught up.
CAPTURE_CLOCK_AHEAD_H = 1.0


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


POOL_ACK_KEY = "MEM0_POOL_HEALTH_ACK"
_POOL_ACK_RE = re.compile(r"^([A-Za-z]+):(\d{4}-\d{2}-\d{2})$")


def read_pool_ack(environ=None) -> Optional[str]:
    """The operator's pool-health acknowledgment (`<STATE>:<YYYY-MM-DD>`), read on EVERY call: the process
    environment first, then ~/.mem0/stack.env through the server's own parser (the server unit does not
    load stack.env into its environment, so an env-only read would be a silent no-op in production).
    None when neither names it; a blank value is None."""
    environ = os.environ if environ is None else environ
    raw = environ.get(POOL_ACK_KEY)
    if raw is None or not str(raw).strip():
        raw = read_stack_env().get(POOL_ACK_KEY)
    raw = None if raw is None else str(raw).strip()
    return raw or None


def _pool_ack(raw: Optional[str], live_health: str, today: dt.date) -> Optional[dict]:
    """The `pool.health_ack` object for an ack value against the live pool health, or None for no ack.
    Active iff it parses, `today` (UTC) <= its date, and the live health equals its STATE. Otherwise the
    object says why: malformed | expired | mismatch."""
    if raw is None or not raw.strip():
        return None
    raw = raw.strip()
    m = _POOL_ACK_RE.match(raw)
    until: Optional[dt.date] = None
    if m:
        try:
            until = dt.date.fromisoformat(m.group(2))
        except ValueError:
            until = None
    if not m or until is None:
        return {"state": None, "until": None, "active": False, "reason": "malformed", "value": raw[:80]}
    state = m.group(1).upper()
    ack = {"state": state, "until": m.group(2), "active": False}
    if today > until:
        ack["reason"] = "expired"
    elif live_health.upper() != state:
        ack["reason"] = "mismatch"
    else:
        ack["active"] = True
    return ack


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


def _write_path(reader: Optional[Callable[[], dict]]) -> Optional[dict]:
    """The write-path snapshot for the payload (write_path.snapshot: ok, last_ok_at, last_error_at, last_error,
    errors_1h, writes_1h), or None when no reader is wired: the payload then has no `write_path` key and `ok` is
    unchanged. A reader that raises, or answers with something that is not a dict, reads as unknown (`ok: None`):
    fail-open on the reader, loud in the value."""
    if reader is None:
        return None
    try:
        snap = reader()
        if not isinstance(snap, dict):
            raise TypeError("a write-path reader returns a dict")
        return dict(snap)
    except Exception:  # noqa: BLE001 — a health endpoint never raises on a reader
        return {"ok": None, "note": "write-path reader failed"}


def capture_state(success_age_h: Optional[float], activity_age_h: Optional[float],
                  first_activity_age_h: Optional[float] = None) -> str:
    """ok | quiet | stalled | unknown. Pure. The authority-side twin of capabilities._l1a_state (review F12).

    `success` = a PC's L1a finished a run (a complete episode); `activity` = a PC session is happening (any episode
    touched, which every UserPromptSubmit does whether or not L1a ever runs); `first_activity` = when the sessions
    since the last success began. One stale stamp must not convict: a long weekend, a trip or both PCs switched off
    look exactly like a dead extractor from the success side alone.

      ok        a run finished within CAPTURE_QUIET_H
      stalled   no run for more than CAPTURE_STALLED_H while a session was active within CAPTURE_QUIET_H, and those
                sessions have been going for at least CAPTURE_GRACE_H (L1a has had its chances)
      quiet     stale, but the PCs were quiet too, or the sessions only just began, or the silence is still inside the
                grace window: cannot convict
      unknown   no run on record, so a broken extractor cannot be told from a new install (F12)
    first_activity_age_h None (a reader without it) skips the grace check.
    """
    if success_age_h is None:
        return "unknown"
    if success_age_h <= CAPTURE_QUIET_H:
        return "ok"
    if (activity_age_h is not None and activity_age_h <= CAPTURE_QUIET_H and success_age_h > CAPTURE_STALLED_H
            and (first_activity_age_h is None or first_activity_age_h >= CAPTURE_GRACE_H)):
        return "stalled"
    return "quiet"


def _capture(reader: Optional[Callable[[], dict]], now: dt.datetime) -> Optional[dict]:
    """The `capture` block, or None when no reader is wired (the payload then has no `capture` key).

    `reader()` returns {"success_at": iso|None, "activity_at": iso|None[, "first_activity_at": iso|None]}
    (episodic.capture_signals). A reader that raises, or answers with something else, reads as `unknown` and never as
    `stalled`: fail-open on the reader, loud in the value (the write-path rule). A stamp more than
    CAPTURE_CLOCK_AHEAD_H ahead of this server is ignored (a PC clock error) and named in `note`. `stalled` is always a
    boolean so a Gatus condition can read it."""
    if reader is None:
        return None
    out: dict = {"state": "unknown", "stalled": False, "success_at": None, "success_age_h": None,
                 "activity_at": None, "activity_age_h": None,
                 "quiet_after_h": CAPTURE_QUIET_H, "stalled_after_h": CAPTURE_STALLED_H}
    try:
        sig = reader()
        if not isinstance(sig, dict):
            raise TypeError("a capture reader returns a dict")
        ages: dict = {}
        ahead: list = []
        for key in ("success", "activity", "first_activity"):
            ts = _parse_ts(sig.get(f"{key}_at") or "")
            if ts is None:
                continue
            age = (now - ts).total_seconds() / 3600.0
            if -age > CAPTURE_CLOCK_AHEAD_H:
                ahead.append(f"{key}_at")
                continue
            ages[key] = max(0.0, age)      # a small skew never makes a negative age
            if key == "first_activity":
                continue                   # used for the grace check only: the payload keeps its documented keys
            out[f"{key}_at"] = ts.astimezone(dt.timezone.utc).isoformat()
            out[f"{key}_age_h"] = round(ages[key], 1)
        out["state"] = capture_state(ages.get("success"), ages.get("activity"), ages.get("first_activity"))
        out["stalled"] = out["state"] == "stalled"
        if ahead:
            out["note"] = "ignored a stamp from the future (a PC clock ahead): " + ", ".join(ahead)
    except Exception:  # noqa: BLE001 — a health endpoint never raises on a reader
        out["state"], out["stalled"], out["note"] = "unknown", False, "capture reader failed"
    return out


def build(receipts_path: Path, now: dt.datetime, pool_reader: Callable[[], tuple],
          boots_reader: Callable[[], list[str]], judge_transport: Callable[[], str],
          usage_reader: Optional[Callable[[], dict]] = None,
          pool_health_reader: Optional[Callable[[], str]] = None,
          pool_ack_reader: Optional[Callable[[], Optional[str]]] = None,
          wiki_stamp_dir: Optional[Path] = None,
          drift_reader: Optional[Callable[[], dict]] = None,
          write_path_reader: Optional[Callable[[], dict]] = None,
          capture_reader: Optional[Callable[[], dict]] = None) -> dict:
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
    # The urgent subset, derived from the list above (same shape, same order). `ok` and `failed_steps` are
    # computed from the whole list and never read this one.
    critical_failed_steps = [dict(e) for e in failed_steps if e["step"] not in PC_DEPENDENT_STEPS]
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
    # A known, dated, non-ONLINE pool (a planned disk swap) is acknowledged by the operator: reported, not
    # alarmed, until the date passes. The ack is read per call; a reader that fails reads as no ack (the alarm
    # stays). It can only lower an alarm for exactly the state it names; it never raises one.
    raw_ack: Optional[str] = None
    if pool_ack_reader is not None:
        try:
            raw_ack = pool_ack_reader()
        except Exception:  # noqa: BLE001
            raw_ack = None
    ack = _pool_ack(raw_ack, health, now.astimezone(dt.timezone.utc).date())
    if ack is not None:
        pool["health_ack"] = ack
        if ack["active"]:
            pool["health_alarm"] = False
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
    # A write path whose latest write failed is a memory server that cannot remember, whatever the nightly chain
    # says. Only a reading of exactly `ok: false` counts: no reader, or an unreadable one (`ok: None`), never reddens.
    write_path = _write_path(write_path_reader)
    if write_path is not None and write_path.get("ok") is False:
        ok = False
    capture = _capture(capture_reader, now)   # informational: never folded into ok / failed_steps / stale_steps
    out = {"ok": ok, "steps": steps, "stale_steps": stale, "failed_steps": failed_steps,
           "critical_failed_steps": critical_failed_steps,
           "degraded_steps": degraded_steps, "judge_transport": jt, "pool": pool,
           "drift": _drift(drift_reader), "wiki": _wiki(wiki_stamp_dir, now),
           "usage": usage, "boots_7d": boots, "generated": now.isoformat()}
    if dataset is not None:
        out["dataset"] = dataset
    if write_path is not None:
        out["write_path"] = write_path
    if capture is not None:
        out["capture"] = capture
    return out
