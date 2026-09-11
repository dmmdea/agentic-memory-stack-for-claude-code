#!/usr/bin/env python3
"""ams_env — the one resolver for every brain-side job (spec §4, §7).

On the native authority there is no ~/.mem0/api-key (the key arrives through systemd
LoadCredentialEncrypted as $MEM0_API_KEY_FILE), the server binds the tailnet address (never
loopback), Codex's auth.json lives in the secrets dataset and the eval harness is optional.
Every job that used to hardcode `http://127.0.0.1:18791` + `~/.mem0/api-key` resolves here
instead; the WSL brain keeps working because each precedence chain ends at the old default.

Scripts are deployed flat into ~/apps/mem0-scripts, so a caller imports this as a sibling:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import ams_env
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path

MODEL_SYNTHESIS = "gpt-6-astra"      # open-ended synthesis + consequential judgment
MODEL_CLASSIFY = "gpt-5.6-terra"     # bounded extraction / classification / routing
EFFORT_SYNTHESIS = "medium"          # operator directive 2026-09-07: medium, not high
# Closed enum, counted not grepped (memory-common.ps1 Write-CodexUsageLog + the quota skip).
USAGE_OUTCOMES = {"ok", "empty", "timeout", "exit_nonzero", "parse_fail", "lock_unavailable",
                  "skipped_no_candidates", "skipped_quota", ""}


def _mem0_dir() -> Path:
    return Path.home() / ".mem0"


def stack_env() -> dict[str, str]:
    """~/.mem0/stack.env as a dict (KEY=VALUE lines, '#' comments); {} when absent."""
    out: dict[str, str] = {}
    try:
        for line in (_mem0_dir() / "stack.env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def mem0_url() -> str:
    """MEM0_URL env > ~/.mem0/authority-url > loopback (the replay-ops.py precedence)."""
    env = (os.environ.get("MEM0_URL") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        for line in (_mem0_dir() / "authority-url").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                return line.rstrip("/")
    except OSError:
        pass
    return "http://127.0.0.1:18791"


def api_key() -> str:
    """$MEM0_API_KEY_FILE (the unit's credential) > MEM0_KEY > MEM0_API_KEY > ~/.mem0/api-key > ''."""
    p = (os.environ.get("MEM0_API_KEY_FILE") or "").strip()
    if p:
        try:
            return Path(p).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    for var in ("MEM0_KEY", "MEM0_API_KEY"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v
    try:
        return (_mem0_dir() / "api-key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def codex_home() -> str:
    """CODEX_HOME env > <MEM0_SECRETS_DIR>/codex when it holds auth.json > ~/.codex."""
    env = (os.environ.get("CODEX_HOME") or "").strip()
    if env:
        return env
    sec = stack_env().get("MEM0_SECRETS_DIR", "")
    if sec and (Path(sec) / "codex" / "auth.json").exists():
        return str(Path(sec) / "codex")
    return str(Path.home() / ".codex")


def eval_root() -> str:
    """Where eval/retrieval-drift lives; '' means the drift canary no-ops (never a false alarm)."""
    return (os.environ.get("MEM0_EVAL_ROOT") or stack_env().get("MEM0_EVAL_ROOT") or "").strip()


def user_id() -> str:
    env = (os.environ.get("MEM0_DEFAULT_USER_ID") or "").strip()
    if env:
        return env
    kv = stack_env()
    return kv.get("MEM0_DEFAULT_USER_ID") or kv.get("MEM0_WSL_USER") or ""


def state_dir() -> Path:
    d = _mem0_dir() / "maintenance"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log(component: str, msg: str) -> None:
    """Append to maintenance/logs/<component>.log and echo (journalctl + the receipt note)."""
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{component}: {msg}", flush=True)
    try:
        d = state_dir() / "logs"
        d.mkdir(exist_ok=True)
        with open(d / f"{component}.log", "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def throttle_ok(name: str, min_interval_s: int) -> bool:
    """Pure read: has min_interval_s elapsed since mark_throttle(name)? Epochs are written by
    Python only, UTC — the PS 5.1 `%s` skew class cannot recur here."""
    try:
        last = int((state_dir() / f"last-{name}").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return int(time.time()) - last >= min_interval_s


def mark_throttle(name: str) -> None:
    (state_dir() / f"last-{name}").write_text(str(int(time.time())), encoding="utf-8")


def usage_log_path() -> Path:
    return state_dir() / "codex-usage.jsonl"


def write_usage(component: str, tokens_used: int = 0, duration_ms=0, status: str = "ok",
                items_posted: int = 0, model_requested: str = "", effort_requested: str = "",
                model_resolved: str = "", effort_resolved: str = "", outcome: str = "") -> None:
    """One ledger row per Codex call (the Write-CodexUsageLog record shape)."""
    if outcome not in USAGE_OUTCOMES:
        raise ValueError(f"unknown usage outcome {outcome!r}")
    rec = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(), "component": component,
           "tokens_used": int(tokens_used or 0), "duration_ms": duration_ms, "status": status,
           "items_posted": int(items_posted or 0), "model_requested": model_requested,
           "effort_requested": effort_requested, "model_resolved": model_resolved,
           "effort_resolved": effort_resolved, "outcome": outcome}
    try:
        with open(usage_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
