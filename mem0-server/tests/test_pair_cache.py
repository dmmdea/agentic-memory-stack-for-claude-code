"""Headless pins for pair_cache (W5 ADOPT-4, review R2 bindings)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pair_cache  # noqa: E402


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def test_roundtrip_and_unordered_contradiction_key(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    k_ab = pair_cache.make_key("contradiction", "codex", "gpt", "v1", "A", "B")
    k_ba = pair_cache.make_key("contradiction", "codex", "gpt", "v1", "B", "A")
    assert k_ab == k_ba  # symmetric predicate -> unordered key
    assert pair_cache.get(k_ab) is None
    assert pair_cache.put(k_ab, True, "contradiction", "codex") is True
    assert pair_cache.get(k_ba) is True


def test_supersession_key_is_ordered(monkeypatch, tmp_path):
    # 'does B supersede A' is direction-dependent: swapped texts MUST miss.
    _isolate(monkeypatch, tmp_path)
    k_ab = pair_cache.make_key("supersession", "codex", "gpt", "v1", "old", "new")
    k_ba = pair_cache.make_key("supersession", "codex", "gpt", "v1", "new", "old")
    assert k_ab != k_ba
    pair_cache.put(k_ab, True, "supersession", "codex")
    assert pair_cache.get(k_ba) is None


def test_prompt_version_and_mode_invalidate(monkeypatch, tmp_path):
    # M5 target: a key that ignores PROMPT_VERSION serves stale verdicts
    # across a prompt change — this goes red if the version leaves the key.
    _isolate(monkeypatch, tmp_path)
    k_v1 = pair_cache.make_key("contradiction", "codex", "gpt", "v1", "A", "B")
    k_v2 = pair_cache.make_key("contradiction", "codex", "gpt", "v2", "A", "B")
    k_local = pair_cache.make_key("contradiction", "local", "gpt", "v1", "A", "B")
    k_model = pair_cache.make_key("contradiction", "codex", "other", "v1", "A", "B")
    assert len({k_v1, k_v2, k_local, k_model}) == 4
    pair_cache.put(k_v1, False, "contradiction", "codex")
    assert pair_cache.get(k_v2) is None
    assert pair_cache.get(k_local) is None
    assert pair_cache.get(k_model) is None


def test_none_and_error_verdicts_never_cached(monkeypatch, tmp_path):
    # R2: a transient judge outage must not suppress judging for 30 days.
    _isolate(monkeypatch, tmp_path)
    k = pair_cache.make_key("contradiction", "codex", "gpt", "v1", "A", "B")
    assert pair_cache.put(k, None, "contradiction", "codex") is False
    assert pair_cache.put(k, "error", "contradiction", "codex") is False
    assert pair_cache.get(k) is None


def test_ttl_expiry_is_a_miss(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    k = pair_cache.make_key("contradiction", "codex", "gpt", "v1", "A", "B")
    old = time.time() - pair_cache.TTL_SECONDS - 60
    pair_cache.put(k, True, "contradiction", "codex", now=old)
    assert pair_cache.get(k) is None       # expired -> miss
    # and a later write prunes the expired row
    k2 = pair_cache.make_key("contradiction", "codex", "gpt", "v1", "C", "D")
    pair_cache.put(k2, True, "contradiction", "codex")
    assert pair_cache.stats()["rows"] == 1


def test_cache_failures_degrade_to_miss(monkeypatch, tmp_path):
    # R2: a broken cache must never fail (or block) the sweep.
    _isolate(monkeypatch, tmp_path)
    def boom():
        raise RuntimeError("locked")
    monkeypatch.setattr(pair_cache, "_connect", boom)
    k = "any"
    assert pair_cache.get(k) is None
    assert pair_cache.put(k, True, "contradiction", "codex") is False
    assert pair_cache.stats() == {"rows": None, "oldest_ts": None}


def test_false_verdict_roundtrips_as_false_not_miss(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    k = pair_cache.make_key("contradiction", "codex", "gpt", "v1", "A", "B")
    pair_cache.put(k, False, "contradiction", "codex")
    assert pair_cache.get(k) is False      # NO verdicts are cacheable too
