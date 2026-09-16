package merge_test

// The rows of blueprint section 4.3 that no fixture reached.
//
// Coverage over internal/merge, taken from the merge, sync and cli packages together,
// showed count 0 for four blocks of deletion.go: both-deleted, added-on-ours-only,
// added-on-theirs-only and the identical-bytes short circuit. Rows the table handles but
// nothing exercises are rows a refactor can delete for free, which is the opposite of what
// a deletion table is for.
//
// Reaching them takes two deliberate fixtures, and both are shapes the fleet really
// produces:
//
//  1. RENAME PAIRING. auditPaths exists because git's rename heuristic decides presence
//     differently on different gits - measured while this was built, git 2.43 honours
//     NOTHING that turns it off for `merge-tree --write-tree`, and the supported floor is
//     2.38. These fixtures turn merge.renames back ON for the merging PC, which is the
//     only honest way to model that git on the git installed here: with the pairing live,
//     a re-homed fact is folded into its new slug and the table - not git - has to rule on
//     the paths the two sides disagree about.
//
//  2. A MODE DIFFERENCE. Two PCs that create the same slug with the same bytes but
//     different file modes (a fact file that arrived with the exec bit on a Linux PC) is
//     an add/add conflict to git although neither side's CONTENT is in question. That is
//     the one shape that reaches the identical-bytes short circuit, because a path both
//     sides carry is only handed to the table when merge-tree conflicted on it.

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// renamesOn models a git that pairs a remove+add as a rename whatever the config says.
func (p *pc) renamesOn() {
	p.t.Helper()
	p.git("config", "merge.renames", "true")
	p.git("config", "diff.renames", "true")
}

// adopt takes the hub's history as this PC's first sync.
func adopt(t *testing.T, p *pc) {
	t.Helper()
	p.fetch()
	if err := p.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", p.mo()); err != nil {
		t.Fatalf("%s: adopt: %v", p.name, err)
	}
}

// TestMerge_BothSidesDeletedTheSameFile_StaysDeleted is the deletion table's third row.
//
// Two PCs re-home the same fact under two different slugs. To a git with rename detection
// live that is rename/rename: the source path is reported conflicted although NEITHER side
// still has it, and the merged tree holds the two destinations. The table's answer for a
// path both sides deleted is "deleted" - the union rule this design replaced would have
// put the old slug back on every PC forever, next to both of its replacements.
func TestMerge_BothSidesDeletedTheSameFile_StaysDeleted(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	body := longBody("a body long enough for any similarity heuristic to pair")
	a.write(ws, "old.md", fact("Old", "d", "h", body))
	a.write(ws, "keep.md", fact("Keep", "d", "h", "keep\n"))
	a.syncOnce("seed", a.mo(), ws)
	adopt(t, b)

	// B re-homes it under one slug and pushes.
	b.tick(time.Minute)
	b.remove(ws, "old.md")
	b.write(ws, "from-b.md", fact("From B", "d", "h", body))
	b.syncOnce("b re-homes", b.mo(), ws)

	// A re-homes the same fact under a different slug, on a git that pairs.
	a.tick(2 * time.Minute)
	a.renamesOn()
	a.remove(ws, "old.md")
	a.write(ws, "from-a.md", fact("From A", "d", "h", body))
	rep := a.syncOnce("a re-homes", a.mo(), ws)

	if rep.Clean {
		t.Fatal("the fixture did not produce the rename/rename conflict it exists to produce: the both-deleted row was never reached")
	}
	if a.exists(ws, "old.md") {
		t.Fatal("a path both sides deleted came back: that is the union rule the deletion table replaced")
	}
	for _, name := range []string{"from-a.md", "from-b.md", "keep.md"} {
		if !a.exists(ws, name) {
			t.Fatalf("%s was lost while resolving the pairing", name)
		}
	}
	if tracked := a.git("ls-tree", "-r", "--name-only", "refs/heads/main"); strings.Contains(tracked, "old.md") {
		t.Fatalf("old.md is still tracked after both sides deleted it:\n%s", tracked)
	}
	// It must also stay gone on B once A's merge comes back.
	b.tick(3 * time.Minute)
	b.syncOnce("b catches up", b.mo(), ws)
	if b.exists(ws, "old.md") {
		t.Fatal("the deletion came back to B on the next round trip")
	}
}

// TestMerge_AddedOnOursOnly_GitPairingNeverRewritesIt is the table's "absent, added,
// absent" row, reached the way it is reached in the field: git pairs OUR remove+add with
// the other side's edit to the old path and writes the other side's bytes into our new
// slug. The table keeps the adding side's bytes, so the judge's rework stays on the path
// the judge wrote it to and our new file is ours.
func TestMerge_AddedOnOursOnly_GitPairingNeverRewritesIt(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	body := longBody("a body long enough for any similarity heuristic to pair")
	a.write(ws, "old.md", fact("Old", "d", "h", body))
	a.syncOnce("seed", a.mo(), ws)
	adopt(t, b)

	b.tick(time.Minute)
	b.write(ws, "old.md", fact("Old", "d", "h", longBody("JUDGE-REWORKED")))
	b.syncOnce("judge reworks old.md", b.mo(), ws)

	a.tick(2 * time.Minute)
	a.renamesOn()
	a.remove(ws, "old.md")
	ours := fact("New", "d", "h", body)
	a.write(ws, "new.md", ours)
	rep := a.syncOnce("a re-homes", a.mo(), ws)

	got, ok := a.read(ws, "new.md")
	if !ok {
		t.Fatal("the file we added was lost")
	}
	if got != ours {
		t.Fatalf("the merged tree's version of our own new file was taken instead of ours:\n%q", got)
	}
	if strings.Contains(got, "JUDGE-REWORKED") {
		t.Fatalf("git's pairing carried the other side's edit onto our new slug: %q", got)
	}
	old, ok := a.read(ws, "old.md")
	if !ok {
		t.Fatal("the reworked old.md vanished: the pairing swallowed a path the table had to rule on")
	}
	if !strings.Contains(old, "JUDGE-REWORKED") {
		t.Fatalf("old.md must survive as the side that modified it: %q", old)
	}
	if !contains(rep.Resurrected, ws+"/memory/old.md") {
		t.Fatalf("the keep must be reported as resurrected, got %v", rep.Resurrected)
	}
}

// TestMerge_AddedOnTheirsOnly_GitPairingNeverRewritesIt is the mirror row: the other PC
// re-homed the fact and WE edited the old path, so the pairing writes our edit into their
// new slug. Their file must come across as they wrote it.
func TestMerge_AddedOnTheirsOnly_GitPairingNeverRewritesIt(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	body := longBody("a body long enough for any similarity heuristic to pair")
	a.write(ws, "old.md", fact("Old", "d", "h", body))
	a.syncOnce("seed", a.mo(), ws)
	adopt(t, b)

	// B re-homes the fact under a new slug and pushes.
	theirs := fact("New", "d", "h", body)
	b.tick(time.Minute)
	b.remove(ws, "old.md")
	b.write(ws, "new.md", theirs)
	b.syncOnce("b re-homes", b.mo(), ws)

	// A edited the old path meanwhile, on a git that pairs.
	a.tick(2 * time.Minute)
	a.renamesOn()
	a.write(ws, "old.md", fact("Old", "d", "h", longBody("A-EDITED")))
	rep := a.syncOnce("a edits the old path", a.mo(), ws)

	got, ok := a.read(ws, "new.md")
	if !ok {
		t.Fatal("their new file never arrived")
	}
	if got != theirs {
		t.Fatalf("the merged tree's version of their new file was taken instead of theirs:\n%q", got)
	}
	if strings.Contains(got, "A-EDITED") {
		t.Fatalf("git's pairing carried our edit onto their new slug: %q", got)
	}
	old, ok := a.read(ws, "old.md")
	if !ok {
		t.Fatal("our edit of old.md was swallowed by the pairing")
	}
	if !strings.Contains(old, "A-EDITED") {
		t.Fatalf("old.md must survive as the side that modified it: %q", old)
	}
	if !contains(rep.Resurrected, ws+"/memory/old.md") {
		t.Fatalf("the keep must be reported as resurrected, got %v", rep.Resurrected)
	}
}

// TestMerge_TwoPCsAddTheSameSlugWithIdenticalBytes_KeptOnceAsLF is the table's
// identical-bytes row.
//
// Both PCs write the same fact under the same slug, byte for byte - a harvest that ran on
// two PCs from the same source - but one of them has the file's exec bit set, which git
// calls an add/add conflict. There is nothing to merge: the bytes are identical, so the
// file is kept as it is, normalized to LF like every other byte this engine writes.
func TestMerge_TwoPCsAddTheSameSlugWithIdenticalBytes_KeptOnceAsLF(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "seed.md", fact("Seed", "d", "h", "seed\n"))
	a.syncOnce("seed", a.mo(), ws)
	adopt(t, b)

	// The same bytes on both PCs, CRLF as ten live fact files really are.
	twin := strings.ReplaceAll(fact("Twin", "d", "h", "twin body\n"), "\n", "\r\n")
	a.tick(time.Minute)
	a.write(ws, "twin.md", twin)
	a.syncOnce("a harvests the twin", a.mo(), ws)

	b.tick(2 * time.Minute)
	b.write(ws, "twin.md", twin)
	// B's copy carries the exec bit: same content, different mode, which is what makes
	// git conflict on a path neither side disagrees about (measured on both installed
	// gits: 2.43 and 2.55 each report CONFLICT (add/add) for identical blobs at 100644
	// vs 100755).
	//
	// The bit has to be set in BOTH places, and that is not belt and braces. On a
	// Windows checkout `core.filemode` is false, the file system has no exec bit to
	// read and only the INDEX carries the mode, so `update-index --chmod=+x` is the
	// only way to record it. On Linux `core.filemode` is true and the staging pass that
	// runs inside syncOnce re-stats the file, so an index mode with no bit on disk is
	// reset to 100644 before the commit is made - the fixture then produces no conflict
	// at all and the row it exists to reach is never entered. Setting it on disk first
	// and in the index second is what makes the fixture mean the same thing on both.
	if err := os.Chmod(filepath.Join(b.storeDir(ws), "twin.md"), 0o755); err != nil {
		t.Fatal(err)
	}
	b.git("update-index", "--add", "--chmod=+x", ws+"/memory/twin.md")
	rep := b.syncOnce("b harvests the same twin", b.mo(), ws)

	if rep.Clean {
		t.Fatal("the fixture did not produce the add/add conflict it exists to produce: the identical-bytes row was never reached")
	}
	if len(rep.Conflicts) != 0 {
		t.Fatalf("identical bytes are not a conflict in history: %+v", rep.Conflicts)
	}
	got, ok := b.read(ws, "twin.md")
	if !ok {
		t.Fatal("the twin was dropped although both sides added the same bytes")
	}
	if strings.Contains(got, "\r") {
		t.Fatalf("every byte this engine writes leaves as LF; got CRLF: %q", got)
	}
	if want := strings.ReplaceAll(twin, "\r\n", "\n"); got != want {
		t.Fatalf("the kept twin is not the bytes both sides wrote:\ngot  %q\nwant %q", got, want)
	}
	// And A converges on the same bytes without a second merge.
	a.tick(3 * time.Minute)
	a.syncOnce("a catches up", a.mo(), ws)
	if aGot, _ := a.read(ws, "twin.md"); aGot != got {
		t.Fatalf("the two PCs did not converge on the same bytes:\nA %q\nB %q", aGot, got)
	}
}
