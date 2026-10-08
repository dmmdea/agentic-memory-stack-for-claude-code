#!/usr/bin/env python3
"""embedder-migrate.py — move the store to another embedding space, beside the old one.

Vectors from two embedding models live in different spaces even at the same width, so changing
the embedder means re-embedding every point. This tool builds the target space's collections NEXT
TO the source ones and never writes to a source collection: the source stays the rollback anchor
until the operator prunes it.

What it does per kind (the collection names come from embedder_profile):
  memories   mem0's facts             re-embed payload["data"]     (document prefix)
  entities   mem0's entity vectors    re-embed payload["data"]     (document prefix; mem0 embeds entities with "add")
  episodes   episode summaries        re-embed payload["summary"]  (document prefix; episode_embeddings does the same)
The point id, the whole payload and every sparse vector (the BM25 leg, `bm25`) are copied verbatim;
only the dense vector is new. The target collection copies the source's vector, HNSW and optimizer
settings and its payload indexes. The wiki index is not copied: it is rebuilt from the vault with
wiki-index-build.py, which reads the active profile.

Modes:
  (default)    build: create the targets and embed every point missing from them
  --catch-up   also re-embed points whose payload changed in the source and delete target points
               the source no longer has, at most --max-delete (default 200). Run it with mem0
               STOPPED, right before the profile switch (docs/MIGRATION.md); in reverse it brings
               the old space up to date for a rollback.
  --verify     no writes: compare counts, and re-embed a sample to prove each stored target vector
               is the target model's vector for that point's text (cosine >= --min-cos)
  --dry-run    with any mode: count what would be embedded (and, with --catch-up, list what would
               be deleted), then stop
Safety: no mode writes a collection the stack is using — the server's bound collections
(/health/deep) or the active profile's (stack.env, which still answers while mem0 is stopped) —
unless --force. Episodes are embedded from episodic.db's full summary_text (the payload holds only
its first 800 characters), as the live path and episode-embed-backfill.py do.

On success it records the target's identity in ~/.mem0/embed-identity.json (profile, model,
template version, counts, source collection, time, and --gguf-sha256 when given): the record a
reader checks /health/deep's embed_profile against.

Usage (on the box that holds the store; the server venv provides httpx + mem0):
  ~/apps/mem0-server/.venv/bin/python embedder-migrate.py --to egemma2 [--from egemma-300m]
      [--kinds memories,entities,episodes] [--qdrant http://127.0.0.1:6333]
      [--embed-url http://127.0.0.1:11436/v1] [--model ALIAS] [--batch 16]
      [--catch-up | --verify | --dry-run] [--gguf-sha256 HEX] [--json]
Exit: 0 ok, 1 error, 2 verify found a mismatch.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import random
import sys
import time
from pathlib import Path

# The server modules live in mem0-server/: a sibling of scripts/ in the repo layout, and
# ~/apps/mem0-server when the scripts are deployed flat into ~/apps/mem0-scripts.
_SERVER_DIRS = [Path(__file__).resolve().parents[2] / "mem0-server",
                Path.home() / "apps" / "mem0-server"]
for _d in _SERVER_DIRS:
    if _d.is_dir():
        sys.path.insert(0, str(_d))
        break

import httpx  # noqa: E402

import embedder_profile  # noqa: E402

# The embed input is truncated exactly as the live shim truncates it, so a migrated vector equals
# the vector the server would have written for the same text.
from egemma_embedder import _truncate_for_embedding, budget_for  # noqa: E402

KINDS = {
    "memories": "data",
    "entities": "data",
    "episodes": "summary",
}
SCROLL_PAGE = 256
IDENTITY_FILE = Path.home() / ".mem0" / "embed-identity.json"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Qdrant:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.http = httpx.Client(timeout=120.0)

    def info(self, name: str) -> dict | None:
        r = self.http.get(f"{self.base}/collections/{name}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()["result"]

    def count(self, name: str) -> int:
        r = self.http.post(f"{self.base}/collections/{name}/points/count", json={"exact": True})
        r.raise_for_status()
        return int(r.json()["result"]["count"])

    def scroll(self, name: str, with_vector, with_payload=True):
        offset = None
        while True:
            body = {"limit": SCROLL_PAGE, "with_payload": with_payload, "with_vector": with_vector}
            if offset is not None:
                body["offset"] = offset
            r = self.http.post(f"{self.base}/collections/{name}/points/scroll", json=body)
            r.raise_for_status()
            res = r.json()["result"]
            yield from res["points"]
            offset = res.get("next_page_offset")
            if offset is None:
                return

    def retrieve(self, name: str, ids: list, with_vector=True) -> list:
        r = self.http.post(f"{self.base}/collections/{name}/points",
                           json={"ids": ids, "with_payload": True, "with_vector": with_vector})
        r.raise_for_status()
        return r.json()["result"]

    def create_like(self, source_info: dict, target: str, dims: int) -> None:
        cfg = source_info["config"]
        params = cfg["params"]
        vectors = dict(params["vectors"])
        if "size" in vectors:            # one unnamed dense vector (every AMS collection)
            vectors["size"] = dims
        else:                            # named dense vectors: resize each
            vectors = {k: {**v, "size": dims} for k, v in vectors.items()}
        body = {"vectors": vectors, "on_disk_payload": params.get("on_disk_payload", True)}
        if params.get("sparse_vectors"):
            body["sparse_vectors"] = params["sparse_vectors"]
        hnsw = {k: v for k, v in (cfg.get("hnsw_config") or {}).items() if v is not None}
        if hnsw:
            body["hnsw_config"] = hnsw
        opt = {k: v for k, v in (cfg.get("optimizer_config") or {}).items() if v is not None}
        if opt:
            body["optimizers_config"] = opt
        r = self.http.put(f"{self.base}/collections/{target}", json=body)
        r.raise_for_status()
        for field, schema in (source_info.get("payload_schema") or {}).items():
            ftype = schema.get("data_type")
            if not ftype:
                continue
            rr = self.http.put(f"{self.base}/collections/{target}/index?wait=true",
                               json={"field_name": field, "field_schema": ftype})
            rr.raise_for_status()

    def upsert(self, name: str, points: list) -> None:
        r = self.http.put(f"{self.base}/collections/{name}/points?wait=true", json={"points": points})
        if r.status_code >= 400:
            raise RuntimeError(f"upsert {name}: HTTP {r.status_code} {r.text[:300]}")

    def delete(self, name: str, ids: list) -> None:
        r = self.http.post(f"{self.base}/collections/{name}/points/delete?wait=true", json={"points": ids})
        r.raise_for_status()


class Embedder:
    def __init__(self, base: str, model: str, profile: embedder_profile.EmbedProfile):
        self.base = base.rstrip("/")
        self.model = model
        self.profile = profile
        self.budget = budget_for(profile)
        self.http = httpx.Client(timeout=300.0)
        self.calls = 0

    def doc_inputs(self, texts: list[str]) -> list[str]:
        return [self.profile.doc_prefix + _truncate_for_embedding(t or "", self.budget) for t in texts]

    def embed_docs(self, texts: list[str]) -> list[list[float]]:
        inputs = self.doc_inputs(texts)
        delays = (5, 15, 30)
        for attempt in range(len(delays) + 1):
            try:
                r = self.http.post(f"{self.base}/embeddings", json={"model": self.model, "input": inputs})
                if r.status_code in (429, 502, 503, 504) or (r.status_code == 500 and "prematurely" in r.text):
                    raise httpx.HTTPStatusError(f"cold/busy {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                vecs = [d["embedding"] for d in data]
                if len(vecs) != len(inputs):
                    raise RuntimeError(f"embedder returned {len(vecs)} vectors for {len(inputs)} inputs")
                for v in vecs:
                    if len(v) != self.profile.dims:
                        raise RuntimeError(f"embedder returned dim {len(v)}, profile {self.profile.name} "
                                           f"expects {self.profile.dims}: wrong model behind {self.model!r}?")
                self.calls += 1
                return vecs
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if attempt == len(delays):
                    raise RuntimeError(f"embedder {self.base} model {self.model}: {e}") from e
                time.sleep(delays[attempt])
        raise AssertionError("unreachable")


def _cos(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _dense(vec):
    """The unnamed dense vector of a point, whatever shape Qdrant returned it in."""
    if isinstance(vec, list):
        return vec
    if isinstance(vec, dict):
        if "" in vec:
            return vec[""]
        for v in vec.values():
            if isinstance(v, list):
                return v
    return None


def _sparse_names(info: dict) -> list[str]:
    return list((info["config"]["params"].get("sparse_vectors") or {}).keys())


class _EpisodeText:
    """The text the server embedded for an episode: the FULL summary. The Qdrant payload carries only
    its first 800 characters (app.py `_ep_payload`), so a re-embed from the payload would give long
    episodes a vector the live path never writes; read episodic.db, as episode-embed-backfill.py does."""

    def __init__(self):
        self.by_id: dict | None = None
        self.source = "not loaded"

    def load(self, path: Path) -> None:
        import sqlite3
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = con.execute("SELECT id, summary_text FROM episodes WHERE summary_text IS NOT NULL").fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            self.by_id, self.source = None, f"payload (episodic.db unreadable: {e}; summaries cut at 800 chars)"
            return
        self.by_id = {str(i): t for i, t in rows}
        self.source = f"episodic.db ({len(self.by_id)} summaries)"

    def get(self, pid, payload: dict):
        if self.by_id is not None and str(pid) in self.by_id:
            return self.by_id[str(pid)]
        return payload.get("summary")


EPISODE_TEXT = _EpisodeText()


def text_of(kind: str, pid, payload: dict):
    """The text the live path embedded for this point."""
    if kind == "episodes":
        return EPISODE_TEXT.get(pid, payload)
    return payload.get(KINDS[kind])


def live_collections(mem0_url: str) -> tuple[set, str]:
    """Every collection the stack is using right now: the server's bound memories collection and the
    space it reports (/health/deep), plus the active profile's collections from this box's stack.env,
    which still answers while mem0 is stopped for a switch. Returns (names, note)."""
    names: set = set()
    act = embedder_profile.active()
    names.update(embedder_profile.collection(k, act) for k in ("memories", "entities", "episodes"))
    note = f"active profile {act.name}"
    try:
        r = httpx.get(f"{mem0_url.rstrip('/')}/health/deep", timeout=20.0)
        d = r.json()
        bound = d.get("collection")
        if bound:
            names.update({bound, bound + "_entities"})
        names.update(((d.get("embed_profile") or {}).get("collections") or {}).values())
        note += f"; server bound to {bound}"
    except (httpx.HTTPError, ValueError) as e:
        note += f"; server not reachable ({type(e).__name__}): judged from stack.env alone"
    return names, note


def migrate_kind(kind: str, src: str, dst: str, q: Qdrant, emb: Embedder, args, report: dict) -> None:
    rec = report.setdefault(kind, {"source": src, "target": dst})
    sinfo = q.info(src)
    if sinfo is None:
        rec.update(skipped="source collection missing")
        return
    sparse = _sparse_names(sinfo)
    tinfo = q.info(dst)
    if tinfo is None and not args.dry_run:
        q.create_like(sinfo, dst, emb.profile.dims)
        rec["created"] = True
    # Target's current state: ids and (for --catch-up) payloads.
    # Point ids are UUID strings (memories, entities) or integers (episodes): keyed by their string
    # form for comparison, but always sent back to Qdrant as the original object.
    target_payload: dict = {}
    target_raw: dict = {}
    if tinfo is not None or not args.dry_run:
        if q.info(dst) is not None:
            for p in q.scroll(dst, with_vector=False, with_payload=bool(args.catch_up)):
                target_payload[str(p["id"])] = p.get("payload") if args.catch_up else None
                target_raw[str(p["id"])] = p["id"]
    todo, unchanged, empty = [], 0, 0
    source_ids = set()
    for p in q.scroll(src, with_vector=(sparse or False)):
        pid = str(p["id"])
        source_ids.add(pid)
        payload = p.get("payload") or {}
        if pid in target_payload and (not args.catch_up or target_payload[pid] == payload):
            unchanged += 1
            continue
        text = text_of(kind, p["id"], payload)
        if not isinstance(text, str) or not text.strip():
            empty += 1          # nothing to embed: the server never indexed such a point either
            continue
        vecs = p.get("vector") or {}
        sparse_vals = {n: vecs[n] for n in sparse if isinstance(vecs, dict) and n in vecs}
        todo.append((p["id"], payload, text, sparse_vals))
    stale = [target_raw[k] for k in sorted(set(target_payload) - source_ids)] if args.catch_up else []
    rec.update(source_points=len(source_ids), to_embed=len(todo), unchanged=unchanged,
               skipped_empty_text=empty, to_delete=len(stale),
               to_delete_ids=[str(x) for x in stale[:50]])
    if kind == "episodes":
        rec["text_source"] = EPISODE_TEXT.source
    over_cap = len(stale) > args.max_delete
    if args.dry_run:
        rec["over_delete_cap"] = over_cap
        return
    if over_cap:
        # A catch-up deletes target points the source no longer has. Many at once means the source and
        # the target are not what the operator thinks (a half-built space, the wrong direction).
        raise RuntimeError(f"{kind}: catch-up would delete {len(stale)} points from {dst} "
                           f"(--max-delete {args.max_delete}); see to_delete_ids with --dry-run")
    t0 = time.time()
    done = 0
    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        dense = emb.embed_docs([c[2] for c in chunk])
        points = []
        for (pid, payload, _text, sparse_vals), vec in zip(chunk, dense):
            vector = {"": vec, **sparse_vals} if sparse_vals or sparse else vec
            points.append({"id": pid, "vector": vector, "payload": payload})
        q.upsert(dst, points)
        done += len(chunk)
        if not args.json and (done % (args.batch * 20) == 0 or done == len(todo)):
            rate = done / max(time.time() - t0, 1e-6)
            print(f"  {kind}: {done}/{len(todo)} embedded ({rate:.1f}/s)", flush=True)
    if stale:
        for i in range(0, len(stale), 256):
            q.delete(dst, stale[i:i + 256])
    rec.update(embedded=done, deleted=len(stale), seconds=round(time.time() - t0, 1),
               target_points=q.count(dst))


def verify_kind(kind: str, src: str, dst: str, q: Qdrant, emb: Embedder, args, report: dict) -> bool:
    rec = report.setdefault(kind, {"source": src, "target": dst})
    if q.info(src) is None or q.info(dst) is None:
        rec.update(ok=False, error="source or target collection missing")
        return False
    # ids keyed by string for set arithmetic, sent back to Qdrant as the original object (UUID
    # strings for memories/entities, integers for episodes)
    src_raw = {str(p["id"]): p["id"] for p in q.scroll(src, with_vector=False, with_payload=False)}
    dst_raw = {str(p["id"]): p["id"] for p in q.scroll(dst, with_vector=False, with_payload=False)}
    src_ids, dst_set = set(src_raw), set(dst_raw)
    dst_ids = sorted(dst_set)
    missing = sorted(src_ids - dst_set)
    extra = sorted(dst_set - src_ids)
    # Points with no text were never embedded: they are expected to be missing.
    if missing:
        rows = q.retrieve(src, [src_raw[k] for k in missing[:2000]], with_vector=False)
        missing = [str(r["id"]) for r in rows
                   if (text_of(kind, r["id"], r.get("payload") or {}) or "").strip()]
    rng = random.Random(args.seed)
    sample = [dst_raw[k] for k in rng.sample(dst_ids, min(args.sample, len(dst_ids)))] if dst_ids else []
    worst = 1.0
    low = []
    if sample:
        rows = q.retrieve(dst, sample, with_vector=True)
        texts = [text_of(kind, r["id"], r.get("payload") or {}) or "" for r in rows]
        fresh = []
        for i in range(0, len(texts), args.batch):
            fresh.extend(emb.embed_docs(texts[i:i + args.batch]))
        for r, f in zip(rows, fresh):
            c = _cos(_dense(r.get("vector")) or [], f)
            worst = min(worst, c)
            if c < args.min_cos:
                low.append({"id": str(r["id"]), "cos": round(c, 5)})
    ok = not missing and not extra and not low
    rec.update(ok=ok, source_points=len(src_ids), target_points=len(dst_set), missing=len(missing),
               missing_ids=missing[:20], extra=len(extra), extra_ids=extra[:20],
               sampled=len(sample), worst_cos=round(worst, 5), below_min_cos=low[:20])
    return ok


def _default_mem0_url() -> str:
    env = (os.environ.get("MEM0_URL") or "").strip()
    if env:
        return env
    try:
        for line in (Path.home() / ".mem0" / "authority-url").read_text(encoding="utf-8").splitlines():
            if line.strip():
                return line.strip()
    except OSError:
        pass
    return "http://127.0.0.1:18791"


def write_identity(profile: embedder_profile.EmbedProfile, model: str, report: dict, args) -> None:
    try:
        data = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for kind, rec in report.items():
        if not isinstance(rec, dict) or "target" not in rec or rec.get("skipped"):
            continue
        data[rec["target"]] = {
            "kind": kind, "profile": profile.name, "model": model,
            "template_version": profile.template_version, "dims": profile.dims,
            "token_budget": profile.token_budget, "source_collection": rec["source"],
            "points": rec.get("target_points"), "built_at": _now(),
            "gguf_sha256": args.gguf_sha256 or None,
        }
    IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = IDENTITY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, IDENTITY_FILE)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--to", required=True, help="target embedding profile (e.g. egemma2)")
    ap.add_argument("--from", dest="src", default=None, help="source profile (default: the active one)")
    ap.add_argument("--kinds", default="memories,entities,episodes")
    ap.add_argument("--qdrant", default=os.environ.get("MEM0_QDRANT_URL", "http://127.0.0.1:6333"))
    ap.add_argument("--embed-url", default=None, help="OpenAI-compatible base URL (default: embedder_profile.base_url())")
    ap.add_argument("--model", default=None, help="model alias for the TARGET space (default: the target profile's)")
    ap.add_argument("--batch", type=int, default=16)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--catch-up", action="store_true")
    mode.add_argument("--verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="count (and with --catch-up, list) what would be embedded and deleted, then stop")
    ap.add_argument("--max-delete", type=int, default=200,
                    help="--catch-up refuses to delete more target points than this (default 200)")
    ap.add_argument("--mem0-url", default=None,
                    help="the server whose bound collections are never written (default: MEM0_URL, "
                         "~/.mem0/authority-url, then http://127.0.0.1:18791)")
    ap.add_argument("--force", action="store_true",
                    help="write even into a collection the stack is using (never needed by the runbook)")
    ap.add_argument("--episodic-db", default=os.environ.get("MEM0_EPISODIC_DB", str(Path.home() / ".mem0" / "episodic.db")),
                    help="where the episodes' full summaries are read from")
    ap.add_argument("--sample", type=int, default=64, help="--verify: points re-embedded per kind")
    ap.add_argument("--min-cos", type=float, default=0.995, help="--verify: lowest acceptable cosine")
    ap.add_argument("--seed", type=int, default=20261008)
    ap.add_argument("--gguf-sha256", default="", help="recorded in the identity file")
    ap.add_argument("--json", action="store_true", help="print only the JSON report")
    args = ap.parse_args(argv)

    target = embedder_profile.get(args.to)
    source = embedder_profile.get(args.src) if args.src else embedder_profile.active()
    if source.name == target.name:
        print(f"FAIL: source and target are the same profile ({source.name})", file=sys.stderr)
        return 1
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    for k in kinds:
        if k not in KINDS:
            print(f"FAIL: unknown kind {k!r} ({', '.join(KINDS)}; the wiki is rebuilt by wiki-index-build.py)",
                  file=sys.stderr)
            return 1
    # Collection names come straight from each profile, never from the env overrides: an override
    # names ONE space's collection and would point the source and the target at the same place.
    pairs = {k: (getattr(source, k), getattr(target, k)) for k in kinds}
    model = args.model or embedder_profile.embed_model(target)
    emb = Embedder(args.embed_url or embedder_profile.base_url(), model, target)
    q = Qdrant(args.qdrant)
    report: dict = {"ts": _now(), "from": source.name, "to": target.name, "model": model,
                    "mode": ("verify" if args.verify else "catch-up" if args.catch_up else "build")
                            + (" (dry-run)" if args.dry_run else "")}
    if "episodes" in kinds:
        EPISODE_TEXT.load(Path(args.episodic_db))
    # Never write into a collection the stack is using: a catch-up mirrors (it deletes target points
    # the source lacks), so pointed at the live space one step early it would delete live memories.
    if not args.verify and not args.dry_run:
        live, note = live_collections(args.mem0_url or _default_mem0_url())
        hit = sorted(dst for _, dst in pairs.values() if dst in live)
        report["live_check"] = note
        if hit and not args.force:
            print(f"FAIL: {', '.join(hit)} is in use ({note}); writing it would rebuild or mirror the live "
                  f"space. Stop mem0 and switch the profile first (docs/MIGRATION.md), or pass --force.",
                  file=sys.stderr)
            return 1
    rc = 0
    try:
        for k, (src, dst) in pairs.items():
            if args.verify:
                if not verify_kind(k, src, dst, q, emb, args, report):
                    rc = 2
            else:
                if not args.json:
                    print(f"{k}: {src} -> {dst}", flush=True)
                migrate_kind(k, src, dst, q, emb, args, report)
        if not args.verify and not args.dry_run:
            write_identity(target, model, report, args)
    except Exception as e:  # report what was done before the failure, then fail loud
        report["error"] = f"{type(e).__name__}: {e}"
        rc = 1
    report["embed_calls"] = emb.calls
    print(json.dumps(report, indent=None if args.json else 2, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
