---
status: Accepted
date: "2026-11-05"
---

# The judge provider is selectable per box: the Codex CLI or the Claude API

## Context

[Codex as judge and extractor](codex-as-judge-and-extractor.md) routed every extraction,
consolidation and judgment call to the Codex CLI on a ChatGPT login. The reason was the
interactive Claude login: it allows one session at a time, so a headless call made by a hook
collides with the session the operator is working in. That ADR ruled a paid API key out of scope.

Two things changed. The stack's whole LLM workload turned out to be small: measured over a
month, one PC's extraction is roughly a thousand short calls, and the authority's nightly and
weekly jobs are fewer and larger. At current API prices that is a few dollars a month, with the
bounded jobs on the small model. And an operator may not want to keep a second subscription only
to run background jobs.

The single-session constraint belongs to the interactive login, not to the API. A call made with
an API key shares nothing with the operator's session.

## Decision

The judge provider is a per-box setting, `MEM0_JUDGE_PROVIDER`:

- `codex` stays the default. It needs no key and has no API spend.
- `anthropic` runs the same jobs on the Claude API, on a key the operator supplies.

The provider is a dispatch inside the two functions every LLM call already passes through
(`codex_shim_client.judge()` and `Invoke-CodexSubagent`), so no job changes. A job names a class,
`synthesis` or `classify`, and the provider maps the class to a model.

Four rules come with the API key:

1. The key is a file or a systemd credential, read at call time. It is never an environment
   variable.
2. Estimated spend is written to a ledger by the transport itself and capped per box per month.
3. A missing key is an error. The stack never falls back to the other provider on its own.
4. Local models still never judge.

## Consequences

- A box can run the whole stack with one subscription and one API key, or with no API key at all.
- There is real, metered spend on the `anthropic` setting. The cap bounds it per box, and the
  estimate can differ from the bill.
- Two transports now exist per language, and the price table lives in two files that must agree.
- Extraction and insight wording changes with the model. The trust model, the gates and the tier
  rules do not.
- The Windows HTTP shim is needed only for the Codex provider on a WSL-hosted brain.
- Names that say `codex` (functions, a flag value, a ledger, a log component) stay, and now mean
  "the configured LLM judge". Renaming them would touch every caller for no change in behavior.

## Alternatives considered

- **Replace Codex with the Claude API.** Rejected: it would force a paid key on every install,
  and the default should cost nothing beyond what the operator already has.
- **Headless `claude --print` on the interactive login.** Rejected for the reason the original
  ADR gives: it collides with the operator's own session.
- **A local model as judge.** Rejected, unchanged: the judgment quality the gates rely on is not
  there, and the trust model depends on it.
- **A provider-neutral proxy in front of both.** Rejected: one more service to run, to do a
  dispatch that fits in a function.
- **The key in an environment variable or in `stack.env`.** Rejected: every child process
  inherits the environment, a coding agent that sees the variable stops using its own login, and
  `stack.env` is a settings file that scripts source and logs quote.

## Related code

- [`mem0-server/anthropic_judge.py`](../../../mem0-server/anthropic_judge.py)
- [`mem0-server/codex_shim_client.py`](../../../mem0-server/codex_shim_client.py)
- [`scripts/windows/memory-common.ps1`](../../../scripts/windows/memory-common.ps1)
- [`systemd/judge-anthropic.conf`](../../../systemd/judge-anthropic.conf)
- [`install/linux-authority.sh`](../../../install/linux-authority.sh), [`install.ps1`](../../../install.ps1)

## Related docs

- [Judge provider](../../systems/judge-provider.md)
- [Codex as judge and extractor](codex-as-judge-and-extractor.md)
- [Key custody](../../systems/key-custody.md)
