package porting

import "testing"

// Task numbers are the task list of plans/2026-09-15-ams-session6-plan.md:
//
//	task 3 - derive (harvest, hygiene, render order, floor, truncate, doctrine, anchors)
//	task 4 - the merge engine
//	task 5 - sync, watch, lock, gate, lint
//	task 6 - judge-apply (hub-only)
//	task 7 - integration, the counterpart-table check, CI
//
// Each placeholder carries the Pester file:line it is the counterpart of, so the task
// that ports it does not have to re-derive the mapping.

// --------------------------------------------------------------------------------
// MemoryStoreLib.Tests.ps1 - the 11 scenarios this scaffold does not yet own.
// --------------------------------------------------------------------------------

// MemoryStoreLib.Tests.ps1:119 - byte truncation never splits a surrogate pair.
func TestTruncate_SurrogateSafe(t *testing.T) { t.Skip("ported in task 3") }

// MemoryStoreLib.Tests.ps1:188 - anchor tokens (numbers, paths, backticks, ALL-CAPS).
func TestAnchors_NumbersPathsBackticksAllCaps(t *testing.T) { t.Skip("ported in task 3") }

// MemoryStoreLib.Tests.ps1:235 - the PS 5.1 parity scenario has no Go obligation; the
// blueprint replaces it with a linux/windows golden compare of the CLI's output.
func TestCLI_CrossPlatformIdenticalOutput(t *testing.T) { t.Skip("ported in task 7") }

// MemoryStoreLib.Tests.ps1:261 - orphan, dangling, dup-slug, long-line, oversized-file,
// no-frontmatter.
func TestLint_AllPerEntryAndPerFileFindings(t *testing.T) { t.Skip("ported in task 5") }

// MemoryStoreLib.Tests.ps1:279 - a clean store yields zero findings and correct stats.
func TestLint_CleanStoreZeroFindings(t *testing.T) { t.Skip("ported in task 5") }

// MemoryStoreLib.Tests.ps1:293 - liveness probe: recent, 3 h old, absent.
func TestLiveness_RecentStaleAbsent(t *testing.T) { t.Skip("ported in task 5") }

// MemoryStoreLib.Tests.ps1:317 - no .git in the tree, no remote.
func TestHistory_OutOfTreeNoGitInStore(t *testing.T) { t.Skip("ported in task 5") }

// MemoryStoreLib.Tests.ps1:323 - snapshot the store only; a second snapshot is a no-op.
func TestHistory_SnapshotExcludesTranscriptsIdempotent(t *testing.T) { t.Skip("ported in task 5") }

// MemoryStoreLib.Tests.ps1:331 - records a deletion, restores exactly that file.
func TestHistory_PerFileRestoreLeavesNewerFiles(t *testing.T) { t.Skip("ported in task 5") }

// MemoryStoreLib.Tests.ps1:345 - LastJudgeUtc is the newest judge_called receipt.
func TestReceipts_LastJudgeUtcNewestCall(t *testing.T) { t.Skip("ported in task 5") }

// MemoryStoreLib.Tests.ps1:359 - LastJudgeUtc is nil when no receipt called the judge.
func TestReceipts_LastJudgeUtcNilWhenNoCall(t *testing.T) { t.Skip("ported in task 5") }

// --------------------------------------------------------------------------------
// MemoryCompact.Tests.ps1
// --------------------------------------------------------------------------------

// MemoryCompact.Tests.ps1:15 - below trigger does nothing.
func TestDerive_BelowTriggerNoChange(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompact.Tests.ps1:28 - the v2 form of the liveness skip: a live session defers
// materialization instead of gating maintenance.
func TestMaterialize_LiveSessionDeferred_NoIndexChange(t *testing.T) { t.Skip("ported in task 4") }

// MemoryCompact.Tests.ps1:41 - compare-and-swap abort on a mid-run write.
func TestDerive_AbortsOnConcurrentIndexWrite(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompact.Tests.ps1:56 - doctrine is untouchable and never even offered.
func TestJudge_DoctrineNeverOfferedNeverEdited(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:72 - applies a genuine shortening.
func TestJudge_AppliesGenuineShorten(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:86 - rejects not-shorter and anchor-dropping rewrites.
func TestJudge_RejectsNoShrinkAndAnchorLoss(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:97 - the seal prevents a re-offer. The port must keep the
// :110 assertion that the shortened hook stays OVER 130 B, or the byte filter, not the
// seal, is what excludes it on run 2 and the test passes with the seal deleted.
func TestJudge_SealPreventsReoffer(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:125 - rejects a markdown link in the hook.
func TestJudge_RejectsHookWithMarkdownLink(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:137 - regex/wildcard anchor, no false accept.
func TestAnchors_NoWildcardFalseAccept(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompact.Tests.ps1:154 - migration removes line and file only after a byte-equal
// read-back by id.
func TestMigrate_WriteThenVerifyByID(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:167 - no id returned, keep the line.
func TestMigrate_NoIDKeepsLine(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:176 - read-back mismatch, keep the line.
func TestMigrate_ReadBackMismatchKeepsLine(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:187 - hygiene without the judge, throttle still marked.
func TestDerive_HygieneWithoutJudge(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompact.Tests.ps1:206 - protected-set overflow.
func TestFeasibility_ProtectedSetOverflow(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:225 - the lock is held, so the contender exits 0 with no
// receipt.
func TestLock_ContenderSkipsImmediately(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompact.Tests.ps1:238 - the lock is free, so the run proceeds and receipts.
func TestLock_FreeLockRuns(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompact.Tests.ps1:248 - a judge attempt inside the 20 h window is skipped.
func TestJudge_OncePerStorePerWindow_Skips(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:261 - a last attempt older than 20 h calls the judge.
func TestJudge_OncePerStorePerWindow_Calls(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:273 - the PC catch-up spawn is removed; the hub's once-nightly
// run replaces it.
func TestNightly_HubOnceNightly(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:287 - the catch-up force/workspace bypass becomes the judge's
// explicit window bypass.
func TestJudge_ForceBypassesWindow(t *testing.T) { t.Skip("ported in task 6") }

// --------------------------------------------------------------------------------
// MemoryCompactRobustness.Tests.ps1
// --------------------------------------------------------------------------------

// MemoryCompactRobustness.Tests.ps1:9 - never duplicates an unparsable pointer and never
// grows the store.
func TestHygiene_UnparsablePointerNotDuplicatedNoGrowth(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:31 - aborts when zero fact files enumerate.
func TestDerive_AbortsWhenNoFactFiles(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:43 - the blast cap aborts on mass dangling.
func TestHygiene_BlastCapAborts(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:57 - a concurrent write aborts with the file still
// on disk.
func TestDerive_ConcurrentAbortDeletesNothing(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:73 - skipped-judge-unavailable is not productive.
func TestJudge_UnavailableIsNotProductive(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:84 - never deletes a DEDUPLICATED id.
func TestMigrate_NeverDeletesDeduplicatedID(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:95 - a concurrent abort undoes the corpus write.
func TestMigrate_UndoOnConcurrentAbort(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:111 - undoes an unverifiable write.
func TestMigrate_UndoUnverifiedWrite(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:123 - an unrepairable entry ghost aborts before any
// write or post, twice.
func TestHygiene_UnrepairableGhostAbortsBeforeWrite(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:149 - a non-entry mention of a missing file is not a
// ghost.
func TestHygiene_NonEntryMentionIsNotAGhost(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:159 - repairs a dead extra link, keeps the live one.
func TestHygiene_RepairsDeadExtraLinkKeepsLive(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:174 - a checkbox line is kept; a fenced item is not
// an entry.
func TestHygiene_CheckboxAndFencedItemUntouched(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:190 - the blast-cap boundary: 23 aborts, 22 applies.
func TestHygiene_BlastCapBoundary(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:208 - a re-indexed orphan then migrated carries its
// line in the receipt.
func TestReceipt_ReindexedThenMigratedCarriesLine(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:226 - out-of-tree commit and diff, no .git in a
// store.
func TestHistory_CommitAndDiffOutOfTree(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompactRobustness.Tests.ps1:243 - a second run is idempotent.
func TestDerive_Idempotent(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:265 - a dry run reports and writes nothing.
func TestDerive_DryRunWritesNothing(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:301 - floors an over-limit index deterministically.
func TestFloor_OverLimitConvergesBelowTrigger(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:321 - floors while the judge lock is held.
func TestFloor_RunsWithoutJudge(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:332 - unconverged exits 1, throttle unmarked.
func TestFloor_UnconvergedExitsOne(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:344 - never offers or migrates a body over 4000
// chars.
func TestMigrate_NeverOffersOverCapBody(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:365 - re-indexes an orphan in a below-trigger store.
func TestHygiene_ReindexesOrphanBelowTrigger(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:378 - no receipt, no log for a clean below-trigger
// store.
func TestDerive_CleanStoreSilent(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:391 - the line floor migrates the oldest pullable
// facts to target.
func TestLineFloor_MigratesOldestToTarget(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:435 - the line floor never migrates an attributed
// statement.
func TestLineFloor_NeverMigratesAttributed(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:456 - a synthesized hook comes from the first prose
// line and never injects a slug.
func TestHygiene_SynthesizedHookNoInjectedSlug(t *testing.T) { t.Skip("ported in task 3") }

// MemoryCompactRobustness.Tests.ps1:470 - the usage ledger writes outcome=empty.
func TestUsageLedger_EmptyJudgeOutputIsAnOutcome(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:523 - under the limit, a live session skips and the
// streak is counted.
func TestStarvation_SkipStreakCounted(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompactRobustness.Tests.ps1:535 - at the limit after two skips, override.
func TestStarvation_OverrideAfterTwoSkips(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompactRobustness.Tests.ps1:548 - at the limit and quiet, override.
func TestStarvation_OverrideWhenQuietFiveMinutes(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompactRobustness.Tests.ps1:558 - at the limit but written a minute ago, still
// skips.
func TestStarvation_NoOverrideWhenJustWritten(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompactRobustness.Tests.ps1:568 - the starvation metric surfaces a starved store.
func TestStarvation_G7MetricSurfacesStarvation(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompactRobustness.Tests.ps1:583 - a recent decision raises no alarm.
func TestStarvation_RecentDecisionNoAlarm(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompactRobustness.Tests.ps1:594 - lint raises compactor-starved on two skips.
func TestLint_StarvedOnTwoSkips(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompactRobustness.Tests.ps1:608 - no compactor-starved on a single skip.
func TestLint_NoStarvedOnSingleSkip(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompactRobustness.Tests.ps1:620 - skipped-judge-attempted-today is neutral.
func TestLint_NeutralJudgeSkipNotUnproductive(t *testing.T) { t.Skip("ported in task 5") }

// MemoryCompactRobustness.Tests.ps1:635 - unproductive fires across interleaved neutrals.
func TestLint_UnproductiveAcrossNeutrals(t *testing.T) { t.Skip("ported in task 5") }

// --------------------------------------------------------------------------------
// MemoryIndexWriteGate.Tests.ps1 - the production hook runtime changes from
// powershell.exe to this binary, so these five port as in-process tests.
// --------------------------------------------------------------------------------

// MemoryIndexWriteGate.Tests.ps1:37 - silent under every cap.
func TestGate_SilentUnderCaps(t *testing.T) { t.Skip("ported in task 5") }

// MemoryIndexWriteGate.Tests.ps1:45 - ignores a path that is not a store's MEMORY.md.
func TestGate_IgnoresNonIndexPath(t *testing.T) { t.Skip("ported in task 5") }

// MemoryIndexWriteGate.Tests.ps1:53 - advises but does not rewrite over the line cap
// while under the sync limit.
func TestGate_AdvisesWithoutRewritingUnderSyncLimit(t *testing.T) { t.Skip("ported in task 5") }

// MemoryIndexWriteGate.Tests.ps1:63 - normalizes at or over the limit, receipts it, and
// says the in-context copy is STALE.
func TestGate_NormalizesAtSyncLimitAndWarnsStale(t *testing.T) { t.Skip("ported in task 5") }

// MemoryIndexWriteGate.Tests.ps1:77 - never truncates doctrine, and says so.
func TestGate_DoctrineOnlyOverflowReported(t *testing.T) { t.Skip("ported in task 5") }
