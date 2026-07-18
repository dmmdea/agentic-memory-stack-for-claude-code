"""test_egemma_rollback_prune.py — v0.22 H2/L12: the rollback-prune gate must
SKIP when mem0 has been rolled back (bound to `memories`), and only PRUNE when the
stack is still bound to the new EmbeddingGemma collection.

WHY this exists (adversarial review HIGH H2): egemma-rollback-prune.sh DELETEs the
old nomic `memories` Qdrant collection + snapshots — the migration rollback anchor —
on a one-shot timer (fires 2026-06-21). The original gate was ARTIFACT-based (mem0
embedder dim:768 + the egemma collection green); both stay GREEN after a documented
rollback (the egemma collection still exists and egemma is still served), so the
prune could fire and destroy the live store out from under a rolled-back stack now
writing to `memories`. The fix made the gate BINDING-based: /health/deep reports the
live bound collection_name, and the gate SKIPs unless it equals mem0_egemma_768.

These tests drive the REAL script (scripts/wsl/egemma-rollback-prune.sh) in its
EGEMMA_PRUNE_DRY_RUN=1 mode (prints DECISION: PRUNE|SKIP, no deletion) against a
stub /health/deep + Qdrant served from this process — so the gate logic is exercised
end-to-end with NO live data at risk. Mirrors the project's destructive-shell-script
test pattern (test_dpapi_fetch_key.py / test_stack_restore.py: subprocess + stubs).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "wsl" / "egemma-rollback-prune.sh"

# The stack runs under WSL; bash is required to exercise the shell gate.
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available (run under WSL/Linux)"
)


def _make_handler(bound_collection: str, egemma_points: int, egemma_status: str,
                  embedder_dim: int):
    """Stub server that mimics mem0 /health/deep and Qdrant /collections/<name>.

    bound_collection drives the rollback signal: 'mem0_egemma_768' = healthy,
    'memories' = rolled back. The egemma collection stays green/full in BOTH cases
    (that is the whole point — only the BINDING changes after a rollback)."""

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def _send(self, obj):
            # Compact separators to match FastAPI's JSONResponse (the gate greps
            # for the no-space '"dim":768' the real server emits).
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health/deep":
                self._send({
                    "ok": True,
                    "collection": bound_collection,
                    "checks": {"embedder": {"ok": embedder_dim == 768, "dim": embedder_dim}},
                })
            elif self.path == "/collections/mem0_egemma_768":
                self._send({"result": {"points_count": egemma_points, "status": egemma_status}})
            elif self.path == "/collections/memories":
                self._send({"result": {"points_count": 2165, "status": "green"}})
            else:
                self.send_response(404)
                self.end_headers()

    return _H


def _run_gate(tmp_path, bound_collection, egemma_points=2279, egemma_status="green",
              embedder_dim=768):
    handler = _make_handler(bound_collection, egemma_points, egemma_status, embedder_dim)
    srv = HTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        env = {
            **os.environ,
            "EGEMMA_PRUNE_DRY_RUN": "1",
            "EGEMMA_PRUNE_MEM0_URL": f"http://127.0.0.1:{port}",
            "EGEMMA_PRUNE_QDRANT_URL": f"http://127.0.0.1:{port}",
            "EGEMMA_PRUNE_LOG": str(tmp_path / "prune.log"),
            "EGEMMA_PRUNE_AUDIT_FLAGS": str(tmp_path / "audit-flags.jsonl"),
            "HOME": str(tmp_path),  # isolate snapshot dir / self-disable from the real box
        }
        r = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True, text=True, timeout=60, env=env,
        )
        return r, tmp_path / "audit-flags.jsonl"
    finally:
        srv.shutdown()
        srv.server_close()


def test_script_exists():
    assert SCRIPT.exists(), f"egemma-rollback-prune.sh not found at {SCRIPT}"


def test_gate_prunes_when_bound_to_egemma(tmp_path):
    """Happy path: mem0 still bound to mem0_egemma_768 -> gate would PRUNE."""
    r, _ = _run_gate(tmp_path, bound_collection="mem0_egemma_768")
    assert r.returncode == 0, r.stderr
    assert "DECISION: PRUNE" in r.stdout, f"expected PRUNE, got:\n{r.stdout}"


def test_gate_skips_on_rollback_to_memories(tmp_path):
    """THE H2 GUARD: mem0 rolled back (bound to `memories`) while the egemma
    collection is STILL green/full -> the old artifact-based gate would have
    PRUNED (destroying the rollback anchor); the binding-based gate must SKIP."""
    r, audit = _run_gate(tmp_path, bound_collection="memories")
    assert r.returncode == 0, r.stderr
    assert "DECISION: SKIP" in r.stdout, f"expected SKIP, got:\n{r.stdout}"
    assert "ROLLBACK DETECTED" in r.stdout
    # the skip is recorded to the audit flags with the bound collection
    line = audit.read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["event"] == "egemma-rollback-prune-skipped"
    assert rec["bound_collection"] == "memories"


def test_gate_skips_when_bound_to_unexpected_collection(tmp_path):
    """Any binding other than the expected collection is treated as a rollback/
    misconfiguration -> SKIP (fail-safe; never delete on an unknown binding)."""
    r, _ = _run_gate(tmp_path, bound_collection="some_other_collection")
    assert r.returncode == 0, r.stderr
    assert "DECISION: SKIP" in r.stdout
    assert "ROLLBACK DETECTED" in r.stdout


def test_gate_skips_when_embedder_unhealthy(tmp_path):
    """Embedder dim != 768 (mem0 down / wrong embedder) -> SKIP even if bound
    correctly. The dim:768 leg of the gate still fires."""
    r, _ = _run_gate(tmp_path, bound_collection="mem0_egemma_768", embedder_dim=384)
    assert r.returncode == 0, r.stderr
    assert "DECISION: SKIP" in r.stdout


def test_gate_skips_when_new_collection_thin(tmp_path):
    """New collection below the 1000-point floor -> SKIP (incomplete re-embed)."""
    r, _ = _run_gate(tmp_path, bound_collection="mem0_egemma_768", egemma_points=12)
    assert r.returncode == 0, r.stderr
    assert "DECISION: SKIP" in r.stdout
