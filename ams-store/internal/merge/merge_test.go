package merge_test

// The spec fixtures of blueprint section 10.1, one named Go test each, every one against
// a real git in a temp fleet.

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lint"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

const ws = "g--work"

// TestMerge_ThreeWriters_AllChangesRetained is the design's first spec fixture: three
// PCs each write a different fact and every change survives the round trip.
func TestMerge_ThreeWriters_AllChangesRetained(t *testing.T) {
	f := newFleet(t, "a", "b", "c")
	a, b, c := f.pcs["a"], f.pcs["b"], f.pcs["c"]

	// A seeds the hub.
	a.write(ws, "shared.md", fact("Shared", "shared fact", "shared hook", longBody("shared middle")))
	a.write(ws, "from-a.md", fact("From A", "a fact", "a hook", "a body\n"))
	a.syncOnce("seed", a.mo(), ws)

	// B and C start from the seed.
	for _, p := range []*pc{b, c} {
		p.fetch()
		if err := p.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", p.mo()); err != nil {
			t.Fatalf("%s: adopt: %v", p.name, err)
		}
	}
	if _, ok := b.read(ws, "shared.md"); !ok {
		t.Fatal("B did not receive the seed")
	}

	b.tick(time.Minute)
	b.write(ws, "from-b.md", fact("From B", "b fact", "b hook", "b body\n"))
	b.syncOnce("b writes", b.mo(), ws)

	c.tick(2 * time.Minute)
	c.write(ws, "from-c.md", fact("From C", "c fact", "c hook", "c body\n"))
	rep := c.syncOnce("c writes", c.mo(), ws)
	if len(rep.Conflicts) != 0 {
		t.Fatalf("three disjoint writers must not conflict: %+v", rep.Conflicts)
	}

	a.tick(3 * time.Minute)
	a.syncOnce("a catches up", a.mo(), ws)

	for _, p := range []*pc{a, c} {
		for _, name := range []string{"shared.md", "from-a.md", "from-b.md", "from-c.md"} {
			if !p.exists(ws, name) {
				t.Fatalf("%s lost %s: three writers must all be retained", p.name, name)
			}
		}
	}
}

// TestMerge_DeleteOnA_UntouchedOnB_Absent is the deletion table's first row: deleted on
// one side, untouched on the other, means deleted - not resurrected, which is what the
// union rule this replaces did to every deliberate deletion forever.
//
// It covers the row TWICE on purpose, because only the second form reaches our code.
// When B genuinely has not touched the file, git resolves the deletion itself and the
// path never appears in merge-tree's conflicted list - so a version of this test that
// only did that would pass with the whole deletion table deleted. The row that lands on
// `resolvePath` is the one where B's copy DIFFERS but only in an advisory line: that is
// a modify/delete conflict as far as git is concerned, and it is our table, reading the
// difference as "not a modification", that has to decide it.
//
// The advisory-line case is not a contrivance. It is what every PC does every day: derive
// harvests the index's hook back into the fact file, so a store nobody edited still has
// `hook:` churn waiting for the next sync.
func TestMerge_DeleteOnA_UntouchedOnB_Absent(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	harvested := fact("Harvested", "hook churn only", "the hook as A wrote it", "harvested body\n")
	a.write(ws, "doomed.md", fact("Doomed", "to be deleted", "doomed hook", "doomed body\n"))
	a.write(ws, "harvested.md", harvested)
	a.write(ws, "keep.md", fact("Keep", "kept", "keep hook", "keep body\n"))
	a.syncOnce("seed", a.mo(), ws)

	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}
	if !b.exists(ws, "doomed.md") || !b.exists(ws, "harvested.md") {
		t.Fatal("B did not receive the seed")
	}

	// A deletes both. B touches doomed.md not at all, and rewrites ONLY harvested.md's
	// hook line - a derive harvest, not an edit.
	a.tick(time.Minute)
	a.remove(ws, "doomed.md")
	a.remove(ws, "harvested.md")
	a.syncOnce("a deletes", a.mo(), ws)

	b.tick(2 * time.Minute)
	b.write(ws, "harvested.md", withHook(harvested, "the hook as B harvested it"))
	rep := b.syncOnce("b syncs", b.mo(), ws)

	if b.exists(ws, "doomed.md") {
		t.Fatal("a deletion on A with no change on B must leave the file ABSENT on B; it was resurrected")
	}
	if text, ok := b.read(ws, "harvested.md"); ok {
		t.Fatalf("a hook-only difference is not a modification: the deletion must still win, got %q", text)
	}
	if !b.exists(ws, "keep.md") {
		t.Fatal("the untouched file was lost")
	}
	if len(rep.Resurrected) != 0 {
		t.Fatalf("nothing should have been reported resurrected: %v", rep.Resurrected)
	}

	// And it must stay deleted on A after B's push comes back.
	a.tick(3 * time.Minute)
	a.syncOnce("a syncs", a.mo(), ws)
	if a.exists(ws, "doomed.md") || a.exists(ws, "harvested.md") {
		t.Fatal("the deletion came back to A on the next round trip")
	}
}

// withHook rewrites a fact file's `hook:` line and nothing else, which is exactly the
// shape of the churn derive's harvest step leaves behind.
func withHook(file, hook string) string {
	return reHookLine.ReplaceAllString(file, "hook: \""+hook+"\"\n")
}

var reHookLine = regexp.MustCompile(`(?m)^hook:.*\n`)

// TestMerge_ModifyOnA_DeleteOnB_Resurrected is the deletion table's modify/delete row: a
// local edit since the merge base keeps the file, and the keep is REPORTED.
func TestMerge_ModifyOnA_DeleteOnB_Resurrected(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "contested.md", fact("Contested", "d", "h", longBody("original")))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	// B (playing the judge) deletes it.
	b.tick(time.Minute)
	b.remove(ws, "contested.md")
	b.syncOnce("judge migrates contested.md", b.mo(), ws)

	// A edited the body in the meantime.
	a.tick(2 * time.Minute)
	a.write(ws, "contested.md", fact("Contested", "d", "h", longBody("edited on A")))
	rep := a.syncOnce("a edits", a.mo(), ws)

	body, ok := a.read(ws, "contested.md")
	if !ok {
		t.Fatal("a file modified on A and deleted on B must be KEPT on A")
	}
	if !strings.Contains(body, "edited on A") {
		t.Fatalf("the surviving version must be A's edit, got %q", body)
	}
	want := ws + "/memory/contested.md"
	if !contains(rep.Resurrected, want) {
		t.Fatalf("the keep must be reported as resurrected, got %v", rep.Resurrected)
	}
}

// TestMerge_HookOnlyDifference_NoConflict pins two rules at once, because the mutation
// gate names this test for both:
//
//   - a difference confined to `hook:` (or `modified:`) is NOT a modification for the
//     deletion table, so a hook-only edit does not resurrect a deleted file; and
//   - when both sides really did edit `hook:`, the LOCAL value wins - the judge's hook is
//     taken only when the local side left it alone.
func TestMerge_HookOnlyDifference_NoConflict(t *testing.T) {
	t.Run("both sides edit only the hook: ours wins, no conflict", func(t *testing.T) {
		f := newFleet(t, "a", "b")
		a, b := f.pcs["a"], f.pcs["b"]

		a.write(ws, "h.md", fact("H", "desc", "base hook", longBody("stable")))
		a.syncOnce("seed", a.mo(), ws)
		b.fetch()
		if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
			t.Fatalf("adopt: %v", err)
		}

		// B is the hub-authored side (the judge SHORTENs the hook).
		b.tick(time.Minute)
		b.write(ws, "h.md", fact("H", "desc", "judge hook", longBody("stable")))
		b.syncOnce("judge shortens", b.mo(), ws)

		// A harvested its own hook locally.
		a.tick(2 * time.Minute)
		a.write(ws, "h.md", fact("H", "desc", "local hook", longBody("stable")))
		rep := a.syncOnce("a harvests", a.mo(), ws)

		text, _ := a.read(ws, "h.md")
		if !strings.Contains(text, `hook: "local hook"`) {
			t.Fatalf("when BOTH sides changed hook:, the local value wins; got %q", text)
		}
		if len(rep.Conflicts) != 0 {
			t.Fatalf("a hook-only difference is never a conflict: %+v", rep.Conflicts)
		}
	})

	t.Run("a hook-only edit does not resurrect a deleted file", func(t *testing.T) {
		f := newFleet(t, "a", "b")
		a, b := f.pcs["a"], f.pcs["b"]

		a.write(ws, "g.md", fact("G", "desc", "base hook", longBody("stable")))
		a.syncOnce("seed", a.mo(), ws)
		b.fetch()
		if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
			t.Fatalf("adopt: %v", err)
		}

		b.tick(time.Minute)
		b.remove(ws, "g.md")
		b.syncOnce("judge migrates g.md", b.mo(), ws)

		// A changed nothing but the hook (and the advisory modified: stamp).
		a.tick(2 * time.Minute)
		a.write(ws, "g.md", strings.Replace(
			strings.Replace(fact("G", "desc", "base hook", longBody("stable")), `hook: "base hook"`, `hook: "harvested hook"`, 1),
			"modified: 2026-08-01", "modified: 2026-09-14", 1))
		rep := a.syncOnce("a harvests", a.mo(), ws)

		if a.exists(ws, "g.md") {
			t.Fatal("a hook-only (and modified-only) edit is not a modification: the deletion must stand")
		}
		if len(rep.Resurrected) != 0 {
			t.Fatalf("nothing should have been resurrected: %v", rep.Resurrected)
		}
	})
}

// TestMerge_BodyConflict_NewerCommitWins_LoserInHistory: only a real body conflict has a
// loser; the winner is the side whose last COMMIT to that path is newer - never the
// model-written `modified:` stamp - and the loser stays reachable in history.
func TestMerge_BodyConflict_NewerCommitWins_LoserInHistory(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "c.md", fact("C", "desc", "hook", longBody("ORIGINAL")))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	// A edits first and carries the NEWER `modified:` stamp - the decoy.
	a.tick(time.Minute)
	a.write(ws, "c.md", strings.Replace(
		fact("C", "desc", "hook", longBody("A-VERSION")),
		"modified: 2026-08-01", "modified: 2099-12-31", 1))
	a.syncOnce("a edits", a.mo(), ws)

	// B edits the same line LATER, with an older `modified:` stamp.
	b.tick(5 * time.Minute)
	b.write(ws, "c.md", strings.Replace(
		fact("C", "desc", "hook", longBody("B-VERSION")),
		"modified: 2026-08-01", "modified: 1999-01-01", 1))
	rep := b.syncOnce("b edits", b.mo(), ws)

	text, ok := b.read(ws, "c.md")
	if !ok {
		t.Fatal("the contested file vanished")
	}
	if !strings.Contains(text, "B-VERSION") {
		t.Fatalf("the newer COMMIT (B) must win, whatever the modified: stamps say; got %q", text)
	}
	if strings.Contains(text, "A-VERSION") {
		t.Fatalf("the loser's text must not appear in the merged file: %q", text)
	}

	want := ws + "/memory/c.md"
	var found *merge.Conflict
	for i := range rep.Conflicts {
		if rep.Conflicts[i].Path == want {
			found = &rep.Conflicts[i]
		}
	}
	if found == nil {
		t.Fatalf("a real body conflict must be reported as conflict-in-history: %+v", rep.Conflicts)
	}
	if found.LoserCommit == "" {
		t.Fatal("conflict-in-history must carry the LOSER's commit id, or the finding points at nothing")
	}
	// The loser must still be reachable: that is what "stays in history" means.
	loserText := b.git("show", found.LoserCommit+":"+want)
	if !strings.Contains(loserText, "A-VERSION") {
		t.Fatalf("the loser's version is not reachable at the reported commit: %q", loserText)
	}
	// And the merge commit must name BOTH parents, or the loser drops out of every
	// reachable history the moment the hub is re-cloned.
	parents := strings.Fields(b.git("rev-list", "--parents", "-n", "1", rep.Commit))
	if len(parents) != 3 {
		t.Fatalf("the merge commit must have two parents, got %v", parents)
	}
}

// TestMerge_BodyConflict_EqualCommitTime_MachineIDTiebreak: equal commit seconds are
// common on a fast sync, so the tiebreak must be deterministic and the same on both PCs.
// The rule is the lexically GREATER machine id.
func TestMerge_BodyConflict_EqualCommitTime_MachineIDTiebreak(t *testing.T) {
	f := newFleet(t, "aaa", "zzz")
	a, z := f.pcs["aaa"], f.pcs["zzz"]
	// One clock for both sides: the commits land in the same second.
	same := time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)
	a.clock, z.clock = same, same

	a.write(ws, "t.md", fact("T", "desc", "hook", longBody("ORIGINAL")))
	a.syncOnce("seed", a.mo(), ws)
	z.fetch()
	if err := z.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", z.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	a.write(ws, "t.md", fact("T", "desc", "hook", longBody("AAA-VERSION")))
	a.syncOnce("aaa edits", a.mo(), ws)

	z.write(ws, "t.md", fact("T", "desc", "hook", longBody("ZZZ-VERSION")))
	rep := z.syncOnce("zzz edits", z.mo(), ws)

	text, _ := z.read(ws, "t.md")
	if !strings.Contains(text, "ZZZ-VERSION") {
		t.Fatalf("with equal commit times the lexically greater machine id (zzz) wins; got %q", text)
	}
	if len(rep.Conflicts) == 0 {
		t.Fatal("the conflict must still be reported")
	}
	if rep.Conflicts[0].Tiebreak != merge.TiebreakMachineID {
		t.Fatalf("the report must say the tiebreak was the machine id, got %q", rep.Conflicts[0].Tiebreak)
	}
}

// TestMerge_CRLFOnlyDifference_Identical: git reports a content conflict for a
// CRLF-versus-LF pair (measured), so the engine normalizes before it compares. A side
// whose only difference is line endings has not modified the file.
//
// Two files carry the claim, because the normalization has two jobs and only one of them
// is visible in the merged bytes:
//
//   - e.md is the WRITE side: whatever arrives, what lands on disk is LF, so a CRLF
//     rewrite and a real edit merge without a conflict and without churn.
//   - crlfdel.md is the COMPARE side, the half that would otherwise go untested. Ten live
//     fact files are CRLF today. When one of them meets a deliberate deletion, the
//     deletion table asks whether the CRLF side counts as a modification - and if the
//     comparison is byte-exact, every one of those ten resurrects a fact the judge
//     migrated away, on the first sync after this ships.
func TestMerge_CRLFOnlyDifference_Identical(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	lf := fact("E", "desc", "hook", longBody("STABLE"))
	doomedLF := fact("Doomed", "desc", "hook", longBody("DOOMED"))
	a.write(ws, "e.md", lf)
	a.write(ws, "crlfdel.md", doomedLF)
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	// A rewrites the same text with CRLF and nothing else.
	a.tick(time.Minute)
	a.write(ws, "e.md", strings.ReplaceAll(lf, "\n", "\r\n"))
	a.write(ws, "crlfdel.md", strings.ReplaceAll(doomedLF, "\n", "\r\n"))
	a.syncOnce("a rewrites with CRLF", a.mo(), ws)

	// B makes a real body edit to one file and deletes the other.
	b.tick(2 * time.Minute)
	b.write(ws, "e.md", fact("E", "desc", "hook", longBody("B-EDIT")))
	b.remove(ws, "crlfdel.md")
	rep := b.syncOnce("b edits one and deletes the other", b.mo(), ws)

	text, ok := b.read(ws, "e.md")
	if !ok {
		t.Fatal("the file vanished")
	}
	if len(rep.Conflicts) != 0 {
		t.Fatalf("a CRLF-only difference is not a modification and must never conflict: %+v", rep.Conflicts)
	}
	if !strings.Contains(text, "B-EDIT") {
		t.Fatalf("B's real edit must survive, got %q", text)
	}
	if strings.Contains(text, "\r\n") {
		t.Fatalf("the merge engine writes LF, got CRLF in %q", text)
	}
	if got, ok := b.read(ws, "crlfdel.md"); ok {
		t.Fatalf("A's CRLF rewrite is not a modification, so B's deletion stands; the file came back as %q", got)
	}
	if len(rep.Resurrected) != 0 {
		t.Fatalf("a CRLF-only difference must not resurrect a deleted file: %v", rep.Resurrected)
	}
}

// TestMerge_RecreatedSlugCarriesMigratedID: a slug the judge migrated and a PC
// re-created carries `migrated: <mem0 id>`, so the judge updates by id instead of adding
// a fresh variant every night.
func TestMerge_RecreatedSlugCarriesMigratedID(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "anchor.md", fact("Anchor", "d", "h", "anchor\n"))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	// The judge (B) re-files the slug with its mem0 id.
	b.tick(time.Minute)
	b.write(ws, "recreated.md", fact("Recreated", "judge copy", "judge hook", longBody("SHARED"), "migrated: 8f3c9a21"))
	b.syncOnce("judge files migrated id", b.mo(), ws)

	// A re-created the same slug locally, knowing nothing about the id.
	a.tick(2 * time.Minute)
	a.write(ws, "recreated.md", fact("Recreated", "local copy", "local hook", longBody("SHARED")))
	a.syncOnce("a re-creates the slug", a.mo(), ws)

	text, ok := a.read(ws, "recreated.md")
	if !ok {
		t.Fatal("the re-created slug vanished")
	}
	if !strings.Contains(text, "migrated: 8f3c9a21") {
		t.Fatalf("the migrated id must be carried onto the re-created slug, got %q", text)
	}
}

// TestMerge_NoConflictMarkersOrOrigFilesInStore: no conflict copy ever appears in a
// store or in the injected index. A store is globbed by agents; a .orig file is a second
// copy of a fact that lint has no rule for and the model reads as real.
func TestMerge_NoConflictMarkersOrOrigFilesInStore(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "x.md", fact("X", "d", "h", longBody("ORIGINAL")))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	a.tick(time.Minute)
	a.write(ws, "x.md", fact("X", "d", "h", longBody("A-VERSION")))
	a.syncOnce("a edits", a.mo(), ws)

	b.tick(2 * time.Minute)
	b.write(ws, "x.md", fact("X", "d", "h", longBody("B-VERSION")))
	b.syncOnce("b edits", b.mo(), ws)

	reCopy := regexp.MustCompile(`(?i)\.(orig|rej|LOCAL|REMOTE|BASE)$`)
	entries, err := os.ReadDir(b.storeDir(ws))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if reCopy.MatchString(e.Name()) {
			t.Fatalf("a conflict copy was left in the store: %s", e.Name())
		}
		body, err := os.ReadFile(filepath.Join(b.storeDir(ws), e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		for _, line := range strings.Split(string(body), "\n") {
			if strings.HasPrefix(line, "<<<<<<< ") || strings.HasPrefix(line, ">>>>>>> ") || line == "=======" {
				t.Fatalf("%s contains a conflict marker: %q", e.Name(), line)
			}
		}
	}
}

// TestMerge_RenamesOff_MigrationNotPairedWithNewFile: a PC re-homing a fact under a new
// slug must not let git's similarity heuristic decide what happens to the old one.
//
// Both branches of this fixture were measured against two installed gits before it was
// written. Let rename detection run and ort reads A's remove+add as a rename
// old.md -> new.md, carries B's rework onto new.md, and drops old.md from the merged tree
// with NO conflicted path at all: the deletion table is never consulted, nothing is
// reported, and the judge's version of old.md is simply gone. That is DESIGN's "a
// migration is silently paired with a new file and the deletion is lost", exactly.
//
// This test runs green on git 2.55, where `merge.renames=false` is honoured, AND on git
// 2.43, where nothing turns rename detection off at all - because the guard that decides
// it is not the config. It is the audit in audit.go, which rules on every path the two
// sides disagree about rather than only the ones merge-tree calls conflicted. A rule that
// held on one PC's git and not another's would not be a rule.
//
// So old.md comes back as an ordinary modify/delete, the table keeps the modified side,
// and the keep is REPORTED. The report is the point: a fact two PCs disagreed about must
// never disappear quietly.
func TestMerge_RenamesOff_MigrationNotPairedWithNewFile(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	body := longBody("a long shared body that rename detection would happily pair")
	a.write(ws, "old.md", fact("Old", "d", "h", body))
	a.write(ws, "keep.md", fact("Keep", "d", "h", "keep\n"))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	// B (the hub-side judge) reworks old.md in place.
	b.tick(time.Minute)
	b.write(ws, "old.md", fact("Old", "d", "h", longBody("JUDGE-REWORKED")))
	b.syncOnce("judge reworks old.md", b.mo(), ws)

	// A, meanwhile, re-homes the same content under a new slug: a remove plus an add,
	// which is a rename to any similarity heuristic.
	a.tick(2 * time.Minute)
	a.remove(ws, "old.md")
	a.write(ws, "new.md", fact("New", "d", "h", body))
	rep := a.syncOnce("a re-homes", a.mo(), ws)

	got, ok := a.read(ws, "old.md")
	if !ok {
		t.Fatal("old.md was paired with new.md and vanished: with renames on the judge's rework is never even offered to the deletion table")
	}
	if !strings.Contains(got, "JUDGE-REWORKED") {
		t.Fatalf("the surviving old.md must be the judge's reworked version, got %q", got)
	}
	newText, ok := a.read(ws, "new.md")
	if !ok {
		t.Fatal("the local new file was lost")
	}
	if strings.Contains(newText, "JUDGE-REWORKED") {
		t.Fatalf("the judge's edit was carried onto the new slug, which is the pairing renames are off to prevent: %q", newText)
	}
	if !a.exists(ws, "keep.md") {
		t.Fatal("the untouched file was lost")
	}
	want := ws + "/memory/old.md"
	if !contains(rep.Resurrected, want) {
		t.Fatalf("the keep must be reported as resurrected, got %v", rep.Resurrected)
	}
	if len(rep.Conflicts) != 0 {
		t.Fatalf("a modify/delete is decided by the table, not by a body merge: %+v", rep.Conflicts)
	}
}

// TestSync_OfflineCommitThenResume: the local commit happens BEFORE the fetch, so a PC
// that worked offline keeps its history and pushes it whole when the hub reappears.
func TestSync_OfflineCommitThenResume(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "seed.md", fact("Seed", "d", "h", "seed\n"))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	// B goes offline: point its remote at a path that does not exist.
	b.git("remote", "set-url", hubRemote, filepath.Join(f.dir, "nowhere.git"))

	b.tick(time.Minute)
	b.write(ws, "offline-1.md", fact("Offline 1", "d", "h", "one\n"))
	oid1 := b.commit("offline work 1", ws)
	if oid1 == "" {
		t.Fatal("the offline commit must be made even with no reachable remote")
	}
	if err := b.eng.Fetch(context.Background(), hubRemote); err == nil {
		t.Fatal("the fetch to a missing remote should have failed; the fixture is not offline")
	}

	b.tick(2 * time.Minute)
	b.write(ws, "offline-2.md", fact("Offline 2", "d", "h", "two\n"))
	oid2 := b.commit("offline work 2", ws)
	if oid2 == "" {
		t.Fatal("the second offline commit was not made")
	}

	// Meanwhile A pushes something of its own.
	a.tick(3 * time.Minute)
	a.write(ws, "from-a.md", fact("From A", "d", "h", "a\n"))
	a.syncOnce("a works", a.mo(), ws)

	// The hub comes back.
	b.git("remote", "set-url", hubRemote, f.hub)
	b.tick(4 * time.Minute)
	b.syncOnce("b resumes", b.mo(), ws)

	for _, name := range []string{"offline-1.md", "offline-2.md", "from-a.md", "seed.md"} {
		if !b.exists(ws, name) {
			t.Fatalf("%s is missing after the resume", name)
		}
	}
	// Both offline commits must still be in the history, not squashed away.
	log := b.git("log", "--format=%H", "refs/heads/main")
	for _, oid := range []string{oid1, oid2} {
		if !strings.Contains(log, oid) {
			t.Fatalf("offline commit %s is not in the post-resume history", oid)
		}
	}
	// And A must receive them.
	a.tick(5 * time.Minute)
	a.syncOnce("a catches up", a.mo(), ws)
	if !a.exists(ws, "offline-1.md") || !a.exists(ws, "offline-2.md") {
		t.Fatal("A did not receive B's offline work")
	}
}

// TestMerge_OverTriggerJSON_MinWins is decision Q13: the over_trigger_since stamp rides
// in the synced tree at <PROJECTS_ROOT>/.ams/over-trigger.json - outside every store,
// because nothing but MEMORY.md and fact files may live inside one - and merges with a
// MIN reducer, because the first crossing is what the starvation metric measures.
func TestMerge_OverTriggerJSON_MinWins(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "anchor.md", fact("Anchor", "d", "h", "anchor\n"))
	a.writeRaw(merge.OverTriggerPath, `{"over_trigger_since":{"`+ws+`":"2026-09-10T08:00:00Z"}}`+"\n")
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	// B crossed later for this workspace and first for another.
	b.tick(time.Minute)
	b.writeRaw(merge.OverTriggerPath, `{"over_trigger_since":{"`+ws+`":"2026-09-12T08:00:00Z","other":"2026-09-01T00:00:00Z"}}`+"\n")
	b.syncOnce("b stamps", b.mo(), ws)

	// A crossed EARLIER than the seed for this workspace.
	a.tick(2 * time.Minute)
	a.writeRaw(merge.OverTriggerPath, `{"over_trigger_since":{"`+ws+`":"2026-09-05T08:00:00Z"}}`+"\n")
	a.syncOnce("a stamps", a.mo(), ws)

	raw, err := os.ReadFile(filepath.Join(a.projects, filepath.FromSlash(merge.OverTriggerPath)))
	if err != nil {
		t.Fatalf("over-trigger.json is missing after the merge: %v", err)
	}
	got, err := merge.ParseOverTrigger(raw)
	if err != nil {
		t.Fatalf("parse merged over-trigger.json: %v", err)
	}
	if got.OverTriggerSince[ws] != "2026-09-05T08:00:00Z" {
		t.Fatalf("min wins: expected the earliest crossing 2026-09-05T08:00:00Z, got %q (%s)", got.OverTriggerSince[ws], raw)
	}
	if got.OverTriggerSince["other"] != "2026-09-01T00:00:00Z" {
		t.Fatalf("a workspace known to only one side must be kept, got %s", raw)
	}
	if strings.Contains(string(raw), "\r\n") {
		t.Fatalf("the merged state file must be LF: %q", raw)
	}
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

// TestMerge_IndexIsNeverTracked: MEMORY.md is derived, not merged. It is untracked on
// every PC and on the hub, so it can never conflict and a sync can never clobber the
// copy a live session is reading.
//
// Two guards keep it out and BOTH are load-bearing: info/exclude, and the `:(exclude)`
// pathspec on the forced `git add` (the -f that lets fact files past the exclude would
// otherwise force MEMORY.md in with them).
func TestMerge_IndexIsNeverTracked(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "one.md", fact("One", "d", "h", "one\n"))
	aIndex := "# Memory Index\n\n- [One](one.md) - rendered on A\n"
	a.write(ws, "MEMORY.md", aIndex)
	a.syncOnce("seed", a.mo(), ws)

	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}
	bIndex := "# Memory Index\n\n- [One](one.md) - rendered on B, deliberately different\n"
	b.write(ws, "MEMORY.md", bIndex)

	b.tick(time.Minute)
	b.write(ws, "two.md", fact("Two", "d", "h", "two\n"))
	rep := b.syncOnce("b adds", b.mo(), ws)

	for _, rev := range []string{"refs/heads/main", "refs/remotes/" + hubRemote + "/main"} {
		listed := b.git("ls-tree", "-r", "--name-only", rev)
		if strings.Contains(listed, store.IndexName) {
			t.Fatalf("%s tracks the index; it must be untracked everywhere:\n%s", rev, listed)
		}
	}
	if rep.MergedTree != "" {
		listed := b.git("ls-tree", "-r", "--name-only", rep.MergedTree)
		if strings.Contains(listed, store.IndexName) {
			t.Fatalf("the merged tree carries the index:\n%s", listed)
		}
	}

	a.tick(2 * time.Minute)
	a.syncOnce("a catches up", a.mo(), ws)

	if got, _ := a.read(ws, "MEMORY.md"); got != aIndex {
		t.Fatalf("A's index was changed by the sync: %q", got)
	}
	if got, _ := b.read(ws, "MEMORY.md"); got != bIndex {
		t.Fatalf("B's index was changed by the sync: %q", got)
	}
}

// TestMerge_UnrelatedHistoriesMergeOnEverySupportedGit is the FIRST sync of the fleet:
// two PCs that each ran `git init` locally before either had ever pushed, so their
// histories share no commit at all. It is the shape the seed produces on every PC after
// the Qube's, and it happens exactly once per box - which is also why it can ship broken.
//
// The failure it pins is VERSION-dependent, and therefore invisible on the machine it was
// written on. The unrelated-histories path has to hand merge-tree a base; git 2.55 accepts
// the empty TREE for --merge-base, and git 2.43 refuses it outright as "not a commit". The
// design's floor is git 2.38, so the version that refuses is inside the supported range
// and the version that accepts is the one the engine was developed against. The Linux CI
// runner is what notices.
func TestMerge_UnrelatedHistoriesMergeOnEverySupportedGit(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "a.md", fact("A", "written on A", "the a hook", "a body\n"))
	a.commit("A writes")
	if r := a.push(); !r.OK {
		t.Fatalf("A could not seed the hub: %s", r.Stderr)
	}

	// B has never fetched, so its own main is a root commit unrelated to A's.
	b.write(ws, "b.md", fact("B", "written on B", "the b hook", "b body\n"))
	b.commit("B writes")

	rep := b.mergeOnly(b.mo())
	if rep.Commit == "" {
		t.Fatalf("nothing was merged across the unrelated histories: %+v", rep)
	}
	for _, name := range []string{"a.md", "b.md"} {
		if !b.exists(ws, name) {
			t.Errorf("%s is missing from B's work tree after the merge; an unrelated-history"+
				" merge must be a union, not a replacement", name)
		}
	}
	if r := b.push(); !r.OK {
		t.Fatalf("B could not push the merge: %s", r.Stderr)
	}
}

// TestOverTrigger_ProducerAndReducerRenderTheSameBytes pins the two halves of the Q13
// stamp to ONE renderer.
//
// The producer (lint.RecordOverTrigger, the maintenance path) and the reducer
// (merge.MergeOverTrigger, every sync that sees the path on both sides) write the same
// logical content to the same tracked file. Two renderers means the file flips format on
// every change: each stamp costs an extra commit, and no two PCs agree on the bytes until
// a merge has run. Byte-identity is the only assertion that cannot be satisfied by a
// second implementation that happens to be equivalent today.
func TestOverTrigger_ProducerAndReducerRenderTheSameBytes(t *testing.T) {
	root := t.TempDir()
	crossed := time.Date(2026, 9, 10, 8, 0, 0, 0, time.UTC)
	if _, err := lint.RecordOverTrigger(root, ws, true, crossed); err != nil {
		t.Fatalf("record the crossing: %v", err)
	}
	produced, err := os.ReadFile(lint.StampPath(root))
	if err != nil {
		t.Fatalf("the producer wrote no stamp file: %v", err)
	}
	reduced, err := merge.MergeOverTrigger(produced, produced)
	if err != nil {
		t.Fatalf("reduce the produced bytes with themselves: %v", err)
	}
	if !bytes.Equal(produced, reduced) {
		t.Fatalf("two renderers for one file: producer wrote %d B %q, the reducer rewrites it as %d B %q",
			len(produced), produced, len(reduced), reduced)
	}
}

// mustRecord drives the MAINTENANCE path's stamp writer, which is the only producer of
// the Q13 clock. Tests that hand-build the file with writeRaw cannot see a clear at all:
// a clear is an ABSENCE in the producer's output, and an absence is exactly what a union
// reducer cannot distinguish from "this side has not heard yet".
func mustRecord(t *testing.T, projectsRoot, workspace string, overTrigger bool, now time.Time) {
	t.Helper()
	if _, err := lint.RecordOverTrigger(projectsRoot, workspace, overTrigger, now); err != nil {
		t.Fatalf("record over-trigger=%v at %s: %v", overTrigger, now.Format(time.RFC3339), err)
	}
}

// hoursOverTrigger is what G7 reads: nil when the store has no live clock.
func hoursOverTrigger(t *testing.T, projectsRoot, workspace string, now time.Time) *float64 {
	t.Helper()
	s, err := lint.ReadStamps(projectsRoot)
	if err != nil {
		t.Fatalf("read the stamp file at %s: %v", projectsRoot, err)
	}
	return s.HoursOverTrigger(workspace, now)
}

// TestMerge_OverTriggerJSON_ClearSurvivesAPCThatStillCarriesTheStamp is the other half of
// decision Q13, and the half the min reducer got wrong.
//
// MIN is right for two PCs that both crossed: the earliest crossing is what the G7
// starvation metric measures. But a store that converges back under the trigger CLEARS
// its stamp, and a clear is an absence. A union over keys cannot tell "this store is
// fixed" from "this PC has not re-derived yet", so the stamp came straight back from the
// PC that still had it and the clock could never reset once any PC had started it - the
// 24 h alarm would fire forever on a store that was fixed hours ago.
//
// The clear therefore has to be representable in the merged state, and a re-cross after
// a clear has to start a NEW clock rather than resurrect the old one.
func TestMerge_OverTriggerJSON_ClearSurvivesAPCThatStillCarriesTheStamp(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	crossed := time.Date(2026, 9, 10, 8, 0, 0, 0, time.UTC)
	cleared := crossed.Add(6 * time.Hour)
	recrossed := cleared.Add(6 * time.Hour)

	// A's store crosses the trigger and the fleet learns when.
	a.write(ws, "anchor.md", fact("Anchor", "d", "h", "anchor\n"))
	mustRecord(t, a.projects, ws, true, crossed)
	a.syncOnce("a stamps", a.mo(), ws)

	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}
	if hoursOverTrigger(t, b.projects, ws, cleared) == nil {
		t.Fatal("B never received A's stamp; nothing after this point would prove anything")
	}

	// The store converges. A's maintenance pass clears the clock.
	a.tick(time.Hour)
	mustRecord(t, a.projects, ws, false, cleared)
	a.syncOnce("a clears", a.mo(), ws)

	// B has not re-derived, so it still carries the stamp. Its next sync merges the two.
	b.tick(2 * time.Hour)
	b.write(ws, "b.md", fact("B", "d", "h", "b body\n"))
	b.syncOnce("b syncs", b.mo(), ws)
	if h := hoursOverTrigger(t, b.projects, ws, recrossed); h != nil {
		t.Fatalf("the clear was undone on B: the merge restored the old clock at %.1f h", *h)
	}

	// And it must not come back to A on the return trip either.
	a.tick(time.Hour)
	a.syncOnce("a catches up", a.mo(), ws)
	if h := hoursOverTrigger(t, a.projects, ws, recrossed); h != nil {
		t.Fatalf("the clear was undone on A: B's stale stamp came back at %.1f h", *h)
	}

	// A re-cross after the clear is a NEW clock, not a resumption of the old one.
	b.tick(time.Hour)
	mustRecord(t, b.projects, ws, true, recrossed)
	b.write(ws, "b2.md", fact("B2", "d", "h", "b2 body\n"))
	b.syncOnce("b re-crosses", b.mo(), ws)

	a.tick(time.Hour)
	a.write(ws, "a2.md", fact("A2", "d", "h", "a2 body\n"))
	a.syncOnce("a catches up again", a.mo(), ws)
	h := hoursOverTrigger(t, a.projects, ws, recrossed.Add(time.Hour))
	if h == nil {
		t.Fatal("the re-cross after the clear never reached A; a cleared store can never be stamped again")
	}
	if *h != 1.0 {
		t.Fatalf("the re-cross must start a fresh clock: G7 reads %.1f h, want 1.0 - the old crossing at %s is dead",
			*h, crossed.Format(time.RFC3339))
	}
}
