#!/usr/bin/env python3
"""codex-usage-report.py — what the Codex judges actually cost, per job (port of the PS report).

    codex-usage-report.py                 # last 7 days, the table
    codex-usage-report.py --days 1        # yesterday's shape
    codex-usage-report.py --json          # machine-readable, for the health row
    codex-usage-report.py --probe         # read the plan window first (appends a codex-window row)
    codex-usage-report.py --gate          # print `allow|deny <reason>`; exit 0 / 3 (the 25% reserve)

Read-only apart from the probe's ledger row. Never dies on a missing ledger, an unreadable
token or an unreachable endpoint — each degrades to a stated unknown.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ams_env  # noqa: E402
import codex_usage as cu  # noqa: E402

_ROW = "{:<20} {:>6} {:>8} {:>10} {:>8} {:>8} {:>6} {:>5} {:>8}  {}"
_RULE = "-" * 118


def _print_table(rep: dict) -> None:
    days = rep["days"]
    print("")
    print(f"Codex judge usage - last {days} day(s)")
    print(_ROW.format("job", "calls", "/day", "tokens", "p50 ms", "max ms", "fail", "drift", "unparsed", "model"))
    print(_RULE)
    for r in rep["jobs"]:
        print(_ROW.format(r["job"], r["calls"], r["per_day"], r["tokens"], r["p50_ms"], r["max_ms"],
                          r["failed"], r["drift"], r["unparsed"], r["model"]))
    print(_RULE)
    total_calls, total_tokens = rep["total_calls"], rep["total_tokens"]
    print("{:<20} {:>6} {:>8} {:>10}".format("TOTAL", total_calls, round(total_calls / max(1, days), 1), total_tokens))
    # Never let a dropped sample pass as a complete one.
    bad_total = sum(j["bad_duration"] for j in rep["jobs"])
    if bad_total > 0:
        print(f"NOTE: {bad_total} row(s) carry a duration_ms that is present but not a number. They are excluded "
              "from p50/max, so those latencies describe fewer calls than the calls column.")
        print("")
    w = rep["window"] or {}
    if w.get("used_percent") is not None:
        print(f"7-day plan window: {w['used_percent']}% used, resets in {w.get('resets_in_days')} day(s)")
    else:
        print(f"7-day plan window: UNKNOWN - {w.get('note', 'window not read')}")
    # The decision this report exists to inform: stated as a comparison, not a verdict.
    l1a = [j for j in rep["jobs"] if j["job"] == "l1a"]
    if l1a and l1a[0]["calls"] > 0:
        print("")
        print("NLI write-gate sizing (the gate is currently OFF):")
        print(f"  the extractor runs {l1a[0]['per_day']} call(s)/day at {l1a[0]['tokens']} tokens total over {days} day(s).")
        print("  the gate would fire on EVERY mem0 write, uncached - compare its projected write")
        print("  rate against the row above before enabling it, and re-run this after a week.")
    print("")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Codex judge usage per job, plan window, 25% reserve gate.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--probe", action="store_true", help="read the plan window now (appends a codex-window row)")
    ap.add_argument("--gate", action="store_true", help="print allow|deny <reason>; exit 0 allow / 3 deny")
    args = ap.parse_args(argv)

    window = None
    if args.probe:
        window = cu.probe_window(ams_env.codex_home())
        if not (args.gate or args.json):
            # a bare --probe (the dream step's ExecStartPre) prints one line, not the whole table
            print(f"codex-window: used_percent={window.get('used_percent')} resets_in_days={window.get('resets_in_days')} {window.get('note') or ''}".rstrip(), flush=True)
            return 0

    if args.gate:
        w = window if window is not None else cu.last_window()
        if w is None:
            w = cu.probe_window(ams_env.codex_home())
        gate = cu.quota_gate(w)
        print(("allow " if gate["allow"] else "deny ") + gate["reason"], flush=True)
        return 0 if gate["allow"] else 3

    rep = cu.report(days=args.days, window=window)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        _print_table(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
