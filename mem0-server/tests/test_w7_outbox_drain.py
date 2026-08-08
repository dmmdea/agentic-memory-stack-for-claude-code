"""W7 AMS-28/48: the outbox drainer must not abandon retryable writes.

The finding: replay-ops treated EVERY HTTPStatusError as terminal, so a 503
(the authority still coming up — precisely the condition the outbox exists
for) routed the queued write into mutation-conflicts.jsonl, which nothing
alarms on. These pins exercise the real drain loop against a mock transport.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import uuid
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "wsl" / "replay-ops.py"

# replay-ops reads ~/.mem0/api-key AT IMPORT, so a clean CI runner explodes on
# module load (caught by CI, not locally, where the file exists). Import it
# against a throwaway HOME holding a dummy key — that keeps these pins GATING
# in CI instead of skipping the whole file.
_tmp_home = tempfile.mkdtemp(prefix="w7-outbox-home-")
(Path(_tmp_home) / ".mem0").mkdir(parents=True, exist_ok=True)
(Path(_tmp_home) / ".mem0" / "api-key").write_text("ci-dummy-key",
                                                   encoding="utf-8")
_saved_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
os.environ["HOME"] = _tmp_home
os.environ["USERPROFILE"] = _tmp_home
try:
    _spec = importlib.util.spec_from_file_location("replay_ops", SCRIPT)
    ro = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ro)
finally:
    for _k, _v in _saved_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


def _drain(monkeypatch, outbox: Path) -> dict:
    """The module's entry point is replay(outbox, authority, key); the
    reachability probe and role are stubbed so the test exercises the DRAIN
    loop (mirrors test_replay_ops' setup)."""
    monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)
    monkeypatch.setattr(ro, "_role", lambda: "brain")
    return ro.replay(outbox, "http://authority.invalid", "test-key")


def _outbox(tmp_path: Path, n: int = 3) -> Path:
    ob = tmp_path / "outbox.jsonl"
    with ob.open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({
                "op": "add", "key": str(uuid.uuid4()),
                "args": {"messages": f"queued fact {i}", "user_id": "u"},
            }) + "\n")
    return ob


def test_ams28_retryable_status_keeps_the_write_and_stops_the_drain(
        tmp_path, monkeypatch):
    calls = {"n": 0}

    def dispatch(op, args):
        calls["n"] += 1
        req = httpx.Request("POST", "http://authority/v1/memories")
        resp = httpx.Response(503, request=req, headers={"retry-after": "30"})
        raise httpx.HTTPStatusError("service unavailable", request=req,
                                    response=resp)

    monkeypatch.setattr(ro, "dispatch", dispatch)
    ob = _outbox(tmp_path, 3)
    stats = _drain(monkeypatch, ob)

    assert stats["kept"] == 3, "queued writes were abandoned on a 503"
    assert stats["conflicts"] == 0
    assert stats["stopped_retryable"]["status"] == 503
    assert stats["stopped_retryable"]["retry_after"] == "30"
    assert calls["n"] == 1, "the drain kept hammering after a retryable failure"
    conflicts = tmp_path / "mutation-conflicts.jsonl"
    assert not conflicts.exists() or conflicts.read_text().strip() == ""
    # the records survive, in order, for the next cycle
    replaying = tmp_path / "outbox.replaying.jsonl"
    assert len(replaying.read_text(encoding="utf-8").splitlines()) == 3


def test_ams28_terminal_status_still_conflicts(tmp_path, monkeypatch):
    """A 409/400 can never succeed on retry — it must still be conflict-logged
    (keeping it would loop forever)."""
    def dispatch(op, args):
        req = httpx.Request("POST", "http://authority/v1/memories")
        resp = httpx.Response(409, request=req)
        raise httpx.HTTPStatusError("conflict", request=req, response=resp)

    monkeypatch.setattr(ro, "dispatch", dispatch)
    ob = _outbox(tmp_path, 2)
    stats = _drain(monkeypatch, ob)
    assert stats["conflicts"] == 2 and stats["kept"] == 0
    assert stats["conflicts_total"] == 2


def test_ams28_conflict_depth_is_surfaced_in_stats(tmp_path, monkeypatch):
    """A non-empty conflicts file is an abandoned write — its depth must ride
    the stats every caller prints, so it cannot sit unnoticed."""
    (tmp_path / "mutation-conflicts.jsonl").write_text(
        json.dumps({"op": "add", "reason": "old"}) + "\n", encoding="utf-8")

    monkeypatch.setattr(ro, "dispatch", lambda op, args: None)
    ob = _outbox(tmp_path, 1)
    stats = _drain(monkeypatch, ob)
    assert stats["replayed"] == 1
    assert stats["conflicts_total"] == 1


def test_ams28_happy_path_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(ro, "dispatch", lambda op, args: None)
    ob = _outbox(tmp_path, 4)
    stats = _drain(monkeypatch, ob)
    assert stats["replayed"] == 4 and stats["kept"] == 0
    assert not (tmp_path / "outbox.replaying.jsonl").exists()


def test_ams48_watcher_logs_the_drain_and_runs_the_deployed_copy():
    """The watcher used to send the drain's output to Out-Null and execute the
    CHECKOUT copy — no trace of an abandoned write, and a stale worktree could
    be what ran."""
    src = (REPO_ROOT / "scripts" / "travel" / "offline-watcher.ps1").read_text(
        encoding="utf-8")
    i = src.find("replay-ops.py")
    assert i != -1
    window = src[max(0, i - 400):i + 400]
    assert "apps/mem0-scripts/replay-ops.py" in window, \
        "the watcher still runs the checkout copy, not the deployed one"
    assert "outbox-drain.log" in window
    assert "| Out-Null" not in window.split("replay-ops.py")[0][-200:], \
        "the drain output is still discarded"
