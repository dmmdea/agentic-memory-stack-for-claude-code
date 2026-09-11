---
status: Accepted
date: "2026-09-10"
---

# Fleet store sync and a native-Linux authority

## Context

Two failures were measured on 2026-09-10 after a week in which every watchdog reported the
nightly maintenance healthy:

- **The nightly had no window.** The brain box was a workstation that is powered off overnight
  on about half of nights. Missed runs were made up by daytime catch-ups, which then displaced the
  next night's slot (a 23-hour throttle), and the daytime run met live sessions and skipped. A
  session-start catch-up added in v1.20.20 had no cross-instance mutex and ran at every session
  start: 243 receipts in nine hours, four compactor instances on one store in the same second,
  history-repository lock failures, and a judge invoked ~50 times on one store with every result
  rejected. Zero stores were compacted for two days while lint, the receipt-age watchdog and the
  unproductive-run watchdog all stayed quiet, because a skip writes a receipt.
- **The harness's own per-workspace stores had no replica anywhere.** The harness's auto-memory
  is machine-local; the offline replicas this stack ships cover the memory corpus only. A standing
  order written on one workstation did not exist on another. The load cap is a silent truncation
  (first 25,000 bytes or 200 lines of the index), so an over-cap index drops the rules written
  last.

The operator decided (2026-09-10) that the authority and the nightly move to an always-on
native-Linux server, that every workstation must keep working memory with that server absent,
that the harness stores are synced across workstations, and that several workstations may write
the same workspace at the same time. The design was reviewed adversarially by three independent
reviewers (79 findings, 8 blockers) before this record was written; the resolutions are folded
in below.

## Decision

1. **The brain box is an always-on native-Linux server.** The installer gains a native-authority
   path (`MEM0_HOST_KIND=native`) that gates every WSL- and Windows-only line in the units and
   scripts. The server binds to the overlay-network interface only, never all interfaces. Keys
   are held with `systemd-creds` (TPM-backed where present) on a dataset excluded from
   replication. The judge is the native Codex CLI behind the same transport interface and mutex
   the Windows shim exposed; the shim is not installed on the authority.
2. **One nightly chain, not independent timers.** A single persistent timer with a boot guard
   starts an ordered target (consolidate → dedup → store judge → backup → off-box copy → morning
   summary → health stamp). Steps hang off the target with `Wants=`/`After=`, so a failed step
   never blocks the backup. A run missed while the box was off fires at the next boot.
3. **The harness stores replicate through a git hub on the brain box.** One bare repository on
   the server, owned by a `git-shell` user with per-machine keys and non-fast-forward and delete
   refusals; one local repository per workstation that replaces the maintainer's history
   repository (same shape, git directory outside the store, work tree at the projects root). The
   history repository may carry exactly this remote and no other; lint reports any other host.
4. **The index is derived, after it is harvested.** The live index is authored: hooks differ from
   file descriptions, some fact files lack frontmatter, and line endings are mixed. `derive`
   therefore first copies every index hook into its fact file's frontmatter (`hook:`), then renders
   the index from the fact set with a fixed order (doctrine first, then commit-time recency),
   always LF, with the byte floor and the 200-line cap applied as pure functions of the fact set.
   The index is untracked and re-derived after every merge, so it is never merged.
5. **Merges use git's three-way semantics and never touch a live store in place.** Deleted on one
   side and unchanged on the other is a deletion; modified on one side and deleted on the other
   keeps the modification and reports it. Frontmatter merges by field; bodies merge three-way by
   line; a real body conflict is won by the newer commit and the loser stays in history, reported
   by lint. The merge is computed out of tree and materialized with per-file atomic replaces,
   fact files first and the index last. While a session is live in a workspace, files it modified
   are never replaced and deletions are deferred to the session boundary.
6. **Maintenance runs at session boundaries on every machine; the judge runs once per night on
   the hub's checkout.** Deterministic work (harvest, derive, floor, hygiene) runs at session
   start, at session end, after each write of the index, and after each merge, under one
   per-machine lock. The judge runs on the authority's own checkout, one attempt per store per
   night; its push uses a bounded fetch-merge-push loop. There is no local fallback judge: offline,
   a workstation is floor-only and says so.
7. **Network is never on a hook's critical path.** The write-time gate derives and commits
   locally; fetch, merge and push run at session start, at session end and from a session-scoped
   helper on a short tick.
8. **Health is one number per store**: hours over trigger without an applied decision, from a
   synced first-crossing stamp, never from receipt age. The authority exposes a maintenance
   health endpoint that the session-start line reads with a short timeout.
9. **Nothing is removed before its replacement has passed its gate.** The chain is proven on a
   staging copy before the corpus is cut over; the workstation cutover happens after the store
   client's fixtures and a zero-hooks-lost check pass on every store.

## Consequences

- Every workstation is the same shape: local stores, a dormant read-only corpus replica, capture
  hooks, and a store client. No workstation depends on the server for correctness; it depends on
  it for the judge and for cross-machine convergence.
- The harness stores gain a replica and a history that spans machines for the first time.
- Concurrent writers cannot lose a fact: every version is in history, and a deletion is honoured
  instead of resurrected by a union.
- The store client is one implementation for three operating systems (a compiled binary, with the
  existing PowerShell library under pwsh 7 on Linux as the named fallback if the binary slips).
  Its test suite must carry a 1:1 counterpart of every existing store-library scenario.
- Costs: a Linux port of the consolidator and its judge transport; a store client rewrite; the
  first harvest changes the look of every index (fixed order, no groups). The pool that hosts the
  authority is single-vdev, so off-box replication and the mirror are the real backup, and the
  health endpoint alarms on pool usage.
- Two pre-existing defects are fixed as part of the cutover: Windows hooks resolved the authority
  from an environment variable that the installer's authority setting never wrote, and the travel
  mode redirected that variable to the disposable replica, so hooks could post into a store that
  is discarded on reconnect. Every hook now resolves the authority from the role file and queues to
  the Outbox on failure.


**Phase status.** Phase 0 (readiness of the Linux box: dataset with reservation and quota, off-box
replication, secrets under `systemd-creds`, native Codex login, bind rule staged, embedder and
reranker latency measured, RTC wake proven) closed on 2026-09-10. Phase 1 shipped its first half in
v1.21.0: `install/linux-authority.sh`, the native judge transport, `/health/maintenance`, the
nightly chain skeleton and the 503 mapping — proven on a staging copy, no cutover. v1.22.0 shipped
the second half: the nightly jobs ported to Python on the authority (dream, autopromote, index
refresh, usage report and its quota gate), every nightly job as a receipted step of the one chain,
and the workstation-side `cold-embedder` handling. The Phase 1 gate (seven complete staging nights,
canaries retrievable after each) counts from the first full-chain night. Cutover (Phase 2) has not
begun.

**Gather input on the authority.** The consolidator's gather phase reads the store — the last
36 hours of evidence and the recent episodes — rather than workstation transcripts, which never
reach the authority (transcript extraction stays per workstation, see Alternatives). Transcripts
that exist locally on the authority are appended as before.

## Alternatives considered

- **Keep the authority on the workstation and only run the nightly on the server.** Rejected:
  the corpus is offline exactly when the jobs run.
- **Point the harness's memory directory at a network mount on the server.** Rejected: an
  outage would blank memory in every session on every workstation.
- **A file-sync daemon per workstation for the stores.** Rejected: a daemon per box, and conflict
  files inside a store that agents glob, are what the store rules forbid; git plus hook-invoked
  sync covers a few kilobytes a day.
- **Centralize transcript extraction on the authority.** Rejected in review: it added an
  endpoint, a transport, a non-idempotent replay and a brand-tagging hazard to solve a problem
  the native Codex CLI on Linux solves directly.
- **A local fallback judge per workstation.** Rejected in review: a second judge on the same
  store is the concurrency bug in another form.
- **Union of fact files as the merge rule.** Rejected in review: it resurrects every deliberate
  deletion forever.

## Related code

- [`install/1-wsl-services.sh`](../../../install/1-wsl-services.sh) — the WSL install path the native path is derived from.
- [`install/linux-replica.sh`](../../../install/linux-replica.sh) — the interpreter selection and unit pattern the native authority reuses.
- [`install/linux-authority.sh`](../../../install/linux-authority.sh) — the native authority install path (Phase 1).
- [`systemd/mem0-native.conf`](../../../systemd/mem0-native.conf) — the drop-in that replaces the WSL pre-start with `LoadCredentialEncrypted` and the bind wait.
- [`scripts/wsl/ams-step.sh`](../../../scripts/wsl/ams-step.sh) — the receipted step wrapper of the single nightly chain.
- [`mem0-server/maintenance_health.py`](../../../mem0-server/maintenance_health.py) — `GET /health/maintenance`.
- [`scripts/windows/memory-store-lib.ps1`](../../../scripts/windows/memory-store-lib.ps1) — the store invariants the store client must carry 1:1.
- [`scripts/windows/memory-compact.ps1`](../../../scripts/windows/memory-compact.ps1) — the v1 nightly compactor this design retires from workstations.
- [`scripts/windows/memory-index-write-gate.ps1`](../../../scripts/windows/memory-index-write-gate.ps1) — the write-time gate that becomes derive-and-commit.

## Related docs

- [`one-brain-rule.md`](./one-brain-rule.md) — amended by this record.
- [`auto-memory-system-a-vs-b.md`](./auto-memory-system-a-vs-b.md) — the two systems stay separate; the harness store now has a replica and a derived index.
- [`offline-first-emergent.md`](./offline-first-emergent.md) — the replica behaviour every workstation now shares.
- [`auto-memory-maintenance.md`](../../systems/auto-memory-maintenance.md) — the v1 maintainer; carries the v2 pointer.
- [`installer-and-deploy.md`](../../systems/installer-and-deploy.md) — roles and the native-Linux install paths.
