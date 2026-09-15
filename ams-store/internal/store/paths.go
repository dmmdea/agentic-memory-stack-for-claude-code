package store

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
)

// Roots are the two directory roots every verb works against. They are a struct rather
// than package globals so --projects-root / --state-root can inject a sandbox in tests
// and so no code path can accidentally read the operator's real store.
type Roots struct {
	ProjectsRoot string
	StateRoot    string
}

// HomeDir resolves the user's home. On Windows it prefers USERPROFILE, which is what the
// PowerShell original reads (LIB:35) and what the harness itself uses; os.UserHomeDir is
// the fallback. Off Windows os.UserHomeDir is authoritative - reading $HOME blindly
// picks up whatever a service manager happened to export.
func HomeDir() (string, error) {
	if runtime.GOOS == "windows" {
		if p := os.Getenv("USERPROFILE"); p != "" {
			return p, nil
		}
	}
	h, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	if h == "" {
		return "", errors.New("cannot resolve the user's home directory")
	}
	return h, nil
}

// DefaultRoots resolves the roots for this machine:
//
//	PROJECTS_ROOT = <home>/.claude/projects
//	STATE_ROOT    = <home>/.claude/state/automemory
//
// Maintainer state lives OUTSIDE the store: the store is synced by the harness and
// globbed by agents, so a maintenance folder inside it resurfaces removed facts in every
// agent glob.
func DefaultRoots() (Roots, error) {
	home, err := HomeDir()
	if err != nil {
		return Roots{}, err
	}
	return Roots{
		ProjectsRoot: filepath.Join(home, ".claude", "projects"),
		StateRoot:    filepath.Join(home, ".claude", "state", "automemory"),
	}, nil
}

// WorkspaceStateDir is the per-workspace maintainer state directory, created on demand.
func (r Roots) WorkspaceStateDir(workspace string) (string, error) {
	p := filepath.Join(r.StateRoot, workspace)
	if err := os.MkdirAll(p, 0o755); err != nil {
		return "", err
	}
	return p, nil
}

// HistoryGitDir is the out-of-tree git directory for the local history repo. The work
// tree is the projects root, so no .git ever lands inside a store.
func (r Roots) HistoryGitDir() string { return filepath.Join(r.StateRoot, "history.git") }

// Dir is the store (memory) directory for a workspace under a projects root.
func Dir(projectsRoot, workspace string) string {
	return filepath.Join(projectsRoot, workspace, "memory")
}

// IndexPath is the MEMORY.md path for a workspace under a projects root.
func IndexPath(projectsRoot, workspace string) string {
	return filepath.Join(Dir(projectsRoot, workspace), IndexName)
}
