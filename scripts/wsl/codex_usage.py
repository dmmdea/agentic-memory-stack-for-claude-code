#!/usr/bin/env python3
"""codex_usage — what the Codex judges cost, per job, and how much plan window is left.

Python port of scripts/windows/codex-usage-report.ps1 + Get-CodexPlanWindow (memory-common.ps1)
for the native authority (spec §4 quota rule). Three things live here:

  * plan_window / probe_window — the 7-day plan window from the UNOFFICIAL chatgpt
    /wham/usage endpoint, or an explicit unknown. Because the endpoint is unofficial, a renamed
    or dropped field lets the CALL succeed; the shape check makes sure an unknown READS as
    unknown instead of a confident "0% used". Every probe appends one `codex-window` row to
    the usage ledger — the row /health/maintenance and the quota gate read.
  * quota_gate — the 25% reserve rule. An unknown window ALLOWS: the rule protects a reserve,
    it cannot act on a number it does not have (the reason string says so).
  * report — the per-job aggregation, verbatim from the PS version: calls, tokens, latency,
    and how often the job FAILED (a job that is cheap because half its calls die is not cheap).

Read-only apart from the ledger append. Never raises on a missing ledger, an unreadable token
or an unreachable endpoint — each degrades to a stated unknown.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ams_env  # noqa: E402

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
WINDOW_COMPONENT = "codex-window"
_OK_OUTCOMES = {"ok", "skipped_no_candidates"}


def _unknown(note: str) -> dict:
    return {"used_percent": None, "resets_in_days": None, "note": note}


def _as_float(v) -> float | None:
    """The [double]::TryParse([string]$v) of the PS version: bools and NaN/inf are not numbers."""
    if isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def plan_window(response: dict | None) -> dict:
    """rate_limit.primary_window.{used_percent, reset_after_seconds} -> window dict, or an
    explicit unknown with the note saying which field was missing or non-numeric."""
    pw = None
    try:
        pw = response["rate_limit"]["primary_window"]  # type: ignore[index]
    except (TypeError, KeyError, AttributeError):
        pw = None
    if not isinstance(pw, dict) or pw.get("used_percent") is None or pw.get("reset_after_seconds") is None:
        return _unknown("unexpected response shape (rate_limit.primary_window.used_percent / reset_after_seconds missing)")
    u = _as_float(pw.get("used_percent"))
    if u is None:
        return _unknown("used_percent is present but not numeric")
    r = _as_float(pw.get("reset_after_seconds"))
    if r is None:
        return _unknown("reset_after_seconds is present but not numeric")
    return {"used_percent": int(round(u)), "resets_in_days": round(r / 86400.0, 1), "note": ""}


def _append_window_row(window: dict) -> None:
    rec = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(), "component": WINDOW_COMPONENT,
           "used_percent": window.get("used_percent"), "resets_in_days": window.get("resets_in_days"),
           "note": window.get("note", "")}
    try:
        with open(ams_env.usage_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def probe_window(codex_home: str, http=None, url: str = USAGE_URL) -> dict:
    """GET the plan window with <codex_home>/auth.json's access token. ALWAYS appends one
    `codex-window` row to the usage ledger, including the unknown on failure."""
    import httpx

    own_client = http is None
    client = None
    try:
        auth = json.loads(Path(codex_home, "auth.json").read_text(encoding="utf-8"))
        tok = (auth.get("tokens") or {}).get("access_token") if isinstance(auth, dict) else None
        if not isinstance(tok, str) or not tok.strip():
            raise ValueError("no access_token")
        client = http if http is not None else httpx.Client(timeout=20.0)
        resp = client.get(url, headers={"Authorization": f"Bearer {tok.strip()}"}, timeout=20.0)
        resp.raise_for_status()
        window = plan_window(resp.json())
    except Exception as err:  # noqa: BLE001 — every failure is one stated unknown
        window = _unknown(f"window unavailable ({err})")
    finally:
        if own_client and client is not None:
            client.close()
    _append_window_row(window)
    return window


def _parse_ts(value) -> _dt.datetime | None:
    """ISO ts -> aware UTC datetime; None when absent or unparseable (the row is skipped)."""
    if not value:
        return None
    try:
        t = _dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.astimezone(_dt.timezone.utc)


def _read_rows(ledger: Path) -> list[dict]:
    """Every parseable JSON object in the ledger; a torn line must not end the report."""
    rows: list[dict] = []
    try:
        text = Path(ledger).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict):
            rows.append(o)
    return rows


def last_window(max_age_h: float = 12) -> dict | None:
    """The newest `codex-window` row younger than max_age_h, else None."""
    best = None
    best_t = None
    for o in _read_rows(ams_env.usage_log_path()):
        if o.get("component") != WINDOW_COMPONENT:
            continue
        t = _parse_ts(o.get("ts"))
        if t is None:
            continue
        if best_t is None or t > best_t:
            best, best_t = o, t
    if best is None:
        return None
    age_h = (_dt.datetime.now(_dt.timezone.utc) - best_t).total_seconds() / 3600.0
    return best if age_h < max_age_h else None


def quota_gate(window: dict, reserve_pct: int = 25) -> dict:
    """allow when used_percent <= 100 - reserve_pct; an unknown window allows and says so."""
    used = window.get("used_percent") if isinstance(window, dict) else None
    ceiling = 100 - reserve_pct
    if used is None:
        note = window.get("note", "") if isinstance(window, dict) else ""
        return {"allow": True, "reason": f"window unknown ({note}); cannot protect a reserve it cannot see"}
    if used <= ceiling:
        return {"allow": True, "reason": f"window {used}% used <= {ceiling}% ({reserve_pct}% reserve kept)"}
    return {"allow": False, "reason": f"window {used}% used > {ceiling}% ({reserve_pct}% reserve breached)"}


def _as_int(v) -> int | None:
    """[int]::TryParse([string]$v): bools are not ints; a whole float renders as its int."""
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _job_summary(name: str, group: list[dict], days: int) -> dict:
    tok = 0.0
    for r in group:
        f = _as_float(r.get("tokens_used"))
        if f is not None:
            tok += f
    # A duration_ms that is present but not a number does NOT kill the report and does NOT
    # silently vanish from the latency sample: it is skipped deliberately and counted VISIBLY.
    durs = sorted(d for d in (_as_int(r.get("duration_ms")) for r in group) if d is not None and d > 0)
    bad_dur = 0
    for r in group:
        raw = r.get("duration_ms")
        if raw is None or str(raw) == "":
            continue  # ABSENT is not MALFORMED
        if _as_int(raw) is None:
            bad_dur += 1
    p50 = durs[int(math.floor(len(durs) * 0.5))] if durs else 0
    mx = durs[-1] if durs else 0
    failed = sum(1 for r in group if r.get("outcome") and r.get("outcome") not in _OK_OUTCOMES)
    models: list[str] = []
    for r in group:
        m = r.get("model_requested")
        if m and m not in models:
            models.append(str(m))
    # a requested/resolved mismatch is silent model drift — surface it, never average it away;
    # 'unparsed' is an unknown, not a mismatch, and is counted SEPARATELY so it never reads as 0 drift.
    drift = sum(1 for r in group if r.get("model_requested") and r.get("model_resolved")
                and r.get("model_resolved") != "unparsed" and r.get("model_requested") != r.get("model_resolved"))
    unparsed = sum(1 for r in group if r.get("model_resolved") == "unparsed")
    return {"job": name, "calls": len(group), "tokens": int(round(tok)),
            "per_day": round(len(group) / max(1, days), 1), "p50_ms": p50, "max_ms": mx,
            "failed": failed, "drift": drift, "unparsed": unparsed, "bad_duration": bad_dur,
            "model": ",".join(models)}


def report(days: int = 7, ledger: Path | None = None, now=None, window: dict | None = None) -> dict:
    """Per-job aggregation over the last `days` days of the usage ledger + the plan window."""
    ledger = Path(ledger) if ledger is not None else ams_env.usage_log_path()
    now = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    cutoff = now - _dt.timedelta(days=abs(days))
    groups: dict[str, list[dict]] = {}
    for o in _read_rows(ledger):
        t = _parse_ts(o.get("ts"))
        if t is None or t < cutoff:
            continue
        name = o.get("component") or ""
        if name == WINDOW_COMPONENT:
            continue  # a probe receipt, not a Codex call — it must not count as a job
        groups.setdefault(name, []).append(o)
    jobs = sorted((_job_summary(n, g, days) for n, g in groups.items()), key=lambda j: j["tokens"], reverse=True)
    if window is None:
        window = last_window() or _unknown("window not read")
    return {"days": days, "jobs": jobs, "window": window,
            "total_calls": sum(j["calls"] for j in jobs), "total_tokens": sum(j["tokens"] for j in jobs)}
