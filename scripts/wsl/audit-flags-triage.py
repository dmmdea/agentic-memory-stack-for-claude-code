#!/usr/bin/env python3
"""Triage + resolve the L10 audit-flag backlog (~/.mem0/audit-flags.jsonl).

l10-audit.py only WRITES heuristic quality flags (missing-provenance, oversize,
possible-injection, possible-credential, canonical-without-actor) — watermarked via
l10-state.json["audited_keys"] so the same (memory_id:flag_type) is never re-flagged. The
SLOWDRIP alert counts flags whose dedup-key ("<memory_id>:<flag_type>") is NOT in
l10-state.json["reviewed_keys"]. There was no tool to MARK flags reviewed, so the backlog
could only grow. This is that missing mechanism (what the storage-cap hook calls
"/memory-prune"), and it uses the system's own designed review set.

Usage (run with the mem0-server venv python):
  audit-flags-triage.py --summary
      Breakdown by flag_type + age span; lists the SECURITY-critical flags
      (possible-credential / possible-injection / canonical-without-actor) in full so they
      can be eyeballed before resolving.
  audit-flags-triage.py --resolve --reason "..."
      Mark every current flag reviewed (add its dedup-key to l10-state.json["reviewed_keys"]),
      which clears SLOWDRIP while PRESERVING the full audit trail in audit-flags.jsonl. Use
      --keep-types a,b to leave some flag types still-open (unreviewed).

ZERO memory mutations: this only reads flags and updates the review-state file. Deleting or
fixing a flagged memory itself (e.g. a real leaked credential) is a separate, deliberate
operator action.
"""
from __future__ import annotations
import argparse
import collections
import datetime
import json
import sys
from pathlib import Path

MEM0 = Path.home() / ".mem0"
FLAGS_FILE = MEM0 / "audit-flags.jsonl"
STATE_FILE = MEM0 / "l10-state.json"
SECURITY_TYPES = ("possible-credential", "possible-injection", "canonical-without-actor")


def _load_flags() -> list[dict]:
    if not FLAGS_FILE.exists():
        return []
    out = []
    for line in FLAGS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def _iso(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).isoformat()
    except (TypeError, ValueError):
        return "?"


def _key(row: dict) -> str:
    return f"{row.get('memory_id')}:{row.get('flag_type')}"


def summary(rows: list[dict], state: dict) -> None:
    reviewed = set(state.get("reviewed_keys", []))
    unreviewed = [r for r in rows if _key(r) not in reviewed]
    by_type = collections.Counter(r.get("flag_type", "?") for r in rows)
    by_type_open = collections.Counter(r.get("flag_type", "?") for r in unreviewed)
    ats = [r["audited_at"] for r in rows if r.get("audited_at")]
    print(f"TOTAL flags: {len(rows)}  |  unreviewed (SLOWDRIP counts these): {len(unreviewed)}")
    print(f"by flag_type (all): {dict(by_type)}")
    print(f"by flag_type (unreviewed): {dict(by_type_open)}")
    if ats:
        print(f"audited span: {_iso(min(ats))} .. {_iso(max(ats))}")
    for ft in SECURITY_TYPES:
        sec = [r for r in unreviewed if r.get("flag_type") == ft]
        print(f"\n=== {ft}: {len(sec)} unreviewed (review each) ===")
        for r in sec:
            print(f"  {str(r.get('memory_id',''))[:8]}  src={r.get('source')!r} tier={r.get('tier')!r}")
            print(f"        {str(r.get('preview',''))[:160]}")


def resolve(rows: list[dict], state: dict, reason: str, keep_types: set[str]) -> None:
    if not rows:
        print("nothing to resolve (flags file empty/absent)")
        return
    reviewed = set(state.get("reviewed_keys", []))
    before = len(reviewed)
    marked = 0
    for r in rows:
        if r.get("flag_type") in keep_types:
            continue
        k = _key(r)
        if k not in reviewed:
            reviewed.add(k)
            marked += 1
    state["reviewed_keys"] = sorted(reviewed)
    state.setdefault("review_log", []).append({
        "resolved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reason": reason, "marked": marked, "kept_open_types": sorted(keep_types),
    })
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    still_open = sum(1 for r in rows if r.get("flag_type") in keep_types and _key(r) not in reviewed)
    print(f"marked {marked} flag(s) reviewed (reviewed_keys {before} -> {len(reviewed)})")
    print(f"still-open (kept types {sorted(keep_types)}): {still_open}")
    print("SLOWDRIP backlog now reflects only unreviewed flags; audit trail preserved in audit-flags.jsonl")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--resolve", action="store_true")
    ap.add_argument("--reason", default="operator triage")
    ap.add_argument("--keep-types", default="", help="comma-separated flag types to leave open")
    a = ap.parse_args()
    rows = _load_flags()
    state = _load_state()
    if a.summary or not (a.summary or a.resolve):
        summary(rows, state)
    if a.resolve:
        keep = {t.strip() for t in a.keep_types.split(",") if t.strip()}
        resolve(rows, state, a.reason, keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
