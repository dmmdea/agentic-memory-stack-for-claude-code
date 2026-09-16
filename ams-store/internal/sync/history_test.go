package sync

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// historyFixture builds a sandbox with one store, a transcript that must never be
// tracked, and an initialized out-of-tree history repo.
func historyFixture(t *testing.T) (*testutil.Sandbox, Repo, store.Roots) {
	t.Helper()
	testutil.RequireGit(t)
	sb := testutil.NewSandbox(t)
	sb.AddStore("ws", []string{"# Memory Index", "", "- [A](a.md)", "- [B](b.md)"}, map[string]string{
		"a.md": testutil.FactFile("a", "d", "project", "body a"),
		"b.md": testutil.FactFile("b", "d", "project", "body b"),
	})
	// A transcript in the workspace directory: the file the history repo must never see.
	if err := os.WriteFile(filepath.Join(sb.ProjectsRoot, "ws", "transcript.jsonl"), []byte(`{"secret":1}`), 0o644); err != nil {
		t.Fatal(err)
	}
	roots := store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot}
	repo := NewRepo(roots)
	if err := repo.Initialize(context.Background()); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	return sb, repo, roots
}

func snapshot(t *testing.T, repo Repo, ws, msg string) string {
	t.Helper()
	ctx := context.Background()
	if err := repo.Stage(ctx, ws); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	sha, err := repo.Commit(ctx, msg, "test-machine", "local")
	if err != nil {
		t.Fatalf("Commit: %v", err)
	}
	return sha
}

// TestHistory_OutOfTreeNoGitInStore is the counterpart of MemoryStoreLib.Tests.ps1:317 -
// "creates no .git inside the tree and has no remote".
func TestHistory_OutOfTreeNoGitInStore(t *testing.T) {
	sb, repo, _ := historyFixture(t)
	for _, p := range []string{
		filepath.Join(sb.ProjectsRoot, ".git"),
		filepath.Join(sb.ProjectsRoot, "ws", ".git"),
		filepath.Join(sb.ProjectsRoot, "ws", "memory", ".git"),
	} {
		if _, err := os.Stat(p); err == nil {
			t.Fatalf("a .git appeared inside the tree at %s", p)
		}
	}
	if _, err := os.Stat(filepath.Join(repo.GitDir, "HEAD")); err != nil {
		t.Fatalf("the history repo is not where it belongs: %v", err)
	}
	remotes, err := repo.Remotes(context.Background())
	if err != nil {
		t.Fatalf("Remotes: %v", err)
	}
	if len(remotes) != 0 {
		t.Fatalf("a freshly initialized history repo has remotes: %v", remotes)
	}
}

// TestHistory_SnapshotExcludesTranscriptsIdempotent is the counterpart of
// MemoryStoreLib.Tests.ps1:323 - "snapshots the store only (transcripts excluded), and a
// second identical snapshot is a no-op".
func TestHistory_SnapshotExcludesTranscriptsIdempotent(t *testing.T) {
	_, repo, _ := historyFixture(t)
	ctx := context.Background()

	sha1 := snapshot(t, repo, "ws", "snap 1")
	if len(sha1) != 40 {
		t.Fatalf("snapshot returned %q, want a 40-char sha", sha1)
	}
	tracked, err := repo.Tracked(ctx, "ws")
	if err != nil {
		t.Fatalf("Tracked: %v", err)
	}
	joined := strings.Join(tracked, "\n")
	if strings.Contains(joined, ".jsonl") {
		t.Fatalf("a transcript was tracked: %v", tracked)
	}
	ok, err := repo.HasFile(ctx, sha1, "ws/memory/b.md")
	if err != nil || !ok {
		t.Fatalf("the snapshot does not contain ws/memory/b.md: %v %v", ok, err)
	}

	// A second snapshot with nothing changed commits nothing: git's own diff is the
	// no-op guard, so an unchanged night leaves no commit and no noise.
	if sha2 := snapshot(t, repo, "ws", "snap 1 again"); sha2 != "" {
		t.Fatalf("an identical second snapshot committed %s", sha2)
	}
}

// TestSync_IndexNeverConflicts pins the rule the whole merge design rests on: MEMORY.md
// is DERIVED, so it is untracked everywhere and can never be a merge conflict.
//
// It is the test the mutation "remove MEMORY.md from info/exclude" must turn red. The
// force flag on the staging pathspec is exactly what would otherwise drag it in.
func TestSync_IndexNeverConflicts(t *testing.T) {
	_, repo, _ := historyFixture(t)
	ctx := context.Background()
	snapshot(t, repo, "ws", "snap")

	tracked, err := repo.Tracked(ctx, "ws")
	if err != nil {
		t.Fatalf("Tracked: %v", err)
	}
	for _, p := range tracked {
		if strings.HasSuffix(p, "/"+store.IndexName) {
			t.Fatalf("MEMORY.md is tracked (%s); it must be derived, never merged", p)
		}
	}
	if len(tracked) != 2 {
		t.Fatalf("tracked %v, want exactly the two fact files", tracked)
	}
}

// TestHistory_PerFileRestoreLeavesNewerFiles is the counterpart of
// MemoryStoreLib.Tests.ps1:331 - "records a deletion and restores exactly that file,
// leaving newer files alone".
func TestHistory_PerFileRestoreLeavesNewerFiles(t *testing.T) {
	sb, repo, _ := historyFixture(t)
	ctx := context.Background()
	sha1 := snapshot(t, repo, "ws", "snap 1")

	memDir := filepath.Join(sb.ProjectsRoot, "ws", "memory")
	if err := os.Remove(filepath.Join(memDir, "b.md")); err != nil {
		t.Fatal(err)
	}
	// A file a live session wrote after the snapshot. A DIRECTORY restore would revert
	// it; the per-file rule is what makes a rollback safe under a live session.
	newBySession := filepath.Join(memDir, "new-by-session.md")
	if err := os.WriteFile(newBySession, []byte("live session wrote this"), 0o644); err != nil {
		t.Fatal(err)
	}

	sha2 := snapshot(t, repo, "ws", "after delete")
	if sha2 == "" {
		t.Fatal("the deletion was not committed")
	}
	if ok, err := repo.HasFile(ctx, sha2, "ws/memory/b.md"); err != nil || ok {
		t.Fatalf("b.md survived the deletion commit: %v %v", ok, err)
	}
	if err := repo.RestoreFile(ctx, sha1, "ws/memory/b.md"); err != nil {
		t.Fatalf("RestoreFile: %v", err)
	}
	if _, err := os.Stat(filepath.Join(memDir, "b.md")); err != nil {
		t.Fatalf("b.md was not restored: %v", err)
	}
	if _, err := os.Stat(newBySession); err != nil {
		t.Fatalf("the per-file restore clobbered a file the job never touched: %v", err)
	}

	diff, err := repo.Diff(ctx, sha1, sha2, "ws/memory")
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	if !strings.Contains(diff, "b.md") {
		t.Fatalf("the diff does not mention b.md:\n%s", diff)
	}
}

// TestHistory_CommitAndDiffOutOfTree is the counterpart of
// MemoryCompactRobustness.Tests.ps1:226 - "commits to an out-of-tree repo with no remote,
// writes a diff, and never creates .git in a store".
//
// One assertion is deliberately NOT carried over. The Pester original asserts the diff
// contains `diff --git a/ws/memory/MEMORY.md`, because in v1 the index was tracked. In v2
// MEMORY.md is derived and untracked (DESIGN:199-200), so the audit diff is over the FACT
// FILES - which is the thing that actually changed. Asserting the old line would pin a
// behaviour the design removed on purpose.
func TestHistory_CommitAndDiffOutOfTree(t *testing.T) {
	sb, repo, _ := historyFixture(t)
	ctx := context.Background()
	sha1 := snapshot(t, repo, "ws", "snap 1")

	factPath := filepath.Join(sb.ProjectsRoot, "ws", "memory", "a.md")
	if err := os.WriteFile(factPath, []byte(testutil.FactFile("a", "d", "project", "detail number 1 kept short")), 0o644); err != nil {
		t.Fatal(err)
	}
	sha2 := snapshot(t, repo, "ws", "shortened a")
	if sha2 == "" || sha2 == sha1 {
		t.Fatalf("the edit produced no commit (sha1=%s sha2=%s)", sha1, sha2)
	}
	if len(sha2) != 40 {
		t.Fatalf("commit %q is not a 40-char sha", sha2)
	}

	diff, err := repo.Diff(ctx, sha1, sha2, "ws/memory")
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	if !strings.Contains(diff, "diff --git a/ws/memory/a.md") {
		t.Fatalf("the audit diff does not name the changed fact file:\n%s", diff)
	}
	if !strings.Contains(diff, "+detail number 1 kept short") {
		t.Fatalf("the audit diff does not show what changed:\n%s", diff)
	}
	if strings.Contains(diff, store.IndexName) {
		t.Fatalf("MEMORY.md appears in the history diff; it must be untracked:\n%s", diff)
	}
	for _, p := range []string{
		filepath.Join(sb.ProjectsRoot, ".git"),
		filepath.Join(sb.ProjectsRoot, "ws", "memory", ".git"),
	} {
		if _, err := os.Stat(p); err == nil {
			t.Fatalf("a .git appeared at %s", p)
		}
	}
}

// TestHistory_InitializeIsIdempotentAndPinsConfig: a second Initialize converges a
// hand-edited repo back instead of leaving it drifted.
func TestHistory_InitializeIsIdempotentAndPinsConfig(t *testing.T) {
	_, repo, _ := historyFixture(t)
	ctx := context.Background()
	// Drift it the way a helpful global config would.
	gitRun(t, repo, "config", "merge.renames", "true")
	gitRun(t, repo, "config", "core.autocrlf", "true")
	if err := os.WriteFile(filepath.Join(repo.GitDir, "info", "exclude"), []byte("\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := repo.Initialize(ctx); err != nil {
		t.Fatalf("second Initialize: %v", err)
	}
	if got := gitOut(t, repo, "config", "--get", "merge.renames"); got != "false" {
		t.Fatalf("merge.renames is %q after re-init; renames OFF is what keeps a migration from being paired with a new file", got)
	}
	if got := gitOut(t, repo, "config", "--get", "core.autocrlf"); got != "false" {
		t.Fatalf("core.autocrlf is %q after re-init", got)
	}
	b, err := os.ReadFile(filepath.Join(repo.GitDir, "info", "exclude"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), store.IndexName) {
		t.Fatalf("info/exclude was not restored:\n%s", b)
	}
}

func gitRun(t *testing.T, repo Repo, args ...string) {
	t.Helper()
	full := append([]string{"--git-dir=" + repo.GitDir, "--work-tree=" + repo.WorkTree}, args...)
	if out, err := exec.Command("git", full...).CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}

func gitOut(t *testing.T, repo Repo, args ...string) string {
	t.Helper()
	full := append([]string{"--git-dir=" + repo.GitDir, "--work-tree=" + repo.WorkTree}, args...)
	out, err := exec.Command("git", full...).Output()
	if err != nil {
		t.Fatalf("git %v: %v", args, err)
	}
	return strings.TrimSpace(string(out))
}

// TestHistory_InitializeSetsLongPathsOnWindows: git on Windows refuses to open a work-tree
// directory whose path exceeds MAX_PATH unless core.longpaths is on. The projects root
// holds workspace directories over 230 characters, so a store under one would be
// unstageable (seen on a live repo, 2026-09-15). Off Windows the key is not set at all.
func TestHistory_InitializeSetsLongPathsOnWindows(t *testing.T) {
	_, repo, _ := historyFixture(t)
	if err := repo.Initialize(context.Background()); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	out, _ := exec.Command("git", "--git-dir="+repo.GitDir, "config", "--get", "core.longpaths").Output()
	got := strings.TrimSpace(string(out))
	if runtime.GOOS == "windows" {
		if got != "true" {
			t.Fatalf("core.longpaths is %q on windows after Initialize, want true", got)
		}
		return
	}
	if got != "" {
		t.Fatalf("core.longpaths is %q off windows, want unset", got)
	}
}
