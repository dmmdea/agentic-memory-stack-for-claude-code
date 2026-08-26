"""The auto-memory migration exemption in scripts/wsl/semantic-dedup.py.

The compactor migrates a fact out of a workspace auto-memory index into mem0 and then removes
the index line AND the fact file, so the mem0 record becomes the only live copy. Dedup deletes
the NEWER side of a near-duplicate pair, and a migration is always the newer side (the L1a
extractor has usually already captured the same fact from a session transcript). Without the
exemption, a fact verified as migrated at 05:02 is gone by 04:30 the next morning.

These tests pin the decision, not the plumbing: which of the two records survives.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "semantic_dedup", REPO_ROOT / "scripts" / "wsl" / "semantic-dedup.py")
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)


def test_migration_source_is_protected():
    assert sd._is_migration_protected({"source": "automemory:g--ws/some-fact.md"}) is True


def test_ordinary_sources_are_not_protected():
    for src in ("l1a-extractor", "dream-consolidator", "user-direct", "", None):
        assert sd._is_migration_protected({"source": src}) is False
    assert sd._is_migration_protected({}) is False


def test_protection_is_prefix_anchored_not_substring():
    # A source that merely mentions the word must not inherit the exemption.
    assert sd._is_migration_protected({"source": "notautomemory:x"}) is False
    assert sd._is_migration_protected({"source": "session-automemory:x"}) is False


def _decide(p_older, p_newer):
    """Mirror of the keep/delete branch in _run(): returns 'delete-newer',
    'delete-older' (the pair was swapped) or 'skip'."""
    if p_older.get("tier") == "canonical":
        return "delete-older"
    if sd._is_migration_protected(p_newer):
        if sd._is_migration_protected(p_older):
            return "skip"
        return "delete-older"
    return "delete-newer"


def test_migrated_record_survives_against_an_l1a_near_duplicate():
    older = {"source": "l1a-extractor", "tier": "evidence"}
    newer = {"source": "automemory:g--ws/store-backend-parity.md", "tier": "evidence"}
    assert _decide(older, newer) == "delete-older"


def test_two_migrations_delete_neither():
    older = {"source": "automemory:g--ws/a.md", "tier": "evidence"}
    newer = {"source": "automemory:g--ws/b.md", "tier": "evidence"}
    assert _decide(older, newer) == "skip"


def test_canonical_still_wins_over_a_migration():
    older = {"source": "user-direct", "tier": "canonical"}
    newer = {"source": "automemory:g--ws/a.md", "tier": "evidence"}
    assert _decide(older, newer) == "delete-older"


def test_unrelated_pairs_keep_the_old_behaviour():
    older = {"source": "l1a-extractor", "tier": "evidence"}
    newer = {"source": "dream-consolidator", "tier": "evidence"}
    assert _decide(older, newer) == "delete-newer"
