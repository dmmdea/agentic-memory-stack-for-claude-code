#!/usr/bin/env python3
"""memory-index-refresh.py — the decoupled MEMORY.md index refresh (port of memory-index-refresh.ps1).

The dream rebuilds the index as its phase 4; if the dream skips a night (throttle, quota, dedup
mutex, codex down) the index would freeze with it. This step runs the SAME builder on its own cheap
throttle — zero Codex, local Qdrant only. A successful dream stamps this throttle too, so on a
full-dream night this step is a receipted no-op.

    memory-index-refresh.py [--min-interval-s 21600] [--force]

Exit 0 on a throttle/lock skip; the builder's exit code when the build fails (the chain receipt
must show a failed build as ok:false). The throttle is marked only on success (the 2026-06-08
"don't burn the window on failure" finding). mkdir is the mutex (v1.12 F6: two session starts
raced through the throttle gap and both built); a lock older than 30 min is reclaimed.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # deployed flat: ~/apps/mem0-scripts
import ams_env  # noqa: E402

COMPONENT = "index-refresh"
DEFAULT_INTERVAL_S = 21600
LOCK_STALE_S = 30 * 60


def _run_deployed(script: str) -> tuple[int, str]:
    deployed = Path.home() / "apps" / "mem0-scripts" / script
    target = deployed if deployed.exists() else Path(__file__).resolve().parent / script
    try:
        cp = subprocess.run([sys.executable, str(target)], capture_output=True, text=True, timeout=1800, env=dict(os.environ))
        return cp.returncode, (cp.stdout + cp.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _acquire_lock(lock: Path) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
        return True
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime < LOCK_STALE_S:
                return False
            lock.rmdir()
            lock.mkdir()
            return True
        except OSError:
            return False


def main(argv=None, runner=_run_deployed) -> int:
    p = argparse.ArgumentParser(description="decoupled MEMORY.md index refresh")
    p.add_argument("--min-interval-s", type=int, default=DEFAULT_INTERVAL_S)
    p.add_argument("--force", action="store_true", help="ignore the throttle (the lock still applies)")
    a = p.parse_args(argv)
    if not a.force and not ams_env.throttle_ok(COMPONENT, a.min_interval_s):
        ams_env.log(COMPONENT, "skip: throttle (index built within the interval)")
        return 0
    lock = ams_env.state_dir() / "locks" / "index-refresh.lock"
    if not _acquire_lock(lock):
        ams_env.log(COMPONENT, "skip: another refresh holds the lock")
        return 0
    try:
        rc, out = runner("memory-index-build.py")
        if out:
            ams_env.log(COMPONENT, f"  {out}")
        if rc != 0:
            ams_env.log(COMPONENT, f"index build failed (exit={rc}); throttle NOT marked")
            return rc
        ams_env.mark_throttle(COMPONENT)
        ams_env.log(COMPONENT, "MEMORY.md index refreshed (decoupled from dream)")
        return 0
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
