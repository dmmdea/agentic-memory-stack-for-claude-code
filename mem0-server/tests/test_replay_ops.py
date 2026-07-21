# mem0-server/tests/test_replay_ops.py
from __future__ import annotations
import importlib.util, json
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MOD_PATH = REPO_ROOT / "scripts" / "wsl" / "replay-ops.py"

@pytest.fixture()
def ro():
    try:
        spec = importlib.util.spec_from_file_location("replay_ops_ut", MOD_PATH)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    except Exception as e:
        pytest.skip(f"import needs httpx: {e}")
    return mod

def test_adds_replay_before_mutations_and_ledger_dedups(ro, tmp_path, monkeypatch):
    ob = tmp_path / "outbox.jsonl"
    entries = [
        {"op": "promote", "args": {"memory_id": "m1", "tier": "stable"}, "key": "k-prom"},
        {"op": "add", "args": {"text": "x", "user_id": "u", "infer": False, "metadata": {}}, "key": "k-add"},
    ]
    ob.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    order = []
    class R:  # fake response
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {}
    monkeypatch.setattr(ro, "dispatch", lambda op, args: (order.append(op) or R()))
    monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)
    stats = ro.replay(ob, "http://authority.invalid", "test-key")
    assert order == ["add", "promote"]           # adds first
    assert stats["replayed"] == 2
    # second run: simulate an interrupted replay re-queueing the SAME entries (same keys);
    # the ledger must dedup them — dispatch is never called again.
    ob.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    stats2 = ro.replay(ob, "http://authority.invalid", "test-key")
    assert stats2["replayed"] == 0
    assert order == ["add", "promote"]           # ledger-skip: no new dispatch calls

def test_in_batch_duplicate_keys_apply_once(ro, tmp_path, monkeypatch):
    # A crash between the rotating-file fold-in and its unlink can duplicate a whole batch;
    # the same key appearing twice in one replay must dispatch exactly once.
    ob = tmp_path / "outbox.jsonl"
    e = {"op": "delete", "args": {"memory_id": "m1"}, "key": "k-dup"}
    ob.write_text(json.dumps(e) + "\n" + json.dumps(e) + "\n", encoding="utf-8")
    calls = []
    class R:
        status_code = 200
        def raise_for_status(self): pass
    monkeypatch.setattr(ro, "dispatch", lambda op, args: (calls.append(op) or R()))
    monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)
    stats = ro.replay(ob, "http://authority.invalid", "test-key")
    assert calls == ["delete"]                   # applied exactly once
    assert stats["replayed"] == 1

def test_stranded_replaying_entries_retried_without_live_outbox(ro, tmp_path, monkeypatch):
    ob = tmp_path / "outbox.jsonl"  # never created — only the kept-from-transient file exists
    (tmp_path / "outbox.replaying.jsonl").write_text(
        json.dumps({"op": "delete", "args": {"memory_id": "m9"}, "key": "k-stranded"}) + "\n", encoding="utf-8")
    calls = []
    class R:
        status_code = 200
        def raise_for_status(self): pass
    monkeypatch.setattr(ro, "dispatch", lambda op, args: (calls.append(op) or R()))
    monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)
    stats = ro.replay(ob, "http://authority.invalid", "test-key")
    assert calls == ["delete"]
    assert stats["replayed"] == 1

def test_old_format_record_goes_to_conflicts_not_retried_forever(ro, tmp_path, monkeypatch):
    # An old-format record (no 'op' — e.g. written by the retired outbox.py CLI) raises
    # KeyError on dispatch: deterministic, so it must conflict-log, never retry forever.
    ob = tmp_path / "outbox.jsonl"
    ob.write_text(json.dumps({"idempotency_key": "h1", "payload": {"messages": "old"}}) + "\n",
                  encoding="utf-8")
    monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)
    stats = ro.replay(ob, "http://authority.invalid", "test-key")
    assert stats["conflicts"] == 1 and stats["kept"] == 0 and stats["replayed"] == 0
    rec = json.loads((tmp_path / "mutation-conflicts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "reason" in rec and rec["op"] is None
    assert not (tmp_path / "outbox.replaying.jsonl").exists()   # nothing kept for a retry loop

def test_unknown_op_goes_to_conflicts(ro, tmp_path, monkeypatch):
    # dispatch raises ValueError('unknown op: ...') before any network call — deterministic.
    ob = tmp_path / "outbox.jsonl"
    ob.write_text(json.dumps({"op": "frobnicate", "args": {}, "key": "k-unk"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)
    stats = ro.replay(ob, "http://authority.invalid", "test-key")
    assert stats["conflicts"] == 1 and stats["kept"] == 0
    rec = json.loads((tmp_path / "mutation-conflicts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["op"] == "frobnicate" and "unknown op" in rec["reason"]

def test_unparseable_line_preserved_in_conflicts(ro, tmp_path, monkeypatch):
    # A torn line (crash mid-append) must land in the conflict log, not be silently
    # destroyed by the replaying-file rewrite. The good line still replays.
    ob = tmp_path / "outbox.jsonl"
    ob.write_text(json.dumps({"op": "delete", "args": {"memory_id": "m1"}, "key": "k-ok"}) + "\n"
                  + '{"op": "add", "args": {"text": "torn', encoding="utf-8")
    class R:
        status_code = 200
        def raise_for_status(self): pass
    monkeypatch.setattr(ro, "dispatch", lambda op, args: R())
    monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)
    stats = ro.replay(ob, "http://authority.invalid", "test-key")
    assert stats["replayed"] == 1 and stats["conflicts"] == 1
    confs = [json.loads(l) for l in (tmp_path / "mutation-conflicts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(c.get("reason") == "unparseable" and "torn" in c.get("raw", "") for c in confs)

def test_conflict_is_logged_not_dropped(ro, tmp_path, monkeypatch):
    import httpx
    ob = tmp_path / "outbox.jsonl"
    ob.write_text(json.dumps({"op": "delete", "args": {"memory_id": "gone"}, "key": "k1"}) + "\n", encoding="utf-8")
    def boom(op, args):
        req = httpx.Request("DELETE", "http://x/v1/memories/gone")
        raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
    monkeypatch.setattr(ro, "dispatch", boom)
    monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)
    stats = ro.replay(ob, "http://authority.invalid", "test-key")
    assert stats["conflicts"] == 1
    assert (tmp_path / "mutation-conflicts.jsonl").exists()


# --- 2026-07-21: authority resolution + the One-Brain refusal guard --------------------------
# These cover the paths added when the replica was found to be silently losing every write.

@pytest.mark.parametrize("url,expected_local", [
    ("http://127.0.0.1:18791", True),
    ("http://localhost:18791", True),
    ("http://0.0.0.0:18791", True),
    ("http://[::1]:18791", True),
    ("", True),                      # empty
    ("not a url", True),             # malformed -> fails CLOSED (treated as local, refuse)
    ("http://brain-host:18791", False),
    ("https://brain.example.net", False),
])
def test_is_local_url_fails_closed(ro, url, expected_local):
    assert ro._is_local_url(url) is expected_local


def test_replica_refuses_to_replay_into_its_own_loopback(ro, tmp_path, monkeypatch):
    """One-Brain Rule: replaying a replica's outbox into loopback would write the queued
    mutations to its DISPOSABLE local store and ledger them as delivered — they then vanish on
    teardown with nothing reporting a loss. The outbox must survive untouched."""
    ob = tmp_path / "outbox.jsonl"
    rec = {"op": "add", "args": {"text": "x", "user_id": "u"}, "key": "k-1"}
    ob.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    monkeypatch.setattr(ro, "_role", lambda: "replica")
    called = []
    monkeypatch.setattr(ro, "dispatch", lambda op, args: called.append(op))
    monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)

    stats = ro.replay(ob, "http://127.0.0.1:18791", "test-key")

    assert "refused" in stats and stats["replayed"] == 0
    assert called == []                                   # nothing was dispatched
    assert json.loads(ob.read_text().strip()) == rec      # outbox preserved byte-identical


def test_brain_may_replay_into_loopback(ro, tmp_path, monkeypatch):
    """The guard is role-scoped: on the brain, loopback IS the authority, so refusing there
    would break the single-machine install."""
    ob = tmp_path / "outbox.jsonl"
    ob.write_text(json.dumps({"op": "add", "args": {"text": "x"}, "key": "k-2"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(ro, "_role", lambda: "brain")
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {}
    monkeypatch.setattr(ro, "dispatch", lambda op, args: R())
    monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)

    stats = ro.replay(ob, "http://127.0.0.1:18791", "test-key")

    assert "refused" not in stats and stats["replayed"] == 1


def test_unmarked_box_defaults_to_brain(ro, tmp_path, monkeypatch):
    """A box with no ~/.mem0/role is a single-machine install, where loopback is correct."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".mem0").mkdir()
    assert ro._role() == "brain"
