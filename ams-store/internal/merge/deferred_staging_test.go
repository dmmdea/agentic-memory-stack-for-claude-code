package merge_test

// The deferred queue is a PENDING MERGE RESULT, not a note in a file.
//
// Two things have to be true of it, and neither was. A queued path must be invisible to
// the next staging pass - otherwise the very next `git add` re-adds the file whose
// deletion was withheld, and re-commits the stale local bytes over the merged blob the
// merge already wrote to history - and the queue has to be drained by a code path the
// binary actually reaches.
//
// This file pins the first half against real git, twice over: for a withheld DELETION and
// for a withheld REPLACE, after one sync pass and after the next one.

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
)

func TestFleet_DeferredPathsAreNotResurrectedByTheNextStagingPass(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "doomed.md", fact("Doomed", "d", "h", "doomed\n"))
	a.write(ws, "shared.md", fact("Shared", "d", "h", numbered("line one", "line twelve")))
	a.write(ws, "keep.md", fact("Keep", "d", "h", "keep\n"))
	a.syncOnce("seed", a.mo(), ws)

	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	// A deletes one fact (a judge migration is exactly this) and edits the TOP of
	// another, then pushes both.
	a.tick(time.Minute)
	a.remove(ws, "doomed.md")
	a.write(ws, "shared.md", fact("Shared", "d", "h", numbered("FROM-A", "line twelve")))
	a.syncOnce("a deletes one and edits another", a.mo(), ws)

	// B has a live session. It edited the BOTTOM of the shared file, so the merge itself
	// is clean and the only reason anything is withheld is the live-session guard.
	b.tick(2 * time.Minute)
	b.markLive(ws, time.Minute)
	sessionText := fact("Shared", "d", "h", numbered("line one", "FROM-THE-LIVE-SESSION"))
	b.write(ws, "shared.md", sessionText)
	touched := b.clock.Add(-30 * time.Second)
	if err := os.Chtimes(filepath.Join(b.storeDir(ws), "shared.md"), touched, touched); err != nil {
		t.Fatal(err)
	}

	rep := b.syncOnce("b syncs under a live session", b.mo(), ws)
	if !contains(rep.Deferred, ws+"/memory/doomed.md") || !contains(rep.Deferred, ws+"/memory/shared.md") {
		t.Fatalf("the scenario did not defer both changes, so it proves nothing: %v", rep.Deferred)
	}

	assertDeferredHeld := func(when string) {
		t.Helper()
		// The deletion must be gone from history on BOTH sides. The file is still on
		// disk under the live session, which is the whole point of the deferral - but
		// history has moved past it and staging it again undoes the merge fleet-wide.
		if tree := b.git("ls-tree", "-r", "--name-only", "HEAD"); strings.Contains(tree, "doomed.md") {
			t.Fatalf("%s: the deferred deletion was re-staged into B's history:\n%s", when, tree)
		}
		if tree := f.hubTree(); strings.Contains(tree, "doomed.md") {
			t.Fatalf("%s: the deferred deletion was pushed back to the hub:\n%s", when, tree)
		}
		if !b.exists(ws, "doomed.md") {
			t.Fatalf("%s: the deferral must leave the file on disk for the live session", when)
		}
		// And the withheld REPLACE is the same defect in the other direction: the merged
		// blob is in history, and a staging pass that re-adds the session's older bytes
		// over it reverts another PC's edit without a word.
		for _, got := range []string{b.git("show", "HEAD:"+ws+"/memory/shared.md"), f.hubBlob(ws + "/memory/shared.md")} {
			if !strings.Contains(got, "FROM-A") {
				t.Fatalf("%s: the stale local bytes were committed over the merged blob:\n%s", when, got)
			}
		}
		if got, _ := b.read(ws, "shared.md"); got != sessionText {
			t.Fatalf("%s: the live session's file must be left exactly as it wrote it, got %q", when, got)
		}
	}

	assertDeferredHeld("after the merge pass")

	// The second pass is the one that resurrects: nothing has changed, the session is
	// still live, and the blanket `git add` sees a file that history no longer has.
	b.syncOnce("b syncs again while the session is still live", b.mo(), ws)
	assertDeferredHeld("after the next sync pass")

	// The queue is untouched by any of it - the session is still live.
	d, err := merge.LoadDeferred(b.stateDir, ws)
	if err != nil {
		t.Fatalf("load deferred: %v", err)
	}
	if len(d.Entries) != 2 {
		t.Fatalf("both entries must still be queued while the session is live: %+v", d.Entries)
	}
}

// numbered renders a twelve-line body whose first and last lines are the caller's, so two
// PCs can edit opposite ends and the three-way body merge is CLEAN. A conflict would pick
// a winner and the test would be measuring the winner rule instead of the staging rule.
func numbered(first, last string) string {
	lines := []string{first}
	for i := 2; i <= 11; i++ {
		lines = append(lines, fmt.Sprintf("body line %d", i))
	}
	return strings.Join(append(lines, last), "\n") + "\n"
}

func (f *fleet) hubTree() string {
	f.t.Helper()
	return mustRun(f.t, f.dir, "git", "--git-dir="+f.hub, "ls-tree", "-r", "--name-only", "main")
}

func (f *fleet) hubBlob(rel string) string {
	f.t.Helper()
	return mustRun(f.t, f.dir, "git", "--git-dir="+f.hub, "show", "main:"+rel)
}
