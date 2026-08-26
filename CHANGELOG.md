# Changelog

This repo is the PRIMARY source for the agentic-memory-stack product; this file is the
product's version authority as of v1.17.0 (the earlier private-side history is summarized
in the first entries below — full pre-inversion history lives in the maintainer archive).

## v1.20.0 (2026-08-26) — auto-memory maintenance

The coding-agent harness keeps its own per-workspace file memory — an index of one-line
pointers, injected in full at every session start, plus one fact file per pointer. Nothing
in this stack maintained it. A live store was found at 96% of its hard per-file limit, with
an unindexed fact file no session had ever loaded and an index line pointing at a deleted
file; no job existed that would ever have noticed. This release makes those stores
self-maintaining, in three pillars.

**Lint** (`memory-lint.ps1`, spawned at session start, read-only, 6h throttle): enumerates
every populated store — deduplicating alias directories by canonical path — and recomputes
findings from disk: orphan, dangling link, duplicate slug, over-long line, oversized fact
file, missing frontmatter, near or over a budget. Stateless by design: the finding set is a
handful of items recomputable in milliseconds, and a monotone watermark would have silently
suppressed a defect that was fixed and later recurred. Two findings watch the maintainer
itself — a store above trigger with no run receipt in 48h, and a history repo that has
gained a remote.

**Write-time lint** (`memory-index-write-lint.sh`, PostToolUse on Write/Edit): the harness
warns on its *line* cap, but nothing checked *bytes per line* — which is what fills the byte
budget first (the store above was at 64% of the line cap and 96% of the byte cap). The hook
reports an over-long index line to the agent that just wrote it, in the same turn, so the
bloat is fixed at the source instead of being compacted forever. Advisory; always exits 0.

**Compaction** (`memory-compact.ps1`, new 5:00am task, every role — these stores are
machine-local, unlike the shared corpus): fires at 20,000 B or 160 lines, targets below
17,000 B and 140 lines. Deterministic hygiene first (dangling and duplicate lines removed,
orphans re-indexed from their own frontmatter), then one judge call over the *delta only* —
long lines and migration candidates, never the whole index.

Five guards, one behavioural test each, written so that removing the guard fails the test:

- **Liveness gate + compare-and-swap.** No process locks the index, and a box that sleeps
  runs its catch-up at the next logon — exactly when sessions start. Observed during the
  build: a store grew three entries mid-flight. The job skips a workspace with recent session
  activity, and re-reads the index hash and file set immediately before the swap, aborting on
  any drift. Abort, never roll back: a directory-level revert would clobber the live write.
- **Doctrine is untouchable.** `metadata.type: feedback` is *nested* — a top-level match finds
  nothing, which would have made the rule inert and every standing order eligible for deletion.
  Doctrine is classified deterministically and never even offered to the judge.
- **Strict decrease, seal, blast cap.** A judge edit applies only if it strictly shrinks the
  index past the hygiene baseline (hygiene is correctness and is exempt); each line may be
  rewritten by the judge at most once, ever; no run removes more than a fifth of the lines.
  A rewritten hook must retain an anchor token, so a line cannot be reduced to a label that
  no longer says when to open the file.
- **Write-then-verify migration.** A migrated fact is posted verbatim, tagged
  `source: automemory:<workspace>/<file>`, and read back **by id** with byte equality before
  its line and file are removed. A write returning no id counts as unverifiable and the line
  stays. Verification by semantic search was rejected: ranking top for its own text can be
  satisfied by a pre-existing near-duplicate.
- **Feasibility.** If doctrine alone exceeds the target budget, the job stops and reports
  rather than loosening the hard rule.

Supporting changes: `semantic-dedup.py` now protects auto-memory migrations — it deletes the
newer of a near-duplicate pair, and a migration is always the newer side, so an unguarded run
would have evicted a just-verified fact the next morning; two migrations delete neither, and
canonical still wins. History is a local git repository with its git-dir outside the tree and
no remote, replacing a hand-rolled archive: commits are the audit trail, per-file checkout is
the undo, and lint fails if a remote ever appears. All maintainer state lives outside the
store directory — an in-store archive would have resurfaced removed facts in every agent
search and re-exposed the credential-bearing file that started this work. Two health-check
rows added: store budgets and structural cleanliness (invariants), and maintainer liveness
(recovery) — a registered task proves nothing if it never fires.

## v1.19.0 (2026-08-08) — the hardening-program waves

Waves W1–W5 of the audit-driven hardening program (55 adjudicated findings; see the
audit register). W1: the verification spine — launch-path parity gates, deploy
pre-flight, behaviour-verified fixes on the LAUNCH PATH rather than the repo. W2:
PUT payload carry-over (atomic pre-merge, per-record locks), CP437 mojibake repair
across four stores, the BM25 sparse leg revived with a gating /health/deep canary.
W3: alarm delivery legs — capability manifest, job-liveness surface, drift-guard
cross-run legs, SessionStart heartbeat digest. W4: revive-or-bury — redaction rule
set fixed and widened under one three-runtime fixture, the Codex judgment leg
live-proven after two dead dependencies, the one-brain guard made real, DPAPI docs
truth-pass. W5: retrieval observability (`explain`, `POST /v1/memories/diagnose` +
`memory_diagnose`, `rerank_status`), gap annotations (withheld-family counters +
recall age summary + conditional staleness line), the keyword-recall union leg
(AMS-56: fail-closed on rerank, deliberate path only), per-pair judge cache +
retrieval-pair dry-run, real-query replay harness + deploy-gated retrieval
families, count-only entrance-redaction telemetry, and sparse-leg reboot survival
(durable fastembed cache + bounded sentinel self-heal + pre-reboot cache gate).
Note: the redaction rule set has a fourth copy in SkillOpt on the offline replica —
it adopts the shared fixture on that box's next return.

## v1.18.0 (2026-07-25) — the silent-failure week

Twenty PRs repairing a family of defects that shared one trait: **something stopped working and
nothing said so.** Every one was found by hand or by audit, never by an alarm, because each failed
into a shape indistinguishable from "nothing to do".

### Outages fixed

- **Memory injection was dead on every prompt** (~1000 recorded failures). Claude Code passes a
  hook command with no `args` array to Git Bash, where an unquoted backslash is an escape
  character, so the client's absolute path was shredded and the hook exited 127 — silently. Hooks
  registered *with* an `args` array are exec'd directly and kept working, so the event looked
  healthy throughout.
- **Episodic capture was dead for 9 days**, from two independent causes at once: the hook launcher
  was pinned to a version-stamped WindowsApps PowerShell path that Windows deletes on update, and
  the command strings carried the backslash bug above. Fixing either alone left it dead.
- **The weekly contradiction sweep had never judged anything** in the deployed layout — its
  `sys.path` resolved correctly in the repo but to a non-existent directory once deployed, so the
  Codex bridge import failed and the run exited 0 every week.
- **A replica silently queued every write to the Outbox.** The MCP shim read its authority from an
  environment variable, but `wsl.exe -e` execs directly (no login shell, no `WSLENV`
  pass-through), so the value never arrived and the shim fell back to a dead loopback.
- **The offsite backup was deleting archives.** `robocopy /MIR` mirrors, so every source-side
  retention prune destroyed the offsite copy too; 688 MB existed only offsite and was hours from
  being purged.

### Systemic fixes

- Authority resolution is a per-host file (`~/.mem0/authority-url`), read identically by the shim,
  `replay-ops`, the SessionStart bundle and the offline watcher. The Outbox drains at shim
  startup, and a replica refuses to replay into its own disposable store (One-Brain Rule).
- Throttle arithmetic uses a shell-independent epoch helper: PowerShell 5.1's
  `Get-Date -UFormat %s` is offset by the machine's UTC offset while pwsh 7 is correct, and both
  editions write the same state files.
- The nightly dream throttle is 23h, not 24h — the stamp is written at cycle completion, so a
  strict 24h window against a fixed 03:00 trigger made the dream run every *other* night.
- Installer values **inherit** rather than silently revert: omitting `-AuthorityUrl` or
  `-EvalRootWsl` on a re-run keeps what the box already had.
- Scheduled tasks run windowless through a `wscript` shim and register `Hidden`.
- Verifiers stopped crying wolf — role-aware checks (a replica's local store is *designed* to be
  down while online), a retry-hardened round-trip, and probe timeouts that report a slow CPU model
  as WARN rather than FAIL.

### Guards, so these classes cannot recur silently

- `3-verify` fails when any hook command carries an unquoted backslash path — the check that would
  have caught both hook outages on day one.
- `RegressionGuards.Tests.ps1` pins the throttle constant *and* its behaviour, the epoch helper,
  receipt inheritance, and the bash-safe hook builders. Mutation-tested: reverting each fix turns
  it red.
- The missing-bridge failure is receipt-gated — quiet on a fresh or partial deploy, loud on a box
  that completed an install.
- `check-docs.py` enumerates via `git ls-files`, so local scratch files no longer trip the gate
  while CI behaviour is unchanged.

### Restored from the carve

`_debris_patterns.py` + `conftest.py` (89 live-stack tests could not even be collected) and
`Run-PesterTests.ps1` (documented but never published; two defects fixed in the port — unquoted
`Start-Process -ArgumentList` elements broke on any path containing a space, and a locale-specific
module path).

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
