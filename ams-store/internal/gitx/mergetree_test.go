package gitx

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// tempRepo builds a throwaway repo with the ams config and returns its path.
func tempRepo(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git is not on PATH")
	}
	dir := t.TempDir()
	run := func(args ...string) {
		t.Helper()
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	run("init", "-q", "-b", "main", ".")
	for _, kv := range [][2]string{
		{"user.name", "automemory"},
		{"user.email", "automemory@localhost"},
		{"commit.gpgsign", "false"},
		{"core.autocrlf", "false"},
		{"core.safecrlf", "false"},
		{"core.quotepath", "false"},
		{"merge.renames", "false"},
	} {
		run("config", kv[0], kv[1])
	}
	return dir
}

func repoRun(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return string(out)
}

func writeRepoFile(t *testing.T, dir, name, content string) {
	t.Helper()
	p := filepath.Join(dir, filepath.FromSlash(name))
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// TestGitX_MergeTreeOutputShape pins blueprint Q11 against the git actually installed on
// this machine.
//
// `git merge-tree --write-tree`'s conflict framing is NOT a stable API across 2.38 ->
// 2.45, and the fleet runs three different gits. This test builds a real conflict and
// asserts the only three things the merge engine reads: line 1 is a tree object id,
// the lines up to the first blank line are the conflicted paths, and there is an
// advisory section after it. If a future git changes the shape, this fails loudly here
// rather than mis-resolving a merge on one PC and not another.
func TestGitX_MergeTreeOutputShape(t *testing.T) {
	dir := tempRepo(t)
	ctx := context.Background()
	opt := Options{Dir: dir}

	v, err := Require(ctx)
	if err != nil {
		t.Fatalf("git floor: %v", err)
	}
	t.Logf("installed git: %s", v.String())

	writeRepoFile(t, dir, "f.md", "base\n")
	writeRepoFile(t, dir, "untouched.md", "same\n")
	repoRun(t, dir, "add", "-A")
	repoRun(t, dir, "commit", "-qm", "base")
	repoRun(t, dir, "branch", "theirs")

	writeRepoFile(t, dir, "f.md", "ours\n")
	repoRun(t, dir, "add", "-A")
	repoRun(t, dir, "commit", "-qm", "ours")

	repoRun(t, dir, "checkout", "-q", "theirs")
	writeRepoFile(t, dir, "f.md", "theirs\n")
	repoRun(t, dir, "add", "-A")
	repoRun(t, dir, "commit", "-qm", "theirs")
	repoRun(t, dir, "checkout", "-q", "main")

	base, ok, err := MergeBase(ctx, opt, "main", "theirs")
	if err != nil || !ok {
		t.Fatalf("merge-base: %v ok=%v", err, ok)
	}

	// Clean shape: no conflicts at all.
	clean, err := MergeTree(ctx, opt, base, "main", "main")
	if err != nil {
		t.Fatalf("merge-tree (clean): %v", err)
	}
	if !clean.Clean {
		t.Fatalf("merging main with itself should be clean, got %+v", clean)
	}
	if !IsOID(clean.Tree) {
		t.Fatalf("clean merge-tree line 1 is not an object id: %q", clean.Raw)
	}
	if len(clean.Conflicted) != 0 {
		t.Fatalf("clean merge-tree reported conflicts %v", clean.Conflicted)
	}

	// Conflicted shape: the one the parser must survive.
	got, err := MergeTree(ctx, opt, base, "main", "theirs")
	if err != nil {
		t.Fatalf("merge-tree (conflict): %v", err)
	}
	if got.Clean {
		t.Fatalf("a content conflict should not be reported clean: %q", got.Raw)
	}
	if !IsOID(got.Tree) {
		t.Fatalf("conflicted merge-tree line 1 is not an object id: %q", got.Raw)
	}
	if len(got.Conflicted) != 1 || got.Conflicted[0] != "f.md" {
		t.Fatalf("expected exactly the conflicted path [f.md] before the first blank line, got %v (raw %q)", got.Conflicted, got.Raw)
	}
	if strings.TrimSpace(got.Advisory) == "" {
		t.Fatalf("expected an advisory section after the blank line, got none (raw %q)", got.Raw)
	}
	// The advisory is read by humans only: assert it exists, never what it says.
	t.Logf("advisory from git %s: %q", v.String(), got.Advisory)

	// The merged tree must be a real, readable tree carrying the untouched file.
	entries, err := LsTree(ctx, opt, got.Tree)
	if err != nil {
		t.Fatalf("ls-tree on the merged tree: %v", err)
	}
	if _, ok := entries["untouched.md"]; !ok {
		t.Fatalf("the merged tree lost an untouched path: %v", entries)
	}
}

func TestGitX_ParseMergeTreeOutput_RejectsUnknownShape(t *testing.T) {
	if _, err := ParseMergeTreeOutput("Merged 3 paths\nf.md\n", 1); err == nil {
		t.Fatal("a first line that is not an object id must be refused, not guessed at")
	}
	if _, err := ParseMergeTreeOutput("", 0); err == nil {
		t.Fatal("empty output must be refused")
	}
}

func TestGitX_StageIntoAndCommitTree(t *testing.T) {
	dir := tempRepo(t)
	ctx := context.Background()
	opt := Options{Dir: dir}

	writeRepoFile(t, dir, "a.md", "one\n")
	writeRepoFile(t, dir, "b.md", "two\n")
	repoRun(t, dir, "add", "-A")
	repoRun(t, dir, "commit", "-qm", "base")
	head, ok, err := RevParse(ctx, opt, "HEAD")
	if err != nil || !ok {
		t.Fatalf("rev-parse HEAD: %v ok=%v", err, ok)
	}
	tree := strings.TrimSpace(repoRun(t, dir, "rev-parse", "HEAD^{tree}"))

	blob, err := HashObject(ctx, opt, []byte("rewritten\n"))
	if err != nil {
		t.Fatalf("hash-object: %v", err)
	}
	newTree, err := StageInto(ctx, opt, tree, []IndexChange{
		{Path: "a.md", OID: blob},
		{Path: "b.md"}, // removal
	})
	if err != nil {
		t.Fatalf("StageInto: %v", err)
	}
	entries, err := LsTree(ctx, opt, newTree)
	if err != nil {
		t.Fatalf("ls-tree: %v", err)
	}
	if entries["a.md"].OID != blob {
		t.Fatalf("a.md was not restaged: %v", entries)
	}
	if _, present := entries["b.md"]; present {
		t.Fatalf("b.md was not removed: %v", entries)
	}

	commit, err := CommitTree(ctx, opt, newTree, []string{head}, "test\n\nAms-Machine: qube-abc123\n")
	if err != nil {
		t.Fatalf("commit-tree: %v", err)
	}
	if err := UpdateRef(ctx, opt, "refs/heads/main", commit, head); err != nil {
		t.Fatalf("update-ref: %v", err)
	}
	// The old-value guard must refuse a stale expectation.
	if err := UpdateRef(ctx, opt, "refs/heads/main", head, head); err == nil {
		t.Fatal("update-ref accepted a stale old value; the guard that keeps a concurrent gate commit alive is not armed")
	}

	times, err := CommitTimesByPath(ctx, opt, "refs/heads/main")
	if err != nil {
		t.Fatalf("CommitTimesByPath: %v", err)
	}
	if times["a.md"].OID != commit {
		t.Fatalf("a.md's last change should be the merge commit, got %+v", times["a.md"])
	}
	if times["a.md"].Machine != "qube-abc123" {
		t.Fatalf("the Ams-Machine trailer did not survive the one-pass log: %+v", times["a.md"])
	}
}

func TestGitX_MergeFileReportsConflict(t *testing.T) {
	dir := tempRepo(t)
	ctx := context.Background()
	opt := Options{Dir: dir}

	base := []byte("one\ntwo\nthree\nfour\nfive\nsix\nseven\n")
	ours := []byte("one-ours\ntwo\nthree\nfour\nfive\nsix\nseven\n")
	theirs := []byte("one\ntwo\nthree\nfour\nfive\nsix\nseven-theirs\n")
	clean, err := MergeFile(ctx, opt, ours, base, theirs, "ours", "base", "theirs")
	if err != nil {
		t.Fatalf("MergeFile: %v", err)
	}
	if clean.Conflict {
		t.Fatalf("edits far apart must merge cleanly, got %q", clean.Merged)
	}
	if string(clean.Merged) != "one-ours\ntwo\nthree\nfour\nfive\nsix\nseven-theirs\n" {
		t.Fatalf("clean three-way merge produced %q", clean.Merged)
	}

	bad, err := MergeFile(ctx, opt, []byte("ours\n"), []byte("base\n"), []byte("theirs\n"), "ours", "base", "theirs")
	if err != nil {
		t.Fatalf("MergeFile: %v", err)
	}
	if !bad.Conflict {
		t.Fatalf("a same-line edit on both sides must report a conflict, got %q", bad.Merged)
	}
}
