# Changelog

This repo is the PRIMARY source for the agentic-memory-stack product; this file is the
product's version authority as of v1.17.0 (the earlier private-side history is summarized
in the first entries below — full pre-inversion history lives in the maintainer archive).

## v1.20.12 (2026-09-03) — hygiene on every store; the liveness row measures live

- Deterministic hygiene (orphan re-index, dangling/duplicate-slug removal) now runs nightly on
  EVERY populated store; only stores over the size trigger go on to the judge, migrations and the
  floor. A small store had carried 7 orphaned facts for days while the lint reported them every
  session and nothing ever fixed them. Clean below-trigger stores write no receipt.
- `Test-MemoryStack`'s maintenance-liveness row measures the live stores instead of the
  SessionStart lint snapshot, which kept a stale size for hours after a remediation.

## v1.20.11 (2026-09-03) — auto-memory: converge under the sync limit, or say so

Live failure: the AI-Ecosystem index reached 27.4 KB with 126 of 180 lines over the cap and the
harness loaded only part of it in another session. The compactor had run nightly and stamped
`applied` while leaving the store at 25,219 B (judge-driven shortening converges ~13 lines a
night, "KEEP is the safe default"), skipped the wake-up catch-up night entirely because dream and
dedup held the codex lock, and retried three 413-oversized migrations forever; the write-time
lint was advisory and ignored.

- Shared deterministic **convergence floor** (`Invoke-AmConvergenceFloor`): at/over the sync
  limit, the longest non-doctrine hooks are truncated to the line cap until the index is under the
  trigger. Doctrine is never touched.
- Compactor: floor runs after the judge (with or without it); a held codex lock skips only the
  judge; bodies over the server cap are never migration candidates; `applied-unconverged` /
  `unconverged` statuses with **exit 1**; receipt gains `floored`.
- Write-time gate: `memory-index-write-gate.ps1` (PS 5.1, Windows-native) replaces the bash
  advisory on PostToolUse — same advisory, plus in-place normalization at the sync limit behind a
  content-hash CAS, receipted.
- `Test-MemoryStack`: the maintenance-liveness row is red when any store is at/over the sync limit
  now or the latest receipt is unconverged; R9 tracks the gate.

## v1.20.10 (2026-09-02) — SessionStart banner fires on open, not on every resume

A context audit over 76 transcripts found the `[agentic-memory-stack]` / `[heartbeat]` /
`[storage-cap]` orientation banner re-emitted on every session *resume* (308 resume fires vs
225 startups, ~1.3 KB each) — the single largest routine SessionStart repetition, because the
installer registered the hook with no matcher. It now registers `startup|clear|compact`:
a fresh or cleared session gets its orientation, a compaction re-reads it into the rebuilt
context, a resume already has it. Pinned by a regression guard so a hand edit to
settings.json is never the only copy.

Same audit, second-largest class: the resident daemon's HK-5 dedupe re-injected unchanged open
goals/questions every 12th prompt; the cadence is now every 25th (the re-inject exists only
as a post-compaction guard, which 25 still serves). Pinned by a regression guard.

## v1.20.9 (2026-09-01) — installer: a WSL path that isn't one is refused, not recorded

The drift guard died silently for 4 nights: an operator ran the Windows config phase from
Git Bash, whose MSYS path conversion rewrote `-EvalRootWsl /mnt/...` into
`C:/Program Files/Git/mnt/...` before pwsh ever saw it. The installer recorded it unchecked;
the dream's drift snapshot became `python C:/Program Files/...` (bash split at the space,
exit 2 every night, "no false alarm" skip every night) while the liveness row kept reading
the stale state sidecar as "guard alive" — only the capability manifest's age check
eventually surfaced it. Two defenses, both keyed on the same form check:

- `2-windows-config.ps1` refuses a resolved `EvalRootWsl` (explicit or inherited) that is
  not an absolute POSIX path, with an error naming MSYS conversion and the
  `MSYS_NO_PATHCONV=1` fix — a poisoned receipt can neither be written nor survive a re-run.
- `Test-MemoryStack.ps1`'s drift-guard liveness row FAILs on a malformed receipt value
  before consulting the state sidecar, so an already-poisoned box alarms on the next health
  run instead of after four quiet nights.

## v1.20.8 (2026-09-01) — outbox: a stopped drain must resume, and never rewind a record

Found live: during an embedder-contention window (llama-swap 429 → server 503) a session's
writes queued to the outbox; the drain stopped on the retryable 503 — correctly keeping the
op — but kept it in `outbox.replaying.jsonl`, which the shim's session-start drain trigger
never looked at. The op sat stranded 15 hours across many session starts while
`outbox_depth` read None. Worse, the op was an update whose target the session had already
re-updated directly: a blind replay would have regressed the record to the older draft.

- shim `_drain_outbox_async`: triggers on a non-empty `outbox.replaying.jsonl` too
  (`replay-ops.py` has always resumed it; only the trigger was blind).
- `replay-ops.py`: superseded-update guard — before dispatching an update, compare the
  record's `updated_at` to the op's `queued_ts`; ops the world moved past go to
  `mutation-conflicts.jsonl` with reason `superseded-by-newer-write` (preserved, never
  dispatched, never dropped). Fail-open for legacy ops without `queued_ts` and on GET
  failures — fail-closed would recreate the stranded class.
- `job_liveness`: `outbox_depth` counts both queue files, so a stranded backlog is visible
  to the offline-outbox capability row instead of reading "unknown".

## v1.20.7 (2026-09-01) — server: no record is born without a tier

A record added without `metadata.tier` (a path the add endpoint's own 403 guidance
recommended) was stored tier-less — and `fetch_current_tier` fail-closes an absent tier to
`canonical` (the H1-race shield), so every mutation of that record demanded the user-direct
HMAC. An agent could create a memory it could never correct or delete; 127 such points
existed live, including a malformed add whose metadata parameter block leaked into the
memory text. `POST /v1/memories` now defaults the tier to `evidence` at birth (after the
canonical/insight gates, before hash-dedup), a live test pins born-tier + deletability, and
`scripts/wsl/tier-backfill.py` stamps the existing stock — skipping and reporting any id the
tier ledgers ever named with canonical/insight history rather than silently demoting it.

## v1.20.6 (2026-08-31) — installer: four fresh-install gaps, all silent

Four bugs filed against a fresh install, each invisible on a long-lived box because the
missing piece had been hand-installed or the failure exited 0:

- **fastmcp was never declared.** The MCP shim runs on the server venv's python and imports
  `fastmcp`, but neither `requirements.txt` nor either installer pip line carried it — a
  fresh install produced an MCP that only ever said "Failed to connect". Now in the floors
  file, both pip branches, and the installer's import-gating post-condition; a regression
  guard pins all three.
- **jq was required but not a prerequisite, and the backup swallowed its absence.** The
  nightly backup parses the Qdrant snapshot name with `jq`; without it the parse was empty
  and the block printed a WARN and exited 0 — the vector collection silently absent from
  every backup while the run reported success. jq is now a phase-0 prerequisite check, and
  every skip/failure path in the Qdrant block sets rc=1 like the local-file blocks always did.
- **3s health probes raced.** `3-verify.ps1`'s Qdrant/mem0/authority liveness probes used a
  single `-TimeoutSec 3` attempt and reported false MISSING right after wsl.exe activity
  while the round-trip check passed in the same run (the search leg was hardened 2026-07-25;
  these were the same defect one section up). Probes now retry once with a 10s timeout.
- **PowerShell platform truth.** `3-verify.ps1` was BOM-less UTF-8 with em-dashes and no
  `#Requires` while the docs promised "PowerShell 5.1+" — under 5.1 it parse-dies mid-file.
  Decision: the installer standardizes on pwsh 7. Phases 2–3 now carry a UTF-8 BOM (so 5.1
  parses them) plus `#Requires -Version 7` (so 5.1 refuses cleanly), phase 0 checks pwsh is
  present, and README/skill docs state the real contract: pwsh 7 for the installer, the
  built-in 5.1 for the deployed hooks.

## v1.20.5 (2026-08-28) — health: a replica is checked against the brain it uses

`Test-MemoryStack.ps1` probed loopback for every mem0/Qdrant row, so the first replica it ran
on reported 14 permanent FAILs for services a replica deliberately keeps dormant. It now
resolves the memory authority the way `3-verify.ps1` does (`~/.mem0/authority-url`, then the
receipt, then loopback) and every shared-store row targets it; on a replica the mutation
probes are skipped (server invariants the brain proves daily; they would also need a
canonical key the replica does not serve), brain-only machinery reports "by design", the
dream/dedup task rows flip polarity (present on a replica = FAIL), and a new `memory
authority (one-brain)` row FAILs a replica pointed at itself. The brain path is unchanged.
Regression guards pin the single loopback literal and the role gates. Proof: the Aorus
replica went 14 FAIL → 0 FAIL (46 PASS, 2 genuine WARNs); the Qube brain run is unchanged.

## v1.20.4 (2026-08-28) — installer: the replica fix, fixed for replicas

v1.20.3 defined the shared `$taskUserId` *inside* the brain-role branch. A replica skips that
branch, so the compactor registration (every role, after the gate) received a null `UserId`
and the replica deploy failed again — while the brain deploy passed, and the pre-merge live
probe had exercised the principal expression rather than the installer's control flow. The
definition now precedes the role gate, and the parity test asserts that ordering. The proof
this time is the installer itself completing on the replica.

## v1.20.3 (2026-08-28) — installer: task principals resolve on workgroup boxes

Deploying v1.20.2 to a replica box failed at the compactor task: `Register-ScheduledTask`
returned "No mapping between account names and security IDs". The installer built every
task principal as `$env:USERDOMAIN\$env:USERNAME`, and on a workgroup machine USERDOMAIN is
the literal `WORKGROUP`, which has no SID. The brain-only dream/dedup registrations carried
the same latent bug; the one brain box happened to have a matching USERDOMAIN. All three now
use `WindowsIdentity.GetCurrent().Name`, which resolves on domain, workgroup and
Microsoft-account boxes alike. A parity test fails if `USERDOMAIN` reappears in a principal.

## v1.20.2 (2026-08-26) — auto-memory: a migration is never a no-op

Found by a receipt-fidelity test written after the v1.20.1 live run showed a blank
"original line" for a re-indexed orphan. The test exposed something worse than a blank
field: an orphan that hygiene re-indexes and the judge then migrates leaves the index
text byte-identical to before, so the run reported `no-op` — while the migration write
had been made and verified, the orphan file stayed on disk, and no receipt row named the
corpus id (`migrated=1` beside `status=no-op`). The same unnamed-record class the v1.20.1
review closed on the abort paths, one exit path further along. A run with verified
migrations pending now always proceeds through write → verify → delete → receipt, and a
constructed index line carries itself into the receipt.

## v1.20.1 (2026-08-26) — auto-memory: the fix round reviewed

The operator asked for an adversarial review of the v1.20.0 fix round itself, and it found
what this stack's own notes predict: a fix applied literally recreated the bug class.

**Critical.** The "delete fact files only after the index write" fix placed the delete
between the write and the post-write invariant check. An invariant failure then restored
the pre-run index — which still listed the just-deleted facts — while the receipt reported
`lost=0`. The new reachability rule ("linked from ANY line") simultaneously created ghosts
that hygiene could not repair (a file name mentioned in a heading or a prose note), so the
invariant failed every night. Reproduced end-to-end: up to five files deleted per night,
index restored onto them, forever. Fixed by ordering — write, verify invariants, THEN
delete — and by deciding ghosts from entry links only, checked *before* the judge runs, so
an unrepairable store aborts with nothing written and nothing posted.

**High.** A compare-and-swap abort left verified migration records in the corpus that no
receipt named (now undone, unless the server reported the id as a pre-existing dedup hit —
those are never deleted, which closes the second finding: the shared write helper discarded
the `deduplicated` flag, so an unverifiable write could have deleted an L1a fact or an
earlier migration). The compactor now performs its own migration POST and reads the flag.
An enumeration failure *after* the write now reports `applied-unverified` and deletes
nothing instead of collapsing into a generic error.

**Medium.** The banner staleness guard was inert on Python 3.10 (seven fractional digits);
the unproductive-compactor finding was fleet-size gated by a 40-line tail; the widened
entry regex accepted a checkbox line as a pointer and fenced examples as entries (now:
fenced lines are text, and ambiguous lines are never removed and never ghosts); a
dead-extra-link repair rejected any line that also carried a live extra link; byte
truncation could split a surrogate pair; an empty seal file read as "no seals"; a locked
temp file was swallowed; `-Workspace` with a typo silently rehearsed nothing.

Tests: +7 library, +7 compactor scenarios, boundary assertion on the blast cap. Live
verification at the deployed config: real scheduled-task start (`LastTaskResult 0`, live
store correctly skipped), hook through its registered `wsl.exe` command with stdin, lint
through its real PS 5.1 spawn, banner rendered, exit codes propagate through `run-hidden.vbs`.

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
