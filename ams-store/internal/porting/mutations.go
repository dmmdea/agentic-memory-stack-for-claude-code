package porting

// The mutation gate (blueprint section 10.2).
//
// Every merge rule gets ONE mutation: a minimal, compiling change to the line that
// implements it, plus the name of the test that must turn RED when it is applied. A rule
// whose mutation leaves the suite green is a rule nothing actually tests - the assertion
// was written against a seam, or against a value the mutated code happens to produce
// anyway - and that is worth knowing before the engine is trusted with a fleet's memory.
//
// `ams-store/scripts/mutation-gate.sh` reads this table, applies each mutation in turn
// with `git apply`-free text substitution, runs the named test, expects FAIL, restores
// the file with `git checkout --`, and prints a table. It is a LOCAL gate: a mutation run
// is N times the suite and never a CI job.
//
// Ownership: the table covers the whole section 10.2 list, not only the merge rules, so
// the later tasks inherit a complete gate. An entry whose Test does not exist yet is
// reported PENDING rather than failing the run.

// Hunk is one exact text substitution inside a file. Old must appear EXACTLY ONCE in the
// file or the mutation is refused - a substitution that silently hit the wrong occurrence
// would report a green rule that was never mutated.
type Hunk struct {
	// File overrides the Mutation's File for this hunk. It is set only when a rule is
	// defended in more than one place, and mutating one of them would leave the other
	// still holding the line.
	File string
	Old  string
	New  string
}

// Mutation is one rule, its mutation, and the test that must go red.
type Mutation struct {
	// ID is stable and short, for the gate's table.
	ID string
	// Rule is the invariant being tested, in the blueprint's words.
	Rule string
	// Task is the plan task that owns the code this mutation edits.
	Task int
	// File is relative to the ams-store module root, and is the default for every hunk.
	File string
	// Hunks is the change. A one-line rule is one hunk; a rule whose mutation MOVES a
	// call (the materialize ordering) needs two; a rule with two independent guards
	// mutates both, because either one alone would still hold.
	Hunks []Hunk
	// Test is the exact Go test name that must fail.
	Test string
	// Package is the -run target's package path, relative to the module root.
	Package string
	// Why records what breaks in production if the rule is lost.
	Why string
}

// Mutations is the gate's table.
var Mutations = []Mutation{
	{
		ID: "delete-unchanged", Rule: "deleted on one side, unchanged on the other -> deleted", Task: 4,
		File: "internal/merge/deletion.go", Test: "TestMerge_DeleteOnA_UntouchedOnB_Absent", Package: "./internal/merge",
		Why: "resolving a deletion back to the base blob is the union rule, which resurrected every deliberate deletion forever and made every judge migration a no-op.",
		Hunks: []Hunk{{
			Old: "\tcase base.present && ours.present && !theirs.present:\n\t\tif NormalizedEqual(base.data, ours.data) {\n\t\t\tres.op = OpDelete\n",
			New: "\tcase base.present && ours.present && !theirs.present:\n\t\tif NormalizedEqual(base.data, ours.data) {\n\t\t\tres.op, res.content = OpReplace, toLF(base.data)\n",
		}},
	},
	{
		ID: "modify-delete", Rule: "modified on one side, deleted on the other -> keep the modified side, report resurrected", Task: 4,
		File: "internal/merge/deletion.go", Test: "TestMerge_ModifyOnA_DeleteOnB_Resurrected", Package: "./internal/merge",
		Why: "preferring the deletion silently throws away an edit made on another PC since the merge base.",
		Hunks: []Hunk{{
			Old: "\t\t// Modified here, deleted there: keep the modified side and say so.\n\t\tres.op = OpReplace\n\t\tres.content = toLF(ours.data)\n\t\tres.resurrected = true\n",
			New: "\t\t// Modified here, deleted there: keep the modified side and say so.\n\t\tres.op = OpDelete\n\t\tres.content = nil\n\t\tres.resurrected = false\n",
		}},
	},
	{
		ID: "canon-eol", Rule: "the comparison normalizes CRLF to LF", Task: 4,
		File: "internal/merge/canon.go", Test: "TestMerge_CRLFOnlyDifference_Identical", Package: "./internal/merge",
		Why: "ten live fact files are CRLF; without the normalization each of them conflicts with its own LF twin on first sync.",
		Hunks: []Hunk{{
			Old: "\ts := bytes.ReplaceAll(b, []byte(\"\\r\\n\"), []byte(\"\\n\"))\n",
			New: "\ts := append([]byte(nil), b...)\n",
		}},
	},
	{
		ID: "canon-advisory-keys", Rule: "the comparison ignores the hook: and modified: lines", Task: 4,
		File: "internal/merge/canon.go", Test: "TestMerge_HookOnlyDifference_NoConflict", Package: "./internal/merge",
		Why: "a PC that merely harvested its own hook then reads as having modified the file, and resurrects a deletion the judge made on purpose.",
		Hunks: []Hunk{{
			Old: "\t\tif reIgnoredLine.Match(ln) {\n",
			New: "\t\tif false && reIgnoredLine.Match(ln) {\n",
		}},
	},
	{
		ID: "renames-off", Rule: "rename detection never decides whether a path exists", Task: 4,
		File: "internal/merge/audit.go", Test: "TestMerge_RenamesOff_MigrationNotPairedWithNewFile", Package: "./internal/merge",
		Why: "with renames deciding presence, a judge's rework of a fact is folded into whatever new slug a PC re-homed it to and the old path vanishes with no conflict and no report. BOTH guards are mutated together because the config alone is not portable: measured while building this, git 2.55 honours merge.renames=false for merge-tree and git 2.43 honours nothing at all, so the audit is the guard that holds on every PC and the config only shortens its work.",
		Hunks: []Hunk{
			{
				File: "internal/merge/engine.go",
				Old:  "\t\t{\"merge.renames\", \"false\"},\n",
				New:  "\t\t{\"merge.renames\", \"true\"},\n",
			},
			{
				Old: "\tconflicted := make(map[string]bool, len(mt.Conflicted))\n",
				New: "\treturn append([]string(nil), mt.Conflicted...)\n\tconflicted := make(map[string]bool, len(mt.Conflicted))\n",
			},
		},
	},
	{
		ID: "hook-field-rule", Rule: "hook: takes the judge's value unless the local side changed hook: itself", Task: 4,
		File: "internal/frontmatter/merge.go", Test: "TestMerge_HookOnlyDifference_NoConflict", Package: "./internal/merge",
		Why: "always taking the judge overwrites a PC's own harvest on every sync.",
		Hunks: []Hunk{{
			Old: "\t\t\tcase ourChanged && hasOurs:\n\t\t\t\treturn ourRaw, true, false\n",
			New: "\t\t\tcase ourChanged && hasOurs:\n\t\t\t\treturn theirRaw, hasTheirs, false\n",
		}},
	},
	{
		ID: "modified-advisory", Rule: "modified: is advisory only and is never the conflict tiebreak", Task: 4,
		File: "internal/merge/body.go", Test: "TestMerge_BodyConflict_NewerCommitWins_LoserInHistory", Package: "./internal/merge",
		Why: "the stamp is written by the model; a model that types 2099 into it would win every conflict on the fleet forever.",
		Hunks: []Hunk{{
			Old: "\toursWon, tiebreak := rc.oursWins()\n",
			New: "\toursWon, tiebreak := frontmatter.ParseText(string(ours.data)).Modified > frontmatter.ParseText(string(theirs.data)).Modified, TiebreakCommitTime\n",
		}},
	},
	{
		ID: "commit-time-winner", Rule: "the body-conflict winner is the side whose last commit to the path is newer", Task: 4,
		File: "internal/merge/winner.go", Test: "TestMerge_BodyConflict_NewerCommitWins_LoserInHistory", Package: "./internal/merge",
		Why: "inverting it makes every conflict resolve to the STALER text.",
		Hunks: []Hunk{{
			Old: "\t\treturn rc.oursCommit.Unix > rc.theirsCommit.Unix, TiebreakCommitTime\n",
			New: "\t\treturn rc.oursCommit.Unix < rc.theirsCommit.Unix, TiebreakCommitTime\n",
		}},
	},
	{
		ID: "machine-id-tiebreak", Rule: "equal commit seconds are broken by the machine id, deterministically", Task: 4,
		File: "internal/merge/winner.go", Test: "TestMerge_BodyConflict_EqualCommitTime_MachineIDTiebreak", Package: "./internal/merge",
		Why: "a tiebreak that is not identical on both PCs has each of them converge on different bytes and then merge each other's results forever.",
		Hunks: []Hunk{{
			Old: "\treturn rc.oursCommit.Machine > rc.theirsCommit.Machine, TiebreakMachineID\n",
			New: "\treturn rc.oursCommit.Machine < rc.theirsCommit.Machine, TiebreakMachineID\n",
		}},
	},
	{
		ID: "loser-in-history", Rule: "the loser of a body conflict stays reachable: the merge commit names both parents", Task: 4,
		File: "internal/merge/mergetree.go", Test: "TestMerge_BodyConflict_NewerCommitWins_LoserInHistory", Package: "./internal/merge",
		Why: "conflict-in-history reports a commit id; drop the second parent and that id points at nothing after the next clone.",
		Hunks: []Hunk{{
			Old: "\tcommit, err := gitx.CommitTree(ctx, opt, newTree, []string{ours, theirs}, e.message(subject, \"merge\"))\n",
			New: "\tcommit, err := gitx.CommitTree(ctx, opt, newTree, []string{ours}, e.message(subject, \"merge\"))\n",
		}},
	},
	{
		ID: "materialize-order", Rule: "fact files are materialized before the index is derived", Task: 4,
		File: "internal/merge/materialize.go", Test: "TestMaterialize_FactFilesBeforeIndex", Package: "./internal/merge",
		Why: "deriving between fact writes publishes an index that points at a file not on disk yet.",
		Hunks: []Hunk{{
			Old: "\t\trep.Written = append(rep.Written, c.path)\n\t\tif ws != \"\" {\n\t\t\ttouched[ws] = true\n\t\t}\n",
			New: "\t\trep.Written = append(rep.Written, c.path)\n\t\tif mo.Derive != nil && ws != \"\" {\n\t\t\tif err := mo.Derive(ws); err != nil {\n\t\t\t\treturn rep, err\n\t\t\t}\n\t\t}\n\t\tif ws != \"\" {\n\t\t\ttouched[ws] = true\n\t\t}\n",
		}},
	},
	{
		ID: "live-session-guard", Rule: "a file a live session touched since its start is never replaced", Task: 4,
		File: "internal/merge/deferred.go", Test: "TestMaterialize_LiveSessionFileDeferred", Package: "./internal/merge",
		Why: "replacing a file under a running session destroys what it just wrote, with no copy anywhere.",
		Hunks: []Hunk{{
			Old: "\treturn !fi.ModTime().Before(sessionStart)\n",
			New: "\treturn false && !fi.ModTime().Before(sessionStart)\n",
		}},
	},
	{
		ID: "deletion-deferral", Rule: "no deletion is materialized while a session is live in the workspace", Task: 4,
		File: "internal/merge/deferred.go", Test: "TestMaterialize_LiveSessionDeletionDeferred", Package: "./internal/merge",
		Why: "a fact file vanishing under a running session is the one change it cannot recover from.",
		Hunks: []Hunk{{
			Old: "\t\t// only the files it touched. A fact file vanishing under a running session is\n\t\t// the one change it cannot recover from.\n\t\treturn true\n",
			New: "\t\t// only the files it touched. A fact file vanishing under a running session is\n\t\t// the one change it cannot recover from.\n\t\treturn false\n",
		}},
	},
	{
		ID: "migrated-carry", Rule: "migrated: is carried when only one side has it", Task: 4,
		File: "internal/frontmatter/merge.go", Test: "TestMerge_RecreatedSlugCarriesMigratedID", Package: "./internal/merge",
		Why: "losing the mem0 id makes the judge file a fresh variant of the same fact every night instead of updating one.",
		Hunks: []Hunk{{
			Old: "\t\t\tcase hasOurs && !hasTheirs:\n\t\t\t\treturn ourRaw, true, false\n\t\t\tcase !hasOurs && hasTheirs:\n\t\t\t\treturn theirRaw, true, false\n",
			New: "\t\t\tcase hasOurs && !hasTheirs:\n\t\t\t\treturn \"\", false, false\n\t\t\tcase !hasOurs && hasTheirs:\n\t\t\t\treturn \"\", false, false\n",
		}},
	},
	{
		ID: "index-untracked", Rule: "MEMORY.md is untracked everywhere: derived, never merged", Task: 4,
		File: "internal/merge/engine.go", Test: "TestMerge_IndexIsNeverTracked", Package: "./internal/merge",
		Why: "a tracked index conflicts on every sync and a merge can clobber the copy a live session is reading. BOTH guards are mutated together because either one alone still keeps it out - which is the point of having two.",
		Hunks: []Hunk{
			{
				Old: "\t\"*/memory/\" + store.IndexName + \"\\n\" +\n",
				New: "\t\"\" +\n",
			},
			{
				// The pathspec is neutered rather than deleted so `store` stays
				// referenced: an unused import is a BUILD failure, and a build failure
				// reads as a red test while proving nothing about the rule.
				Old: "\t\t\trel, \":(exclude)\"+rel+\"/\"+store.IndexName); err != nil {\n",
				New: "\t\t\trel, \":(exclude)\"+rel+\"/not-\"+store.IndexName); err != nil {\n",
			},
		},
	},

	// ------------------------------------------------------------------
	// Rules owned by the tasks that follow. The gate reports these PENDING
	// until the named test exists.
	// ------------------------------------------------------------------
	{
		ID: "local-commit-before-fetch", Rule: "sync commits locally BEFORE it fetches", Task: 5,
		File: "internal/sync/sync.go", Test: "TestSync_OfflineCommitThenResume", Package: "./internal/sync",
		Why: "committing after the fetch loses a whole day of offline history whenever the hub is unreachable.",
		Hunks: []Hunk{{
			Old: "\t// 1. derive, so the index is correct whether or not the hub is reachable.\n\tres.Derived = deriveAll(ctx, opt, workspaces, now, logw)\n\n\t// 2. stage and commit LOCALLY, before any network call.\n",
			New: "\t// 1. derive, so the index is correct whether or not the hub is reachable.\n\tres.Derived = deriveAll(ctx, opt, workspaces, now, logw)\n\n\tif hub, _ := HasHub(ctx, repo); hub {\n\t\tif fErr := fetchHub(ctx, gitx.Options{GitDir: repo.GitDir, WorkTree: repo.WorkTree,\n\t\t\tTimeout: opt.Timeout, ExtraEnv: []string{\"GIT_SSH_COMMAND=\" + SSHCommand(opt.Roots.StateRoot)}}); fErr != nil {\n\t\t\tres.Err = fErr\n\t\t\tres.ExitCode = exitNetwork\n\t\t\tres.Receipt.Status = StatusOffline\n\t\t\tres.Receipt.Offline = true\n\t\t\tres.Receipt.Note = \"fetch failed: \" + oneLine(fErr.Error())\n\t\t\twriteReceipt(opt.Roots.StateRoot, res.Receipt, logw)\n\t\t\treturn res\n\t\t}\n\t}\n\n\t// 2. stage and commit LOCALLY, before any network call.\n",
		}},
	},
	{
		ID: "push-loop-bound", Rule: "the push loop is bounded at three attempts", Task: 5,
		File: "internal/sync/sync.go", Test: "TestSync_PushLoopUnderConcurrentPush_BoundedAtThree", Package: "./internal/sync",
		Why: "an unbounded loop under a busy hub spins forever on a hook-adjacent path.",
		Hunks: []Hunk{{
			Old: "\tfor attempt := 1; attempt <= MaxPushAttempts; attempt++ {\n",
			New: "\tfor attempt := 1; attempt <= MaxPushAttempts+2; attempt++ {\n",
		}, {
			Old: "\t\tif attempt == MaxPushAttempts {\n",
			New: "\t\tif attempt == MaxPushAttempts+2 {\n",
		}},
	},
	{
		ID: "contender-skips", Rule: "a lock contender skips immediately and never waits", Task: 5,
		File: "internal/lock/lock.go", Test: "TestLock_ContenderSkipsImmediately", Package: "./internal/lock",
		Why: "the gate runs on every Write|Edit; a lock that blocks puts the network's worst case on the hook path.",
		Hunks: []Hunk{{
			Old: "\t\tif IsLive(*existing, now, stale) {\n",
			New: "\t\tif false {\n",
		}},
	},
	{
		ID: "lock-stale-10m", Rule: "a lock older than ten minutes is broken and taken", Task: 5,
		File: "internal/lock/lock.go", Test: "TestLock_StaleAfterTenMinutes", Package: "./internal/lock",
		Why: "a crashed holder otherwise wedges every derive on the PC until someone notices.",
		Hunks: []Hunk{{
			Old: "\tif now.Sub(h.AcquiredAt.UTC()) >= stale {\n\t\treturn false\n\t}\n",
			New: "\tif false {\n\t\treturn false\n\t}\n",
		}},
	},
	{
		ID: "derive-doctrine-first", Rule: "the derived render puts doctrine first", Task: 3,
		File: "internal/index/render.go", Test: "TestDerive_DoctrineFirst", Package: "./internal/derive",
		Why: "doctrine below the 200-line cap is a standing order nobody is shown.",
		Hunks: []Hunk{{
			Old: "\t\tif dx, dy := isDoctrine(x), isDoctrine(y); dx != dy {\n\t\t\treturn dx\n\t\t}\n",
			New: "\t\tif false {\n\t\t\treturn false\n\t\t}\n",
		}},
	},
	{
		ID: "floor-descending", Rule: "the floor truncates in descending rendered length", Task: 3,
		File: "internal/derive/floor.go", Test: "TestDerive_FloorDescendingRenderedLength", Package: "./internal/derive",
		Why: "ascending order truncates many short hooks to save what one long one would have.",
		Hunks: []Hunk{{
			Old: "\tsort.SliceStable(long, func(i, j int) bool { return long[i].Bytes > long[j].Bytes })\n",
			New: "\tsort.SliceStable(long, func(i, j int) bool { return long[i].Bytes < long[j].Bytes })\n",
		}},
	},
	{
		ID: "floor-stop-below", Rule: "the floor stops below trigger, not below the sync limit", Task: 3,
		File: "internal/derive/floor.go", Test: "TestDerive_FloorStopsBelowTrigger", Package: "./internal/derive",
		Why: "stopping at the sync limit leaves the index one edit away from being refused by the harness.",
		Hunks: []Hunk{{
			Old: "\tstop := opt.StopBelowBytes\n\tif stop <= 0 {\n\t\tstop = store.TriggerBytes\n\t}\n",
			New: "\tstop := opt.StopBelowBytes\n\tif stop <= 0 {\n\t\tstop = store.SyncLimitBytes\n\t}\n",
		}},
	},
	{
		ID: "two-hundred-line-stop", Rule: "the render stops at the injection cap and reports over-inject-limit", Task: 3,
		File: "internal/index/render.go", Test: "TestDerive_TwoHundredLineStop_ReportsOverInjectLimit", Package: "./internal/derive",
		Why: "entries past line 200 are not loaded into context and must be reported, not silently carried.",
		Hunks: []Hunk{{
			Old: "\t\tif limit > 0 && used >= limit {\n\t\t\tres.Omitted = append(res.Omitted, r.Slug)\n\t\t\tcontinue\n\t\t}\n",
			New: "\t\tif false {\n\t\t\tres.Omitted = append(res.Omitted, r.Slug)\n\t\t\tcontinue\n\t\t}\n",
		}},
	},
	{
		ID: "always-lf", Rule: "derive writes LF unconditionally", Task: 3,
		File: "internal/derive/derive.go", Test: "TestDerive_AlwaysLF", Package: "./internal/derive",
		Why: "the prevailing-newline rule makes two PCs render different bytes for the same fact set.",
		Hunks: []Hunk{{
			Old: "\tfinal := index.RenderDerived(hy.Keep, renderOpt)\n\tnewText := final.Text\n",
			New: "\tfinal := index.RenderDerived(hy.Keep, renderOpt)\n\tnewText := strings.ReplaceAll(final.Text, \"\\n\", idx.Newline)\n",
		}},
	},
	{
		ID: "doctrine-never-floored", Rule: "the floor never truncates doctrine", Task: 3,
		File: "internal/derive/floor.go", Test: "TestFloor_NeverTruncatesDoctrine", Package: "./internal/derive",
		Why: "a truncated standing order is a standing order with its condition cut off.",
		Hunks: []Hunk{{
			Old: "\t\tif opt.Doctrine != nil && opt.Doctrine(rec) {\n\t\t\tcontinue\n\t\t}\n",
			New: "\t\tif false {\n\t\t\tcontinue\n\t\t}\n",
		}},
	},
}

// Pending reports whether a mutation's target is not built yet.
func (m Mutation) Pending() bool { return len(m.Hunks) == 0 }
