#!/usr/bin/env python3
"""brand-backfill - give already-stored facts the brand the C3 brand map says they belong to.

Why: L1a used to post every fact brand-neutral, and dream insights carried no brand, so the
brand isolation of the store covers only what a human tagged by hand. brand-scope-audit.py
counts the untagged records that mention a brand; this tool fixes them, in two steps a person
sits between:

  1. brand-backfill.py --dry-run --out report.jsonl
       One JSON row per record it would tag:
         {"id", "tier", "current", "proposed", "rule", "text_head", "fp"}
       `rule` is "path" (the record's workspace/project matched a `rules` entry) or "content"
       (exactly one brand's `content_rules` matched its text). Nothing is written to the store.
       Review the file: delete a row to skip it, set "proposed" to null to skip it, or correct the
       brand (it must be one the map can route).
  2. brand-backfill.py --apply --from report.jsonl
       Applies exactly the rows in the file. A row is REFUSED, not applied, when its record
       changed since the report (its fingerprint no longer matches: text, brand or updated_at) or
       when someone tagged it meanwhile. Each write is PATCH /v1/memories/<id>/metadata with the
       actor "brand-backfill" and only {"brand": ...}; the ledger records every one.

Canonical and insight records are NEVER patched here: those tiers require the operator's HMAC
signature (or an insight actor), which this tool must not hold. --apply prints the exact
`mem0-canonize.sh --action patch_metadata` command for each so the operator can sign them.

A run without a brand map refuses (exit 3): with nothing to route by, "no proposals" would look like
"nothing to fix". Exit 0 = done, 1 = at least one PATCH failed, 3 = no brand map / bad input.

Run on the authority: ~/apps/mem0-server/.venv/bin/python ~/apps/mem0-scripts/brand-backfill.py ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # deployed flat: ~/apps/mem0-scripts
import ams_env  # noqa: E402
import brand_routing  # noqa: E402

QDRANT = "http://127.0.0.1:6333"
COLLECTION = os.environ.get("MEM0_QDRANT_COLLECTION", "mem0_egemma_768")
ACTOR = "brand-backfill"
HMAC_TIERS = ("canonical", "insight")
HEAD = 80


class Store:
    """Qdrant for reads (one scroll, one point read) and the mem0 API for the write."""

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client()
        self.url = ams_env.mem0_url()
        self.h = {"X-API-Key": ams_env.api_key(), "Content-Type": "application/json"}

    def points(self) -> list[dict]:
        pts: list[dict] = []
        off = None
        uid = ams_env.user_id()
        while True:
            body: dict = {"limit": 256, "with_payload": True, "with_vector": False}
            if uid:
                body["filter"] = {"must": [{"key": "user_id", "match": {"value": uid}}]}
            if off is not None:
                body["offset"] = off
            r = self.http.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll", json=body, timeout=30.0)
            r.raise_for_status()
            res = r.json().get("result") or {}
            pts.extend(res.get("points") or [])
            off = res.get("next_page_offset")
            if not off:
                return pts

    def get(self, pid: str) -> dict | None:
        r = self.http.post(f"{QDRANT}/collections/{COLLECTION}/points",
                           json={"ids": [pid], "with_payload": True, "with_vector": False}, timeout=30.0)
        r.raise_for_status()
        res = r.json().get("result") or []
        return res[0] if res else None

    def patch(self, pid: str, metadata: dict, actor: str, reason: str) -> bool:
        r = self.http.patch(f"{self.url}/v1/memories/{pid}/metadata", headers=self.h,
                            json={"metadata": metadata, "actor": actor, "reason": reason}, timeout=10.0)
        r.raise_for_status()
        return True


def _payload(p: dict) -> dict:
    return p.get("payload") or {}


def _text(p: dict) -> str:
    pl = _payload(p)
    return str(pl.get("data") or pl.get("memory") or "")


def _brand(p: dict) -> str | None:
    return str(_payload(p).get("brand") or "").strip() or None


def fingerprint(p: dict) -> str:
    """What --apply re-checks: the text, the brand and the last-update stamp the API bumps on every
    metadata write. Any edit since the report changes it."""
    pl = _payload(p)
    raw = json.dumps([_text(p), _brand(p), pl.get("updated_at"), pl.get("tier")], ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _live(p: dict) -> bool:
    pl = _payload(p)
    return not pl.get("retired_at") and pl.get("retrievable") is not False


def propose(p: dict, brand_map) -> tuple[str | None, str | None]:
    """(brand, rule) for one record, or (None, None), by the C3 contract (brand_routing):
    1. a path rule on its workspace, else on its project -> "path";
    2. else, when its workspace or project is a content-rule workspace, or it carries no path at all
       (nothing to route by), exactly one brand's content rules over its text -> "content";
    3. else nothing: a record whose path routes nowhere outside the content-rule workspaces stays
       brand-neutral, exactly as the live resolver leaves a new fact from that path."""
    pl = _payload(p)
    paths = [str(v) for v in (pl.get("workspace"), pl.get("project")) if v]
    for path in paths:
        b = brand_routing.resolve(brand_map, path, "")
        if b:
            return b, "path"
    if paths and not any(brand_routing.in_content_rule_workspace(brand_map, path) for path in paths):
        return None, None
    b = brand_routing.resolve_by_content(brand_map, _text(p))
    return (b, "content") if b else (None, None)


def run_dry(store, brand_map, out_path: str) -> int:
    rows = []
    for p in store.points():
        if not _live(p) or _brand(p):
            continue
        proposed, rule = propose(p, brand_map)
        if not proposed:
            continue
        rows.append({"id": str(p.get("id")), "tier": _payload(p).get("tier"), "current": None,
                     "proposed": proposed, "rule": rule,
                     "text_head": " ".join(_text(p).split())[:HEAD], "fp": fingerprint(p)})
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=True) + "\n")
    hmac_n = sum(1 for r in rows if r["tier"] in HMAC_TIERS)
    by: dict[str, int] = {}
    for r in rows:
        by[r["proposed"]] = by.get(r["proposed"], 0) + 1
    print(f"brand-backfill: {len(rows)} proposal(s) written to {out_path} "
          f"({', '.join(f'{b}: {n}' for b, n in sorted(by.items())) or 'none'}); "
          f"{hmac_n} need the operator's HMAC path. Nothing was changed. Review the file, then --apply --from it.",
          flush=True)
    return 0


def run_apply(store, brand_map, from_path: str) -> int:
    routable = brand_routing.routable_brands(brand_map)
    try:
        with open(from_path, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError) as e:
        print(f"brand-backfill: cannot read {from_path}: {e}", flush=True)
        return 3
    applied = refused = failed = 0
    hmac_rows: list[dict] = []
    for r in rows:
        pid, proposed = str(r.get("id") or ""), r.get("proposed")
        if not pid or not proposed:
            continue                                   # the reviewer blanked it: skip
        if str(proposed).strip().lower() not in routable:
            print(f"  REFUSED {pid}: brand {proposed!r} is not one the brand map can route", flush=True)
            refused += 1
            continue
        cur = store.get(pid)
        if cur is None:
            print(f"  REFUSED {pid}: record no longer exists", flush=True)
            refused += 1
            continue
        if _brand(cur) or fingerprint(cur) != r.get("fp"):
            print(f"  REFUSED {pid}: changed since the report (re-run --dry-run)", flush=True)
            refused += 1
            continue
        if _payload(cur).get("tier") in HMAC_TIERS:
            hmac_rows.append({"id": pid, "proposed": proposed, "tier": _payload(cur).get("tier")})
            continue
        try:
            store.patch(pid, {"brand": proposed}, ACTOR, f"brand backfill ({r.get('rule')} rule, reviewed report)")
            applied += 1
        except Exception as e:  # noqa: BLE001 - one bad write must not stop the rest
            print(f"  FAILED {pid}: {e}", flush=True)
            failed += 1
    # A native authority holds the key only inside a unit that loads its credential, and
    # ams-canonize.sh is what starts that unit; mem0-canonize.sh run from a shell there finds no key.
    signer = "ams-canonize.sh" if ams_env.stack_env().get("MEM0_HOST_KIND") == "native" else "mem0-canonize.sh"
    for h in hmac_rows:
        meta = json.dumps({"brand": h["proposed"]})
        print(f"  HMAC ({h['tier']}): bash ~/apps/mem0-scripts/{signer} --action patch_metadata {h['id']} "
              f"\"brand backfill\" --metadata-json '{meta}'", flush=True)
    print(f"brand-backfill: {applied} applied, {refused} refused, {failed} failed, "
          f"{len(hmac_rows)} left for the HMAC path", flush=True)
    return 1 if failed else 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="tag stored facts with the brand the C3 brand map routes them to")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="write the proposals to --out; change nothing")
    g.add_argument("--apply", action="store_true", help="apply exactly the reviewed rows of --from")
    p.add_argument("--out", help="the dry-run report (JSON lines)")
    p.add_argument("--from", dest="from_file", help="the reviewed report to apply")
    a = p.parse_args(argv)
    if a.dry_run and not a.out:
        p.error("--dry-run needs --out <file>")
    if a.apply and not a.from_file:
        p.error("--apply needs --from <reviewed report>")
    return a


def main(argv=None, store=None) -> int:
    a = parse_args(argv)
    brand_map = brand_routing.load_brand_map()
    if not (brand_map.get("rules") or brand_map.get("content_rules")):
        print(f"brand-backfill: no brand map with rules found at {brand_routing.map_path()}; refusing "
              f"(set MEM0_BRAND_MAP or deploy brands.json)", flush=True)
        return 3
    store = store or Store()
    return run_dry(store, brand_map, a.out) if a.dry_run else run_apply(store, brand_map, a.from_file)


if __name__ == "__main__":
    sys.exit(main())
