"""AMS-01 (P0, audit 2026-08-07): PUT payload carry-over — pure helper.

mem0 2.0.4's ``_update_memory`` rebuilds the Qdrant payload from scratch on
every update: only ``{data, hash, text_lemmatized, created_at, updated_at}``
plus the carried session ids ``{user_id, agent_id, run_id, actor_id, role}``
survive. Every other payload key — source, brand, workspace, project,
retrievable, retired_at, superseded_by, provenance stamps — was silently
destroyed on every PUT, breaking brand isolation and making tombstoned
records resurrectable with a plain API key.

``compute_carryover()`` returns the keys the PUT handler must pass as
``mem.update(metadata=...)`` so preservation happens ATOMICALLY inside the
upsert (no post-hoc restore window). mem0 deepcopies the dict and then
overwrites its own keys, so carryover can never poison the recomputed
``data/hash/text_lemmatized/created_at/updated_at``.

Design decisions (adversarial review 2026-08-07, findings F3/F5 binding):

- **Blind-preserve minus owned keys, not an allowlist.** An allowlist would
  re-create the defect class the moment a new custom key ships (the original
  failure mode: nobody remembered to restore anything but ``tier``). Unknown
  keys survive; that is the documented cost.
- **NLI check-markers are dropped** (``nli_gate_checked_at``,
  ``contradiction_checked_at``, ``contradicts_canonical_pending``): they
  assert "this exact text was judged", and the text just changed. Carrying
  them forward would let any API-key holder evade contradiction re-judging
  forever (the sweep skips already-checked records).
  ``contradicts_canonical`` itself IS preserved — the standing verdict holds
  until a re-judge clears it (fail-closed).
- Caller-supplied metadata is NEVER an input here. Carry-over only
  re-asserts what the record already carried, so the R5 forged-metadata
  surface (``_ADD_FORBIDDEN_META``) stays closed.
"""

# Keys mem0's _update_memory recomputes (data/hash/text_lemmatized/updated_at),
# pins (created_at), or carries from the existing payload itself (session ids).
# Re-supplying them via metadata= would clobber the fresh values or shadow the
# carry-from-existing branch with a stale copy.
MEM0_OWNED_KEYS = frozenset({
    "data", "hash", "text_lemmatized", "created_at", "updated_at",
    "user_id", "agent_id", "run_id", "actor_id", "role",
})

# Stamps meaning "this text was NLI-judged" — must not survive a text change
# (review F3). contradicts_canonical is deliberately NOT here.
NLI_CHECK_MARKERS = frozenset({
    "nli_gate_checked_at", "contradiction_checked_at",
    "contradicts_canonical_pending",
})

DROPPED_KEYS = MEM0_OWNED_KEYS | NLI_CHECK_MARKERS


def compute_carryover(existing_payload):
    """Return the metadata dict a PUT must pass to ``mem.update()``.

    ``existing_payload`` is the record's full Qdrant payload read BEFORE the
    update (``None``/empty → ``{}``). The caller is responsible for the
    fail-closed pre-read: a read *error* must abort the PUT (503), because
    proceeding with an empty carryover IS the AMS-01 wipe.
    """
    if not existing_payload:
        return {}
    return {k: v for k, v in existing_payload.items() if k not in DROPPED_KEYS}
