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
