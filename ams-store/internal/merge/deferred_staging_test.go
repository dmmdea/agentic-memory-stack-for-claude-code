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
	if !contains(rep.DeferredPaths(), ws+"/memory/doomed.md") || !contains(rep.DeferredPaths(), ws+"/memory/shared.md") {
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

// TestDeferred_DrainRechecksTheDiskBeforeApplying is the other half of the queue: what it
// means to APPLY an entry whose file the session went on editing.
//
// The queue is drained after the session ends, which can be hours after the merge that
// filled it. Replaying it blindly would overwrite every edit made in between - the exact
// data loss the deferral was protecting against, arriving late instead of on time. So the
// drain re-checks the file against the bytes the entry was queued against: an untouched
// file takes the merged result, a file edited since is merged three-way with the disk
// side winning, and a deletion whose file was edited is abandoned and reported.
func TestDeferred_DrainRechecksTheDiskBeforeApplying(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "doomed.md", fact("Doomed", "d", "h", body3("first", "middle", "last")))
	a.write(ws, "shared.md", fact("Shared", "d", "h", body3("first", "middle", "last")))
	a.write(ws, "quiet.md", fact("Quiet", "d", "h", body3("first", "middle", "last")))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	a.tick(time.Minute)
	a.remove(ws, "doomed.md")
	a.write(ws, "shared.md", fact("Shared", "d", "h", body3("FROM-A", "middle", "last")))
	a.write(ws, "quiet.md", fact("Quiet", "d", "h", body3("FROM-A", "middle", "last")))
	a.syncOnce("a deletes one and edits two", a.mo(), ws)

	b.tick(2 * time.Minute)
	b.markLive(ws, time.Minute)
	b.touch(ws, "shared.md", fact("Shared", "d", "h", body3("first", "middle", "SESSION")), 30*time.Second)
	b.touch(ws, "quiet.md", fact("Quiet", "d", "h", body3("first", "middle", "last")), 30*time.Second)
	rep := b.syncOnce("b syncs under a live session", b.mo(), ws)
	if len(rep.Deferred) != 3 {
		t.Fatalf("the scenario needs all three changes deferred, got %v", rep.Deferred)
	}

	// The session keeps working AFTER the merge: it rewrites the middle of the file the
	// merge is holding a replacement for, and edits the file the merge wants to delete.
	b.tick(time.Minute)
	b.touch(ws, "shared.md", fact("Shared", "d", "h", body3("first", "LATER-EDIT", "SESSION")), 0)
	b.touch(ws, "doomed.md", fact("Doomed", "d", "h", body3("first", "STILL-IN-USE", "last")), 0)

	// Session over.
	b.tick(time.Hour)
	drain, err := b.eng.ApplyDeferred(context.Background(), ws, b.mo())
	if err != nil {
		t.Fatalf("drain: %v", err)
	}

	if !b.exists(ws, "doomed.md") {
		t.Fatal("a queued deletion whose file was EDITED after the merge must not be applied:" +
			" the edit is the later decision, and the deleted side is still in history")
	}
	if !contains(drain.Resurrected, ws+"/memory/doomed.md") {
		t.Fatalf("an abandoned deletion has to be reported as resurrected, got %+v", drain)
	}
	got, _ := b.read(ws, "shared.md")
	for _, want := range []string{"FROM-A", "LATER-EDIT", "SESSION"} {
		if !strings.Contains(got, want) {
			t.Fatalf("the drain lost %q: it must merge the queued result with the later edit,"+
				" never overwrite it.\n%s", want, got)
		}
	}
	if !contains(drain.Merged, ws+"/memory/shared.md") {
		t.Fatalf("a reconciled replacement must be reported as merged, got %+v", drain)
	}
	// The control: a file nobody touched after the merge takes the merged bytes verbatim.
	quiet, _ := b.read(ws, "quiet.md")
	if !strings.Contains(quiet, "FROM-A") {
		t.Fatalf("an untouched file must take the queued result:\n%s", quiet)
	}
	if len(drain.StillQueued) != 0 {
		t.Fatalf("the session is gone; nothing may stay queued: %v", drain.StillQueued)
	}
}

// TestDeferred_CorruptQueueIsAnErrorNotAnEmptyQueue pins LoadDeferred's fail-closed
// contract at all three of its call sites.
//
// A queue file that exists but does not parse must never read as "nothing is pending".
// Every user of it acts on that answer: the drain would report an empty queue and move
// on, and the staging pass would commit the work tree as if the merge had never withheld
// anything - re-adding the deleted fact and reverting the merged blob, which is precisely
// what the queue exists to prevent. A refactor that swallows the parse error would
// silently restore that behaviour, so it is a test rather than a comment.
func TestDeferred_CorruptQueueIsAnErrorNotAnEmptyQueue(t *testing.T) {
	f := newFleet(t, "a")
	a := f.pcs["a"]
	a.write(ws, "a.md", fact("A", "d", "h", "body"))
	a.commit("seed", ws)

	queue := merge.DeferredPath(a.stateDir, ws)
	mustMkdir(t, filepath.Dir(queue))
	if err := os.WriteFile(queue, []byte("{ this is not json"), 0o644); err != nil {
		t.Fatal(err)
	}

	if q, err := merge.LoadDeferred(a.stateDir, ws); err == nil {
		t.Fatalf("LoadDeferred read a corrupt queue as %+v instead of failing", q)
	}
	if rep, err := a.eng.ApplyDeferred(context.Background(), ws, a.mo()); err == nil {
		t.Fatalf("the drain reported %+v on an unreadable queue instead of refusing", rep)
	}
	a.write(ws, "b.md", fact("B", "d", "h", "body"))
	if _, _, err := a.eng.Commit(context.Background(), merge.CommitOptions{
		Message: "stage while the queue is unreadable", Kind: "local",
		Workspaces: []string{ws}, Date: a.clock, StateRoot: a.stateDir,
	}); err == nil {
		t.Fatal("staging went ahead although what is pending could not be read:" +
			" a pass that cannot tell what the merge withheld must not commit over it")
	}
}

// numbered renders a twelve-line body whose first and last lines are the caller's, so two
// PCs can edit opposite ends and the three-way body merge is CLEAN. A conflict would pick
// a winner and the test would be measuring the winner rule instead of the staging rule.
func numbered(first, last string) string { return body3(first, "body line 6", last) }

// body3 is the same twelve lines with a third editable slot in the middle.
// TestDeferred_HarvestStampDoesNotResurrectAQueuedDeletion pins the 2026-09-17 incident.
//
// The judge migrated a fact on the hub and deleted its file. A PC synced under a live
// session, so the deletion was queued; then that PC's own post-merge derive wrote the
// queued file - it stamped `migrated: <id>` from the trailer that had just arrived in the
// merge, and re-harvested the hook. Neither write is a session's edit, but the drain
// compared bytes and read them as one: every queued deletion came back "resurrected", the
// next push re-added the files, and the migrations were undone for the whole fleet. The
// drain's re-check has to use the deletion table's normalized comparison, so that only a
// difference Canon keeps counts as the session's later decision.
func TestDeferred_HarvestStampDoesNotResurrectAQueuedDeletion(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "migrated.md", fact("Migrated", "d", "h", body3("first", "middle", "last")))
	a.write(ws, "edited.md", fact("Edited", "d", "h", body3("first", "middle", "last")))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	a.tick(time.Minute)
	a.remove(ws, "migrated.md")
	a.remove(ws, "edited.md")
	a.syncOnce("the judge migrated both", a.mo(), ws)

	b.tick(2 * time.Minute)
	b.markLive(ws, time.Minute)
	rep := b.syncOnce("b syncs under a live session", b.mo(), ws)
	if len(rep.Deferred) != 2 {
		t.Fatalf("the scenario needs both deletions deferred, got %v", rep.Deferred)
	}

	// Derive runs after the merge. On the first file it stamps the migrated id and
	// re-harvests the hook - machine writes, nothing a person decided. On the second the
	// session itself rewrites the body afterwards - the one case the queue protects.
	b.tick(time.Minute)
	b.touch(ws, "migrated.md", fact("Migrated", "d", "re-harvested", body3("first", "middle", "last"),
		"migrated: 5bb28df3-243a-4bef-8d1e-79a59045246b"), 0)
	b.touch(ws, "edited.md", fact("Edited", "d", "h", body3("first", "STILL-IN-USE", "last")), 0)

	// Session over.
	b.tick(time.Hour)
	drain, err := b.eng.ApplyDeferred(context.Background(), ws, b.mo())
	if err != nil {
		t.Fatalf("drain: %v", err)
	}

	if b.exists(ws, "migrated.md") {
		t.Fatal("a queued deletion whose file only gained harvest output (migrated:, hook:) must be applied:" +
			" harvest is not a session's edit, and reading it as one resurrects every migration the judge makes")
	}
	if !contains(drain.Applied, ws+"/memory/migrated.md") || contains(drain.Resurrected, ws+"/memory/migrated.md") {
		t.Fatalf("the harvest-only file must be reported applied, never resurrected: %+v", drain)
	}
	// The control: a real body edit after the merge is still the later decision.
	if !b.exists(ws, "edited.md") || !contains(drain.Resurrected, ws+"/memory/edited.md") {
		t.Fatalf("a queued deletion whose BODY the session edited must still be resurrected: %+v", drain)
	}
	if len(drain.StillQueued) != 0 {
		t.Fatalf("the session is gone; nothing may stay queued: %v", drain.StillQueued)
	}
}

func body3(first, mid, last string) string {
	lines := []string{first}
	for i := 2; i <= 11; i++ {
		if i == 6 {
			lines = append(lines, mid)
			continue
		}
		lines = append(lines, fmt.Sprintf("body line %d", i))
	}
	return strings.Join(append(lines, last), "\n") + "\n"
}

// touch writes a fact file and pins its mtime relative to this PC's clock, which is what
// the live-session guard reads.
func (p *pc) touch(workspace, name, content string, before time.Duration) {
	p.t.Helper()
	p.write(workspace, name, content)
	when := p.clock.Add(-before)
	if err := os.Chtimes(filepath.Join(p.storeDir(workspace), name), when, when); err != nil {
		p.t.Fatal(err)
	}
}

func (f *fleet) hubTree() string {
	f.t.Helper()
	return mustRun(f.t, f.dir, "git", "--git-dir="+f.hub, "ls-tree", "-r", "--name-only", "main")
}

func (f *fleet) hubBlob(rel string) string {
	f.t.Helper()
	return mustRun(f.t, f.dir, "git", "--git-dir="+f.hub, "show", "main:"+rel)
}
