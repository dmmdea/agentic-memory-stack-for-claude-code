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
// MemoryStoreLib.Tests.ps1 - the scenarios no package owns yet.
// --------------------------------------------------------------------------------

// MemoryStoreLib.Tests.ps1:235 - the PS 5.1 parity scenario has no Go obligation; the
// blueprint replaces it with a linux/windows golden compare of the CLI's output.
func TestCLI_CrossPlatformIdenticalOutput(t *testing.T) { t.Skip("ported in task 7") }

// --------------------------------------------------------------------------------
// MemoryCompact.Tests.ps1
// --------------------------------------------------------------------------------

// MemoryCompact.Tests.ps1:15 - below trigger does nothing: ported in internal/derive as
// TestDerive_BelowTriggerNoChange. Task 3.
//
// MemoryCompact.Tests.ps1:28 - the v2 form of the liveness skip (a live session defers
// materialization instead of gating maintenance) is ported in internal/merge as
// TestMaterialize_LiveSessionDeferred_NoIndexChange. Task 4.

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

// MemoryCompact.Tests.ps1:154 - migration removes line and file only after a byte-equal
// read-back by id.
func TestMigrate_WriteThenVerifyByID(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:167 - no id returned, keep the line.
func TestMigrate_NoIDKeepsLine(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:176 - read-back mismatch, keep the line.
func TestMigrate_ReadBackMismatchKeepsLine(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompact.Tests.ps1:206 - protected-set overflow.
func TestFeasibility_ProtectedSetOverflow(t *testing.T) { t.Skip("ported in task 6") }

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

// MemoryCompactRobustness.Tests.ps1:73 - skipped-judge-unavailable is not productive.
func TestJudge_UnavailableIsNotProductive(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:84 - never deletes a DEDUPLICATED id.
func TestMigrate_NeverDeletesDeduplicatedID(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:95 - a concurrent abort undoes the corpus write.
func TestMigrate_UndoOnConcurrentAbort(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:111 - undoes an unverifiable write.
func TestMigrate_UndoUnverifiedWrite(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:208 - a re-indexed orphan then migrated carries its
// line in the receipt.
func TestReceipt_ReindexedThenMigratedCarriesLine(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:344 - never offers or migrates a body over 4000
// chars.
func TestMigrate_NeverOffersOverCapBody(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:391 - the line floor migrates the oldest pullable
// facts to target.
func TestLineFloor_MigratesOldestToTarget(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:435 - the line floor never migrates an attributed
// statement.
func TestLineFloor_NeverMigratesAttributed(t *testing.T) { t.Skip("ported in task 6") }

// MemoryCompactRobustness.Tests.ps1:470 - the usage ledger writes outcome=empty.
func TestUsageLedger_EmptyJudgeOutputIsAnOutcome(t *testing.T) { t.Skip("ported in task 6") }

// --------------------------------------------------------------------------------
// MemoryIndexWriteGate.Tests.ps1 - the production hook runtime changes from
// powershell.exe to this binary, so these five port as in-process tests.
// --------------------------------------------------------------------------------
