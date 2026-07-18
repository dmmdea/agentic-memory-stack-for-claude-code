---
status: Accepted
date: "2026-06-16"
---

# Operator-neutral source via sentinel placeholders and an install receipt

## Context

The stack runs across a WSL app directory, systemd units, and a Windows hook layer, each of which
needs values that differ per machine: the WSL username, the Windows username, the WSL distro, the
mem0 bind address, and the chosen repository root. Committing those real values would leak operator
identity into the source and couple the repository to one machine — unacceptable once the repository
is edited directly and shipped publicly.

## Decision

The repository ships **free of real operator values**. Every operator-specific dimension is a
**sentinel** placeholder token — `__WSL_USER__`, `__WIN_USER__`, `__WSL_DISTRO__`, `__MEM0_BIND__`,
`__REPO_ROOT_WSL__` — carried literally in the scripts and systemd units. At deploy time the values
become real in exactly two substitution points: the installer's `Resolve-StackTokens` (a literal,
non-regex replace) and `deploy.sh`'s `sed` block. The install **Receipt**
(`~/.claude/scripts/mem0-stack.config.psd1`, mirrored to `~/.mem0/stack.env`) records the operator's
choices, and runtime scripts read the Receipt instead of hardcoding any handle or path. Nothing
operator-specific is ever committed.

## Consequences

- The repository is PII-free at rest; the docs/CI neutrality gate enforces it on every change.
- R9 parity normalizes the repo text with the same receipt-driven substitution *before* hashing the
  deployed copy, so a legitimate substitution is never mistaken for drift.
- A sentinel token is a four-place contract — repo source, the installer resolver, `deploy.sh`'s
  `sed`, and R9's normalizer must change together, or parity checks report false or missed drift.

## Alternatives considered

Not recorded. The record states the sentinel-plus-Receipt mechanism and its neutrality invariant, not
a weighed-and-rejected design that committed real values.

## Related code

- [`install/2-windows-config.ps1`](../../../install/2-windows-config.ps1) — `Resolve-StackTokens`, the Receipt, and the sentinel tokens it resolves.
- [`scripts/wsl/deploy.sh`](../../../scripts/wsl/deploy.sh) — the `sed` substitution of `__MEM0_BIND__` and the identity/distro/repo sentinels into deployed units.

## Related docs

- [`installer-and-deploy.md`](../../systems/installer-and-deploy.md) — the Sentinel and Receipt mechanics in full.
- [`glossary.md`](../../glossary.md) — Sentinel and Receipt definitions.
