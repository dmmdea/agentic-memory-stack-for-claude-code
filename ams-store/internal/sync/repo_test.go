package sync

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
)

// These four pin the SHAPE of the history repository, which is the one artifact every
// other package assumes and none of them owns end to end.

// TestSync_TracksTheSharedOverTriggerStamp is decision Q13's transport.
//
// The G7 metric is "hours over trigger without an applied decision", and it is only
// meaningful fleet-wide: a store that has been over the trigger for two days on the Qube
// and was first seen crossing on the Aorus must report the EARLIER time. That needs the
// stamp in the synced tree. It lives outside every store, because nothing but MEMORY.md
// and fact files may sit in a store.
//
// Left untracked, stores[].over_trigger_hours is whatever this PC happens to remember and
// the alarm never fires on the box that did not notice first.
func TestSync_TracksTheSharedOverTriggerStamp(t *testing.T) {
	sb, repo, _ := pcFixture(t, "ws", map[string]string{"a.md": "---\nname: a\n---\n\nbody\n"})
	stamp := filepath.Join(sb.ProjectsRoot, filepath.FromSlash(merge.OverTriggerPath))
	if err := os.MkdirAll(filepath.Dir(stamp), 0o755); err != nil {
		t.Fatal(err)
	}
	doc, _ := json.Marshal(map[string]any{"over_trigger_since": map[string]string{"ws": "2026-09-01T00:00:00Z"}})
	if err := os.WriteFile(stamp, doc, 0o644); err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()
	if err := repo.Stage(ctx, "ws"); err != nil {
		t.Fatal(err)
	}
	if err := repo.StageShared(ctx); err != nil {
		t.Fatalf("StageShared: %v", err)
	}
	if _, err := repo.Commit(ctx, "seed", "pc", "local"); err != nil {
		t.Fatal(err)
	}

	out := mustGit(t, "", "--git-dir="+repo.GitDir, "--work-tree="+repo.WorkTree, "ls-files", "--", merge.OverTriggerPath)
	if !strings.Contains(out, merge.OverTriggerPath) {
		t.Fatalf("%s is not tracked, so the min reducer has nothing to merge and the G7"+
			" clock is per-PC guesswork.\nls-files: %q", merge.OverTriggerPath, out)
	}
}

// TestSync_WholeStoreRemovalPropagates closes the gap the live 2026-09-15 sync exposed:
// it staged five enumerated stores and left 61 deletions of a store whose directory no
// longer exists unstaged, so that store stays tracked on the hub - and on every PC that
// clones it - forever.
//
// The guard is deliberately narrow: the whole WORKSPACE directory must be gone, not just
// its memory folder. A store dir that vanished while its workspace is still there is more
// likely a mount hiccup than a decision, and propagating that as a deletion would carry a
// fleet-wide removal off one bad stat.
func TestSync_WholeStoreRemovalPropagates(t *testing.T) {
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
	if err := os.RemoveAll(filepath.Join(sb.ProjectsRoot, "gone")); err != nil {
		t.Fatal(err)
	}

	removed, err := repo.StageVanishedStores(ctx, []string{"keep"})
	if err != nil {
		t.Fatalf("StageVanishedStores: %v", err)
	}
	if len(removed) != 1 || removed[0] != "gone" {
		t.Fatalf("staged removals = %v, want [gone]", removed)
	}
	if _, err := repo.Commit(ctx, "remove gone", "pc", "local"); err != nil {
		t.Fatal(err)
	}

	still, err := repo.Tracked(ctx, "gone")
	if err != nil {
		t.Fatal(err)
	}
	if len(still) != 0 {
		t.Fatalf("the vanished store is still tracked: %v", still)
	}
	kept, err := repo.Tracked(ctx, "keep")
	if err != nil {
		t.Fatal(err)
	}
	if len(kept) == 0 {
		t.Fatalf("the surviving store was untracked too - a removal pass that takes the" +
			" live stores with it is worse than the leak it fixes")
	}
}

// TestSync_HistoryRepoPinsARepoLocalHooksPath.
//
// A global core.hooksPath (the operator's account-separation and leak-scan hooks) applies
// to every repository on the box, including this one. Those hooks exist to guard GitHub
// pushes; this repo's only remote is the private hub on the tailnet, and its content is
// exactly the private material the scanner refuses. Without a repo-local override the
// hub push is blocked by a hook that was never aimed at it, and every other repo on the
// box must stay guarded, so the override is repo-local and points at an empty directory
// under the state root.
func TestSync_HistoryRepoPinsARepoLocalHooksPath(t *testing.T) {
	_, repo, _ := pcFixture(t, "ws", map[string]string{"a.md": "---\nname: a\n---\n\nbody\n"})
	out := strings.TrimSpace(mustGit(t, "", "--git-dir="+repo.GitDir, "config", "--local", "core.hooksPath"))
	if out == "" {
		t.Fatalf("core.hooksPath is not set locally: a global hooks path refuses the hub push")
	}
	info, err := os.Stat(out)
	if err != nil || !info.IsDir() {
		t.Fatalf("core.hooksPath = %q, which is not a directory that exists: %v", out, err)
	}
	entries, err := os.ReadDir(out)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("core.hooksPath = %q holds %d entries; it must be empty", out, len(entries))
	}
}

// TestSync_RepoShapeMatchesTheMergeEngine is the anti-drift test.
//
// sync writes info/exclude on every pass and the merge engine writes it on every
// Initialize. Two spellings of "what is tracked" that disagree means whichever ran last
// decides, and the loser's rule - here, the re-include that makes the shared stamp
// trackable at all - is silently undone.
func TestSync_RepoShapeMatchesTheMergeEngine(t *testing.T) {
	_, repo, _ := pcFixture(t, "ws", map[string]string{"a.md": "---\nname: a\n---\n\nbody\n"})
	got, err := os.ReadFile(filepath.Join(repo.GitDir, "info", "exclude"))
	if err != nil {
		t.Fatal(err)
	}

	eng := &merge.Engine{GitDir: repo.GitDir, WorkTree: repo.WorkTree, MachineID: "pc"}
	if err := eng.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	want, err := os.ReadFile(filepath.Join(repo.GitDir, "info", "exclude"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(want) {
		t.Fatalf("sync and the merge engine write different info/exclude files.\nsync:\n%s\nmerge:\n%s", got, want)
	}
}
