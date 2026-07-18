#!/usr/bin/env python3
"""v0.17 F.4.3: tier-ledger schema validator + monotonic timestamp checker + orphan detector.

Audits ~/.mem0/tier-ledger.jsonl:
1. Every line parses as valid JSON.
2. Every line has required base fields: ts (ISO 8601), event.
3. Timestamps are monotonically non-decreasing.
4. Event-type-specific required fields (per SCHEMA below).
5. memory_id references resolve to either:
   - A live Qdrant point (current)
   - A delete/decay-delete event in this same ledger (deleted; OK)
   - Orphan (neither — possible bug or pre-v0.17 historical entry)

Pre-v0.17 entries (no schema_version field) are flagged as "legacy_schema" in the
findings list but are NOT counted as schema_violations — they pre-date the schema
and won't recur post-F.4.4.

Outputs a summary to stdout + writes full findings to ~/.mem0/ledger-audit-report.jsonl.
Exit code: 0 if no errors (legacy entries are not errors); 1 if any hard errors.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

import httpx

LEDGER = Path.home() / ".mem0" / "tier-ledger.jsonl"
REPORT = Path.home() / ".mem0" / "ledger-audit-report.jsonl"
BASELINE = Path.home() / ".mem0" / "ledger-audit-baseline.json"

# MEM-16 (2026-07-03): the ledger is now the legacy tier-ledger.jsonl (frozen
# historical archive) PLUS monthly segments tier-ledger-YYYY-MM.jsonl written
# by app.py _append_ledger and the maintenance scripts (decay-scan /
# goals-stale-sweep / semantic-dedup — all moved in the same change). The
# strict YYYY-MM regex excludes tier-ledger-restore.jsonl (stack-restore.sh's
# inspection copy) and any other non-segment sibling.
_SEGMENT_RE = re.compile(r"^tier-ledger-\d{4}-\d{2}\.jsonl$")


def ledger_files() -> list[Path]:
    """Every ledger file to audit, in walk order: legacy archive first, then
    monthly segments sorted by name (== chronological), so the monotonic
    timestamp check spans the cutover naturally. Derives the segment dir from
    LEDGER so tests that repoint LEDGER at tmp_path get segments from there."""
    files: list[Path] = []
    if LEDGER.exists():
        files.append(LEDGER)
    if LEDGER.parent.exists():
        files.extend(sorted(
            p for p in LEDGER.parent.glob("tier-ledger-*.jsonl")
            if _SEGMENT_RE.match(p.name)
        ))
    return files

# v0.18 MED-15: counts captured by --baseline and subtracted by --accept-baseline.
# v0.19 M8: --baseline additionally records finding IDENTITIES (orphan_ids set +
# monotonic_keys (prev_ts, ts) pairs); --accept-baseline subtracts those two
# categories BY IDENTITY so new findings always surface even if old ones vanish
# (count subtraction is fungible: 5 resolved + 5 new = adjusted 0 = silent).
# parse_errors / schema_violations remain count-based (line-bound, low churn).
BASELINE_FIELDS = (
    "parse_errors",
    "monotonic_violations",
    "schema_violations",
    "orphan_count",
    "hard_findings",
)
QDRANT = "http://127.0.0.1:6333"
COLLECTION = "memories"

# Per-event-type required fields.
# Fields listed here are enforced for entries WITH schema_version (v17+).
# Pre-v0.17 entries (no schema_version) are exempt — legacy_schema annotation only.
SCHEMA: dict[str, dict] = {
    "add": {
        "required": ["ts", "event", "memory_id", "actor"],
        "optional": ["tier", "source", "reason", "schema_version"],
    },
    "tier-change": {
        "required": ["ts", "event", "memory_id", "tier", "actor"],
        "optional": ["reason", "transport", "schema_version"],
    },
    "metadata-merge": {
        "required": ["ts", "event", "memory_id", "merged_keys", "actor"],
        "optional": ["reason", "transport", "prior_tier", "schema_version"],
    },
    "delete": {
        "required": ["ts", "event", "memory_id", "actor"],
        "optional": ["reason", "prior_tier", "prior_source", "transport", "cascade", "schema_version"],
    },
    "decay-delete": {
        "required": ["ts", "event", "memory_id", "actor", "reason"],
        "optional": ["kept_id", "schema_version"],
    },
    "memory-update": {
        "required": ["ts", "event", "memory_id", "actor"],
        "optional": ["prior_tier", "reason", "transport", "schema_version"],
    },
    "goal-status-change": {
        "required": ["ts", "event", "goal_id", "new_status", "actor"],
        "optional": ["reason", "schema_version"],
    },
    "goal-abandoned": {
        "required": ["ts", "event", "goal_id", "actor", "reason"],
        "optional": ["schema_version"],
    },
    "goal-priority-change": {
        "required": ["ts", "event", "goal_id", "new_priority", "actor"],
        "optional": ["reason", "schema_version"],
    },
    "goal-merged": {
        "required": ["ts", "event", "source_goal_id", "target_goal_id", "actor", "reason"],
        "optional": ["relinked_episodes", "schema_version"],
    },
    "open-question-resolved": {
        "required": ["ts", "event", "open_question_id", "actor", "session_id"],
        "optional": ["resolution_preview", "schema_version"],
    },
    "open-question-status-change": {
        "required": ["ts", "event", "open_question_id", "new_status", "actor"],
        "optional": ["reason", "schema_version"],
    },
    # Scan events (no memory_id)
    "decay-scan-skip": {
        "required": ["ts", "event", "actor", "reason"],
        "optional": ["schema_version"],
    },
    "decay-scan-abort": {
        "required": ["ts", "event", "actor", "reason"],
        "optional": ["partial_deleted", "partial_flagged", "schema_version"],
    },
    "decay-scan-noop": {
        "required": ["ts", "event", "actor", "reason"],
        "optional": ["schema_version"],
    },
    "dedup-scan-skip": {
        "required": ["ts", "event", "actor", "reason"],
        "optional": ["schema_version"],
    },
    "dedup-scan-abort": {
        "required": ["ts", "event", "actor", "reason"],
        "optional": ["partial_deletions", "schema_version"],
    },
    "dedup-scan-noop": {
        "required": ["ts", "event", "actor", "reason"],
        "optional": ["schema_version"],
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description="tier-ledger schema/monotonic/orphan auditor")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--baseline", action="store_true",
                       help="record current finding counts AND identities (orphan_ids, monotonic_keys) "
                            "to ~/.mem0/ledger-audit-baseline.json and exit 0; refuses to ratchet "
                            "counts upward over an existing baseline unless --force is given")
    group.add_argument("--accept-baseline", action="store_true",
                       help="subtract the recorded baseline before the exit-code decision: by identity "
                            "for orphans/monotonic (v0.19 M8), by count (floor 0) for the rest; "
                            "report raw and adjusted numbers")
    parser.add_argument("--force", action="store_true",
                        help="with --baseline: allow re-recording even when a current count exceeds "
                             "the existing baseline (only after triage — see docs/systems/tier-policy.md)")
    args = parser.parse_args()

    files = ledger_files()
    if not files:
        print("ledger-audit: no ledger to audit (not found)", flush=True)
        return 0

    findings: list[dict] = []
    counts: Counter = Counter()
    last_ts = ""
    monotonic_violations = 0
    monotonic_pairs: list[list[str]] = []  # v0.19 M8: (prev_ts, ts) identity per violation
    parse_errors = 0
    schema_violations = 0
    legacy_entries = 0  # pre-v0.17 entries without schema_version
    deleted_memory_ids: set[str] = set()
    schema_versioned_entries = 0

    # ---- Walk ledger: legacy archive first, then monthly segments (MEM-16).
    # last_ts carries ACROSS files — the walk order is chronological by design,
    # so the monotonic check still covers the legacy->segment cutover boundary.
    # Findings gain a "file" key (line numbers are per-file after segmentation).
    total_lines = 0
    for lf in files:
        for line_no, line in enumerate(lf.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            total_lines += 1

            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                parse_errors += 1
                findings.append({"file": lf.name, "line": line_no, "type": "parse_error", "detail": str(e)})
                continue

            ts = entry.get("ts", "")
            event = entry.get("event")
            schema_ver = entry.get("schema_version")
            counts[event or "unknown"] += 1

            if schema_ver:
                schema_versioned_entries += 1
            else:
                legacy_entries += 1
                # Legacy entries: annotate but do NOT count as violations
                findings.append({
                    "file": lf.name,
                    "line": line_no,
                    "type": "legacy_schema",
                    "event": event,
                    "note": "pre-v0.17 entry; no schema_version field; won't recur post-F.4.4",
                })

            # 3. Monotonic timestamp check
            if ts and last_ts and ts < last_ts:
                monotonic_violations += 1
                monotonic_pairs.append([last_ts, ts])  # v0.19 M8: identity key
                findings.append({
                    "file": lf.name,
                    "line": line_no,
                    "type": "non_monotonic",
                    "ts": ts,
                    "prev_ts": last_ts,
                })
            if ts:
                last_ts = ts

            # 4. Schema validation (v17+ entries only)
            if schema_ver:
                spec = SCHEMA.get(event)
                if spec is None:
                    findings.append({
                        "file": lf.name,
                        "line": line_no,
                        "type": "unknown_event",
                        "event": event,
                        "note": "not in SCHEMA — may be a new event type; add to SCHEMA if recurring",
                    })
                    schema_violations += 1
                else:
                    for required in spec["required"]:
                        if required not in entry or entry[required] is None:
                            findings.append({
                                "file": lf.name,
                                "line": line_no,
                                "type": "missing_field",
                                "event": event,
                                "field": required,
                            })
                            schema_violations += 1

            # 5. Track deleted memory_ids
            if event in ("delete", "decay-delete"):
                mid = entry.get("memory_id")
                if mid:
                    deleted_memory_ids.add(str(mid))

    # ---- Optional: Qdrant orphan detection ----
    orphan_count = 0
    current_orphans: set[str] = set()  # v0.19 M8: identity set for baseline subtraction
    qdrant_reachable = False
    # v0.18 MED-2: only run orphan computation if the Qdrant scroll completed
    # naturally (offset exhausted). A mid-scroll error (non-200 page) leaves
    # live_memory_ids partial — diffing against a partial set would emit
    # false-positive orphans for every memory in the unscanned pages.
    scroll_complete = False
    try:
        r = httpx.get(f"{QDRANT}/collections/{COLLECTION}", timeout=5)
        if r.status_code == 200:
            qdrant_reachable = True
            live_memory_ids: set[str] = set()
            offset = None
            while True:
                body: dict = {"limit": 1000, "with_payload": False, "with_vector": False}
                if offset is not None:
                    body["offset"] = offset
                rr = httpx.post(
                    f"{QDRANT}/collections/{COLLECTION}/points/scroll",
                    json=body, timeout=10,
                )
                if rr.status_code != 200:
                    break
                res = rr.json().get("result", {})
                for pt in res.get("points", []):
                    live_memory_ids.add(str(pt.get("id")))
                offset = res.get("next_page_offset")
                if not offset:
                    scroll_complete = True  # natural end-of-scroll
                    break

            if not scroll_complete:
                findings.append({
                    "type": "scroll_incomplete",
                    "note": (
                        "scroll incomplete; orphan detection skipped. "
                        "Qdrant scroll terminated early (non-200 page) — live point set "
                        "is partial, so orphan diffing would produce false positives."
                    ),
                })
            else:
                # Collect all memory_ids referenced in the ledger
                # (MEM-16: across the legacy archive + every monthly segment)
                referenced: set[str] = set()
                for lf in files:
                    for line in lf.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                            mid = e.get("memory_id")
                            if mid:
                                referenced.add(str(mid))
                        except Exception:
                            continue

                # Orphan = referenced in ledger but neither in live Qdrant NOR in a delete event
                orphans = referenced - live_memory_ids - deleted_memory_ids
                current_orphans = orphans  # v0.19 M8
                orphan_count = len(orphans)
                if orphans:
                    findings.append({
                        "type": "orphan_memory_ids",
                        "count": orphan_count,
                        "sample": sorted(orphans)[:5],
                        "note": (
                            "memory_id referenced in ledger but not in Qdrant and not deleted. "
                            "Possible causes: manual Qdrant delete outside this stack, "
                            "collection wipe/restore with different IDs, or pre-migration records."
                        ),
                    })
    except Exception as exc:
        findings.append({
            "type": "qdrant_unreachable",
            "detail": str(exc)[:200],
            "note": "orphan detection skipped; run again when Qdrant is up",
        })

    # ---- Hard error counts (exclude legacy_schema from error total) ----
    hard_finding_count = sum(
        1 for f in findings
        if f.get("type") not in ("legacy_schema",)
    )

    current_counts = {
        "parse_errors": parse_errors,
        "monotonic_violations": monotonic_violations,
        "schema_violations": schema_violations,
        "orphan_count": orphan_count,
        "hard_findings": hard_finding_count,
    }

    # ---- v0.18 MED-15 / v0.19 M8: --baseline records counts + finding identities ----
    if args.baseline:
        # v0.19 M8 ratchet guard: refuse to normalize a regression. If a baseline
        # already exists and any current count exceeds the recorded one, that is a
        # NEW finding — triage it first; --force only after root-causing.
        if BASELINE.exists() and not args.force:
            try:
                prior = json.loads(BASELINE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                prior = {}
            grew = {
                k: (int(prior.get(k, 0)), current_counts[k])
                for k in BASELINE_FIELDS
                if current_counts[k] > int(prior.get(k, 0))
            }
            if grew:
                print(
                    "ledger-audit --baseline: REFUSED — current counts exceed the existing "
                    "baseline (re-baselining now would permanently normalize a regression): "
                    + ", ".join(f"{k} {old}->{new}" for k, (old, new) in grew.items())
                    + ". Triage the new findings first; re-run with --force only after "
                    "root-causing (docs/systems/tier-policy.md).",
                    flush=True,
                )
                return 1
            # v0.20 Phase E (L5): refuse to downgrade an identity-bearing baseline
            # when the orphan scan did not complete — with Qdrant down, v0.19
            # silently wrote orphan_ids=[] and exited 0, erasing the identities
            # that --accept-baseline subtracts BY identity. Reuses the v0.18 E.1
            # scroll_complete flag and the existing --force escape hatch.
            if not (qdrant_reachable and scroll_complete) and prior.get("orphan_ids"):
                print(
                    "ledger-audit --baseline: REFUSED — Qdrant orphan scan did not complete "
                    "(unreachable or scroll terminated early) and the existing baseline carries "
                    f"{len(prior['orphan_ids'])} orphan identities; writing now would replace them "
                    "with an empty set. Re-run with Qdrant up, or --force to override.",
                    flush=True,
                )
                return 1
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        baseline_data = {"ts": dt.datetime.now(dt.timezone.utc).isoformat()}
        baseline_data.update(current_counts)
        # v0.19 M8: identity fields — --accept-baseline subtracts these BY IDENTITY
        baseline_data["orphan_ids"] = sorted(current_orphans)
        baseline_data["monotonic_keys"] = monotonic_pairs
        BASELINE.write_text(json.dumps(baseline_data, indent=2) + "\n", encoding="utf-8")
        print(
            "ledger-audit --baseline: recorded "
            + ", ".join(f"{k}={current_counts[k]}" for k in BASELINE_FIELDS)
            + f", orphan_ids={len(baseline_data['orphan_ids'])} identities"
            + f", monotonic_keys={len(monotonic_pairs)} identities"
            + f" -> {BASELINE}",
            flush=True,
        )
        if not (qdrant_reachable and scroll_complete):
            print(
                "ledger-audit --baseline: WARN — Qdrant orphan scan did not complete; "
                "orphan_ids recorded as empty. Re-run --baseline with Qdrant up.",
                flush=True,
            )
        return 0

    # ---- v0.18 MED-15 / v0.19 M8: --accept-baseline subtracts the recorded baseline ----
    # Orphans + monotonic violations subtract BY IDENTITY when the baseline carries
    # identity fields (new findings always surface, even if old ones vanished);
    # legacy count-based baselines fall back to count subtraction (floor 0).
    adjusted = None
    identity_mode = False
    if args.accept_baseline:
        if not BASELINE.exists():
            print(
                f"ledger-audit --accept-baseline: no baseline at {BASELINE} "
                "(run --baseline first); using raw counts",
                flush=True,
            )
        else:
            try:
                base = json.loads(BASELINE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                print(f"ledger-audit --accept-baseline: baseline unreadable ({exc}); using raw counts", flush=True)
            else:
                adjusted = {
                    k: max(0, current_counts[k] - int(base.get(k, 0)))
                    for k in BASELINE_FIELDS
                }
                if "orphan_ids" in base and "monotonic_keys" in base:
                    identity_mode = True
                    base_orphans = set(base["orphan_ids"])
                    base_mono = {tuple(p) for p in base["monotonic_keys"]}
                    new_orphans = current_orphans - base_orphans
                    new_mono = [p for p in monotonic_pairs if tuple(p) not in base_mono]
                    adjusted["orphan_count"] = len(new_orphans)
                    adjusted["monotonic_violations"] = len(new_mono)
                    if new_orphans:
                        findings.append({
                            "type": "new_orphans_vs_baseline",
                            "count": len(new_orphans),
                            "sample": sorted(new_orphans)[:5],
                        })
                    if new_mono:
                        findings.append({
                            "type": "new_monotonic_vs_baseline",
                            "count": len(new_mono),
                            "sample": new_mono[:5],
                        })
                else:
                    print(
                        "ledger-audit --accept-baseline: legacy count-based baseline "
                        "(no identity fields) — falling back to count subtraction; "
                        "re-run --baseline to upgrade it",
                        flush=True,
                    )

    # ---- Write report ----
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    report_data = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ledger_path": str(LEDGER),
        # MEM-16: every file audited (legacy archive + monthly segments)
        "ledger_files": [str(p) for p in files],
        "total_lines": total_lines,
        "total_entries": sum(counts.values()),
        "event_counts": dict(counts.most_common()),
        "schema_versioned_entries": schema_versioned_entries,
        "legacy_entries": legacy_entries,
        "parse_errors": parse_errors,
        "monotonic_violations": monotonic_violations,
        "schema_violations": schema_violations,
        "orphan_count": orphan_count,
        "qdrant_reachable": qdrant_reachable,
        "scroll_complete": scroll_complete,
        "hard_findings": hard_finding_count,
        "total_findings": len(findings),
        "findings_sample": findings[:50],
    }
    if adjusted is not None:
        report_data["baseline_adjusted"] = adjusted
        report_data["baseline_path"] = str(BASELINE)
    REPORT.write_text(json.dumps(report_data, indent=2) + "\n", encoding="utf-8")

    # ---- Print summary ----
    print(
        f"ledger-audit: {sum(counts.values())} entries across {len(files)} file(s) "
        f"({schema_versioned_entries} v17+, {legacy_entries} legacy), "
        f"{parse_errors} parse errors, "
        f"{monotonic_violations} non-monotonic, "
        f"{schema_violations} schema violations, "
        f"{orphan_count} orphans, "
        f"hard_findings={hard_finding_count}",
        flush=True,
    )
    print("event counts:", flush=True)
    for evt, n in counts.most_common(20):
        print(f"  {n:6d}  {evt}", flush=True)
    if legacy_entries:
        print(
            f"\nNote: {legacy_entries} legacy (pre-v0.17) entries have no schema_version — "
            "these are historical and are NOT counted as violations.",
            flush=True,
        )
    print(f"\nfull report: {REPORT}", flush=True)

    # v0.18 MED-15: baseline-adjusted decision — report raw vs adjusted, decide on adjusted
    if adjusted is not None:
        mode_label = (
            "identity-based for orphans/monotonic, count-based for the rest"  # v0.19 M8
            if identity_mode else "count-based (legacy baseline)"
        )
        print(
            f"baseline-adjusted ({mode_label}): "
            + ", ".join(f"{k}={current_counts[k]}->{adjusted[k]}" for k in BASELINE_FIELDS),
            flush=True,
        )
        return 0 if (
            adjusted["parse_errors"] == 0
            and adjusted["monotonic_violations"] == 0
            and adjusted["schema_violations"] == 0
        ) else 1

    # Exit 0 only if no hard errors (parse errors, monotonic violations, schema violations)
    return 0 if (parse_errors == 0 and monotonic_violations == 0 and schema_violations == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
