#!/usr/bin/env python3
"""Brand-scope audit (2026-06-20) — catches the mis-scoping bug that hid a brand's
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

Run: ~/apps/mem0-server/.venv/bin/python scripts/wsl/brand-scope-audit.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys

import httpx

QDRANT = "http://127.0.0.1:6333"
COLLECTION = os.environ.get("MEM0_QDRANT_COLLECTION", "mem0_egemma_768")
# Projects that are legitimately brand-neutral (cross-brand ecosystem facts).
NEUTRAL_PROJECTS = {"", "ecosystem", "none"}


def scroll_canonical() -> list[dict]:
    pts: list[dict] = []
    offset = None
    with httpx.Client() as c:
        while True:
            body = {
                "limit": 256,
                "with_payload": True,
                "filter": {"must": [{"key": "tier", "match": {"value": "canonical"}}]},
            }
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
        pts = scroll_canonical()
    except (httpx.HTTPError, OSError) as e:
        print(f"brand-scope-audit: DEGRADED — Qdrant unreachable: {e}", flush=True)
        return 0  # fail-open: can't audit != audit failed
    mis = find_misscoped(pts)
    print(f"brand-scope-audit: {len(pts)} canonical records; {len(mis)} mis-scoped "
          f"(brand-implied project, no brand tag)", flush=True)
    for m in mis:
        print(f"  MIS-SCOPED {m['id']} project={m['project']}: {m['preview']}", flush=True)
    if mis:
        print("  FIX: tag each with its brand via "
              "scripts/wsl/mem0-canonize.sh --action patch_metadata <id> \"<reason>\" "
              "--metadata-json '{\"brand\":\"<brand>\"}'", flush=True)
    # Persist a status file (overwritten each run, so it self-clears once fixed) that the
    # nightly run produces and the SessionStart storage-cap hook surfaces as a warning when
    # n_misscoped > 0. Fail-open: a write error never affects the audit's exit code.
    try:
        sp = os.path.join(os.path.expanduser("~"), ".mem0", "brand-scope-status.json")
        with open(sp, "w", encoding="utf-8") as fh:
            json.dump({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "n_canonical": len(pts), "n_misscoped": len(mis), "misscoped": mis}, fh)
    except OSError:
        pass
    return 2 if mis else 0


if __name__ == "__main__":
    sys.exit(main())
