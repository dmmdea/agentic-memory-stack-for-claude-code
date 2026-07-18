---
status: Accepted
date: "2026-07-17"
---

# The public repository is the primary source of truth

## Context

The product previously lived as a private repository that was the source of truth, with the public
repository a **generated mirror** that was never hand-edited. That model let the public tree drift from
or lag the private source, kept the test suites and CI away from what the public showed, and left no
way to contribute against the published code.

## Decision

Invert the repository model. **This public repository is now the primary source of truth**, edited
directly through pull requests; the test suites live here and run in CI, and docs and code are edited
together in one place. The private repository becomes a **maintainer / moat archive** holding material
that is not part of the shipped product — the research fit-analyses behind each design, per-release
build plans, audit records, the faithfulness and retrieval eval harnesses, dependency/upgrade notes,
and historical runbooks — and is **not** mirrored here.

## Consequences

- Docs and code are edited in one tree and must agree in every PR; the docs gate enforces the
  structural floor (relative links resolve, prose stays operator-neutral).
- Because the source is now public and directly edited, **operator-neutral at rest** becomes a hard
  requirement — no real operator values may be committed (see
  [`operator-agnostic-sentinels.md`](./operator-agnostic-sentinels.md)).
- The maintainer archive stays private and unmirrored; nothing in the public docs depends on it, so
  the public set is the maintained current-state authority on its own.

## Alternatives considered

- **Keeping the private-primary, generated-public-mirror model** — not adopted. The record states the
  inversion (public is edited directly; private is the archive) but does not capture a weighed
  rejection rationale beyond the drift and lag the inversion resolves: Not recorded.

## Related code

- [`scripts/ci/check-docs.py`](../../../scripts/ci/check-docs.py) — the docs gate that enforces the operator-neutral-at-rest floor this directly-edited public model requires.

## Related docs

- [`README.md`](../../README.md) — the documentation map's "primary source of truth" statement and the maintainer-archive section.
- [`DEVELOPMENT.md`](../../DEVELOPMENT.md) — development happens here; nothing is generated from an upstream mirror.
