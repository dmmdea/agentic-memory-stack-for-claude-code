// Package testutil builds the sandboxes the Go tests run in. It is the port of
// MemoryCompact.Fixture.ps1's New-Sandbox / Add-SandboxStore / New-FactFile helpers,
// with one difference that matters: the fixtures here are in-process, so a scenario
// costs milliseconds rather than the ~7 s a child-process Pester scenario paid.
//
// Nothing here ever touches a real store: every root is under t.TempDir().
package testutil

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// Sandbox is a temporary PROJECTS_ROOT + STATE_ROOT pair, plus an optional out-of-tree
// history repo.
type Sandbox struct {
	T            *testing.T
	Root         string
	ProjectsRoot string
	StateRoot    string
	// HistoryGitDir is empty until InitHistory is called.
	HistoryGitDir string
}

// NewSandbox creates the roots under t.TempDir().
func NewSandbox(t *testing.T) *Sandbox {
	t.Helper()
	root := t.TempDir()
	s := &Sandbox{
		T:            t,
		Root:         root,
		ProjectsRoot: filepath.Join(root, "projects"),
		StateRoot:    filepath.Join(root, "state"),
	}
	mkdirAll(t, s.ProjectsRoot)
	mkdirAll(t, s.StateRoot)
	return s
}

// AddStore writes a store: <ProjectsRoot>/<workspace>/memory with MEMORY.md built from
// indexLines joined by newline (plus a trailing newline), and one file per fact. It
// returns the memory directory.
func (s *Sandbox) AddStore(workspace string, indexLines []string, facts map[string]string) string {
	s.T.Helper()
	return s.AddStoreNL(workspace, indexLines, facts, "\n")
}

// AddStoreNL is AddStore with an explicit line ending, so a CRLF store can be built.
func (s *Sandbox) AddStoreNL(workspace string, indexLines []string, facts map[string]string, newline string) string {
	s.T.Helper()
	dir := filepath.Join(s.ProjectsRoot, workspace, "memory")
	mkdirAll(s.T, dir)
	text := strings.Join(indexLines, newline) + newline
	writeFile(s.T, filepath.Join(dir, "MEMORY.md"), text)
	for name, body := range facts {
		writeFile(s.T, filepath.Join(dir, name), body)
	}
	return dir
}

// AddEmptyWorkspace creates <ProjectsRoot>/<workspace>/memory with no index in it: the
// empty scaffold store enumeration must skip.
func (s *Sandbox) AddEmptyWorkspace(workspace string) string {
	s.T.Helper()
	dir := filepath.Join(s.ProjectsRoot, workspace, "memory")
	mkdirAll(s.T, dir)
	return dir
}

// WriteFile writes content at a path relative to the sandbox root and returns the path.
func (s *Sandbox) WriteFile(rel, content string) string {
	s.T.Helper()
	p := filepath.Join(s.Root, rel)
	mkdirAll(s.T, filepath.Dir(p))
	writeFile(s.T, p, content)
	return p
}

// InitHistory creates the out-of-tree history repo (git dir under STATE_ROOT, work tree
// = PROJECTS_ROOT) and returns its git dir. It skips the test when git is unavailable.
func (s *Sandbox) InitHistory() string {
	s.T.Helper()
	RequireGit(s.T)
	gitDir := filepath.Join(s.StateRoot, "history.git")
	run := func(args ...string) {
		full := append([]string{"--git-dir=" + gitDir, "--work-tree=" + s.ProjectsRoot}, args...)
		cmd := exec.Command("git", full...)
		if out, err := cmd.CombinedOutput(); err != nil {
			s.T.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	run("init", "-q")
	for _, kv := range [][2]string{
		{"user.name", "automemory"},
		{"user.email", "automemory@localhost"},
		{"commit.gpgsign", "false"},
		{"core.autocrlf", "false"},
		{"core.safecrlf", "false"},
		{"core.quotepath", "false"},
	} {
		run("config", kv[0], kv[1])
	}
	mkdirAll(s.T, filepath.Join(gitDir, "info"))
	writeFile(s.T, filepath.Join(gitDir, "info", "exclude"), "*\n")
	s.HistoryGitDir = gitDir
	return gitDir
}

// RequireGit skips the test when git is not on PATH. exec.Command captures a LookPath
// failure at construction time, so the explicit pre-check is what turns "git missing"
// into a skip rather than a confusing failure inside the first Run.
func RequireGit(t *testing.T) string {
	t.Helper()
	p, err := exec.LookPath("git")
	if err != nil {
		t.Skip("git is not on PATH")
	}
	return p
}

func mkdirAll(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", dir, err)
	}
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}
