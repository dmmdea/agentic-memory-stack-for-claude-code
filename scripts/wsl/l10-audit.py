#!/usr/bin/env python3
"""L10 post-hoc memory audit - heuristic + incremental.

Runs on a systemd-user 6h timer. Scans every memory in Qdrant (paginated via the
scroll API so it is not subject to mem0's get_all top_k cap), and writes
idempotent heuristic flags to ~/.mem0/audit-flags.jsonl. Uses an ID-watermarked
state file to avoid re-flagging the same records on every run.

Auto-promotion to `canonical` is INTENTIONALLY DISABLED in this version (audit
finding 2026-06-08: time-on-the-shelf is not evidence of truth). Canonical
promotion now requires explicit user direction via `memory_promote` with
`actor=user-direct`. This script only flags durable-candidate memories for
visibility - it does not mutate any tier.

The "Bayesian trust score" surface from earlier versions has been removed; it
never crossed its own threshold and was security theater. This version is
explicitly heuristic-only.
"""
from __future__ import annotations
import json
import time
import sys
import datetime as dt
from pathlib import Path
from typing import Any

import httpx

MEM0_URL = "http://127.0.0.1:18791"
QDRANT_URL = "http://127.0.0.1:6333"
QDRANT_COLLECTION = "memories"
KEY_FILE = Path.home() / ".mem0" / "api-key"
STATE_FILE = Path.home() / ".mem0" / "l10-state.json"
FLAGS_FILE = Path.home() / ".mem0" / "audit-flags.jsonl"
PROMOTE_LEDGER = Path.home() / ".mem0" / "tier-ledger.jsonl"

DURABILITY_DAYS_REPORT = 30  # memories older than this with no flags are reported, NOT promoted
OVERSIZE_CHARS = 800
ONE_PAGE = 256

# v0.17 F.2.7: slow-drip detection thresholds
# Rationale: the original delta>20 check catches spikes but misses gradual accumulation
# (+1 new flag/day stays under delta forever). These three orthogonal thresholds close it:
#   CUMULATIVE: total unreviewed flags crossing 50 = "backlog large enough to be meaningful"
#   SLOPE: 3 new flags/day for 5 days = gradient that doubles in ~2 weeks without action
#   PERSISTENCE: any single flag going 7 days unreviewed = an item reviewers keep skipping
SLOWDRIP_CUMULATIVE_THRESHOLD = 50   # total unreviewed flags
SLOWDRIP_SLOPE_DAYS = 5              # rolling window for slope calculation
SLOWDRIP_SLOPE_PER_DAY = 3.0        # average new flags/day that triggers alert
SLOWDRIP_PERSISTENCE_DAYS = 7       # days a flag may remain unreviewed before alert


def load_key() -> str:
    if not KEY_FILE.exists():
        sys.exit("FAIL: no mem0 API key")
    return KEY_FILE.read_text(encoding="utf-8").strip()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {
        "last_audit_ts": 0,
        "audited_keys": [],  # ["{memory_id}:{flag_type}", ...]  - dedup across runs
    }


def save_state(state: dict) -> None:
    # Keep audited_keys bounded so the file does not grow forever
    if len(state.get("audited_keys", [])) > 5000:
        state["audited_keys"] = state["audited_keys"][-5000:]
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def scroll_all_qdrant_points(client: httpx.Client) -> list[dict]:
    """Page through every point in the memories collection via Qdrant scroll API.
    Yields dicts with id + payload (no vector). The mem0 server-side list endpoint
    cannot do this reliably; we go to Qdrant directly."""
    points: list[dict] = []
    next_page = None
    while True:
        body: dict[str, Any] = {"limit": ONE_PAGE, "with_payload": True, "with_vector": False}
        if next_page is not None:
            body["offset"] = next_page
        r = client.post(f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/scroll", json=body, timeout=10.0)
        r.raise_for_status()
        result = r.json().get("result", {})
        points.extend(result.get("points", []))
        next_page = result.get("next_page_offset")
        if not next_page:
            break
    return points


def heuristic_flags(payload: dict) -> list[str]:
    """Cheap deterministic signals. No LLM, no priors, no Bayesian theater."""
    flags = []
    text = payload.get("data", "") or payload.get("memory", "") or ""
    if not isinstance(text, str):
        text = str(text)
    tlow = text.lower()
    if len(text) > OVERSIZE_CHARS:
        flags.append("oversize")
    if "ignore previous" in tlow or "ignore all previous" in tlow or "ignore the above" in tlow:
        flags.append("possible-injection")
    if any(k in tlow for k in ("password:", "api_key:", "api-key:", "secret:", "bearer ", "private_key")):
        flags.append("possible-credential")
    if not payload.get("source"):
        flags.append("missing-provenance")
    tier = payload.get("tier")
    if tier == "canonical" and not payload.get("tier_actor"):
        flags.append("canonical-without-actor")
    return flags


def parse_ts(s: str | None) -> dt.datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def main():
    _ = load_key()  # validates API key file exists; not actually needed for Qdrant scroll
    state = load_state()
    audited_keys: set[str] = set(state.get("audited_keys", []))
    last_audit_ts = state.get("last_audit_ts", 0)
    last_audit_dt = dt.datetime.fromtimestamp(last_audit_ts, tz=dt.timezone.utc) if last_audit_ts else None
    now = int(time.time())
    now_dt = dt.datetime.now(dt.timezone.utc)

    # Read all points
    try:
        with httpx.Client() as client:
            points = scroll_all_qdrant_points(client)
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        print(f"L10 audit: Qdrant unreachable or scroll failed: {e}", file=sys.stderr)
        return 1

    new_flags = 0
    skipped_already_flagged = 0
    durable_candidates = []  # memories that would have been auto-promoted in the old design

    with FLAGS_FILE.open("a", encoding="utf-8") as f:
        for p in points:
            mid = p.get("id")
            payload = p.get("payload") or {}
            if not mid:
                continue

            # v0.13: skip records that were retired (retrievable=false) so they don't pollute the audit
            if payload.get("retrievable") is False:
                continue

            # Incremental: skip if created before last audit AND we already saw it
            # (records that have aged in place still get re-considered for new flag types)
            created_dt = parse_ts(payload.get("created_at"))

            # Heuristic flags
            flags = heuristic_flags(payload)
            for flag_type in flags:
                dedup_key = f"{mid}:{flag_type}"
                if dedup_key in audited_keys:
                    skipped_already_flagged += 1
                    continue
                audited_keys.add(dedup_key)
                rec = {
                    "audited_at": now,
                    "memory_id": str(mid),
                    "flag_type": flag_type,
                    "preview": (payload.get("data") or "")[:120],
                    "source": payload.get("source"),
                    "tier": payload.get("tier"),
                }
                f.write(json.dumps(rec) + "\n")
                new_flags += 1

            # Durable-candidate report (NOT auto-promoted)
            if (
                payload.get("tier") == "evidence"
                and payload.get("source") not in ("backfill-v012", None, "")
                and created_dt is not None
                and (now_dt - created_dt).days >= DURABILITY_DAYS_REPORT
                and not flags
            ):
                durable_candidates.append({
                    "id": str(mid),
                    "age_days": (now_dt - created_dt).days,
                    "source": payload.get("source"),
                    "preview": (payload.get("data") or "")[:80],
                })

    state["last_audit_ts"] = now
    state["audited_keys"] = list(audited_keys)
    state["last_durable_candidates"] = durable_candidates[:50]  # cap report size
    save_state(state)

    print(
        f"L10 audit: scanned {len(points)} memories, "
        f"new flags {new_flags}, "
        f"already-flagged skipped {skipped_already_flagged}, "
        f"durable candidates {len(durable_candidates)} (NOT auto-promoted - manual promote only)"
    )

    # v0.17 F.2.7: slow-drip detection — three orthogonal alert paths
    _slowdrip_check()

    return 0


def _slowdrip_check() -> None:
    """v0.17 F.2.7: detect gradual flag accumulation that the delta>20 spike check misses.

    Reads audit-flags.jsonl directly and checks three thresholds:
      1. Cumulative unreviewed flags > SLOWDRIP_CUMULATIVE_THRESHOLD (50)
      2. Average new flags/day > SLOWDRIP_SLOPE_PER_DAY (3.0) for last SLOWDRIP_SLOPE_DAYS (5) days
      3. Any flag persists > SLOWDRIP_PERSISTENCE_DAYS (7) days unreviewed

    "Unreviewed" = present in audit-flags.jsonl and NOT present in a reviewed_keys set stored
    in l10-state.json. Operators mark flags reviewed by adding their dedup-key
    ("<memory_id>:<flag_type>") to state["reviewed_keys"]. If that key is absent (normal for
    existing installs), ALL flags are considered unreviewed — conservative but safe.
    """
    if not FLAGS_FILE.exists():
        return

    state = load_state()
    reviewed: set[str] = set(state.get("reviewed_keys", []))

    now_dt = dt.datetime.now(dt.timezone.utc)
    cutoff_slope = now_dt - dt.timedelta(days=SLOWDRIP_SLOPE_DAYS)
    cutoff_persist = now_dt - dt.timedelta(days=SLOWDRIP_PERSISTENCE_DAYS)

    # Per-key tracking: first_seen datetime for persistence check
    first_seen: dict[str, dt.datetime] = {}
    # Daily bucket: date → count of new (not-yet-in-reviewed) flags that day
    daily_new: dict[str, int] = {}
    total_unreviewed = 0

    try:
        for line in FLAGS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            mid = rec.get("memory_id", "")
            flag_type = rec.get("flag_type", "")
            dedup_key = f"{mid}:{flag_type}"
            if dedup_key in reviewed:
                continue  # operator already reviewed this one

            # audited_at is stored as a Unix timestamp int in the existing schema
            audited_at_raw = rec.get("audited_at")
            try:
                audited_dt = dt.datetime.fromtimestamp(float(audited_at_raw), tz=dt.timezone.utc)
            except (TypeError, ValueError, OSError):
                continue

            total_unreviewed += 1

            # Track first_seen per key for persistence check
            if dedup_key not in first_seen or audited_dt < first_seen[dedup_key]:
                first_seen[dedup_key] = audited_dt

            # Daily bucket (only within slope window)
            if audited_dt >= cutoff_slope:
                day_str = audited_dt.strftime("%Y-%m-%d")
                daily_new[day_str] = daily_new.get(day_str, 0) + 1

    except OSError as e:
        print(f"L10 slowdrip: cannot read flags file: {e}", file=sys.stderr)
        return

    alerts = []

    # 1. Cumulative threshold
    if total_unreviewed > SLOWDRIP_CUMULATIVE_THRESHOLD:
        alerts.append(
            f"SLOWDRIP-CUMULATIVE: {total_unreviewed} unreviewed flags "
            f"(threshold {SLOWDRIP_CUMULATIVE_THRESHOLD}); review audit-flags.jsonl"
        )

    # 2. Slope threshold — average new flags/day over last SLOWDRIP_SLOPE_DAYS days
    if daily_new:
        avg_per_day = sum(daily_new.values()) / SLOWDRIP_SLOPE_DAYS
        if avg_per_day > SLOWDRIP_SLOPE_PER_DAY:
            alerts.append(
                f"SLOWDRIP-SLOPE: {avg_per_day:.1f} new flags/day over last {SLOWDRIP_SLOPE_DAYS}d "
                f"(threshold {SLOWDRIP_SLOPE_PER_DAY}/day); daily={dict(sorted(daily_new.items()))}"
            )

    # 3. Persistence threshold — any flag older than SLOWDRIP_PERSISTENCE_DAYS
    stale_keys = [k for k, fdt in first_seen.items() if fdt < cutoff_persist]
    if stale_keys:
        alerts.append(
            f"SLOWDRIP-PERSIST: {len(stale_keys)} flag(s) unreviewed for >{SLOWDRIP_PERSISTENCE_DAYS}d "
            f"(examples: {stale_keys[:3]})"
        )

    for alert in alerts:
        print(f"L10 audit WARNING: {alert}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
