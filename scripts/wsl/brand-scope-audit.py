#!/usr/bin/env python3
"""Brand-scope audit (2026-06-20) — catches the mis-scoping bug that hid Brand-A's
ground-truth fact for two weeks.

THE BUG: retrieval + the SessionStart brand-block are fail-closed on brand — a
brand=X session sees ONLY brand=X canonical records, never null-brand ones. So a
canonical fact ABOUT a brand that carries no `brand` tag is invisible to that
brand's sessions. One brand fact (and 4 same-brand rules) sat canonical but
brand-untagged, so they never surfaced and the same corrections recurred ~50x.

THE RULE (this audit): a canonical record whose `project` is a brand context (i.e.
NOT in the neutral/ecosystem set) MUST carry a `brand` tag. Ecosystem/neutral
canonical may be brand-null on purpose (cross-brand facts).

Exit 2 if any mis-scoped canonical record is found, so this can gate a ship / alarm
a nightly. Zero Codex, local Qdrant only — no API cost.

ALL TIERS (2026-09, the C3 brand map): the canonical check above used to be the whole audit, so
"0 mis-scoped" said nothing about the ~97% of the store that is not canonical. It now also reports,
without ever changing the exit code:
  - untagged_brand_mentions: live records with NO brand whose text matches a `content_rules`
    pattern of the brand map (a fact about a client brand that every other brand can read). Review
    and fix with scripts/wsl/brand-backfill.py (dry-run report, then --apply of the reviewed rows).
  - unroutable_brands: brands found on records that no rule, content rule or shared label of the
    map can produce (test leftovers, typos, a brand nobody mapped).

Run: ~/apps/mem0-server/.venv/bin/python scripts/wsl/brand-scope-audit.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys

import httpx
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # deployed flat: ~/apps/mem0-scripts
import ams_env  # noqa: E402  (spec §4: URL from authority-url, key from the systemd credential)
import brand_routing  # noqa: E402  (the C3 brand map)

QDRANT = "http://127.0.0.1:6333"
COLLECTION = os.environ.get("MEM0_QDRANT_COLLECTION", "mem0_egemma_768")
MEM0 = ams_env.mem0_url()  # unused by the Qdrant scroll; kept so the audit reads the same authority as its siblings
# Projects that are legitimately brand-neutral (cross-brand ecosystem facts).
NEUTRAL_PROJECTS = {"", "ecosystem", "none"}


def scroll_points(flt: dict | None = None) -> list[dict]:
    """Every point's payload (no vectors), optionally filtered."""
    pts: list[dict] = []
    offset = None
    with httpx.Client() as c:
        while True:
            body: dict = {"limit": 256, "with_payload": True, "with_vector": False}
            if flt:
                body["filter"] = flt
            if offset is not None:
                body["offset"] = offset
            r = c.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll", json=body, timeout=30.0)
            r.raise_for_status()
            res = r.json().get("result", {})
            pts.extend(res.get("points", []))
            offset = res.get("next_page_offset")
            if not offset:
                break
    return pts


def scroll_canonical() -> list[dict]:
    return scroll_points({"must": [{"key": "tier", "match": {"value": "canonical"}}]})


def is_live(p: dict) -> bool:
    """Retired and non-retrievable points are out of every metric: nothing recalls them."""
    pl = p.get("payload") or {}
    return not pl.get("retired_at") and pl.get("retrievable") is not False


def _text(p: dict) -> str:
    pl = p.get("payload") or {}
    return str(pl.get("data") or pl.get("memory") or "")


def find_untagged_mentions(points: list[dict], brand_map) -> dict:
    """Live records with no brand whose text matches a content rule of the brand map. `ambiguous`
    counts those matching several brands (the backfill leaves them alone)."""
    live = [p for p in points if is_live(p)]
    n, ambiguous, by_brand, samples = 0, 0, {}, []
    for p in live:
        if str((p.get("payload") or {}).get("brand") or "").strip():
            continue
        found = brand_routing.content_brands(brand_map, _text(p))
        if not found:
            continue
        n += 1
        if len(found) == 1:
            b = next(iter(found))
            by_brand[b] = by_brand.get(b, 0) + 1
        else:
            ambiguous += 1
        if len(samples) < 20:
            samples.append(p.get("id"))
    return {"n": n, "ambiguous": ambiguous, "by_brand": by_brand, "sample_ids": samples, "n_live": len(live)}


def find_unroutable_brands(points: list[dict], brand_map) -> dict:
    """{brand: count} for brands on live records that the brand map cannot produce (case-insensitive)."""
    routable = brand_routing.routable_brands(brand_map)
    out: dict[str, int] = {}
    for p in points:
        if not is_live(p):
            continue
        b = str((p.get("payload") or {}).get("brand") or "").strip()
        if b and b.lower() not in routable:
            out[b] = out.get(b, 0) + 1
    return out


def find_misscoped(points: list[dict]) -> list[dict]:
    mis = []
    for p in points:
        pl = p.get("payload") or {}
        brand = pl.get("brand")
        proj = str(pl.get("project") or "").strip().lower()
        if not brand and proj not in NEUTRAL_PROJECTS:
            mem = (pl.get("data") or pl.get("memory") or "")[:80].replace("\n", " ")
            mis.append({"id": p.get("id"), "project": proj, "preview": mem})
    return mis


def main() -> int:
    try:
        pts = scroll_points()
    except (httpx.HTTPError, OSError) as e:
        print(f"brand-scope-audit: DEGRADED — Qdrant unreachable: {e}", flush=True)
        return 0  # fail-open: can't audit != audit failed
    canon = [p for p in pts if (p.get("payload") or {}).get("tier") == "canonical"]
    mis = find_misscoped(canon)
    print(f"brand-scope-audit: {len(canon)} canonical records; {len(mis)} mis-scoped "
          f"(brand-implied project, no brand tag)", flush=True)
    for m in mis:
        print(f"  MIS-SCOPED {m['id']} project={m['project']}: {m['preview']}", flush=True)
    if mis:
        print("  FIX: tag each with its brand via "
              "scripts/wsl/mem0-canonize.sh --action patch_metadata <id> \"<reason>\" "
              "--metadata-json '{\"brand\":\"<brand>\"}'", flush=True)

    # All tiers (C3 brand map): metrics only, never an exit code.
    brand_map = brand_routing.load_brand_map()
    unt = find_untagged_mentions(pts, brand_map)
    unr = find_unroutable_brands(pts, brand_map)
    per = ", ".join(f"{b}: {n}" for b, n in sorted(unt["by_brand"].items()))
    if not brand_map.get("content_rules"):
        print("  untagged brand mentions: not measured (the brand map has no content_rules)", flush=True)
    else:
        print(f"  {unt['n']} untagged brand mention(s) of {unt['n_live']} live records"
              f"{' (' + per + ')' if per else ''}; {unt['ambiguous']} match several brands"
              f" — review with scripts/wsl/brand-backfill.py --dry-run", flush=True)
    if unr:
        print("  unroutable brands (on records, no rule/content rule/shared label produces them): "
              + ", ".join(f"{b} ({n})" for b, n in sorted(unr.items())), flush=True)
    # Persist a status file (overwritten each run, so it self-clears once fixed) that the
    # nightly run produces and the SessionStart storage-cap hook surfaces as a warning when
    # n_misscoped > 0. Fail-open: a write error never affects the audit's exit code. The new keys
    # are additive: n_canonical / n_misscoped / misscoped keep their meaning.
    try:
        sp = os.path.join(os.path.expanduser("~"), ".mem0", "brand-scope-status.json")
        with open(sp, "w", encoding="utf-8") as fh:
            json.dump({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "n_canonical": len(canon), "n_misscoped": len(mis), "misscoped": mis,
                       "n_points": unt["n_live"], "untagged_brand_mentions": unt["n"],
                       "untagged_by_brand": unt["by_brand"], "untagged_ambiguous": unt["ambiguous"],
                       "untagged_sample_ids": unt["sample_ids"], "unroutable_brands": unr}, fh)
    except OSError:
        pass
    return 2 if mis else 0


if __name__ == "__main__":
    sys.exit(main())
