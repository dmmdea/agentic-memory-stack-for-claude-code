#!/usr/bin/env python3
"""Replay the offline operation-outbox to the memory authority. Adds first, then mutations.
Idempotent (replayed-key ledger); failures -> mutation-conflicts.jsonl (never dropped).
Only runs when the authority is reachable; atomic rotation closes the concurrent-writer race."""
from __future__ import annotations
import argparse
import json
import os
import sys
import uuid
from pathlib import Path
import httpx

# Default = loopback (correct on the brain box). A replica MUST set MEM0_URL to the
# brain's URL — travel-mode manages this; the old machine-name default resolved only
# on the developer's tailnet.
AUTHORITY = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = (Path.home() / ".mem0" / "api-key").read_text(encoding="utf-8").strip()
_MUTATION_ORDER = 1  # adds sort before everything else
_ADD_ORDER = 0

def _headers() -> dict:
    return {"X-API-Key": KEY, "Content-Type": "application/json"}

def _authority_reachable(url: str) -> bool:
    try:
        r = httpx.get(f"{url}/health", timeout=httpx.Timeout(connect=1.5, read=8.0, write=8.0, pool=1.5))
        return r.status_code == 200 and r.json().get("ok") is True
    except Exception:
        return False

def dispatch(op: str, args: dict) -> httpx.Response:
    """Map a queued op to its authority call. Raises httpx.HTTPStatusError on 4xx/5xx."""
    t = httpx.Timeout(connect=1.5, read=30.0, write=30.0, pool=1.5)
    h = _headers()
    if op == "add":
        body = {"messages": args["text"], "user_id": args.get("user_id", "__WSL_USER__"),
                "infer": args.get("infer", False), "metadata": args.get("metadata") or {}}
        r = httpx.post(f"{AUTHORITY}/v1/memories", json=body, headers=h, timeout=t)
    elif op == "update":
        r = httpx.put(f"{AUTHORITY}/v1/memories/{args['memory_id']}", json={"text": args["text"]}, headers=h, timeout=t)
    elif op == "delete":
        r = httpx.delete(f"{AUTHORITY}/v1/memories/{args['memory_id']}", headers=h, timeout=t)
    elif op in ("promote", "demote"):
        r = httpx.patch(f"{AUTHORITY}/v1/memories/{args['memory_id']}/tier",
                        json={"tier": args["tier"], "actor": "claude-autonomous", "reason": args.get("reason")}, headers=h, timeout=t)
    elif op == "goal_create_manual":
        r = httpx.post(f"{AUTHORITY}/v1/goals", json=args, headers=h, timeout=t)
    elif op in ("goal_complete", "goal_abandon"):
        verb = op.split("_", 1)[1]
        r = httpx.patch(f"{AUTHORITY}/v1/goals/{args['goal_id']}/{verb}",
                        json={"actor": args.get("actor", "claude-autonomous"), "reason": args["reason"]}, headers=h, timeout=t)
    elif op == "goal_set_priority":
        r = httpx.patch(f"{AUTHORITY}/v1/goals/{args['goal_id']}/priority", json=args, headers=h, timeout=t)
    elif op == "goal_link_episode":
        r = httpx.post(f"{AUTHORITY}/v1/goals/{args['goal_id']}/link_episode", json=args, headers=h, timeout=t)
    elif op == "goal_merge":
        r = httpx.post(f"{AUTHORITY}/v1/goals/{args['source_goal_id']}/merge", json=args, headers=h, timeout=t)
    elif op == "open_question_resolve":
        r = httpx.patch(f"{AUTHORITY}/v1/open_questions/{args['open_question_id']}/resolve", json=args, headers=h, timeout=t)
    else:
        raise ValueError(f"unknown op: {op}")
    r.raise_for_status()
    return r

def replay(outbox: Path, authority: str, key: str) -> dict:
    global AUTHORITY
    AUTHORITY = authority  # dispatch must target the same authority the probe checked
    stats = {"replayed": 0, "conflicts": 0, "kept": 0}
    replaying = outbox.with_suffix(".replaying.jsonl")
    tmp = outbox.with_suffix(".rotating.jsonl")
    live_pending = outbox.exists() and outbox.read_text(encoding="utf-8").strip()
    kept_pending = replaying.exists() and replaying.read_text(encoding="utf-8").strip()
    tmp_pending = tmp.exists() and tmp.read_text(encoding="utf-8").strip()
    if not live_pending and not kept_pending and not tmp_pending:
        return stats  # nothing queued, kept from a transient failure, or mid-rotation from a crash
    if not _authority_reachable(authority):
        return stats
    ledger = outbox.parent / "outbox.replayed.jsonl"
    conflicts = outbox.parent / "mutation-conflicts.jsonl"
    # atomic rotation: os.replace() renames the live outbox out of the way in one atomic step
    # (a concurrent writer either lands in the old inode we now own, or in a fresh outbox.jsonl
    # that survives this replay untouched), then its content is folded into the replaying file.
    if tmp.exists():  # leftover from a crash between replace and fold — never drop it
        with replaying.open("a", encoding="utf-8") as rp:
            rp.write(tmp.read_text(encoding="utf-8"))
        tmp.unlink()
    if outbox.exists():
        os.replace(outbox, tmp)
        with replaying.open("a", encoding="utf-8") as rp:
            rp.write(tmp.read_text(encoding="utf-8"))
        tmp.unlink()
    done_keys = set()
    if ledger.exists():
        for ln in ledger.read_text(encoding="utf-8").splitlines():
            try: done_keys.add(json.loads(ln)["key"])
            except Exception: pass
    recs = []
    for ln in replaying.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            try: recs.append(json.loads(ln))
            except Exception:
                # torn/unparseable line: the rewrite below would silently destroy it —
                # preserve the raw bytes in the conflict log for manual recovery instead
                with conflicts.open("a", encoding="utf-8") as cf:
                    cf.write(json.dumps({"raw": ln, "reason": "unparseable"}) + "\n")
                stats["conflicts"] += 1
    recs.sort(key=lambda r: _ADD_ORDER if r.get("op") == "add" else _MUTATION_ORDER)  # stable: adds first
    kept = []
    for rec in recs:
        k = rec.get("key") or str(uuid.uuid4())
        if k in done_keys:
            continue
        try:
            dispatch(rec["op"], rec.get("args") or {})
            with ledger.open("a", encoding="utf-8") as lf:
                lf.write(json.dumps({"key": k, "op": rec["op"]}) + "\n")
            done_keys.add(k)  # in-batch dedup: a duplicated key later in this batch must skip
            stats["replayed"] += 1
        except httpx.HTTPStatusError as e:
            with conflicts.open("a", encoding="utf-8") as cf:
                cf.write(json.dumps({"op": rec["op"], "args": rec.get("args"), "key": k,
                                     "status": e.response.status_code}) + "\n")
            stats["conflicts"] += 1
        except (KeyError, ValueError) as e:
            # deterministic dispatch failure (old-format record with no 'op', unknown op):
            # retrying can never succeed, so keeping it would loop forever — conflict-log it
            with conflicts.open("a", encoding="utf-8") as cf:
                cf.write(json.dumps({"op": rec.get("op"), "args": rec.get("args"), "key": k,
                                     "reason": str(e)}) + "\n")
            stats["conflicts"] += 1
        except Exception:
            kept.append(rec)   # transient — keep for next run
            stats["kept"] += 1
    if kept:
        replaying.write_text("\n".join(json.dumps(r) for r in kept) + "\n", encoding="utf-8")
    else:
        replaying.unlink(missing_ok=True)
    return stats

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--outbox", default=str(Path.home() / ".mem0" / "outbox.jsonl"))
    ap.add_argument("--authority", default=AUTHORITY)
    a = ap.parse_args()
    s = replay(Path(a.outbox), a.authority, KEY)
    print(json.dumps(s))
    sys.exit(0)
