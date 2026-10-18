#!/usr/bin/env python3
"""Weekly decay scanner.
1. Find tier=temporal records whose metadata.expires_at < now -> soft-delete (DELETE).
2. Find tier=evidence records older than 90 days with no incoming `replaces` references
   and that have NEVER been touched by C1/dream -- flag as 'decay-candidate' (manual review).
3. 1.37.0 (audit NC-09/SQ-07, operator decision 2026-10-18): EXPIRY WRITES. A flagged candidate older
   than RETIRE_AGE_DAYS that is long (> RETIRE_LONG_CHARS) or was never retrieved in the retrieval log
   (counted only when the log covers >= RETRIEVAL_LOG_MIN_DAYS) is soft-retired: retrievable=false
   through PATCH /metadata (actor decay-scan, service key), reversible, ledgered, at most
   RETIRE_CAP a run. Never touched: user-direct, auto-memory mirrors, learn-rules corrections and any
   record that opens as a standing directive (RULE, DECISION, LESSON, "Open before" ...).
   Mode from AMS_DECAY_RETIRE (environment, else ~/.mem0/stack.env): off | dry-run (default) | live.
4. --retire-ids FILE --reason TEXT [--live]: the same soft-retire for an operator-reviewed id list
   (one id per line or a JSON list), run by hand through ams-service-run.sh.
5. Print summary; no other side effects on tier=stable/canonical/insight.

EVERY destructive op appends to the central tier-ledger (lens S2).

v0.13.1: preflight probe of Qdrant + mem0 health. Emits decay-scan-skip ledger event
and exits 0 cleanly if either backend is unreachable. Mid-run httpx failures emit
decay-scan-abort with partial counts."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import re
import os
import sys
from pathlib import Path
import httpx
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # deployed flat: ~/apps/mem0-scripts
import ams_env  # noqa: E402  (spec §4: URL from authority-url, key from the systemd credential)

# The server modules live in mem0-server/: a sibling of scripts/ in the repo layout, and
# ~/apps/mem0-server when the scripts are deployed flat into ~/apps/mem0-scripts.
_SERVER_DIRS = [Path(__file__).resolve().parents[2] / "mem0-server",
                Path.home() / "apps" / "mem0-server"]
for _d in _SERVER_DIRS:
    if _d.is_dir():
        sys.path.insert(0, str(_d))
        break
import embedder_profile as ep  # noqa: E402  (collection names of the active embedding space)

QDRANT = "http://127.0.0.1:6333"
COLLECTION = ep.collection("memories")  # env-overridable (MEM0_QDRANT_COLLECTION / MEM0_COLLECTION); default is the active space's collection
MEM0 = ams_env.mem0_url()
KEY = ams_env.api_key()
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
EVIDENCE_AGE_DAYS = 90
RETIRE_AGE_DAYS = 120
RETIRE_LONG_CHARS = 600
RETIRE_CAP = 300
RETRIEVAL_LOG_MIN_DAYS = 28
RETRIEVAL_LOG = Path.home() / ".mem0" / "retrieval-log.jsonl"
DIRECTIVE_LEAD = re.compile(r"^\W*(OPERATOR DECISION|OPERATOR RULE|DECISION|STANDING RULE|STANDING ORDER|HARD RULE|RULE|"
                            r"LESSON|FRAMING PRINCIPLE|PRINCIPLE|POLICY|DIRECTIVE|Open before|NEVER|ALWAYS|Daniel)", re.I)
REPORT = Path.home() / ".mem0" / "decay-report.jsonl"
LEDGER_DIR = Path.home() / ".mem0"

def _ledger_path() -> Path:
    # MEM-16 (2026-07-03): append to the CURRENT-MONTH segment
    # (tier-ledger-YYYY-MM.jsonl), same naming as app.py _append_ledger — the
    # legacy tier-ledger.jsonl is a frozen historical archive. ALL writers moved
    # in the same change so ledger-audit.py's chronological walk (legacy first,
    # then segments) stays monotonic across the cutover.
    return LEDGER_DIR / f"tier-ledger-{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m')}.jsonl"

def _ledger(rec):
    rec.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
    rec.setdefault("schema_version", "v17")  # v0.17 F.4.4: every entry stamps schema version
    ledger = _ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

def scroll_all():
    points, off = [], None
    while True:
        body = {"limit": 256, "with_payload": True, "with_vector": False}
        if off is not None: body["offset"] = off
        r = httpx.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll", json=body, timeout=15.0)
        r.raise_for_status()
        res = r.json()["result"]
        points.extend(res.get("points", []))
        off = res.get("next_page_offset")
        if not off: break
    return points

def retire_mode() -> str:
    m = (os.environ.get("AMS_DECAY_RETIRE") or ams_env.stack_env().get("AMS_DECAY_RETIRE") or "dry-run").strip().lower()
    return m if m in ("off", "dry-run", "live") else "dry-run"


def protected(payload: dict) -> str | None:
    """Why a record must never be expired by this job, or None."""
    src = str(payload.get("source") or "")
    if src == "user-direct":
        return "user-direct"
    if src.startswith("automemory:"):
        return "automemory"
    if src == "learn-rules":
        return "learn-rules"
    if DIRECTIVE_LEAD.search(str(payload.get("data") or "")):
        return "directive-lead"
    return None


def retrieved_ids() -> tuple[set, float]:
    """Ids the retrieval log ever returned, and how many days the log covers."""
    ids: set = set()
    first = None
    try:
        with RETRIEVAL_LOG.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if first is None and row.get("ts"):
                    first = row["ts"]
                ids.update(str(i) for i in (row.get("returned_top_ids") or []))
    except OSError:
        return set(), 0.0
    try:
        start = dt.datetime.fromisoformat(str(first).replace("Z", "+00:00")) if first else None
    except ValueError:
        start = None
    days = (dt.datetime.now(dt.timezone.utc) - start).days if start else 0
    return ids, float(days)


def soft_retire(client: httpx.Client, mid: str, reason: str) -> bool:
    """retrievable=false through the server (tier gate + ledger there), then our own ledger line."""
    r = client.patch(f"{MEM0}/v1/memories/{mid}/metadata",
                     json={"metadata": {"retrievable": False}, "actor": "decay-scan", "reason": reason},
                     headers=ams_env.mem0_headers(KEY))
    if r.status_code != 200:
        print(f"decay-scan: retire {mid[:8]} refused: {r.status_code} {r.text[:160]}", flush=True)
        return False
    _ledger({"event": "decay-retire", "memory_id": mid, "reason": reason, "actor": "decay-scan"})
    return True


def retire_ids(path: str, reason: str, live: bool) -> int:
    raw = Path(path).read_text(encoding="utf-8").strip()
    items = json.loads(raw) if raw.startswith("[") else [ln.strip() for ln in raw.splitlines() if ln.strip()]
    ids = [str(i.get("id") if isinstance(i, dict) else i) for i in items]
    done = refused = 0
    with httpx.Client(timeout=15.0) as c:
        for mid in ids:
            if not live:
                print(f"  would retire {mid}", flush=True)
                continue
            if soft_retire(c, mid, reason):
                done += 1
            else:
                refused += 1
    print(f"decay-scan --retire-ids: {'LIVE' if live else 'DRY-RUN'} ids={len(ids)} retired={done} refused={refused}", flush=True)
    return 1 if refused else 0


def main():
    ap = argparse.ArgumentParser(description="weekly decay scanner")
    ap.add_argument("--retire-ids", help="soft-retire the ids in this file (one per line or a JSON list)")
    ap.add_argument("--reason", default="operator-reviewed soft-retire", help="ledger reason for --retire-ids")
    ap.add_argument("--live", action="store_true", help="write (default for --retire-ids: dry-run)")
    args = ap.parse_args()
    if args.retire_ids:
        return retire_ids(args.retire_ids, args.reason, args.live)
    now = dt.datetime.now(dt.timezone.utc)
    deleted, flagged, retired, would_retire = 0, 0, 0, 0
    mode = retire_mode()
    seen, log_days = retrieved_ids() if mode != "off" else (set(), 0.0)
    use_never_retrieved = log_days >= RETRIEVAL_LOG_MIN_DAYS
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.unlink(missing_ok=True)
    # Preflight: confirm both backends are reachable
    try:
        with httpx.Client(timeout=5.0) as probe:
            probe.get(f"{QDRANT}/readyz").raise_for_status()
            probe.get(f"{MEM0}/health").raise_for_status()
    except (httpx.HTTPError, httpx.ConnectError, OSError) as e:
        _ledger({"event": "decay-scan-skip", "actor": "decay-scan", "reason": f"backend unreachable: {type(e).__name__}: {str(e)[:120]}"})
        print(f"decay-scan: SKIP - backend unreachable ({e})", flush=True)
        return 0
    try:
        with httpx.Client(headers=H, timeout=15.0) as c, REPORT.open("a", encoding="utf-8") as report:
            for p in scroll_all():
                payload = p.get("payload") or {}
                tier = payload.get("tier", "evidence")
                mid = str(p.get("id"))
                if tier == "temporal":
                    exp = payload.get("expires_at")
                    if exp:
                        try:
                            exp_dt = dt.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                            if exp_dt < now:
                                r = c.delete(f"{MEM0}/v1/memories/{mid}")
                                if r.status_code == 200:
                                    deleted += 1
                                    # Preserve full payload in report for restore (lens S3)
                                    report.write(json.dumps({"id": mid, "action": "deleted-expired", "tier": tier, "full_payload": payload}) + "\n")
                                    _ledger({"event": "decay-delete", "memory_id": mid, "reason": f"temporal expired at {exp}", "actor": "decay-scan"})
                        except ValueError:
                            pass
                elif tier == "evidence":
                    created = payload.get("created_at")
                    if created:
                        try:
                            c_dt = dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                            age = (now - c_dt).days
                            if age > EVIDENCE_AGE_DAYS and not payload.get("replaces") and not payload.get("touched_by_dream"):
                                flagged += 1
                                action = "flagged-decay-candidate"
                                text = str(payload.get("data") or "")
                                live_rec = payload.get("retrievable") is not False and not payload.get("superseded_by")
                                why = None
                                if mode != "off" and live_rec and age > RETIRE_AGE_DAYS and not protected(payload):
                                    if len(text) > RETIRE_LONG_CHARS:
                                        why = f"long ({len(text)} chars)"
                                    elif use_never_retrieved and mid not in seen:
                                        why = f"never retrieved in {int(log_days)}d of retrieval log"
                                if why and (retired + would_retire) < RETIRE_CAP:
                                    reason = f"decay expiry: age {age}d > {RETIRE_AGE_DAYS}d, {why}"
                                    if mode == "live":
                                        if soft_retire(c, mid, reason):
                                            retired += 1
                                            action = "retired-decay"
                                    else:
                                        would_retire += 1
                                        action = "would-retire-decay"
                                report.write(json.dumps({"id": mid, "action": action, "age_days": age, "chars": len(text), "preview": text[:80]}) + "\n")
                        except ValueError:
                            pass
    except (httpx.HTTPError, OSError) as e:
        _ledger({"event": "decay-scan-abort", "actor": "decay-scan", "reason": f"mid-run failure: {type(e).__name__}: {str(e)[:120]}", "partial_deleted": deleted, "partial_flagged": flagged})
        print(f"decay-scan: ABORT mid-run after deleted={deleted}, flagged={flagged} ({e})", flush=True)
        return 1
    _ledger({"event": "decay-scan-summary", "actor": "decay-scan", "mode": mode, "deleted_temporal": deleted,
             "flagged": flagged, "retired": retired, "would_retire": would_retire,
             "retrieval_log_days": int(log_days), "never_retrieved_used": use_never_retrieved})
    print(f"decay-scan: deleted_temporal={deleted}, flagged_evidence_candidates={flagged}, "
          f"retire_mode={mode} retired={retired} would_retire={would_retire} (cap {RETIRE_CAP}; "
          f"never-retrieved rule {'on' if use_never_retrieved else 'off'}, log {int(log_days)}d), report={REPORT}")

if __name__ == "__main__":
    sys.exit(main())
