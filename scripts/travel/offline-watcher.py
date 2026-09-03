#!/usr/bin/env python3
"""scripts/travel/offline-watcher.py — native-Linux port of offline-watcher.ps1.

One tick every 2 minutes (systemd user timer). Probes the Brain's /health, steps the same
hysteresis state machine as the PowerShell watcher (3 consecutive down ticks -> offline,
2 consecutive up ticks -> online), and acts ONLY on transitions:

  go_offline  -> start the dormant local qdrant + mem0 (the shim's per-call failover then
                 answers reads from the replica; writes keep queueing to the Outbox)
  go_online   -> drain the Outbox to the Brain through the sibling replay-ops.py, then stop
                 the local services (one live brain)

One improvement over the PowerShell version: the replica is REFRESHED WHILE ONLINE. The old
watcher tried to restore a fresh snapshot at go_offline, which is the one moment the Brain
cannot be reached. Here a steady online tick whose last restore is older than 24h runs
restore-replica.sh (fetch + restore, services stopped again afterwards), so the copy the box
carries into an outage is at most a day old.

Stdlib only; runs on any python3. Testable pieces have no side effects: step_offline_state,
is_local_url, resolve_authority, plan_actions.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import ipaddress
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DOWN_TICKS = 3          # online -> offline after N consecutive unreachable probes
UP_TICKS = 2            # offline -> online after M consecutive reachable probes
REFRESH_AFTER_H = 24.0  # refresh the dormant replica when the last restore is older than this
RETRY_AFTER_H = 6.0     # after a failed/aborted refresh, do not try again sooner than this
PROBE_TIMEOUT_S = 4.0


def is_local_url(url: str) -> bool:
    """True = local/unspecified/malformed: NEVER trust as the authority. Mirrors replay-ops.py
    and the PowerShell Test-IsLocalUrl (fail-closed on anything unparsable)."""
    url = (url or "").strip()
    if not url:
        return True
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname
    except ValueError:
        return True
    if not host:
        return True
    h = host.lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False  # a real hostname other than localhost is remote
    return ip.is_loopback or ip.is_unspecified


def resolve_authority(explicit: str | None, env_url: str | None, authority_file: Path) -> str | None:
    """explicit (always honoured) > MEM0_URL when not local > ~/.mem0/authority-url when not
    local > None (the caller refuses: a watcher with no remote authority must not guess)."""
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    if env_url and not is_local_url(env_url):
        return env_url.strip().rstrip("/")
    try:
        for line in authority_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return None if is_local_url(line) else line.rstrip("/")
    except OSError:
        pass
    return None


def step_offline_state(state: dict, reachable: bool, n: int = DOWN_TICKS, m: int = UP_TICKS) -> dict:
    down = int(state.get("consecutive_down", 0))
    up = int(state.get("consecutive_up", 0))
    mode = str(state.get("mode", "online"))
    transition = "none"
    if reachable:
        up += 1
        down = 0
    else:
        down += 1
        up = 0
    if mode == "online" and down >= n:
        mode, transition = "offline", "go_offline"
    elif mode == "offline" and up >= m:
        mode, transition = "online", "go_online"
    return {"mode": mode, "consecutive_down": down, "consecutive_up": up, "transition": transition}


def plan_actions(next_state: dict, reachable: bool, restore_age_h: float | None,
                 attempt_age_h: float | None = None, refresh_after_h: float = REFRESH_AFTER_H,
                 retry_after_h: float = RETRY_AFTER_H) -> list[str]:
    """What this tick does, as data. Transitions win; a steady online tick may refresh, but a
    refresh that failed is not retried every 2 minutes: attempt_age_h (age of the last restore
    attempt, success or not) must exceed retry_after_h first."""
    t = next_state.get("transition")
    if t == "go_offline":
        return ["start_services"]
    if t == "go_online":
        return ["drain_outbox", "stop_services"]
    stale = restore_age_h is None or restore_age_h > refresh_after_h
    cooled = attempt_age_h is None or attempt_age_h > retry_after_h
    if next_state.get("mode") == "online" and reachable and stale and cooled:
        return ["refresh_replica"]
    return []


# ---------------------------------------------------------------- side effects
def probe(authority: str) -> bool:
    try:
        with urllib.request.urlopen(authority + "/health", timeout=PROBE_TIMEOUT_S) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
            return bool(body.get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def load_state(path: Path) -> dict:
    default = {"mode": "online", "consecutive_down": 0, "consecutive_up": 0, "transition": "none"}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("mode") in ("online", "offline") else default
    except (OSError, ValueError):
        return default


def _age_hours(iso: str) -> float:
    ts = _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() / 3600.0


def restore_age_hours(stamp: Path) -> float | None:
    try:
        return _age_hours(stamp.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def attempt_age_hours(stamp: Path) -> float | None:
    """Age of the last refresh ATTEMPT (written by this watcher before it runs the restore), so a
    failing restore backs off instead of re-running on every tick."""
    try:
        return _age_hours(stamp.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def run(cmd: list[str], env: dict | None = None, timeout: int = 3600) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="offline watcher tick (Linux replica)")
    ap.add_argument("--authority", default="", help="explicit authority URL (overrides env and file)")
    ap.add_argument("--dry-run", action="store_true", help="probe, step and print the plan; act on nothing, persist nothing")
    ap.add_argument("--home", default=str(Path.home()), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    home = Path(a.home)
    mem0 = home / ".mem0"
    here = Path(__file__).resolve().parent
    state_file = mem0 / "offline-mode.json"
    stamp = mem0 / "replica-restored"
    attempt = mem0 / "replica-refresh-attempt"
    log = mem0 / "offline-watcher.log"

    authority = resolve_authority(a.authority, os.environ.get("MEM0_URL"), mem0 / "authority-url")
    if not authority:
        print("offline-watcher: no remote authority (explicit/MEM0_URL/authority-url all local or missing); refusing to tick", file=sys.stderr)
        return 2
    reachable = probe(authority)
    prev = load_state(state_file)
    nxt = step_offline_state(prev, reachable)
    age = restore_age_hours(stamp)
    actions = plan_actions(nxt, reachable, age, attempt_age_hours(attempt))
    line = {"ts": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "authority": authority,
            "reachable": reachable, "mode": nxt["mode"], "transition": nxt["transition"],
            "restore_age_h": None if age is None else round(age, 2), "actions": actions}
    if a.dry_run:
        print(json.dumps(line))
        return 0

    results = {}
    for act in actions:
        if act == "start_services":
            results[act] = run(["systemctl", "--user", "start", "qdrant.service", "mem0.service"])
        elif act == "stop_services":
            results[act] = run(["systemctl", "--user", "stop", "mem0.service", "qdrant.service"])
        elif act == "drain_outbox":
            drainer = here / "replay-ops.py"
            if drainer.exists():
                env = dict(os.environ, MEM0_URL=authority)
                rc, out = run([sys.executable, str(drainer), "--authority", authority], env=env, timeout=1800)
                results[act] = (rc, out)
                try:
                    with (mem0 / "outbox-drain.log").open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"ts": line["ts"], "event": "outbox-drain", "rc": rc, "result": out[-2000:]}) + "\n")
                except OSError:
                    pass
            else:
                results[act] = (1, "replay-ops.py not deployed beside the watcher")
        elif act == "refresh_replica":
            restore = here / "restore-replica.sh"
            try:
                mem0.mkdir(parents=True, exist_ok=True)
                attempt.write_text(line["ts"] + "\n", encoding="utf-8")
            except OSError:
                pass
            results[act] = run(["bash", str(restore)], timeout=3600) if restore.exists() else (1, "restore-replica.sh not deployed beside the watcher")
    line["results"] = {k: {"rc": v[0], "out": v[1][-400:]} for k, v in results.items()}
    try:
        mem0.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({k: nxt[k] for k in ("mode", "consecutive_down", "consecutive_up", "transition")}), encoding="utf-8")
        if actions or nxt["transition"] != "none":
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
    except OSError as e:
        print(f"offline-watcher: could not persist state: {e}", file=sys.stderr)
        return 1
    if actions:
        print(json.dumps(line))
    return 0 if all(v[0] == 0 for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
