# Changelog

This repo is the PRIMARY source for the agentic-memory-stack product; this file is the
product's version authority as of v1.17.0 (the earlier private-side history is summarized
in the first entries below — full pre-inversion history lives in the maintainer archive).

## v1.17.0 (2026-07-18) — repo-local documentation system

A durable, repo-local documentation system for humans and AI agents, reviewed alongside code.

- **Taxonomy** under `docs/`: `systems/` (per-component deep-dives, renamed from `modular/`),
  `flows/` (cross-system pipeline walkthroughs), `architecture/` (long-lived constraints +
  `decisions/` ADRs), `glossary.md`, and `templates/`. `CLAUDE.md` gains a Documentation map
  and the agent workflow; `AGENTS.md` stays a one-line import shim so the guidance can't drift.
- **Six system docs** and **six flow docs** brought to a shared template with verified source
  maps; a **26-term glossary**; **nine seeded ADRs** recording the load-bearing decisions
  (one-brain rule, fail-open hooks, EmbeddingGemma on llama-swap, Codex as judge/extractor,
  the tier trust model, operator-agnostic sentinels, the offline-first supersession of travel
  mode, and public-repo-primary).
- **Docs gate** (`scripts/ci/check-docs.py`, a new 7th CI job): every relative doc link
  resolves to a real file, no operator-specific value leaks into docs, and every ADR carries
  valid frontmatter (`status`/`date`; `superseded_by` iff `Superseded`).
- The **docs-and-code-must-agree** rule is now explicit: every pull request that changes
  behavior, interfaces, security, data, or operational procedures updates the affected
  documentation in the same change.
- The `.claude-plugin/*` manifests are realigned to the release version (they had drifted
  to 1.15.0).

## v1.16.2 (2026-07-17) — operator-neutral test fixtures + suite repairs

- 25 test files neutralized for the public ship (fixtures self-referential; behavior
  preserved). The PII leak-guard tests now read operator-specific patterns from gitignored
  `scripts/windows/tests/pii-patterns.local.txt` (`.example` ships).
- 4 silently-broken tests repaired: Qdrant byte-body mock discriminators (broken since
  v1.12's UTF-8-bytes fix), the offload-invariant test brought to the 2026-07-14 audited
  semantics, and cwd/hostname-dependent fixtures made hermetic. Full Windows suite 459/0.
- Unit-drift commit-back: `decay-scan.service` ships with the destructive dedup
  `ExecStartPost` DISABLED (2026-07-14 audit), `stack-backup.timer` is DAILY (feeds the
  offline-first replica snapshot), and `mem0.service`'s bind address is operator config
  (`__MEM0_BIND__` ← `MEM0_BIND` in `~/.mem0/stack.env`, default loopback).

## v1.16.0/1 (2026-07-17) — deploy-layer-skew hardening

- **Fail-open PreCompact**: the capture hook command is `python3 … || true` — a missing or
  erroring capture script can never hard-block compaction (exit 2 deadlocked live sessions
  when a config-repo untrack+pull deleted a box's deployed script layer).
- **Distro-agnostic hook emission**: no `-d <distro>` when the stack's distro is the WSL
  default, so a machine-synced `settings.json` stays portable.
- **One-brain role gate**: `-Role brain|replica` (receipt-recorded); replicas never register
  the nightly dream/dedup canonical-mutation tasks and remove stale ones. Role-aware verify.
- **Skew guard**: `3-verify.ps1` asserts every hook-referenced deployed script exists.
- Installer is pwsh-only (loud pre-flight); brands.json privacy split
  (`brands.example.json` template + installer fallback).

## v1.15.0 (2026-07-16) — offline-first memory client

Offline behavior EMERGES from connectivity: reads fail over to a local read-only replica,
mutations queue to an operation-outbox replayed to the authority on reconnect. The replica
can never absorb a write; divergence is impossible by construction.

## Earlier

v0.12 → v1.14: the memory stack's build-out (mem0 + Qdrant + EmbeddingGemma on llama-swap,
hook pipeline, dream consolidator, tier governance, promotion gate, travel mode). See the
docs/ runbooks for the operational history.
