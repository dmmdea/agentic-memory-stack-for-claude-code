#!/usr/bin/env python3
"""cp437-repair.py -- AMS-10: one-time deterministic repair of CP437 mojibake.

Audit finding AMS-10: memory text captured through a PowerShell 5.1 boundary
was UTF-8 bytes decoded as code page 437, leaving deterministic multi-character
mojibake sequences in stored records. The capture boundary has since been fixed
at the source; this script repairs the residual damage.

This is a ONE-OFF migration with a receipts discipline: every live write
appends one JSONL receipt line {ts, script, record_id, action, before, after}
(before/after limited to the fields that changed) to a receipts file under
~/.mem0 (env-overridable), so the migration leaves a complete audit trail.

DETECTOR: a character CP437 produces when decoding a UTF-8 LEAD byte
(0xC2-0xF4) immediately followed by a character CP437 produces when decoding a
UTF-8 CONTINUATION byte (0x80-0xBF). Both character classes are built
programmatically at import time from the byte ranges -- NEVER as literal
non-ASCII characters in source, because this file itself crosses the same
Windows-to-WSL encoding boundaries it repairs (non-negotiable).

REPAIR TRANSFORM: each maximal run of non-ASCII characters is re-encoded to
CP437 bytes and strictly re-decoded as UTF-8; runs that do not round-trip are
left untouched. Repaired text no longer matches the detector, so re-runs are
idempotent no-ops.

SCOPE (each phase individually skippable with --apply; all phases in --report):
  (a) Qdrant memory collection    payload data + text_lemmatized; hash, dense
      vector (EmbeddingGemma document prefix) and the sparse bm25 vector are
      recomputed explicitly with fastembed (review F8: never rely on any
      internal recompute path).
  (b) Qdrant episodes collection  payload goal + summary; the summary dense
      vector is re-embedded with the same document-prefix semantics and
      indexing floor as mem0-server/episode_embeddings.py.
  (c) episodic.db                 episodes.goal_text / episodes.summary_text,
      goals.title / goals.description, open_questions.question_text. FTS5
      AFTER UPDATE triggers self-sync -- FTS shadow tables are never touched.
  (d) history.db                  history.new_memory rows.

ORDERING (review F10): run the sqlite phases (c)+(d) BEFORE the episodes
collection phase (b); the memory collection phase (a) requires fastembed.
A single --apply run executes phases in the order (c), (d), (a), (b).

QUOTED-MOJIBAKE PROTECTION (review F9): a candidate whose original text also
matches (?i)cp437|mojibake|encoding|glyph is flagged 'needs-eyeball' in
--report and skipped by --apply unless its record id is named explicitly via
--include-flagged ID,ID,...

CAPTURE-BOUNDARY GATE: the boundary fix must be deployed BEFORE a live repair,
otherwise fresh mojibake keeps arriving mid-migration. The deployed
Windows-side launch-path marker cannot be probed portably from a WSL process,
so the gate cannot self-verify; --apply --live therefore refuses to run under
--require-boundary-marker (the default) until --force is passed as the
operator's attestation that the fix is deployed.

Usage:
  cp437-repair.py                     # --report (default): read-only scan -> JSON report
  cp437-repair.py --report
  cp437-repair.py --apply --dry-run   # zero writes; prints plan + samples
  cp437-repair.py --apply --live --force   # OPERATOR-GATED live repair

Exit codes: 0 success; 2 gating error (bare --apply, boundary gate, bad flag
combination); 3 precondition failure (Qdrant not ready, missing collection or
database file, missing bm25 sparse slot, fastembed/lemmatizer unavailable).
Per-record failures during a live run exit 1.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Environment-driven configuration (same defaults as the sibling scripts)
# ---------------------------------------------------------------------------

QDRANT = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.environ.get("MEM0_COLLECTION", "mem0_egemma_768")
EPISODES_COLLECTION = os.environ.get("MEM0_EPISODES_COLLECTION", "episodes_egemma_768")
EPISODIC_DB = Path(os.environ.get("MEM0_EPISODIC_DB", str(Path.home() / ".mem0" / "episodic.db")))
HISTORY_DB = Path(os.environ.get("MEM0_HISTORY_DB", str(Path.home() / ".mem0" / "history.db")))
EMBED_URL = os.environ.get("MEM0_EMBED_URL", "http://127.0.0.1:11436/v1/embeddings")
EMBED_MODEL = os.environ.get("MEM0_EMBED_MODEL", "embeddinggemma")
REPORT_PATH = Path(os.environ.get("MEM0_CP437_REPORT", str(Path.home() / ".mem0" / "cp437-repair-report.json")))
RECEIPTS_PATH = Path(os.environ.get("MEM0_CP437_RECEIPTS", str(Path.home() / ".mem0" / "cp437-repair-receipts.jsonl")))

SCRIPT_NAME = "cp437-repair"
_SCROLL_PAGE = 256
_SNIPPET_CHARS = 120

# ---------------------------------------------------------------------------
# Detector + repair transform
#
# Character classes are built from byte ranges at import time. cp437 decodes
# every byte 0x00-0xFF to some character, so these decodes never fail. The
# source file stays pure ASCII by construction.
# ---------------------------------------------------------------------------

_CP437_LEAD_CHARS = bytes(range(0xC2, 0xF5)).decode("cp437")
_CP437_CONT_CHARS = bytes(range(0x80, 0xC0)).decode("cp437")
_MOJIBAKE_RE = re.compile(
    "[" + re.escape(_CP437_LEAD_CHARS) + "][" + re.escape(_CP437_CONT_CHARS) + "]"
)
_NON_ASCII_RUN_RE = re.compile(r"[^\x00-\x7f]+")
# Review F9: quoted-mojibake protection -- text that TALKS ABOUT the encoding
# problem may quote mojibake intentionally and needs an operator eyeball.
_FLAG_RE = re.compile(r"(?i)cp437|mojibake|encoding|glyph")


def detect_mojibake(text: Any) -> bool:
    """True when *text* contains at least one CP437 lead+continuation pair."""
    return isinstance(text, str) and bool(_MOJIBAKE_RE.search(text))


def _repair_run(match: re.Match) -> str:
    """Round-trip one maximal non-ASCII run; leave it untouched on any codec
    error (strict in both directions -- no partial rewrites)."""
    run = match.group(0)
    try:
        return run.encode("cp437").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return run


def repair_text(text: str) -> str:
    """Repair every repairable non-ASCII run in *text*. Idempotent: repaired
    output no longer matches the detector."""
    return _NON_ASCII_RUN_RE.sub(_repair_run, text)


def diff_snippet(before: str, after: str) -> dict:
    """First-divergence diff snippet (up to 120 chars each side) for the
    report, so the operator can eyeball each change without raw dumps."""
    limit = min(len(before), len(after))
    at = limit
    for i in range(limit):
        if before[i] != after[i]:
            at = i
            break
    return {
        "at": at,
        "before": before[at:at + _SNIPPET_CHARS],
        "after": after[at:at + _SNIPPET_CHARS],
    }


# ---------------------------------------------------------------------------
# Embedding semantics -- replicated from mem0-server/egemma_embedder.py (the
# authority): EmbeddingGemma document prefix + conservative per-char-class
# token-budget truncation of the EMBEDDING INPUT only (stored text untouched).
# ---------------------------------------------------------------------------

_DOC_PREFIX = "title: none | text: "
_EMBED_TOKEN_BUDGET = 1900
_PREFIX_TOKEN_RESERVE = 16

# Replicated from mem0-server/episode_embeddings.py: summaries below this floor
# are never indexed, so a repair that shrinks a summary under the floor keeps
# the existing vector instead of re-embedding.
_MIN_SUMMARY_CHARS = 64


def _est_char_tokens(ch: str) -> float:
    """Conservative upper-bound token cost per character (see
    egemma_embedder.py for the measured rationale)."""
    o = ord(ch)
    if o < 0x80:
        return 0.9
    if o < 0x400:
        return 1.3
    cat = unicodedata.category(ch)
    if cat.startswith(("L", "S", "P")):
        return 2.3
    return 1.6


def _truncate_for_embedding(text: str,
                            budget: int = _EMBED_TOKEN_BUDGET - _PREFIX_TOKEN_RESERVE) -> str:
    """Truncate the embedding input so the estimated token count (plus prefix
    reserve) stays inside the model context. Storage is never truncated."""
    if not text:
        return text
    total = 0.0
    for i, ch in enumerate(text):
        total += _est_char_tokens(ch)
        if total > budget:
            return text[:i]
    return text


def embed_document(client: httpx.Client, text: str) -> list[float]:
    """Embed *text* as a DOCUMENT (EmbeddingGemma document prefix) against the
    OpenAI-compatible embeddings endpoint. Returns the 768-d vector."""
    prefixed = _DOC_PREFIX + _truncate_for_embedding(text)
    r = client.post(EMBED_URL, json={"model": EMBED_MODEL, "input": prefixed}, timeout=60.0)
    r.raise_for_status()
    return r.json()["data"][0]["embedding"]


_BM25_MODEL = None


def _bm25_model():
    """Lazy singleton for the fastembed bm25 sparse encoder (review F8: the
    sparse vector is always computed explicitly here, never delegated)."""
    global _BM25_MODEL
    if _BM25_MODEL is None:
        from fastembed import SparseTextEmbedding
        _BM25_MODEL = SparseTextEmbedding("Qdrant/bm25")
    return _BM25_MODEL


# ---------------------------------------------------------------------------
# Qdrant helpers (scroll idiom shared with ship_log_reclassify.py / l10-audit.py)
# ---------------------------------------------------------------------------

def qdrant_ready() -> bool:
    """Readyz preflight -- every mode requires a reachable Qdrant."""
    try:
        httpx.get(f"{QDRANT}/readyz", timeout=5.0).raise_for_status()
        return True
    except Exception as e:
        print(f"ERROR: Qdrant readyz preflight failed: {e}", file=sys.stderr)
        return False


def scroll_all_qdrant_points(client: httpx.Client, collection: str, *,
                             with_vector: bool = False) -> list[dict]:
    """Page through every point in *collection* via the Qdrant scroll API."""
    points: list[dict] = []
    next_page: Any = None
    while True:
        body: dict[str, Any] = {
            "limit": _SCROLL_PAGE,
            "with_payload": True,
            "with_vector": with_vector,
        }
        if next_page is not None:
            body["offset"] = next_page
        r = client.post(
            f"{QDRANT}/collections/{collection}/points/scroll",
            json=body,
            timeout=30.0,
        )
        r.raise_for_status()
        result = r.json().get("result", {})
        points.extend(result.get("points", []))
        next_page = result.get("next_page_offset")
        if not next_page:
            break
    return points


def get_collection_info(client: httpx.Client, collection: str) -> dict | None:
    """Collection config, or None when the collection does not exist."""
    try:
        r = client.get(f"{QDRANT}/collections/{collection}", timeout=10.0)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("result", {})
    except httpx.HTTPError as e:
        print(f"ERROR: cannot read collection config for {collection}: {e}", file=sys.stderr)
        return None


def dense_vector_name(info: dict) -> str | None:
    """Resolve the dense vector name from the collection config. An unnamed
    single dense vector is addressed by the default name '' in the named-vector
    upsert form. Returns None when ambiguous (multiple dense vectors)."""
    vectors = ((info.get("config") or {}).get("params") or {}).get("vectors") or {}
    if "size" in vectors:
        return ""
    names = [n for n in vectors if isinstance(vectors[n], dict)]
    if len(names) == 1:
        return names[0]
    return None


def sparse_vector_names(info: dict) -> list[str]:
    sparse = ((info.get("config") or {}).get("params") or {}).get("sparse_vectors") or {}
    return sorted(sparse)


# ---------------------------------------------------------------------------
# Receipts (one-off migration discipline: one JSONL line per live write)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_receipt(record_id: str, action: str, before: dict, after: dict,
                  extra: dict | None = None) -> None:
    RECEIPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {
        "ts": _now_iso(),
        "script": SCRIPT_NAME,
        "record_id": str(record_id),
        "action": action,
        "before": before,
        "after": after,
    }
    if extra:
        rec.update(extra)
    with RECEIPTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Write-precondition probes (checked hard for --apply, reported for --report)
# ---------------------------------------------------------------------------

def _probe_fastembed() -> tuple[bool, str]:
    try:
        import fastembed  # noqa: F401
        return True, ""
    except Exception as e:
        return False, str(e)


def _probe_lemmatizer() -> tuple[bool, str]:
    try:
        from mem0.utils.lemmatization import lemmatize_for_bm25  # noqa: F401
        return True, ""
    except Exception as e:
        return False, str(e)


def memory_write_preconditions(info: dict | None) -> list[str]:
    """Problems that block the memory-collection write path (exit 3)."""
    problems: list[str] = []
    if info is None:
        problems.append(f"memory collection '{COLLECTION}' not found")
        return problems
    name = dense_vector_name(info)
    if name is None:
        problems.append("memory collection has multiple dense vectors; cannot recompute safely")
    sparse = sparse_vector_names(info)
    if "bm25" not in sparse:
        problems.append("memory collection lacks the 'bm25' sparse vector slot")
    extra_sparse = [s for s in sparse if s != "bm25"]
    if extra_sparse:
        problems.append(f"memory collection has extra sparse slots {extra_sparse}; cannot recompute safely")
    ok, err = _probe_fastembed()
    if not ok:
        problems.append(f"fastembed not importable: {err}")
    ok, err = _probe_lemmatizer()
    if not ok:
        problems.append(f"mem0.utils.lemmatization not importable: {err}")
    return problems


# ---------------------------------------------------------------------------
# Phase scans (read-only; shared by --report and --apply)
# ---------------------------------------------------------------------------

_EPISODIC_TARGETS = (
    ("episodes", "id", ("goal_text", "summary_text")),
    ("goals", "id", ("title", "description")),
    ("open_questions", "id", ("question_text",)),
)
_HISTORY_TARGETS = (
    ("history", "id", ("new_memory",)),
)


def scan_memory_phase(client: httpx.Client) -> list[dict]:
    candidates: list[dict] = []
    for pt in scroll_all_qdrant_points(client, COLLECTION):
        payload = pt.get("payload") or {}
        data = payload.get("data") if isinstance(payload.get("data"), str) else ""
        lemma = payload.get("text_lemmatized") if isinstance(payload.get("text_lemmatized"), str) else ""
        data_hit = detect_mojibake(data)
        lemma_hit = detect_mojibake(lemma)
        if not (data_hit or lemma_hit):
            continue
        snippets: dict[str, dict] = {}
        if data_hit:
            fixed = repair_text(data)
            if fixed != data:
                snippets["data"] = diff_snippet(data, fixed)
        if lemma_hit:
            # Preview only: the real value is recomputed at apply time via
            # lemmatize_for_bm25(fixed_data), not by repairing the old lemma.
            preview = diff_snippet(lemma, repair_text(lemma))
            preview["preview_only"] = True
            snippets["text_lemmatized"] = preview
        candidates.append({
            "id": str(pt["id"]),
            "point": pt,
            "data_hit": data_hit,
            "lemma_hit": lemma_hit,
            "flagged": bool(_FLAG_RE.search(data) or _FLAG_RE.search(lemma)),
            "snippets": snippets,
        })
    return candidates


def scan_episodes_phase(client: httpx.Client) -> list[dict]:
    candidates: list[dict] = []
    # Vectors are scrolled too so a goal-only fix can keep the existing dense
    # vector without re-embedding an unchanged summary.
    for pt in scroll_all_qdrant_points(client, EPISODES_COLLECTION, with_vector=True):
        payload = pt.get("payload") or {}
        snippets: dict[str, dict] = {}
        flagged = False
        for field in ("goal", "summary"):
            val = payload.get(field)
            if not isinstance(val, str) or not detect_mojibake(val):
                continue
            fixed = repair_text(val)
            if fixed == val:
                continue
            snippets[field] = diff_snippet(val, fixed)
            if _FLAG_RE.search(val):
                flagged = True
        if snippets:
            candidates.append({
                "id": f"episode:{pt['id']}",
                "point": pt,
                "flagged": flagged,
                "snippets": snippets,
            })
    return candidates


def scan_sqlite_phase(db_path: Path, targets, label: str) -> list[dict]:
    candidates: list[dict] = []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        for table, pk, cols in targets:
            col_list = ", ".join(cols)
            for row in con.execute(f"SELECT {pk}, {col_list} FROM {table}"):
                changes: dict[str, dict] = {}
                flagged = False
                for col in cols:
                    val = row[col]
                    if not isinstance(val, str) or not detect_mojibake(val):
                        continue
                    fixed = repair_text(val)
                    if fixed == val:
                        continue
                    changes[col] = {"before": val, "after": fixed}
                    if _FLAG_RE.search(val):
                        flagged = True
                if changes:
                    candidates.append({
                        "id": f"{label}:{table}:{row[pk]}",
                        "table": table,
                        "pk": pk,
                        "pk_value": row[pk],
                        "changes": changes,
                        "flagged": flagged,
                        "snippets": {
                            col: diff_snippet(ch["before"], ch["after"])
                            for col, ch in changes.items()
                        },
                    })
    finally:
        con.close()
    return candidates


# ---------------------------------------------------------------------------
# Phase applies (dry-run prints the plan; live writes with receipts)
# ---------------------------------------------------------------------------

def _flag_gate(cand: dict, include_flagged: set[str], stats: dict) -> bool:
    """True when the candidate must be skipped by the F9 eyeball gate."""
    if cand["flagged"] and cand["id"] not in include_flagged:
        stats["flag_skipped"] += 1
        print(f"  FLAG-SKIP {cand['id']} (needs-eyeball; pass --include-flagged to process)")
        return True
    return False


def apply_sqlite_phase(db_path: Path, candidates: list[dict], *, live: bool,
                       include_flagged: set[str], stats: dict) -> None:
    # timeout=30: the live server writes history.db on every add/PUT, so
    # "database is locked" mid-repair is a realistic transient, not a bug
    # (diff-review finding 4). Per-record try/except below matches the Qdrant
    # phases' contract: one bad row counts an error and the run continues.
    con = sqlite3.connect(str(db_path), timeout=30) if live else None
    try:
        for cand in candidates:
            if _flag_gate(cand, include_flagged, stats):
                continue
            cols = sorted(cand["changes"])
            stats["planned"] += 1
            if not live:
                print(f"  WOULD-REPAIR {cand['id']} fields={cols}")
                continue
            try:
                # Plain column UPDATE only: the FTS5 AFTER UPDATE triggers keep
                # the shadow tables in sync -- never write to *_fts directly.
                sets = ", ".join(f"{c} = ?" for c in cols)
                params = [cand["changes"][c]["after"] for c in cols] + [cand["pk_value"]]
                con.execute(f"UPDATE {cand['table']} SET {sets} WHERE {cand['pk']} = ?", params)
                con.commit()
            except sqlite3.Error as e:
                stats["errors"] += 1
                print(f"  ERROR {cand['id']}: {e}", file=sys.stderr)
                continue
            write_receipt(
                cand["id"], "cp437-repair",
                {c: cand["changes"][c]["before"] for c in cols},
                {c: cand["changes"][c]["after"] for c in cols},
            )
            stats["repaired"] += 1
            print(f"  REPAIRED {cand['id']} fields={cols}")
    finally:
        if con is not None:
            con.close()


def apply_memory_phase(candidates: list[dict], dense_name: str, *, live: bool,
                       include_flagged: set[str], stats: dict) -> None:
    from qdrant_client import QdrantClient, models
    from mem0.utils.lemmatization import lemmatize_for_bm25

    qc = QdrantClient(url=QDRANT) if live else None
    with httpx.Client() as embed_client:
        for cand in candidates:
            if _flag_gate(cand, include_flagged, stats):
                continue
            payload = dict(cand["point"].get("payload") or {})
            if live:
                # Diff-review finding 1: the scan may be minutes old and this
                # phase upserts a FULL point — writing scan-time state would
                # silently revert any concurrent server write (NLI stamp,
                # PATCH, PUT). Re-read the LIVE payload and repair that; if
                # the live text no longer matches the detector, it is a no-op.
                fresh = qc.retrieve(collection_name=COLLECTION,
                                    ids=[cand["point"]["id"]], with_payload=True)
                if not fresh:
                    stats["noop"] += 1
                    print(f"  GONE {cand['id']} (deleted since scan; skipped)")
                    continue
                payload = dict(fresh[0].payload or {})
            old_data = payload.get("data") if isinstance(payload.get("data"), str) else ""
            old_lemma = payload.get("text_lemmatized")
            new_payload = dict(payload)
            before: dict[str, Any] = {}
            after: dict[str, Any] = {}

            fixed_data = repair_text(old_data)
            if fixed_data != old_data:
                new_payload["data"] = fixed_data
                before["data"] = old_data
                after["data"] = fixed_data
                # Hash recompute matches mem0's own hashing
                # (mem0.memory.main: hashlib.md5(data.encode()).hexdigest()).
                new_hash = hashlib.md5(fixed_data.encode()).hexdigest()
                if "hash" in payload and payload.get("hash") != new_hash:
                    new_payload["hash"] = new_hash
                    before["hash"] = payload.get("hash")
                    after["hash"] = new_hash
            if isinstance(old_lemma, str):
                # Recompute from the fixed data -- the stored lemma is derived
                # state, never repaired in place.
                new_lemma = lemmatize_for_bm25(new_payload.get("data", old_data))
                if new_lemma != old_lemma:
                    new_payload["text_lemmatized"] = new_lemma
                    before["text_lemmatized"] = old_lemma
                    after["text_lemmatized"] = new_lemma

            if new_payload == payload:
                stats["noop"] += 1
                continue
            stats["planned"] += 1
            if not live:
                print(f"  WOULD-REPAIR {cand['id']} fields={sorted(after)} (+dense/bm25 recompute)")
                continue

            try:
                # updated_at is deliberately preserved in new_payload: a
                # mojibake repair is not a semantic update.
                dense = embed_document(embed_client, new_payload.get("data", old_data))
                sparse_source = new_payload.get("text_lemmatized") or new_payload.get("data") or old_data
                emb = next(iter(_bm25_model().embed([sparse_source])))
                sparse = models.SparseVector(
                    indices=[int(i) for i in emb.indices],
                    values=[float(v) for v in emb.values],
                )
                # Last-instant freshness check (diff-review finding 1): the
                # embed calls above take seconds; if the live point changed
                # underneath them, writing our computed state would be a lost
                # update. Cheap re-read, no embed — skip on any divergence
                # (idempotent: the next run picks the record up again).
                recheck = qc.retrieve(collection_name=COLLECTION,
                                      ids=[cand["point"]["id"]], with_payload=True)
                live_now = dict(recheck[0].payload or {}) if recheck else None
                if live_now is None or live_now.get("data") != old_data:
                    stats["raced"] = stats.get("raced", 0) + 1
                    print(f"  RACED {cand['id']} (live point changed during embed; "
                          "skipped — re-run to pick it up)")
                    continue
                # Single upsert carrying BOTH named vectors and the FULL merged
                # payload (review F8). An unnamed dense vector is addressed by
                # the default name '' in the named-vector form.
                qc.upsert(
                    collection_name=COLLECTION,
                    points=[models.PointStruct(
                        id=cand["point"]["id"],
                        vector={dense_name: dense, "bm25": sparse},
                        payload=new_payload,
                    )],
                    wait=True,
                )
            except Exception as e:
                stats["errors"] += 1
                print(f"  ERROR {cand['id']}: {e}", file=sys.stderr)
                continue
            write_receipt(cand["id"], "cp437-repair", before, after,
                          extra={"vectors_recomputed": ["dense", "bm25"]})
            stats["repaired"] += 1
            print(f"  REPAIRED {cand['id']} fields={sorted(after)}")


def apply_episodes_phase(candidates: list[dict], *, live: bool,
                         include_flagged: set[str], stats: dict) -> None:
    from qdrant_client import QdrantClient, models

    qc = QdrantClient(url=QDRANT) if live else None
    with httpx.Client() as embed_client:
        for cand in candidates:
            if _flag_gate(cand, include_flagged, stats):
                continue
            pt = cand["point"]
            payload = dict(pt.get("payload") or {})
            if live:
                # Diff-review finding 1 (same as the memory phase): never
                # upsert scan-time state over a live point. Re-read fresh;
                # a since-repaired/changed point becomes a no-op.
                fresh = qc.retrieve(collection_name=EPISODES_COLLECTION,
                                    ids=[pt["id"]], with_payload=True,
                                    with_vectors=True)
                if not fresh:
                    stats["noop"] += 1
                    print(f"  GONE {cand['id']} (deleted since scan; skipped)")
                    continue
                payload = dict(fresh[0].payload or {})
                pt = {"id": pt["id"], "payload": payload,
                      "vector": fresh[0].vector}
            new_payload = dict(payload)
            before: dict[str, Any] = {}
            after: dict[str, Any] = {}
            for field in ("goal", "summary"):
                val = payload.get(field)
                if not isinstance(val, str) or not detect_mojibake(val):
                    continue
                fixed = repair_text(val)
                if fixed != val:
                    new_payload[field] = fixed
                    before[field] = val
                    after[field] = fixed
            if new_payload == payload:
                stats["noop"] += 1
                continue
            stats["planned"] += 1
            if not live:
                print(f"  WOULD-REPAIR {cand['id']} fields={sorted(after)}")
                continue

            try:
                vector = pt.get("vector")
                reembedded = False
                if "summary" in after:
                    fixed_summary = new_payload.get("summary") or ""
                    # Same floor/prefix rules as episode_embeddings.py: only a
                    # summary at or above the indexing floor is (re-)embedded as
                    # a document; below the floor the existing vector is kept.
                    if len(fixed_summary.strip()) >= _MIN_SUMMARY_CHARS:
                        vector = embed_document(embed_client, fixed_summary)
                        reembedded = True
                if vector is None:
                    stats["errors"] += 1
                    print(f"  ERROR {cand['id']}: no existing vector and summary below "
                          "re-embed floor; skipping", file=sys.stderr)
                    continue
                # Last-instant freshness check (finding 1): skip on divergence
                # during the embed; the next run picks the record up again.
                recheck = qc.retrieve(collection_name=EPISODES_COLLECTION,
                                      ids=[pt["id"]], with_payload=True)
                live_now = dict(recheck[0].payload or {}) if recheck else None
                if live_now is None or any(live_now.get(f) != payload.get(f)
                                           for f in ("goal", "summary")):
                    stats["raced"] = stats.get("raced", 0) + 1
                    print(f"  RACED {cand['id']} (live episode changed during embed; skipped)")
                    continue
                # Episodes collection uses a single unnamed dense vector (see
                # episode_embeddings.ensure_episode_collection), so the plain
                # vector form is used. Full payload preserved.
                qc.upsert(
                    collection_name=EPISODES_COLLECTION,
                    points=[models.PointStruct(id=pt["id"], vector=vector, payload=new_payload)],
                    wait=True,
                )
            except Exception as e:
                stats["errors"] += 1
                print(f"  ERROR {cand['id']}: {e}", file=sys.stderr)
                continue
            write_receipt(cand["id"], "cp437-repair", before, after,
                          extra={"vectors_recomputed": ["dense"] if reembedded else []})
            stats["repaired"] += 1
            print(f"  REPAIRED {cand['id']} fields={sorted(after)}")


# ---------------------------------------------------------------------------
# Report mode
# ---------------------------------------------------------------------------

def _report_candidate(cand: dict) -> dict:
    return {
        "id": cand["id"],
        "flagged": cand["flagged"],
        "fields": sorted(cand["snippets"]),
        "snippets": cand["snippets"],
    }


def run_report() -> int:
    print(f"=== {SCRIPT_NAME} --report (read-only) ===\n")
    if not qdrant_ready():
        return 3

    precondition_failures: list[str] = []
    phases: dict[str, dict] = {}
    all_flagged: list[str] = []

    with httpx.Client() as client:
        info_mem = get_collection_info(client, COLLECTION)
        info_epi = get_collection_info(client, EPISODES_COLLECTION)
        write_problems = memory_write_preconditions(info_mem)

        for name, info, scan in (
            ("memory", info_mem, lambda: scan_memory_phase(client)),
            ("episodes", info_epi, lambda: scan_episodes_phase(client)),
        ):
            if info is None:
                msg = f"collection for phase '{name}' not found"
                phases[name] = {"error": msg, "candidates": []}
                precondition_failures.append(msg)
                continue
            cands = scan()
            phases[name] = {"error": None, "candidates": [_report_candidate(c) for c in cands]}
            all_flagged.extend(c["id"] for c in cands if c["flagged"])

    for name, db_path, targets in (
        ("episodic-db", EPISODIC_DB, _EPISODIC_TARGETS),
        ("history-db", HISTORY_DB, _HISTORY_TARGETS),
    ):
        if not db_path.exists():
            msg = f"database for phase '{name}' not found: {db_path}"
            phases[name] = {"error": msg, "candidates": []}
            precondition_failures.append(msg)
            continue
        try:
            cands = scan_sqlite_phase(db_path, targets, name)
        except sqlite3.Error as e:
            msg = f"phase '{name}' scan failed: {e}"
            phases[name] = {"error": msg, "candidates": []}
            precondition_failures.append(msg)
            continue
        phases[name] = {"error": None, "candidates": [_report_candidate(c) for c in cands]}
        all_flagged.extend(c["id"] for c in cands if c["flagged"])

    report = {
        "generated_at": _now_iso(),
        "script": SCRIPT_NAME,
        "memory_collection": COLLECTION,
        "episodes_collection": EPISODES_COLLECTION,
        "write_preconditions": {"ok": not write_problems, "problems": write_problems},
        "phases": phases,
        "needs_eyeball": sorted(all_flagged),
        "apply_order_note": "run sqlite phases (episodic-db, history-db) before the "
                            "episodes phase (review F10); a full --apply run already "
                            "orders them that way",
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("--- COUNTS ---")
    for name, ph in phases.items():
        if ph["error"]:
            print(f"  {name:<12}: ERROR ({ph['error']})")
        else:
            print(f"  {name:<12}: {len(ph['candidates'])} candidate(s)")
    if all_flagged:
        print("\n--- NEEDS-EYEBALL (F9: quoted-mojibake suspects; --apply skips these "
              "unless named via --include-flagged) ---")
        for rid in sorted(all_flagged):
            print(f"  {rid}")
    if write_problems:
        print("\n--- WRITE-PRECONDITION PROBLEMS (would block --apply, exit 3) ---")
        for p in write_problems:
            print(f"  {p}")
    print(f"\nJSON report written: {REPORT_PATH}")
    print("ZERO writes performed.")
    if precondition_failures:
        print("\nPrecondition failures encountered during the scan:", file=sys.stderr)
        for p in precondition_failures:
            print(f"  {p}", file=sys.stderr)
        return 3
    return 0


# ---------------------------------------------------------------------------
# Apply mode
# ---------------------------------------------------------------------------

def run_apply(args: argparse.Namespace) -> int:
    dry_run: bool = args.dry_run
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"=== {SCRIPT_NAME} --apply ({mode}) ===\n")
    if not qdrant_ready():
        return 3

    include_flagged: set[str] = set()
    if args.include_flagged:
        include_flagged = {x.strip() for x in args.include_flagged.split(",") if x.strip()}

    do_memory = not args.skip_memory
    do_episodes = not args.skip_episodes
    do_episodic_db = not args.skip_episodic_db
    do_history_db = not args.skip_history_db

    # Precondition checks for the ACTIVE phases only (exit 3 on failure).
    problems: list[str] = []
    dense_name = ""
    info_mem = info_epi = None
    with httpx.Client() as client:
        if do_memory:
            info_mem = get_collection_info(client, COLLECTION)
            problems.extend(memory_write_preconditions(info_mem))
            if info_mem is not None:
                dense_name = dense_vector_name(info_mem) or ""
        if do_episodes:
            info_epi = get_collection_info(client, EPISODES_COLLECTION)
            if info_epi is None:
                problems.append(f"episodes collection '{EPISODES_COLLECTION}' not found")
        if do_episodic_db and not EPISODIC_DB.exists():
            problems.append(f"episodic database not found: {EPISODIC_DB}")
        if do_history_db and not HISTORY_DB.exists():
            problems.append(f"history database not found: {HISTORY_DB}")
        if problems:
            print("ERROR: precondition failure(s):", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 3

        # Scans (read-only) for the active phases.
        memory_cands = scan_memory_phase(client) if do_memory else []
        episode_cands = scan_episodes_phase(client) if do_episodes else []

    episodic_cands = scan_sqlite_phase(EPISODIC_DB, _EPISODIC_TARGETS, "episodic-db") if do_episodic_db else []
    history_cands = scan_sqlite_phase(HISTORY_DB, _HISTORY_TARGETS, "history-db") if do_history_db else []

    totals = {"planned": 0, "repaired": 0, "noop": 0, "flag_skipped": 0, "errors": 0}

    def phase_stats() -> dict:
        return {"planned": 0, "repaired": 0, "noop": 0, "flag_skipped": 0, "errors": 0}

    # Execution order per review F10: sqlite phases first, then the memory
    # collection, then the episodes collection.
    plan: list[tuple[str, Any]] = []
    if do_episodic_db:
        plan.append(("episodic-db", lambda s: apply_sqlite_phase(
            EPISODIC_DB, episodic_cands, live=not dry_run,
            include_flagged=include_flagged, stats=s)))
    if do_history_db:
        plan.append(("history-db", lambda s: apply_sqlite_phase(
            HISTORY_DB, history_cands, live=not dry_run,
            include_flagged=include_flagged, stats=s)))
    if do_memory:
        plan.append(("memory", lambda s: apply_memory_phase(
            memory_cands, dense_name, live=not dry_run,
            include_flagged=include_flagged, stats=s)))
    if do_episodes:
        plan.append(("episodes", lambda s: apply_episodes_phase(
            episode_cands, live=not dry_run,
            include_flagged=include_flagged, stats=s)))

    for name, runner in plan:
        print(f"\n--- phase: {name} ---")
        stats = phase_stats()
        runner(stats)
        print(f"  {name}: planned={stats['planned']} repaired={stats['repaired']} "
              f"noop={stats['noop']} flag-skipped={stats['flag_skipped']} "
              f"errors={stats['errors']}")
        for k in totals:
            totals[k] += stats[k]

    print(f"\n=== apply done ({mode}): planned={totals['planned']} "
          f"repaired={totals['repaired']} noop={totals['noop']} "
          f"flag-skipped={totals['flag_skipped']} errors={totals['errors']} ===")
    if dry_run:
        print("ZERO writes performed (dry-run).")
        return 0
    print(f"Receipts appended to: {RECEIPTS_PATH}")
    return 0 if totals["errors"] == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ordering note (review F10): run the sqlite phases (episodic-db, history-db)\n"
            "BEFORE the episodes-collection phase; the memory-collection phase requires\n"
            "fastembed present. A single --apply run already executes phases in the\n"
            "order episodic-db, history-db, memory, episodes.\n\n"
            "Boundary gate: the PowerShell 5.1 capture-boundary fix MUST be deployed\n"
            "before a live repair. That deployment cannot be verified portably from a\n"
            "WSL process, so --apply --live refuses to run under\n"
            "--require-boundary-marker (the default) until --force is passed as the\n"
            "operator's attestation that the boundary fix is live."
        ),
    )
    ap.add_argument("--report", action="store_true", default=False,
                    help="Read-only scan of all four phases -> JSON report (default action)")
    ap.add_argument("--apply", action="store_true", default=False,
                    help="Apply repairs (requires --dry-run or --live; bare --apply exits 2)")
    ap.add_argument("--dry-run", action="store_true", default=False, dest="dry_run",
                    help="With --apply: print the plan, zero writes")
    ap.add_argument("--live", action="store_true", default=False,
                    help="With --apply: perform live mutations (OPERATOR-GATED)")
    ap.add_argument("--skip-memory", action="store_true", default=False,
                    help="Skip the Qdrant memory-collection phase")
    ap.add_argument("--skip-episodes", action="store_true", default=False,
                    help="Skip the Qdrant episodes-collection phase")
    ap.add_argument("--skip-episodic-db", action="store_true", default=False,
                    help="Skip the episodic.db sqlite phase")
    ap.add_argument("--skip-history-db", action="store_true", default=False,
                    help="Skip the history.db sqlite phase")
    ap.add_argument("--include-flagged", default="",
                    help="Comma-separated record ids from the report's needs-eyeball list "
                         "that --apply may process despite the F9 flag")
    ap.add_argument("--require-boundary-marker", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="Require the capture-boundary fix attestation before --apply --live "
                         "(default on; the marker cannot be probed from WSL, so --force is "
                         "the attestation)")
    ap.add_argument("--force", action="store_true", default=False,
                    help="Operator attestation that the capture-boundary fix is deployed; "
                         "unlocks --apply --live under --require-boundary-marker")
    args = ap.parse_args()

    if args.apply:
        if not args.dry_run and not args.live:
            print(
                "ERROR: --apply requires either --dry-run or --live.\n"
                "  --dry-run : safe preview, zero writes\n"
                "  --live    : OPERATOR-GATED live mutation\n"
                "Bare --apply is intentionally blocked to prevent accidental mutation.",
                file=sys.stderr,
            )
            return 2
        if args.dry_run and args.live:
            print("error: pass exactly one of --dry-run / --live", file=sys.stderr)
            return 2
        if args.live and args.require_boundary_marker and not args.force:
            print(
                "ERROR: --apply --live is gated on the capture-boundary fix being deployed.\n"
                "The Windows-side launch-path marker cannot be probed portably from WSL, so\n"
                "pass --force to attest the boundary fix is live (or --no-require-boundary-marker\n"
                "to disable the gate knowingly).",
                file=sys.stderr,
            )
            return 2
        return run_apply(args)

    # --report is the default action
    return run_report()


if __name__ == "__main__":
    sys.exit(main())
