package merge_test

// Materialize is the only part of the merge engine that touches the work tree, and the
// two rules it exists to enforce are ORDER (fact files first, the derived index last)
// and RESTRAINT (a live session's files are never replaced and its deletions never
// applied).

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
)

// markLive writes a transcript jsonl in the workspace directory so the liveness probe
// sees a session, and returns its mtime.
func (p *pc) markLive(workspace string, age time.Duration) time.Time {
	p.t.Helper()
	dir := filepath.Join(p.projects, workspace)
	mustMkdir(p.t, dir)
	path := filepath.Join(dir, "session.jsonl")
	if err := os.WriteFile(path, []byte("{}\n"), 0o644); err != nil {
		p.t.Fatal(err)
	}
	when := p.clock.Add(-age)
	if err := os.Chtimes(path, when, when); err != nil {
		p.t.Fatal(err)
	}
	return when
}

// TestMaterialize_FactFilesBeforeIndex: no index ever points at a file that is not on
// disk yet, so every fact file lands before MEMORY.md is re-derived.
func TestMaterialize_FactFilesBeforeIndex(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "one.md", fact("One", "d", "h", "one\n"))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	a.tick(time.Minute)
	a.write(ws, "two.md", fact("Two", "d", "h", "two\n"))
	a.write(ws, "three.md", fact("Three", "d", "h", "three\n"))
	a.syncOnce("a adds", a.mo(), ws)

	var order []string
	mo := b.mo()
	mo.OnWrite = func(path string) { order = append(order, path) }
	mo.Derive = func(workspace string) error {
		// The real derive writes MEMORY.md; the observer records that it ran here.
		order = append(order, workspace+"/memory/MEMORY.md")
		return os.WriteFile(filepath.Join(b.storeDir(workspace), "MEMORY.md"), []byte("# Memory Index\n"), 0o644)
	}

	b.tick(2 * time.Minute)
	b.syncOnce("b syncs", mo, ws)

	if len(order) < 3 {
		t.Fatalf("expected two fact writes and a derive, got %v", order)
	}
	last := order[len(order)-1]
	if !strings.HasSuffix(last, "MEMORY.md") {
		t.Fatalf("MEMORY.md must be written LAST, got order %v", order)
	}
	for _, p := range order[:len(order)-1] {
		if strings.HasSuffix(p, "MEMORY.md") {
			t.Fatalf("MEMORY.md was written before a fact file: %v", order)
		}
	}
	if !b.exists(ws, "two.md") || !b.exists(ws, "three.md") {
		t.Fatalf("the fact files were not materialized: %v", order)
	}
}

// TestMaterialize_LiveSessionFileDeferred: while a session is live, a fact file it
// touched since the session began is never replaced under it - the replacement is queued.
func TestMaterialize_LiveSessionFileDeferred(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "live.md", fact("Live", "d", "h", longBody("ORIGINAL")))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	a.tick(time.Minute)
	a.write(ws, "live.md", fact("Live", "d", "h", longBody("FROM-A")))
	a.syncOnce("a edits", a.mo(), ws)

	// B has a live session that touched the same file a minute ago and has NOT committed
	// it yet - the window between a session's write and the next gate commit.
	b.tick(2 * time.Minute)
	b.markLive(ws, 2*time.Minute)
	localText := fact("Live", "d", "h", longBody("TOUCHED-BY-THE-LIVE-SESSION"))
	b.write(ws, "live.md", localText)
	touched := b.clock.Add(-time.Minute)
	if err := os.Chtimes(filepath.Join(b.storeDir(ws), "live.md"), touched, touched); err != nil {
		t.Fatal(err)
	}

	rep := b.mergeOnly(b.mo())

	got, _ := b.read(ws, "live.md")
	if !strings.Contains(got, "TOUCHED-BY-THE-LIVE-SESSION") {
		t.Fatalf("a file the live session touched must not be replaced under it, got %q", got)
	}
	want := ws + "/memory/live.md"
	if !contains(rep.Deferred, want) {
		t.Fatalf("the replacement must be queued on the deferred list, got %v", rep.Deferred)
	}
	d, err := merge.LoadDeferred(b.stateDir, ws)
	if err != nil {
		t.Fatalf("load deferred: %v", err)
	}
	if len(d.Entries) != 1 || d.Entries[0].Path != want || d.Entries[0].Op != merge.OpReplace {
		t.Fatalf("deferred.json does not describe the queued replacement: %+v", d)
	}
}

// TestMaterialize_LiveSessionDeletionDeferred: no deletion is materialized in a live
// workspace at all - not just for files the session touched. A fact file vanishing from
// under a running session is the one change it cannot recover from.
func TestMaterialize_LiveSessionDeletionDeferred(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "doomed.md", fact("Doomed", "d", "h", "doomed\n"))
	a.write(ws, "keep.md", fact("Keep", "d", "h", "keep\n"))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	a.tick(time.Minute)
	a.remove(ws, "doomed.md")
	a.syncOnce("a deletes", a.mo(), ws)

	b.tick(2 * time.Minute)
	b.markLive(ws, time.Minute)
	rep := b.syncOnce("b syncs under a live session", b.mo(), ws)

	if !b.exists(ws, "doomed.md") {
		t.Fatal("a deletion must never be materialized while a session is live in the workspace")
	}
	want := ws + "/memory/doomed.md"
	if !contains(rep.Deferred, want) {
		t.Fatalf("the deletion must be queued, got %v", rep.Deferred)
	}
	d, err := merge.LoadDeferred(b.stateDir, ws)
	if err != nil {
		t.Fatalf("load deferred: %v", err)
	}
	if len(d.Entries) != 1 || d.Entries[0].Op != merge.OpDelete {
		t.Fatalf("deferred.json does not describe the queued deletion: %+v", d)
	}
}

// TestDeferred_AppliedAtSessionEnd: the queue is drained when the session is gone, and an
// entry that is STILL blocked stays queued rather than being dropped.
func TestDeferred_AppliedAtSessionEnd(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "doomed.md", fact("Doomed", "d", "h", "doomed\n"))
	a.write(ws, "edited.md", fact("Edited", "d", "h", longBody("ORIGINAL")))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	a.tick(time.Minute)
	a.remove(ws, "doomed.md")
	a.write(ws, "edited.md", fact("Edited", "d", "h", longBody("FROM-A")))
	a.syncOnce("a changes both", a.mo(), ws)

	b.tick(2 * time.Minute)
	b.markLive(ws, time.Minute)
	rep := b.syncOnce("b syncs under a live session", b.mo(), ws)
	if len(rep.Deferred) != 2 {
		t.Fatalf("both changes should be deferred, got %v", rep.Deferred)
	}

	// Still live: applying now must change nothing and keep the queue.
	drain, err := b.eng.ApplyDeferred(context.Background(), ws, b.mo())
	if err != nil {
		t.Fatalf("apply deferred while live: %v", err)
	}
	if len(drain.Applied) != 0 || len(drain.StillQueued) != 2 {
		t.Fatalf("a still-blocked entry must stay queued, applied=%v queued=%v", drain.Applied, drain.StillQueued)
	}

	// SessionEnd: the transcript is now older than the liveness window.
	b.tick(time.Hour)
	drain, err = b.eng.ApplyDeferred(context.Background(), ws, b.mo())
	if err != nil {
		t.Fatalf("apply deferred at session end: %v", err)
	}
	if len(drain.StillQueued) != 0 {
		t.Fatalf("the queue must be drained once the session is gone, still queued: %v", drain.StillQueued)
	}
	if len(drain.Applied) != 2 {
		t.Fatalf("both entries should have been applied, got %v", drain.Applied)
	}
	if b.exists(ws, "doomed.md") {
		t.Fatal("the queued deletion was not applied at session end")
	}
	got, _ := b.read(ws, "edited.md")
	if !strings.Contains(got, "FROM-A") {
		t.Fatalf("the queued replacement was not applied, got %q", got)
	}
	d, err := merge.LoadDeferred(b.stateDir, ws)
	if err != nil {
		t.Fatalf("load deferred: %v", err)
	}
	if len(d.Entries) != 0 {
		t.Fatalf("the deferred file should be empty after a full drain: %+v", d)
	}
}

// TestMaterialize_LiveSessionDeferred_NoIndexChange is the v2 form of the Pester
// scenario MemoryCompact.Tests.ps1:28 ("a live session skips"): in v2 maintenance is no
// longer gated on liveness - derive always runs - and liveness survives only as the
// materialize guard. A live session therefore means a DEFERRED merge, not a skipped
// derive, and the index the live session is reading is left exactly as it is.
func TestMaterialize_LiveSessionDeferred_NoIndexChange(t *testing.T) {
	f := newFleet(t, "a", "b")
	a, b := f.pcs["a"], f.pcs["b"]

	a.write(ws, "one.md", fact("One", "d", "h", "one\n"))
	a.syncOnce("seed", a.mo(), ws)
	b.fetch()
	if err := b.eng.Adopt(context.Background(), "refs/remotes/"+hubRemote+"/main", b.mo()); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	indexText := "# Memory Index\n\n- [One](one.md) - one hook\n"
	b.write(ws, "MEMORY.md", indexText)

	a.tick(time.Minute)
	a.write(ws, "one.md", fact("One", "d", "h", longBody("FROM-A")))
	a.syncOnce("a edits", a.mo(), ws)

	// B's live session has one.md open and uncommitted.
	b.tick(2 * time.Minute)
	b.markLive(ws, time.Minute)
	sessionText := fact("One", "d", "h", longBody("HELD-BY-THE-LIVE-SESSION"))
	b.write(ws, "one.md", sessionText)
	touched := b.clock.Add(-30 * time.Second)
	if err := os.Chtimes(filepath.Join(b.storeDir(ws), "one.md"), touched, touched); err != nil {
		t.Fatal(err)
	}

	derived := 0
	mo := b.mo()
	mo.Derive = func(string) error { derived++; return nil }
	rep := b.mergeOnly(mo)

	if !contains(rep.Deferred, ws+"/memory/one.md") {
		t.Fatalf("the replacement must be queued, not applied: %v", rep.Deferred)
	}
	if got, _ := b.read(ws, "one.md"); got != sessionText {
		t.Fatalf("the live session's file must be left exactly as it wrote it, got %q", got)
	}
	if derived != 0 {
		t.Fatalf("nothing was materialized, so derive - and with it the index rewrite - must not have run: %d", derived)
	}
	if got, _ := b.read(ws, "MEMORY.md"); got != indexText {
		t.Fatalf("the live session's index must be left untouched, got %q", got)
	}

	// And the index is untouchable by the merge for a structural reason, not a lucky
	// one: MEMORY.md is untracked, so it is not in the merged tree at all.
	listed := b.git("ls-tree", "-r", "--name-only", rep.MergedTree)
	if strings.Contains(listed, "MEMORY.md") {
		t.Fatalf("MEMORY.md must never be tracked - it is derived, not merged: %s", listed)
	}
}

// TestLiveness_FailsClosed: "I could not tell" must mean "do not touch it".
func TestLiveness_FailsClosed(t *testing.T) {
	now := time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)
	root := t.TempDir()

	l := merge.Liveness{ProjectsRoot: root, Now: func() time.Time { return now }}

	// A workspace whose directory does not exist is NOT live: a missing probe directory
	// is skipped, not an error.
	if live, _ := l.Probe("absent"); live {
		t.Fatal("a workspace with no directory at all must not read as live")
	}

	// No probe directory resolvable at all: fail closed.
	bare := merge.Liveness{Now: func() time.Time { return now }}
	if live, _ := bare.Probe(""); !live {
		t.Fatal("with nothing to probe, liveness must fail CLOSED and report live")
	}

	dir := filepath.Join(root, "w")
	mustMkdir(t, dir)
	recent := filepath.Join(dir, "a.jsonl")
	if err := os.WriteFile(recent, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	when := now.Add(-5 * time.Minute)
	if err := os.Chtimes(recent, when, when); err != nil {
		t.Fatal(err)
	}
	live, start := l.Probe("w")
	if !live {
		t.Fatal("a transcript written 5 minutes ago means a live session")
	}
	if !start.Equal(when) {
		t.Fatalf("sessionStart should be the oldest live transcript's mtime, got %v want %v", start, when)
	}

	old := now.Add(-3 * time.Hour)
	if err := os.Chtimes(recent, old, old); err != nil {
		t.Fatal(err)
	}
	if live, _ := l.Probe("w"); live {
		t.Fatal("a transcript 3 hours old is not a live session")
	}

	// A nested jsonl is not a probe hit: the rule is TOP-LEVEL files only.
	nested := filepath.Join(dir, "sub")
	mustMkdir(t, nested)
	np := filepath.Join(nested, "b.jsonl")
	if err := os.WriteFile(np, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(np, when, when); err != nil {
		t.Fatal(err)
	}
	if live, _ := l.Probe("w"); live {
		t.Fatal("liveness does not recurse: a jsonl in a subdirectory is not a session")
	}
}
