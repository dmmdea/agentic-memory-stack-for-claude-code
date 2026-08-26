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
    'delete-older' (the pair was swapped) or 'skip'.

    Kept in step with the source by test_decision_mirror_matches_source below.
    """
    if p_newer.get("tier") == "canonical":
        return "skip"
    if sd._is_migration_protected(p_newer):
        if p_older.get("tier") == "canonical" or sd._is_migration_protected(p_older):
            return "skip"
        return "delete-older"
    return "delete-newer"


def test_canonical_is_kept_not_deleted():
    """The canonical branch must move the canonical record to the KEPT side.

    The deletion targets `newer`, so protecting canonical means ending with the canonical on
    `older`. Reachable only if the same-tier filter upstream is ever relaxed - which is exactly
    why the swap direction must be right before someone relaxes it.
    """
    older = {"source": "user-direct", "tier": "canonical"}
    newer = {"source": "l1a-extractor", "tier": "evidence"}
    assert _decide(older, newer) == "delete-newer", "the canonical (older) record must survive"


def test_migration_guard_is_independent_of_the_canonical_branch():
    # As an elif, this pair (older canonical) skipped the migration check entirely.
    older = {"source": "l1a-extractor", "tier": "evidence"}
    newer = {"source": "automemory:g--ws/a.md", "tier": "evidence"}
    assert _decide(older, newer) == "delete-older"


def test_migrated_record_survives_against_an_l1a_near_duplicate():
    older = {"source": "l1a-extractor", "tier": "evidence"}
    newer = {"source": "automemory:g--ws/store-backend-parity.md", "tier": "evidence"}
    assert _decide(older, newer) == "delete-older"


def test_two_migrations_delete_neither():
    older = {"source": "automemory:g--ws/a.md", "tier": "evidence"}
    newer = {"source": "automemory:g--ws/b.md", "tier": "evidence"}
    assert _decide(older, newer) == "skip"


def test_canonical_paired_with_a_migration_deletes_neither():
    # Canonical may never be deleted, and a migration may never be the deleted side either.
    # When both protections apply to opposite halves, the only safe move is to leave the pair.
    older = {"source": "user-direct", "tier": "canonical"}
    newer = {"source": "automemory:g--ws/a.md", "tier": "evidence"}
    assert _decide(older, newer) == "skip"


def test_decision_mirror_matches_source():
    """Guard against this file's `_decide` drifting from the real branch.

    Pins the two structural properties the source must keep: the canonical and migration
    guards are INDEPENDENT ifs (not a chain), and the canonical swap moves the canonical to
    the kept side.
    """
    src = (REPO_ROOT / "scripts" / "wsl" / "semantic-dedup.py").read_text(encoding="utf-8")
    assert "elif _is_migration_protected(p_newer):" not in src, (
        "the migration guard must not be chained off the canonical branch")
    assert "if _is_migration_protected(p_newer):" in src
    assert 'if p_newer.get("tier") == "canonical":\n                        continue' in src, (
        "a canonical record on the newer side must be skipped, never deleted")
    assert "newer, older = older, newer" in src, "the migration swap must still exist"
    # The pre-existing inverted swap on the OLDER-canonical branch must be gone: it moved the
    # canonical record onto the side this job deletes.
    assert 'if p_older.get("tier") == "canonical":\n                        newer, older = older, newer' not in src


def test_unrelated_pairs_keep_the_old_behaviour():
    older = {"source": "l1a-extractor", "tier": "evidence"}
    newer = {"source": "dream-consolidator", "tier": "evidence"}
    assert _decide(older, newer) == "delete-newer"
