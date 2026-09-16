#!/usr/bin/env python3
"""Register the ams-store hooks in a Claude Code settings.json, idempotently.

The Windows installer does this in PowerShell (2-windows-config.ps1 section 2). A native Linux
client has no PowerShell, so this is the same merge in Python, with the same three rules the
2026-06-08 audit wrote down after an installer silently deleted unrelated user hooks:

  1. Identify OUR entries by a command SUBSTRING marker, never by position.
  2. Remove only those, then append fresh ones. Everything else is preserved.
  3. Write the file only when the merged result differs, so a re-run is a no-op and the
     idempotence readback compares equal byte for byte.

Key ordering is fixed (not dict-insertion-by-accident) for the same reason the PowerShell side
uses [ordered]: two identical runs must produce identical bytes.

Exit: 0 written or already current, 2 bad invocation, 1 unreadable/unwritable settings file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

# One row per event: the markers that identify a previously registered version of this entry,
# the command to run, and the optional fields. Mirrors $hookEntries in 2-windows-config.ps1.
GATE_MARKERS = ("memory-index-write-lint.sh", "memory-index-write-gate.ps1", "ams-store")
SYNC_MARKERS = ("ams-store",)


def entries(binary: str, hub_host: str):
    """The three hook entries a Linux client registers, in the Windows installer's shape."""
    gate = f"{binary} gate"
    sync = f"{binary} sync --once --hub-host {hub_host}"
    return {
        # The write gate refuses an index edit that would break the store's invariants.
        "PostToolUse": [
            {"markers": GATE_MARKERS, "command": gate, "matcher": "Write|Edit", "timeout": 10}
        ],
        # Async at session start: a sync must never hold the session open.
        "SessionStart": [
            {"markers": SYNC_MARKERS, "command": sync, "async": True, "timeout": 90}
        ],
        # Synchronous at session end, so a closing session's local commits reach the hub.
        "SessionEnd": [{"markers": SYNC_MARKERS, "command": sync, "timeout": 60}],
    }


def block(entry: dict) -> dict:
    """One settings.json hook block. Field order is fixed on purpose."""
    cmd = {"command": entry["command"], "type": "command"}
    if entry.get("timeout"):
        cmd["timeout"] = entry["timeout"]
    if entry.get("async"):
        cmd["async"] = True
    out = {"hooks": [cmd]}
    if entry.get("matcher"):
        out["matcher"] = entry["matcher"]
    return out


def is_ours(existing_block: dict, markers) -> bool:
    for hook in existing_block.get("hooks") or []:
        command = hook.get("command") or ""
        if any(marker in command for marker in markers):
            return True
    return False


def merge(settings: dict, binary: str, hub_host: str):
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit("FAIL: settings.json 'hooks' is not an object; refusing to rewrite it")
    report = []
    for event, rows in entries(binary, hub_host).items():
        markers = tuple(m for row in rows for m in row["markers"])
        fresh = [block(row) for row in rows]
        current = hooks.get(event) or []
        if not isinstance(current, list):
            raise SystemExit(f"FAIL: settings.json hooks.{event} is not an array")
        preserved = [b for b in current if not is_ours(b, markers)]
        hooks[event] = preserved + fresh
        report.append((event, len(preserved)))
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--settings", required=True, help="path to settings.json")
    ap.add_argument("--binary", required=True, help="absolute path to the ams-store binary")
    ap.add_argument("--hub-host", required=True, help="single-label MagicDNS host of the hub")
    args = ap.parse_args(argv)

    if "/" not in args.binary:
        print("FAIL: --binary must be an absolute path", file=sys.stderr)
        return 2
    if "." in args.hub_host or "/" in args.hub_host:
        # Same rule the store itself enforces: reach is the tailnet MagicDNS name only.
        print(f"FAIL: --hub-host '{args.hub_host}' must be a single-label host", file=sys.stderr)
        return 2

    path = args.settings
    before = ""
    settings = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                before = fh.read()
            settings = json.loads(before) if before.strip() else {}
        except (OSError, ValueError) as exc:
            print(f"FAIL: cannot read {path}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(settings, dict):
            print(f"FAIL: {path} is not a JSON object", file=sys.stderr)
            return 1

    report = merge(settings, args.binary, args.hub_host)
    after = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"

    if before == after:
        print("    hooks already current (no write)")
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if before:
        with open(path + ".bak-ams-hooks", "w", encoding="utf-8") as fh:
            fh.write(before)
    # Write through a temp file in the same directory: a half-written settings.json disables
    # every hook on the box, including the ones this installer did not touch.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".ams-hooks-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(after)
        os.replace(tmp, path)
    except OSError as exc:
        os.unlink(tmp)
        print(f"FAIL: cannot write {path}: {exc}", file=sys.stderr)
        return 1
    for event, preserved in report:
        suffix = f"  (merged with {preserved} preserved entry/entries)" if preserved else ""
        print(f"    hook: {event}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
