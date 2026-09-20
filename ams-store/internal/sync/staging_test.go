package sync

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
)

// TestSync_TrackedNonFactFileDeletionIsStaged closes the other half of the narrowed
// staging.
//
// Narrowing the forced add to *.md stopped new maintenance artifacts reaching the hub -
// and it also stopped git ever noticing that the ones ALREADY TRACKED are gone. The live
// history repo carries five `.bak-*` backups that were moved out of the memory
// directories weeks ago; under a *.md-only pathspec they stay tracked with their stale
// content forever, are pushed to the hub, and land in every other PC's store the first
// time it materializes them. Narrowing what may be ADDED must not narrow what may be
// REMOVED.
func TestSync_TrackedNonFactFileDeletionIsStaged(t *testing.T) {
	sb, repo, _ := pcFixture(t, "ws", map[string]string{"a.md": "---\nname: a\n---\n\nbody\n"})
	ctx := context.Background()
	dir := filepath.Join(sb.ProjectsRoot, "ws", "memory")
	stale := filepath.Join(dir, "a.md.bak-2026-09-15-frontmatter")
	if err := os.WriteFile(stale, []byte("stale backup\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// The history the fleet already has: the artifact is TRACKED, from the days before
	// the pathspec was narrowed. `add -f` on the path itself is how it got there.
	if _, err := gitx.Run(ctx, repo.opts(), "add", "-f", "--", "ws/memory/a.md.bak-2026-09-15-frontmatter"); err != nil {
		t.Fatal(err)
	}
	if err := repo.Stage(ctx, "ws"); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.Commit(ctx, "seed with the legacy artifact", "pc", "local"); err != nil {
		t.Fatal(err)
	}

	// The operator moves the backup out of the store, and writes a NEW artifact that has
	// never been tracked.
	if err := os.Remove(stale); err != nil {
		t.Fatal(err)
	}
	fresh := filepath.Join(dir, "a.md.bak-2026-09-16-linefloor")
	if err := os.WriteFile(fresh, []byte("new backup\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// And a deferred entry names a fact file the live session is holding: the deletion
	// sweep must not touch it either.
	held := "ws/memory/a.md"
	if err := merge.SaveDeferred(sb.StateRoot, "ws", merge.Deferred{
		Tree:    "0000000000000000000000000000000000000000",
		Entries: []merge.DeferredEntry{{Path: held, Op: merge.OpReplace, QueuedAt: "2026-09-15T12:00:00Z"}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, "a.md")); err != nil {
		t.Fatal(err)
	}

	if err := repo.Stage(ctx, "ws"); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.Commit(ctx, "the artifact is gone from disk", "pc", "local"); err != nil {
		t.Fatal(err)
	}

	tracked, err := repo.Tracked(ctx, "ws")
	if err != nil {
		t.Fatal(err)
	}
	set := map[string]bool{}
	for _, p := range tracked {
		set[p] = true
	}
	if set["ws/memory/a.md.bak-2026-09-15-frontmatter"] {
		t.Errorf("a TRACKED artifact that no longer exists on disk is still tracked: %v.\n"+
			"It keeps its stale bytes in every commit and materializes onto every other PC.", tracked)
	}
	if set["ws/memory/a.md.bak-2026-09-16-linefloor"] {
		t.Errorf("an UNTRACKED artifact was added: %v.\n"+
			"Staging deletions of tracked paths must never widen what may be added.", tracked)
	}
	if !set[held] {
		t.Errorf("the deferred path was dropped from the index by the deletion sweep: %v.\n"+
			"A queued change is a merge result already in history; removing it is the same"+
			" resurrection the exclusion exists to prevent.", tracked)
	}
}

// TestSync_StoreRemovedWhileTheWorkspaceStaysIsStaged covers the removal shape the
// workspace-level guard could not see.
//
// Enumeration recognises a store by its index, so a workspace whose `memory` directory is
// gone is never enumerated - and the removal sweep then stats the WORKSPACE directory,
// which is still there, and concludes nothing was removed. The fact files stay tracked
// forever with their stale bytes, are pushed on every sync, and materialize back onto
// every other PC. A project folder outliving its store is the ordinary case: the
// transcripts stay behind when the store is deleted.
func TestSync_StoreRemovedWhileTheWorkspaceStaysIsStaged(t *testing.T) {
	sb, repo, _ := pcFixture(t, "keep", map[string]string{"a.md": "---\nname: a\n---\n\nbody\n"})
	sb.AddStore("gone", []string{"# Memory Index", "", "- [B](b.md)"},
		map[string]string{"b.md": "---\nname: b\n---\n\nbody\n"})

	ctx := context.Background()
	for _, ws := range []string{"keep", "gone"} {
		if err := repo.Stage(ctx, ws); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := repo.Commit(ctx, "seed", "pc", "local"); err != nil {
		t.Fatal(err)
	}

	// The store is deleted; the project directory and its transcripts stay.
	if err := os.RemoveAll(filepath.Join(sb.ProjectsRoot, "gone", "memory")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sb.ProjectsRoot, "gone", "session.jsonl"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}

	removed, err := repo.StageVanishedStores(ctx, []string{"keep"})
	if err != nil {
		t.Fatalf("StageVanishedStores: %v", err)
	}
	if len(removed) != 1 || removed[0] != "gone" {
		t.Fatalf("staged removals = %v, want [gone]: the store is what is tracked, so the"+
			" store directory is what decides whether it is gone", removed)
	}
	if _, err := repo.Commit(ctx, "remove gone", "pc", "local"); err != nil {
		t.Fatal(err)
	}
	still, err := repo.Tracked(ctx, "gone")
	if err != nil {
		t.Fatal(err)
	}
	if len(still) != 0 {
		t.Fatalf("the removed store is still tracked: %v", still)
	}
	kept, err := repo.Tracked(ctx, "keep")
	if err != nil {
		t.Fatal(err)
	}
	if len(kept) == 0 {
		t.Fatal("the surviving store was untracked too - a removal pass that takes the" +
			" live stores with it is worse than the leak it fixes")
	}
}

// TestHistory_CommitHonoursTheDeferredQueueEvenWhenStagedBeforeIt is the commit-time half
// of P5-10 (2026-09-19). Stage excludes queued paths, but a stage taken BEFORE a concurrent
// merge wrote the queue carries the on-disk bytes that merge withheld, and the commit that
// followed re-added three hub deletions on top of the merge. Whatever admitted the
// concurrency, Commit itself must put HEAD's entry back for every queued path first - so
// no verb's commit can ever carry a queued path. A queued path that exists nowhere must
// not fail the commit.
func TestHistory_CommitHonoursTheDeferredQueueEvenWhenStagedBeforeIt(t *testing.T) {
	sb, repo, _ := pcFixture(t, "ws", map[string]string{
		"a.md": "---\nname: a\n---\n\nbody a\n",
		"b.md": "---\nname: b\n---\n\nbody b\n",
	})
	ctx := context.Background()
	if err := repo.Stage(ctx, "ws"); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.Commit(ctx, "seed", "pc", "local"); err != nil {
		t.Fatal(err)
	}
	// The hub deleted a.md and the merge landed: HEAD no longer has it, the file stays on
	// disk because a session is live, and the queue names it.
	if _, err := gitx.Run(ctx, repo.opts(), "rm", "-q", "--cached", "--", "ws/memory/a.md"); err != nil {
		t.Fatal(err)
	}
	merged, err := repo.Commit(ctx, "merge hub: 1 resolved", "pc", "merge")
	if err != nil || merged == "" {
		t.Fatalf("merge commit: %q, %v", merged, err)
	}
	// The stale stage: this pass added a.md from the work tree before the queue existed.
	if _, err := gitx.Run(ctx, repo.opts(), "add", "-f", "--", "ws/memory/a.md"); err != nil {
		t.Fatal(err)
	}
	if err := merge.SaveDeferred(sb.StateRoot, "ws", merge.Deferred{
		Tree: merged,
		Entries: []merge.DeferredEntry{
			{Path: "ws/memory/a.md", Op: merge.OpDelete, QueuedAt: "2026-09-19T17:52:09Z"},
			{Path: "ws/memory/ghost.md", Op: merge.OpDelete, QueuedAt: "2026-09-19T17:52:09Z"},
		},
	}); err != nil {
		t.Fatal(err)
	}

	c, err := repo.Commit(ctx, "sync pc: 1 store(s)", "pc", "local")
	if err != nil {
		t.Fatalf("Commit with a queued path staged: %v", err)
	}
	head, err := repo.Head(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if has, _ := repo.HasFile(ctx, head, "ws/memory/a.md"); has {
		t.Fatalf("commit %s resurrected the queued deletion of a.md", short(c))
	}
	if c != "" {
		t.Fatalf("only the queued path was staged, so nothing should have been committed; got %s", short(c))
	}
	if _, err := os.Stat(filepath.Join(sb.ProjectsRoot, "ws", "memory", "a.md")); err != nil {
		t.Fatalf("the work-tree copy the queue protects must stay on disk: %v", err)
	}
}
