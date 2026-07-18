---
status: Accepted
date: "2026-07-17"
---

# Lifecycle hooks must fail open — never hard-block the host session

## Context

The stack registers Claude Code lifecycle hooks (Stop, PreCompact, UserPromptSubmit, SessionStart)
on the host machine. Some of these events can *block* the host: Claude Code treats a **PreCompact
hook that exits with code 2 as a hard block on compaction**, and `python3` exits 2 when its script
file is missing. On 2026-07-17 a config-repo untrack + pull deleted a box's entire machine-local
deploy layer while the shared `settings.json` kept referencing the now-missing capture script; every
long session that reached its context limit needed to compact, the missing script made the PreCompact
hook exit 2, and the exit-2 hard-block **deadlocked live sessions at "Prompt is too long"** with no
way forward.

## Decision

Hooks are best-effort and must never block, slow, or wedge the host session. The PreCompact capture
command is registered as `wsl.exe … -e bash -lc "python3 … precompact_capture.py || true"`: the
trailing **`|| true`** forces the bash invocation to exit 0 no matter what `python3` does, so a
missing or erroring capture script can never again hard-block compaction. The same fail-open contract
runs across the pipeline — the Stop/PreCompact dispatcher exits 0 immediately after spawning its
detached worker, and the compiled UserPromptSubmit client maps a blocking child exit code 2 to 0.

## Consequences

- Compaction always completes regardless of the capture hook's fate; the worst case is *no memory
  enrichment*, never a blocked session.
- A skew guard at install-verify time backstops the contract by asserting every `settings.json`-
  referenced deployed script exists on disk — catching the missing-script precondition before it can
  bite, since fail-open silences the symptom but not the cause.

## Alternatives considered

Not recorded as a weighed alternative. The exit-2 hard-block was the *incident*, not a chosen design;
the `|| true` fail-open command is the root-cause remediation.

## Related code

- [`install/2-windows-config.ps1`](../../../install/2-windows-config.ps1) — the `|| true` fail-open PreCompact command (registered ~L340-350).
- [`claude-config/precompact_capture.py`](../../../claude-config/precompact_capture.py) — the capture sidecar the command guards.

## Related docs

- [`codex-hooks.md`](../../systems/codex-hooks.md) — the hook pipeline and its fail-open contracts.
- [`compaction-capture-restore.md`](../../flows/compaction-capture-restore.md) — the PreCompact→SessionStart flow and the deadlock class.
