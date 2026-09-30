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
import math
import os
import re
import time
import sys
import datetime as dt
from pathlib import Path
from typing import Any

import httpx
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # deployed flat: ~/apps/mem0-scripts
import ams_env  # noqa: E402  (spec §4: URL from authority-url, key from the systemd credential)

# S12: the possible-credential flag runs the server's redaction rules. redact.py lives in
# mem0-server/ (repo layout: scripts/wsl/ -> <repo>/mem0-server; deployed: ~/apps/mem0-scripts/ ->
# ~/apps/mem0-server). A missing module fails LOUD at import: silently falling back to the old
# keyword tripwire would re-hide exactly the credentials this audit exists to surface.
for _cand in (Path(__file__).resolve().parents[2] / "mem0-server", Path.home() / "apps" / "mem0-server"):
    if (_cand / "redact.py").is_file():
        sys.path.append(str(_cand))
        break
import redact  # noqa: E402

MEM0_URL = ams_env.mem0_url()
QDRANT_URL = "http://127.0.0.1:6333"
QDRANT_COLLECTION = os.environ.get("MEM0_QDRANT_COLLECTION", "mem0_egemma_768")  # env-overridable; default is the live collection (was the dead pre-egemma 'memories' -> 404)
STATE_FILE = Path.home() / ".mem0" / "l10-state.json"
FLAGS_FILE = Path.home() / ".mem0" / "audit-flags.jsonl"
PROMOTE_LEDGER = Path.home() / ".mem0" / "tier-ledger.jsonl"

DURABILITY_DAYS_REPORT = 30  # memories older than this with no flags are reported, NOT promoted
# MEM-10 (2026-07-03): raised 800 -> 1200. 800 flagged what the server ACCEPTS
# (app.py MAX_MEMORY_CHARS=4000 since v0.22) — every rich-but-legitimate fact
# became audit noise, drowning the real multi-topic dumps. Enforcement moved to
# WRITE time (l1a-extract.ps1: atomic <=60-word prompt rule + Split-OversizeFact
# ~700-char sentence-split guard), so anything landing >1200 now is a genuine
# dump from a path that bypassed the extractor — worth a flag. Still well under
# the 4000 server cap.
OVERSIZE_CHARS = 1200
# S12: judge-migrated facts (source "automemory:*") are stored VERBATIM up to the migration cap
# (ams-store internal/store/constants.go Mem0MaxChars = mem0-server MAX_MEMORY_CHARS), so the
# 1200 line flagged them permanently (326 of 1264 oversize flags at audit time) and fed the
# slow-drip thresholds. The cap is pinned to the Go constant by test_l10_audit.py.
AUTOMEMORY_OVERSIZE_CHARS = 4000
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
    key = ams_env.api_key()
    if not key:
        sys.exit("FAIL: no mem0 API key (MEM0_API_KEY_FILE / ~/.mem0/api-key)")
    return key


def load_state() -> dict:
    # 2026-08-24 review round 2: the corrupt-state gate must be PERSISTENT. A
    # one-shot quarantine moved the file aside and the NEXT unattended run then
    # defaulted clean and save_state durably wrote a review state with no
    # reviewed_keys - the erase merely moved from run N to run N+1. An unresolved
    # quarantine file therefore blocks every run until the operator restores.
    # Runs FIRST (review R3): an unresolved quarantine must block before the
    # exists/parse block, else a valid state file beside it silently un-gates.
    stray = sorted(STATE_FILE.parent.glob(STATE_FILE.name + ".corrupt-*"))
    if stray:
        sys.exit(f"FAIL: unresolved l10-state quarantine present ({stray[-1].name}); "
                 "restore reviewed_keys from the newest backups/l10-state-*.json, then "
                 "remove the quarantine file to resume.")
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            # 2026-08-24 review: silently defaulting here + the now-atomic
            # save_state would DURABLY ERASE the operator's reviewed_keys on the
            # very next run — a corrupt state must fail loud and preserve the
            # evidence, never masquerade as a fresh install. Restore path:
            # the daily backup's l10-state-<TS>.json (stack-restore.sh step 5c).
            quarantine = STATE_FILE.with_suffix(
                ".json.corrupt-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S"))
            try:
                os.replace(STATE_FILE, quarantine)
            except OSError:
                quarantine = "(quarantine move failed)"
            sys.exit(f"FAIL: l10-state.json is corrupt ({e}); preserved at {quarantine}. "
                     "Restore reviewed_keys from the newest backups/l10-state-*.json, "
                     "then re-run.")
    return {
        "last_audit_ts": 0,
        "audited_keys": [],  # ["{memory_id}:{flag_type}", ...]  - dedup across runs
    }


def save_state(state: dict) -> None:
    # Keep audited_keys bounded so the file does not grow forever.
    # AMS-41 (2026-08-08): the list must arrive in INSERTION order — it used
    # to be built as list(set), i.e. hash order, so this [-5000:] truncation
    # dropped an arbitrary 'random' subset of dedup keys and their memories
    # got re-flagged on the next run (a re-flag storm after any bulk ingest).
    # With insertion order the truncation deterministically drops the OLDEST
    # keys, which is the intended retention.
    if len(state.get("audited_keys", [])) > 5000:
        state["audited_keys"] = state["audited_keys"][-5000:]
    # 2026-08-24: atomic replace, not truncate-then-write. This file holds the
    # operator's reviewed_keys; the timer floats (OnBootSec+6h) and can coincide
    # with the 03:30 backup, whose raw cp of a half-written file would restore as
    # an empty review state and resurrect every reviewed flag.
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


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


# --- possible-credential detector (S12) -------------------------------------------------------
# Three independent signals, any one flags: (1) the shared redaction rule set (redact.py), minus
# benign matches; (2) a provider-prefix tripwire looser than the rules' length quantifiers;
# (3) a high-entropy token check. It replaced a six-keyword substring tripwire that flagged
# env-var names and missed 10 of 11 real credential-bearing points.
_PROVIDER_PREFIX_RE = re.compile(
    r"(?-i:(?<![A-Za-z0-9])(?:sk-|sk_live_|sk_test_|pk_live_|rk_live_|whsec_|gh[pousr]_|github_pat_|"
    r"glpat-|xox[baprs]-|nvapi-|hf_|npm_|vcp_|sbp_|cfut_|tskey-|AIza)[A-Za-z0-9_-]{12,})")
_RANDOM_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9/.\\])[A-Za-z0-9]{32,}(?![A-Za-z0-9])")
# Entropy alone cannot separate secrets from identifiers at the 32-char floor: a random 32-char
# base62 token measures only ~4.54 bits at the median (p10 4.37, capped at log2(32) = 5.0), while
# English CamelCase ids reach 4.4-4.5 at 40-46 chars. A fixed 4.6 floor therefore caught ~39% of
# exactly-32-char secrets (~90% at 40, ~99% at 48). So the entropy floor is low (4.2) and a second
# statistic carries the separation: the class-transition ratio (share of adjacent character pairs
# that change class among digit / UPPER / lower). Random base62 is ~0.62 (p10 ~0.5 at 32 chars);
# CamelCase ids are ~0.27-0.46, because their words are lowercase runs. Measured (seeded, upper+
# lower+digit tokens): flagged 94-98% at 32-64 chars; benign ids stay unflagged.
ENTROPY_MIN_BITS = 4.2
TRANSITION_MIN_RATIO = 0.48
_BENIGN_VALUE_START = ("$", "<", "{", "%", "/", "~", "./", "../", "[REDACTED")
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


def _entropy_bits(s: str) -> float:
    n = len(s)
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _class_transition_ratio(s: str) -> float:
    def cls(ch: str) -> int:
        return 0 if ch.isdigit() else (1 if ch.isupper() else 2)
    if len(s) < 2:
        return 0.0
    return sum(cls(a) != cls(b) for a, b in zip(s, s[1:])) / (len(s) - 1)


def _benign_generic_value(value: str) -> bool:
    """A generic (`<label>: <value>`) rule match that is not a literal credential: an env-var or
    shell reference (`$VT`), a path, a placeholder, an already-redacted marker, or an UPPER_SNAKE
    env-var name. Family rules (sk-, ghp_, AKIA...) are never vetoed - their shapes are
    unambiguous."""
    v = value.strip().strip("\"'`")
    return v.startswith(_BENIGN_VALUE_START) or bool(_ENV_NAME_RE.fullmatch(v))


def has_credential(text: str) -> bool:
    """True when `text` carries a literal credential shape. Advisory heuristic feeding a review
    flag, so it favours the operator's time: benign env-var/path/nonce shapes do not flag."""
    for key, value in redact.find_credentials(text):
        if not key.startswith("pattern_") or not _benign_generic_value(value):
            return True
    if _PROVIDER_PREFIX_RE.search(text):
        return True
    for m in _RANDOM_TOKEN_RE.finditer(text):
        tok = m.group(0)
        if (any(c.isdigit() for c in tok) and any(c.isupper() for c in tok)
                and any(c.islower() for c in tok) and _entropy_bits(tok) >= ENTROPY_MIN_BITS
                and _class_transition_ratio(tok) >= TRANSITION_MIN_RATIO):
            return True
    return False


def flag_preview(payload: dict, limit: int = 120) -> str:
    """The audit-flags.jsonl preview: REDACTED before truncated, so neither a credential nor the
    head of one cut by the window is written into the flags file."""
    return (redact.redact_secrets(payload.get("data") or "") or "")[:limit]


def oversize_limit(payload: dict) -> int:
    source = payload.get("source")
    if isinstance(source, str) and source.startswith("automemory:"):
        return AUTOMEMORY_OVERSIZE_CHARS
    return OVERSIZE_CHARS


def heuristic_flags(payload: dict) -> list[str]:
    """Cheap deterministic signals. No LLM, no priors, no Bayesian theater."""
    flags = []
    text = payload.get("data", "") or payload.get("memory", "") or ""
    if not isinstance(text, str):
        text = str(text)
    if payload.get("retrievable") is False:
        # v0.13 skips retired points to keep noise down; S12 scans them for the credential flag
        # ONLY, because a retired point is still a readable row in every store and backup.
        return ["possible-credential"] if has_credential(text) else []
    tlow = text.lower()
    if len(text) > oversize_limit(payload):
        flags.append("oversize")
    if "ignore previous" in tlow or "ignore all previous" in tlow or "ignore the above" in tlow:
        flags.append("possible-injection")
    if has_credential(text):
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
    # AMS-41: an ORDERED set — dict preserves insertion order, so the
    # bounded-retention truncation in save_state drops the oldest keys
    # instead of a hash-order-random subset.
    audited_keys: dict[str, None] = dict.fromkeys(state.get("audited_keys", []))
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

            # v0.13: retired records (retrievable=false) don't pollute the audit - heuristic_flags
            # returns ONLY the credential flag for them (S12), and they are never durable candidates.
            retired = payload.get("retrievable") is False

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
                audited_keys[dedup_key] = None
                rec = {
                    "audited_at": now,
                    "memory_id": str(mid),
                    "flag_type": flag_type,
                    "preview": flag_preview(payload),
                    "source": payload.get("source"),
                    "tier": payload.get("tier"),
                }
                f.write(json.dumps(rec) + "\n")
                new_flags += 1

            # Durable-candidate report (NOT auto-promoted)
            if (
                not retired
                and payload.get("tier") == "evidence"
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
