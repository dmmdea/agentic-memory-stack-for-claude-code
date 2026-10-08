"""test_egemma_rollback_prune.py — the rollback-prune gate must SKIP unless mem0 is bound to the NEW
embedding space, and only PRUNE the PREVIOUS space's collections when it is.

WHY this exists (adversarial review HIGH H2, v0.22): egemma-rollback-prune.sh DELETEs the previous
space's Qdrant collections + snapshots — the migration rollback anchor. The original gate was
ARTIFACT-based (embedder dim:768 + the new collection green); both stay GREEN after a documented
rollback (the new collection still exists and its model is still served), so the prune could fire
and destroy the live store out from under a rolled-back stack now writing to the old collections.
The fix made the gate BINDING-based: /health/deep reports the live bound collection_name, and the
gate SKIPs unless it equals the new memories collection.

Generalised for embedding profiles (mem0-server/embedder_profile.py): the binding is now a PAIR —
/health/deep's embed_profile.profile AND collection — and the collections to delete are the OLD
profile's four, named by the module, not by the script. The old hard-coded `memories` GET/DELETE
(a v0.22 leftover that no longer named anything the stack held) is gone.

These tests drive the REAL script (scripts/wsl/egemma-rollback-prune.sh) in its
EGEMMA_PRUNE_DRY_RUN=1 mode (prints DECISION: PRUNE|SKIP, no deletion) against a stub /health/deep
+ Qdrant served from this process — so the gate logic is exercised end-to-end with NO live data at
risk. The real-deletion tests run the same script against the same stub, which records the DELETEs.
Mirrors the project's destructive-shell-script test pattern (test_dpapi_fetch_key.py /
test_stack_restore.py: subprocess + stubs).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import embedder_profile as EP

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "wsl" / "egemma-rollback-prune.sh"

# The stack runs under WSL; bash is required to exercise the shell gate.
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available (run under WSL/Linux)"
)

# The planned migration the script's defaults describe. The names come from the module, never from here.
OLD = EP.PROFILES["egemma-300m"]
NEW = EP.PROFILES["egemma2"]
OLD_COLLECTIONS = (OLD.memories, OLD.entities, OLD.episodes, OLD.wiki)
NEW_COLLECTIONS = (NEW.memories, NEW.entities, NEW.episodes, NEW.wiki)

from _home_isolation import home_env  # noqa: E402


def _make_handler(state: dict):
    """Stub server that mimics mem0 /health/deep and Qdrant /collections/<name>.

    state["bound"] / state["profile"] drive the rollback signal: (new collection, new profile) = healthy,
    (old collection, old profile) = rolled back. Both spaces' collections stay green/full in BOTH cases
    (that is the whole point — only the BINDING changes after a rollback)."""

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def _send(self, obj, code=200):
            # Compact separators to match FastAPI's JSONResponse (the gate greps
            # for the no-space '"dim":768' the real server emits).
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health/deep":
                if state.get("deep_down"):
                    return self._send({"status": "down"}, 503)
                deep = {"ok": True, "collection": state["bound"],
                        "checks": {"embedder": {"ok": state["dim"] == NEW.dims, "dim": state["dim"]}}}
                if state["profile"] is not None:   # a server that predates the profile report says nothing
                    deep["embed_profile"] = {"profile": state["profile"]}
                self._send(deep)
            elif self.path.startswith("/collections/"):
                name = self.path[len("/collections/"):]
                if name in state["collections"]:
                    points, status = state["collections"][name]
                    self._send({"result": {"points_count": points, "status": status}})
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_DELETE(self):
            if self.path.startswith("/collections/"):
                name = self.path[len("/collections/"):]
                state["deleted"].append(name)
                state["collections"].pop(name, None)
                return self._send({"result": True})
            self.send_response(404)
            self.end_headers()

    return _H


def _state(bound=NEW.memories, profile=NEW.name, dim=NEW.dims, new_points=2279, new_status="green",
           old_points=16946, deep_down=False):
    collections = {n: (old_points, "green") for n in OLD_COLLECTIONS}
    collections.update({n: (new_points, new_status) for n in NEW_COLLECTIONS})
    return {"bound": bound, "profile": profile, "dim": dim, "collections": collections, "deleted": [],
            "deep_down": deep_down}


def _run_gate(tmp_path, state, dry_run=True, extra_env=None, with_systemctl=False):
    srv = HTTPServer(("127.0.0.1", 0), _make_handler(state))
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        env = {
            **os.environ,
            "EGEMMA_PRUNE_DRY_RUN": "1" if dry_run else "0",
            "EGEMMA_PRUNE_MEM0_URL": f"http://127.0.0.1:{port}",
            "EGEMMA_PRUNE_QDRANT_URL": f"http://127.0.0.1:{port}",
            "EGEMMA_PRUNE_LOG": str(tmp_path / "prune.log"),
            "EGEMMA_PRUNE_AUDIT_FLAGS": str(tmp_path / "audit-flags.jsonl"),
            "EGEMMA_PRUNE_SNAPSHOT_DIR": str(tmp_path / "snapshots"),
            **home_env(tmp_path, base={}),  # isolate snapshot dir / self-disable from the real box
        }
        for k in [k for k in env if k.startswith("MEM0_")]:
            del env[k]   # the operator's profile / collection overrides must not leak into the gate
        if with_systemctl:
            # the script disables its own timer after a clean prune: a recorder keeps the real user manager out
            b = tmp_path / "bin"
            b.mkdir(exist_ok=True)
            sc = b / "systemctl"
            sc.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{tmp_path / "systemctl.log"}"\n', encoding="utf-8")
            sc.chmod(sc.stat().st_mode | stat.S_IEXEC)
            env["PATH"] = f"{b}{os.pathsep}{env['PATH']}"
        env.update(extra_env or {})
        r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60, env=env)
        return r, tmp_path / "audit-flags.jsonl"
    finally:
        srv.shutdown()
        srv.server_close()


def test_script_exists():
    assert SCRIPT.exists(), f"egemma-rollback-prune.sh not found at {SCRIPT}"


def test_gate_prunes_the_previous_space_when_bound_to_the_new_one(tmp_path):
    """Happy path: mem0 reports the new profile AND is bound to its memories collection -> the gate
    would PRUNE exactly the old profile's four collections (named by embedder_profile)."""
    r, _ = _run_gate(tmp_path, _state())
    assert r.returncode == 0, r.stderr
    assert "DECISION: PRUNE" in r.stdout, f"expected PRUNE, got:\n{r.stdout}"
    would = r.stdout.split("would delete:")[1].split("+ their snapshots")[0].split()
    assert sorted(would) == sorted(OLD_COLLECTIONS), would
    assert not set(would) & set(NEW_COLLECTIONS), "a new-space collection must never be on the list"


def test_gate_skips_on_rollback_to_the_old_space(tmp_path):
    """THE H2 GUARD: mem0 rolled back (bound to the old memories collection, reporting the old
    profile) while the new collections are STILL green/full -> the old artifact-based gate would have
    PRUNED (destroying the rollback anchor); the binding-based gate must SKIP."""
    r, audit = _run_gate(tmp_path, _state(bound=OLD.memories, profile=OLD.name))
    assert r.returncode == 0, r.stderr
    assert "DECISION: SKIP" in r.stdout, f"expected SKIP, got:\n{r.stdout}"
    assert "ROLLBACK DETECTED" in r.stdout
    # the skip is recorded to the audit flags with the binding
    rec = json.loads(audit.read_text().strip().splitlines()[-1])
    assert rec["event"] == "egemma-rollback-prune-skipped"
    assert rec["bound_collection"] == OLD.memories and rec["bound_profile"] == OLD.name


def test_gate_skips_when_the_profile_is_the_old_one_even_though_the_collection_is_right(tmp_path):
    """The profile half of the binding: a server still reporting the OLD profile is not in the new
    space, whatever collection it names (an override can name any collection)."""
    r, _ = _run_gate(tmp_path, _state(bound=NEW.memories, profile=OLD.name))
    assert "DECISION: SKIP" in r.stdout and "ROLLBACK DETECTED" in r.stdout, r.stdout


def test_gate_skips_when_the_collection_is_wrong_even_though_the_profile_is_right(tmp_path):
    r, _ = _run_gate(tmp_path, _state(bound="some_other_collection", profile=NEW.name))
    assert r.returncode == 0, r.stderr
    assert "DECISION: SKIP" in r.stdout and "ROLLBACK DETECTED" in r.stdout


def test_gate_skips_when_the_server_reports_no_profile(tmp_path):
    """A server from before profiles says nothing about its space: fail-safe, never delete on silence."""
    r, _ = _run_gate(tmp_path, _state(profile=None))
    assert "DECISION: SKIP" in r.stdout, r.stdout


def test_gate_skips_when_mem0_does_not_answer(tmp_path):
    r, audit = _run_gate(tmp_path, _state(deep_down=True))
    assert "DECISION: SKIP" in r.stdout and "did not answer" in r.stdout, r.stdout
    assert json.loads(audit.read_text().strip().splitlines()[-1])["event"] == "egemma-rollback-prune-skipped"


def test_gate_skips_when_embedder_unhealthy(tmp_path):
    """Embedder width != the new profile's (mem0 down / wrong embedder) -> SKIP even if bound
    correctly. The dim leg of the gate still fires."""
    r, _ = _run_gate(tmp_path, _state(dim=384))
    assert r.returncode == 0, r.stderr
    assert "DECISION: SKIP" in r.stdout


def test_gate_skips_when_new_collection_thin(tmp_path):
    """New collection below the 1000-point floor -> SKIP (incomplete re-embed)."""
    r, _ = _run_gate(tmp_path, _state(new_points=12))
    assert r.returncode == 0, r.stderr
    assert "DECISION: SKIP" in r.stdout


def test_gate_skips_when_new_collection_is_not_green(tmp_path):
    r, _ = _run_gate(tmp_path, _state(new_status="yellow"))
    assert "DECISION: SKIP" in r.stdout


def test_a_box_that_never_migrated_skips_by_default(tmp_path):
    """No env set: NEW defaults to the planned target. A box still on the old space reports the old
    profile, so the defaults can never prune a store that has not moved."""
    r, _ = _run_gate(tmp_path, _state(bound=OLD.memories, profile=OLD.name))
    assert "DECISION: SKIP" in r.stdout


def test_the_same_profile_on_both_sides_prunes_nothing(tmp_path):
    r, _ = _run_gate(tmp_path, _state(), extra_env={"EGEMMA_PRUNE_OLD_PROFILE": NEW.name})
    assert "DECISION: SKIP" in r.stdout and "nothing to prune" in r.stdout, r.stdout


def test_an_unknown_profile_is_a_skip_not_a_guess(tmp_path):
    r, _ = _run_gate(tmp_path, _state(), extra_env={"EGEMMA_PRUNE_OLD_PROFILE": "no-such-space"})
    assert "DECISION: SKIP" in r.stdout and "could not resolve" in r.stdout, r.stdout


def test_the_profiles_are_parameters(tmp_path):
    """The reverse direction (abandon the new space, keep the old one live) is the same script with
    the profiles swapped: the gate then needs the binding on the OLD profile."""
    env = {"EGEMMA_PRUNE_NEW_PROFILE": OLD.name, "EGEMMA_PRUNE_OLD_PROFILE": NEW.name}
    r, _ = _run_gate(tmp_path, _state(bound=OLD.memories, profile=OLD.name, dim=OLD.dims), extra_env=env)
    assert "DECISION: PRUNE" in r.stdout, r.stdout
    would = r.stdout.split("would delete:")[1].split("+ their snapshots")[0].split()
    assert sorted(would) == sorted(NEW_COLLECTIONS)


def test_an_old_collection_whose_counterpart_is_not_ready_is_kept(tmp_path):
    """Per collection: the old wiki index goes only when the new one exists, is green and holds pages."""
    st = _state()
    st["collections"][NEW.wiki] = (0, "green")
    r, _ = _run_gate(tmp_path, st)
    assert "DECISION: PRUNE" in r.stdout, r.stdout
    would = r.stdout.split("would delete:")[1].split("+ their snapshots")[0].split()
    assert OLD.wiki not in would and OLD.memories in would, r.stdout
    assert OLD.wiki in r.stdout.split("kept:")[1]


def test_a_new_space_name_is_never_deleted_even_when_listed(tmp_path):
    """EGEMMA_PRUNE_OLD_COLLECTIONS is an operator override; a name that is the bound or a new-space
    collection is refused outright, whatever the list says."""
    env = {"EGEMMA_PRUNE_OLD_COLLECTIONS": f"{OLD.wiki} {NEW.memories} {NEW.wiki}"}
    r, _ = _run_gate(tmp_path, _state(), extra_env=env)
    assert "DECISION: PRUNE" in r.stdout, r.stdout
    would = r.stdout.split("would delete:")[1].split("+ their snapshots")[0].split()
    assert would == [OLD.wiki], would


def test_a_real_prune_deletes_only_the_previous_space_and_its_snapshots(tmp_path):
    """Non-dry-run against the stub: the DELETEs are exactly the old profile's four collections (the dead
    hard-coded `memories` is gone), their server-side snapshots go, the new space's snapshots stay, and the
    script disables its own timer once nothing is left."""
    state = _state()
    snaps = tmp_path / "snapshots"
    for c in OLD_COLLECTIONS + NEW_COLLECTIONS:
        d = snaps / c
        d.mkdir(parents=True)
        (d / f"{c}-node-1.snapshot").write_bytes(b"x")
        (d / f"{c}-node-1.snapshot.checksum").write_text("abc")
    r, _ = _run_gate(tmp_path, state, dry_run=False, with_systemctl=True)
    assert r.returncode == 0, r.stderr
    assert "DECISION: PRUNE — done." in r.stdout, r.stdout
    assert sorted(state["deleted"]) == sorted(OLD_COLLECTIONS)
    assert "memories" not in state["deleted"]
    for c in OLD_COLLECTIONS:
        assert not (snaps / c).exists(), f"{c}: snapshots must go with the collection"
    for c in NEW_COLLECTIONS:
        assert (snaps / c / f"{c}-node-1.snapshot").exists(), f"{c}: the live space's snapshots are untouched"
    assert "disable egemma-rollback-prune.timer" in (tmp_path / "systemctl.log").read_text()


def test_a_real_prune_that_keeps_something_leaves_the_timer_armed(tmp_path):
    state = _state()
    state["collections"][NEW.episodes] = (0, "green")
    r, _ = _run_gate(tmp_path, state, dry_run=False, with_systemctl=True)
    assert r.returncode == 0, r.stderr
    assert OLD.episodes not in state["deleted"] and OLD.memories in state["deleted"]
    assert "kept:" in r.stdout
    assert not (tmp_path / "systemctl.log").exists(), "a kept collection means a retry later, not a disabled timer"


def test_a_rolled_back_stack_deletes_nothing_for_real(tmp_path):
    state = _state(bound=OLD.memories, profile=OLD.name)
    r, _ = _run_gate(tmp_path, state, dry_run=False, with_systemctl=True)
    assert "DECISION: SKIP" in r.stdout
    assert state["deleted"] == []


def test_the_script_names_no_collection_of_its_own():
    """The collections come from embedder_profile.py. The old script carried `memories` and
    `mem0_egemma_768` as literals: one of them was dead (the GET and DELETE of `memories`), and the
    other pinned the script to one migration."""
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for literal in ("collections/memories", "mem0_egemma_768", "episodes_egemma_768", "wiki_pages_egemma_768",
                    "mem0_eg2_768", "SNAPDIR=\"$HOME/qdrant-server/snapshots/memories\""):
        assert literal not in code, literal
    assert "embedder_profile" in code
