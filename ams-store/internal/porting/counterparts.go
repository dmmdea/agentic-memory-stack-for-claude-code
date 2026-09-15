package porting

// Counterpart is one Pester scenario and the Go test that carries it.
type Counterpart struct {
	// File is the Pester file's base name under scripts/windows/tests.
	File string
	// Line is the line the `It` starts on. It is the key, not the scenario's prose,
	// because the prose is long and the line is what a reader diffs against the file.
	Line int
	// Test is the exact Go test name. Empty means the scenario is exempt, and Why then
	// says which decision removed the behaviour.
	Test string
	// Why records the reasoning for an exemption, and is required whenever Test is empty.
	Why string
}

// PesterFiles are the four suites this module ports. The rest of scripts/windows/tests
// covers the hook daemon, the installer, the dream chain and the promotion gate, none of
// which ams-store replaces.
var PesterFiles = []string{
	"MemoryStoreLib.Tests.ps1",
	"MemoryCompact.Tests.ps1",
	"MemoryCompactRobustness.Tests.ps1",
	"MemoryIndexWriteGate.Tests.ps1",
}

// Counterparts is blueprint section 10.3, as data.
//
// Three scenarios changed FORM in the port and are mapped to the test that carries the
// obligation they existed for, not dropped:
//
//   - MemoryStoreLib:235 asserted the library loads under PowerShell 5.1 and round-trips a
//     multi-byte index. The mechanic is PowerShell's (5.1 reads a BOM-less UTF-8 file as
//     ANSI). The obligation - one binary, two operating systems, one shared history, no
//     host-dependent bytes in the product - is carried by a committed golden compared on
//     both CI runners.
//   - MemoryCompact:273 and :287 tested the PC catch-up spawn, which DESIGN:255-256 removes
//     from the PCs. The behaviours underneath - run once a night, and an explicit bypass of
//     the window - moved to the hub's nightly and to the judge's --force.
//
// Exactly ONE scenario is exempt, and it is exempt because the behaviour itself is gone,
// not because porting it was awkward.
var Counterparts = []Counterpart{
	// ---------------- MemoryStoreLib.Tests.ps1 ----------------
	{"MemoryStoreLib.Tests.ps1", 24, "TestIO_RoundTripBomlessLFWithEmDash", ""},
	{"MemoryStoreLib.Tests.ps1", 38, "TestIO_ByteCountNotCharCount", ""},
	{"MemoryStoreLib.Tests.ps1", 42, "TestIO_PrevailingNewlineCRLF", ""},
	{"MemoryStoreLib.Tests.ps1", 49, "TestIndex_ParseAndRegenerateVerbatim", ""},
	{"MemoryStoreLib.Tests.ps1", 60, "TestIndex_RebuildsOnlyDirtyRecords", ""},
	{"MemoryStoreLib.Tests.ps1", 69, "TestIndex_ExtraLinksCountForReachability", ""},
	{"MemoryStoreLib.Tests.ps1", 78, "TestIndex_GhostsFromEntryLinksOnly", ""},
	{"MemoryStoreLib.Tests.ps1", 87, "TestIndex_RoundTripExactExtraLinks", ""},
	{"MemoryStoreLib.Tests.ps1", 94, "TestIndex_FencedListItemIsNotAnEntry", ""},
	{"MemoryStoreLib.Tests.ps1", 101, "TestIndex_BracketTitleAndIndentedPointerRoundTrip", ""},
	{"MemoryStoreLib.Tests.ps1", 112, "TestState_EmptyFileErrorsNotEmptyState", ""},
	{"MemoryStoreLib.Tests.ps1", 119, "TestTruncate_SurrogateSafe", ""},
	{"MemoryStoreLib.Tests.ps1", 128, "TestSweep_ReportsRemovedAndFailed", ""},
	{"MemoryStoreLib.Tests.ps1", 144, "TestFrontmatter_NestedMetadataType", ""},
	{"MemoryStoreLib.Tests.ps1", 154, "TestFrontmatter_AbsentBlockIsNil", ""},
	{"MemoryStoreLib.Tests.ps1", 160, "TestImperative_CanaryPositives", ""},
	{"MemoryStoreLib.Tests.ps1", 166, "TestImperative_CanaryNegatives", ""},
	{"MemoryStoreLib.Tests.ps1", 172, "TestDoctrine_FeedbackType", ""},
	{"MemoryStoreLib.Tests.ps1", 178, "TestDoctrine_ImperativeSummary", ""},
	{"MemoryStoreLib.Tests.ps1", 183, "TestDoctrine_PlainFactEligible", ""},
	{"MemoryStoreLib.Tests.ps1", 188, "TestAnchors_NumbersPathsBackticksAllCaps", ""},
	{"MemoryStoreLib.Tests.ps1", 198, "TestStores_EnumerateDedupAliasProbeDirs", ""},
	{"MemoryStoreLib.Tests.ps1", 218, "TestStores_PhysicalWinsOverAlias", ""},
	{"MemoryStoreLib.Tests.ps1", 235, "TestCLI_CrossPlatformIdenticalOutput", ""},
	{"MemoryStoreLib.Tests.ps1", 261, "TestLint_AllPerEntryAndPerFileFindings", ""},
	{"MemoryStoreLib.Tests.ps1", 279, "TestLint_CleanStoreZeroFindings", ""},
	{"MemoryStoreLib.Tests.ps1", 293, "TestLiveness_RecentStaleAbsent", ""},
	{"MemoryStoreLib.Tests.ps1", 317, "TestHistory_OutOfTreeNoGitInStore", ""},
	{"MemoryStoreLib.Tests.ps1", 323, "TestHistory_SnapshotExcludesTranscriptsIdempotent", ""},
	{"MemoryStoreLib.Tests.ps1", 331, "TestHistory_PerFileRestoreLeavesNewerFiles", ""},
	{"MemoryStoreLib.Tests.ps1", 345, "TestReceipts_LastJudgeUtcNewestCall", ""},
	{"MemoryStoreLib.Tests.ps1", 359, "TestReceipts_LastJudgeUtcNilWhenNoCall", ""},

	// ---------------- MemoryCompact.Tests.ps1 ----------------
	{"MemoryCompact.Tests.ps1", 15, "TestDerive_BelowTriggerNoChange", ""},
	{"MemoryCompact.Tests.ps1", 28, "TestMaterialize_LiveSessionDeferred_NoIndexChange", ""},
	{"MemoryCompact.Tests.ps1", 41, "TestDerive_AbortsOnConcurrentIndexWrite", ""},
	{"MemoryCompact.Tests.ps1", 56, "TestJudge_DoctrineNeverOfferedNeverEdited", ""},
	{"MemoryCompact.Tests.ps1", 72, "TestJudge_AppliesGenuineShorten", ""},
	{"MemoryCompact.Tests.ps1", 86, "TestJudge_RejectsNoShrinkAndAnchorLoss", ""},
	{"MemoryCompact.Tests.ps1", 97, "TestJudge_SealPreventsReoffer", ""},
	{"MemoryCompact.Tests.ps1", 125, "TestJudge_RejectsHookWithMarkdownLink", ""},
	{"MemoryCompact.Tests.ps1", 137, "TestAnchors_NoWildcardFalseAccept", ""},
	{"MemoryCompact.Tests.ps1", 154, "TestMigrate_WriteThenVerifyByID", ""},
	{"MemoryCompact.Tests.ps1", 167, "TestMigrate_NoIDKeepsLine", ""},
	{"MemoryCompact.Tests.ps1", 176, "TestMigrate_ReadBackMismatchKeepsLine", ""},
	{"MemoryCompact.Tests.ps1", 187, "TestDerive_HygieneWithoutJudge", ""},
	{"MemoryCompact.Tests.ps1", 206, "TestFeasibility_ProtectedSetOverflow", ""},
	{"MemoryCompact.Tests.ps1", 225, "TestLock_ContenderSkipsImmediately", ""},
	{"MemoryCompact.Tests.ps1", 238, "TestLock_FreeLockRuns", ""},
	{"MemoryCompact.Tests.ps1", 248, "TestJudge_OncePerStorePerWindow_Skips", ""},
	{"MemoryCompact.Tests.ps1", 261, "TestJudge_OncePerStorePerWindow_Calls", ""},
	{"MemoryCompact.Tests.ps1", 273, "TestNightly_HubOnceNightly", ""},
	{"MemoryCompact.Tests.ps1", 287, "TestJudge_ForceBypassesWindow", ""},

	// ---------------- MemoryCompactRobustness.Tests.ps1 ----------------
	{"MemoryCompactRobustness.Tests.ps1", 9, "TestHygiene_UnparsablePointerNotDuplicatedNoGrowth", ""},
	{"MemoryCompactRobustness.Tests.ps1", 31, "TestDerive_AbortsWhenNoFactFiles", ""},
	{"MemoryCompactRobustness.Tests.ps1", 43, "TestHygiene_BlastCapAborts", ""},
	{"MemoryCompactRobustness.Tests.ps1", 57, "TestDerive_ConcurrentAbortDeletesNothing", ""},
	{"MemoryCompactRobustness.Tests.ps1", 73, "TestJudge_UnavailableIsNotProductive", ""},
	{"MemoryCompactRobustness.Tests.ps1", 84, "TestMigrate_NeverDeletesDeduplicatedID", ""},
	{"MemoryCompactRobustness.Tests.ps1", 95, "TestMigrate_UndoOnConcurrentAbort", ""},
	{"MemoryCompactRobustness.Tests.ps1", 111, "TestMigrate_UndoUnverifiedWrite", ""},
	{"MemoryCompactRobustness.Tests.ps1", 123, "TestHygiene_UnrepairableGhostAbortsBeforeWrite", ""},
	{"MemoryCompactRobustness.Tests.ps1", 149, "TestHygiene_NonEntryMentionIsNotAGhost", ""},
	{"MemoryCompactRobustness.Tests.ps1", 159, "TestHygiene_RepairsDeadExtraLinkKeepsLive", ""},
	{"MemoryCompactRobustness.Tests.ps1", 174, "TestHygiene_CheckboxAndFencedItemUntouched", ""},
	{"MemoryCompactRobustness.Tests.ps1", 190, "TestHygiene_BlastCapBoundary", ""},
	{"MemoryCompactRobustness.Tests.ps1", 208, "TestReceipt_ReindexedThenMigratedCarriesLine", ""},
	{"MemoryCompactRobustness.Tests.ps1", 226, "TestHistory_CommitAndDiffOutOfTree", ""},
	{"MemoryCompactRobustness.Tests.ps1", 243, "TestDerive_Idempotent", ""},
	{"MemoryCompactRobustness.Tests.ps1", 265, "TestDerive_DryRunWritesNothing", ""},
	{"MemoryCompactRobustness.Tests.ps1", 301, "TestFloor_OverLimitConvergesBelowTrigger", ""},
	{"MemoryCompactRobustness.Tests.ps1", 321, "TestFloor_RunsWithoutJudge", ""},
	{"MemoryCompactRobustness.Tests.ps1", 332, "TestFloor_UnconvergedExitsOne", ""},
	{"MemoryCompactRobustness.Tests.ps1", 344, "TestMigrate_NeverOffersOverCapBody", ""},
	{"MemoryCompactRobustness.Tests.ps1", 365, "TestHygiene_ReindexesOrphanBelowTrigger", ""},
	{"MemoryCompactRobustness.Tests.ps1", 378, "TestDerive_CleanStoreSilent", ""},
	{"MemoryCompactRobustness.Tests.ps1", 391, "TestLineFloor_MigratesOldestToTarget", ""},
	{"MemoryCompactRobustness.Tests.ps1", 420, "", "" +
		"THE ONE EXEMPTION. The scenario is '-CatchUp exits silently while the throttle " +
		"stamp is fresh, and runs once it is stale'. DESIGN:255-256 removes the catch-up " +
		"spawn from the PCs entirely: there is no local nightly on a PC in v2, so there is " +
		"no fresh-then-stale run stamp for a catch-up to read. Its two siblings at :273 and " +
		":287 kept an obligation and moved (the hub's once-nightly run, and the judge's " +
		"explicit window bypass); this one has nothing left to assert. Porting it would mean " +
		"inventing a PC-side stamp for a PC-side spawn the design deleted, which is a test " +
		"that guards code nobody should write."},
	{"MemoryCompactRobustness.Tests.ps1", 435, "TestLineFloor_NeverMigratesAttributed", ""},
	{"MemoryCompactRobustness.Tests.ps1", 456, "TestHygiene_SynthesizedHookNoInjectedSlug", ""},
	{"MemoryCompactRobustness.Tests.ps1", 470, "TestUsageLedger_EmptyJudgeOutputIsAnOutcome", ""},
	{"MemoryCompactRobustness.Tests.ps1", 523, "TestStarvation_SkipStreakCounted", ""},
	{"MemoryCompactRobustness.Tests.ps1", 535, "TestStarvation_OverrideAfterTwoSkips", ""},
	{"MemoryCompactRobustness.Tests.ps1", 548, "TestStarvation_OverrideWhenQuietFiveMinutes", ""},
	{"MemoryCompactRobustness.Tests.ps1", 558, "TestStarvation_NoOverrideWhenJustWritten", ""},
	{"MemoryCompactRobustness.Tests.ps1", 568, "TestStarvation_G7MetricSurfacesStarvation", ""},
	{"MemoryCompactRobustness.Tests.ps1", 583, "TestStarvation_RecentDecisionNoAlarm", ""},
	{"MemoryCompactRobustness.Tests.ps1", 594, "TestLint_StarvedOnTwoSkips", ""},
	{"MemoryCompactRobustness.Tests.ps1", 608, "TestLint_NoStarvedOnSingleSkip", ""},
	{"MemoryCompactRobustness.Tests.ps1", 620, "TestLint_NeutralJudgeSkipNotUnproductive", ""},
	{"MemoryCompactRobustness.Tests.ps1", 635, "TestLint_UnproductiveAcrossNeutrals", ""},

	// ---------------- MemoryIndexWriteGate.Tests.ps1 ----------------
	{"MemoryIndexWriteGate.Tests.ps1", 37, "TestGate_SilentUnderCaps", ""},
	{"MemoryIndexWriteGate.Tests.ps1", 45, "TestGate_IgnoresNonIndexPath", ""},
	{"MemoryIndexWriteGate.Tests.ps1", 53, "TestGate_AdvisesWithoutRewritingUnderSyncLimit", ""},
	{"MemoryIndexWriteGate.Tests.ps1", 63, "TestGate_NormalizesAtSyncLimitAndWarnsStale", ""},
	{"MemoryIndexWriteGate.Tests.ps1", 77, "TestGate_DoctrineOnlyOverflowReported", ""},
}
