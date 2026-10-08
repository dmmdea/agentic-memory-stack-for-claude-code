"""Driver for test_curation_step_receipts.py: run ONE real curation job with its network faked.

The test starts this as the child of the REAL scripts/wsl/ams-step.sh, so the receipt it reads is the
one the nightly chain would write. Usage: curation_step_driver.py <scenario>. It is not a test (no
test_ prefix) and touches no network and no real host: every HTTP call the job would make is replaced
before the job's own main() runs, and the caller redirects HOME so its state files land in a sandbox.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import types
from pathlib import Path

WSL = Path(__file__).resolve().parents[1]          # scripts/wsl
SERVER = WSL.parents[1] / "mem0-server"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, WSL / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- contradiction sweep

def _sweep(scenario: str) -> int:
    sweep = _load("contradiction_sweep", "contradiction-sweep.py")

    def canonical(cid):
        return {"id": cid, "payload": {"data": "canonical " + cid, "user_id": "u1", "tier": "canonical",
                                       "created_at": "2026-07-01T00:00:00+00:00"},
                "vector": {"": [0.1, 0.2], "bm25": {"indices": [1], "values": [1.0]}}}

    canonicals = [canonical(c) for c in ("c1", "c2", "c3")]
    marker_fail: set = set()
    shim_up = True
    argv = ["--apply", "--judge", "codex"]
    if scenario == "sweep-ok":
        argv += ["--limit", "2"]
    elif scenario == "sweep-no-op":
        shim_up = False
    elif scenario == "sweep-marker-failed":
        marker_fail = {"c2"}
    elif scenario == "sweep-chain-no-op-then-ok":
        # the operator chose the scope (--user-id ""), so zero canonicals is the quiet no-op; the
        # chained re-judge pass then finds nothing to do and finishes ok
        canonicals = []
        argv += ["--user-id", "", "--then-rejudge-stamped"]
    elif scenario == "sweep-chain-failed-then-ok":
        marker_fail = {"c2"}
        argv += ["--then-rejudge-stamped"]
    else:
        raise SystemExit(f"unknown sweep scenario {scenario}")

    sweep._codex = types.SimpleNamespace()
    sweep._preflight_codex_health = lambda: (shim_up, False, {} if shim_up else {"error_type": "unreachable"})
    sweep.httpx.get = lambda *a, **k: types.SimpleNamespace(raise_for_status=lambda: None)
    sweep._api_key_or_raise = lambda: "k"
    sweep.ams_env.user_id = lambda: "u1"
    sweep.scroll_canonicals = lambda http, user_id=None: list(canonicals)
    sweep.scroll_stamped = lambda http: []
    sweep.query_similar = lambda *a, **k: []
    sweep.stamp_candidate = lambda *a, **k: True
    sweep.mark_canonical_checked = lambda http, cid, ts: cid not in marker_fail
    sweep.append_review_queue = lambda path, rec: True
    sweep.prune_stale_review_entries = lambda http, path: 0
    return sweep.main(argv)


# --------------------------------------------------------------------------- semantic dedup

def _point(pid, axis, wobble=0.0, tier="evidence", created="2026-08-01T00:00:00+00:00", user="u1"):
    vec = [0.0] * 8
    vec[axis] = 1.0
    vec[(axis + 1) % 8] = wobble
    return {"id": pid, "vector": {"": vec, "bm25": {"indices": [1, 5], "values": [0.5, 0.25]}},
            "payload": {"tier": tier, "created_at": created, "source": "l1a-extractor", "user_id": user,
                        "workspace": "ws", "project": "p", "data": "text " + pid}}


def _dedup(scenario: str) -> int:
    sd = _load("semantic_dedup", "semantic-dedup.py")
    httpx = sd.httpx
    refuse = scenario == "dedup-refused"

    def handler(request):
        if request.method == "DELETE" and refuse:
            return httpx.Response(500, json={})
        if request.url.path == "/health/deep":
            # the server is bound to the collection the job scans (the binding guard's happy path)
            return httpx.Response(200, json={"collection": sd.COLLECTION})
        return httpx.Response(200, json={})

    real_client = httpx.Client
    httpx.Client = lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    httpx.get = lambda *a, **k: httpx.Response(200, request=httpx.Request("GET", "http://sandbox"))
    old, new = "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"
    if scenario == "dedup-compared-0":
        # a large corpus in which no two records share a partition: nothing can be compared, which
        # is the failure this job hid for weeks (scanned 16k, compared 0, outcome ok)
        corpus = [_point(f"p{i}", i % 8, user=f"user-{i}") for i in range(1001)]
    else:
        corpus = [_point("e1-old", 0, created=old), _point("e1-new", 0, 0.01, created=new),
                  _point("e2-old", 2, created=old), _point("e2-new", 2, 0.01, created=new),
                  _point("c-old", 4, tier="canonical", created=old),
                  _point("c-new", 4, 0.01, tier="canonical", created=new)]
    sd.scroll_all_with_vectors = lambda: corpus
    return sd.main(["--max-deletions", "50"])


# --------------------------------------------------------------------------- episodic reconcile

def _episodic(scenario: str) -> int:
    sys.path.insert(0, str(SERVER))
    from episodic import _connect_to, init_schema      # the REAL ledger schema

    recon = _load("episodic_reconcile", "episodic-reconcile.py")
    db = Path.home() / ".mem0" / "episodic.db"
    conn = _connect_to(db)
    init_schema(conn)
    stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)).isoformat()
    conn.execute("INSERT INTO sessions (session_id, started_at) VALUES ('s1', ?)", (stale,))
    conn.execute("INSERT INTO episodes (id, session_id, started_at, ended_at, goal_text, summary_text, state) "
                 "VALUES (1, 's1', ?, ?, '', ?, 'in_progress')", (stale, stale, "x" * 80))
    conn.commit()
    conn.close()
    recon.embedding_coverage = lambda conn, http: {"eligible": 1000, "embedded": 400, "missing": 600}
    recon._load_backfill = lambda: types.SimpleNamespace(
        run=lambda limit=None, db_path=None: {"embedded": 7, "skipped": 0, "errors": 0,
                                              "remaining": 0, "total_complete": 7})
    recon.history_deleted_ids = lambda ids, db_path=None: set()
    recon.history_delete_row_count = lambda db_path=None: 1
    recon.ledger_deleted = lambda ids, ledger_dir=None: {}
    recon.httpx.get = lambda url, **kw: types.SimpleNamespace(raise_for_status=lambda: None)
    recon.qdrant_present_ids = lambda http, ids: set()
    sys.argv = ["episodic-reconcile.py", "--db", str(db)]
    return recon.main()


def _episodic_upkeep(scenario: str) -> int:
    """`episodic-reconcile.py --upkeep` (the daily episode-upkeep step) over a ledger with one stale checkpoint,
    its vector backfill faked. Nothing may touch Qdrant or the embedder directly: httpx.get raises."""
    sys.path.insert(0, str(SERVER))
    from episodic import _connect_to, init_schema      # the REAL ledger schema

    recon = _load("episodic_reconcile", "episodic-reconcile.py")
    db = Path.home() / ".mem0" / "episodic.db"
    conn = _connect_to(db)
    init_schema(conn)
    stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)).isoformat()
    conn.execute("INSERT INTO sessions (session_id, started_at) VALUES ('s1', ?)", (stale,))
    conn.execute("INSERT INTO episodes (id, session_id, started_at, ended_at, goal_text, summary_text, state) "
                 "VALUES (1, 's1', ?, ?, '', ?, 'in_progress')", (stale, stale, "x" * 80))
    conn.commit()
    conn.close()
    if scenario == "episodic-upkeep-ok":
        result = {"embedded": 3, "skipped": 0, "errors": 0, "remaining": 0, "total_complete": 3,
                  "missing": 3, "missing_ids": [4, 5, 6], "remaining_ids": []}
    elif scenario == "episodic-upkeep-embedder-down":
        result = {"embedded": 0, "skipped": 0, "errors": 0, "remaining": 4, "total_complete": 4, "missing": 4,
                  "missing_ids": [4, 5, 6, 7], "remaining_ids": [4, 5, 6, 7], "aborted": "embedder-down"}
    else:
        raise SystemExit(f"unknown episodic scenario {scenario}")

    def no_network(*a, **k):
        raise AssertionError("the upkeep must not call httpx directly (Qdrant gate / embedder probe)")

    recon._load_backfill = lambda: types.SimpleNamespace(run=lambda limit=None, db_path=None, **kw: dict(result))
    recon.httpx.get = no_network
    sys.argv = ["episodic-reconcile.py", "--db", str(db), "--upkeep"]
    return recon.main()


def main() -> int:
    scenario = sys.argv[1]
    if scenario.startswith("episodic-upkeep"):
        return _episodic_upkeep(scenario)
    if scenario.startswith("sweep-"):
        return _sweep(scenario)
    if scenario.startswith("dedup-"):
        return _dedup(scenario)
    if scenario.startswith("episodic-"):
        return _episodic(scenario)
    raise SystemExit(f"unknown scenario {scenario}")


if __name__ == "__main__":
    sys.exit(main())
