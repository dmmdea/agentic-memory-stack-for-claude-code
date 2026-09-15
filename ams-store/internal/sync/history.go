// Package sync owns the local history repository, the hub remote policy, the one-shot
// sync pass and the singleton watcher.
//
// The repository is the same shape the PowerShell original built: the git directory
// lives under STATE_ROOT and the work tree IS the projects root, so no .git ever lands
// inside a store. A store is synced by the harness and globbed by agents; a .git in
// there would be copied between machines and read back as memory.
package sync

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// HubRemote is the one remote name the policy permits (DESIGN:170).
const HubRemote = "hub"

// Branch is the single branch the fleet shares.
const Branch = "main"

// excludeContent is what info/exclude is rewritten to on every Initialize.
//
// Everything is excluded by default and fact files are re-included, rather than the
// reverse: a new file type dropped into a store (a transcript, an editor backup, a
// .git from a mistaken clone) is then ignored by construction instead of tracked until
// someone notices. MEMORY.md is excluded LAST and stays excluded - it is derived from
// the fact files on every PC and re-derived after every merge, so it never conflicts
// because it is never merged.
const excludeContent = "# ams-store: everything is excluded; fact files are re-included below.\n" +
	"# MEMORY.md is DERIVED, never merged - it must stay untracked on every PC and on the hub.\n" +
	"*\n" +
	"!*/\n" +
	"!*/memory/*.md\n" +
	"MEMORY.md\n"

// Repo is the local history repository.
type Repo struct {
	GitDir   string
	WorkTree string
}

// NewRepo builds a Repo from the resolved roots.
func NewRepo(roots store.Roots) Repo {
	return Repo{GitDir: roots.HistoryGitDir(), WorkTree: roots.ProjectsRoot}
}

func (r Repo) opts() gitx.Options {
	return gitx.Options{GitDir: r.GitDir, WorkTree: r.WorkTree}
}

// Exists reports whether the repository has been initialized.
func (r Repo) Exists() bool {
	_, err := os.Stat(filepath.Join(r.GitDir, "HEAD"))
	return err == nil
}

// Initialize creates the repository if needed and pins its config. It is idempotent and
// rewrites the config and info/exclude on every call, so a hand-edited repo converges
// back the next time any verb runs.
func (r Repo) Initialize(ctx context.Context) error {
	if !r.Exists() {
		if err := os.MkdirAll(filepath.Dir(r.GitDir), 0o755); err != nil {
			return fmt.Errorf("sync: create state dir: %w", err)
		}
		if _, err := gitx.Run(ctx, r.opts(), "init", "-q", "-b", Branch); err != nil {
			return fmt.Errorf("sync: git init: %w", err)
		}
	}
	for _, kv := range [][2]string{
		// An identity is required to commit at all, and the operator's global config may
		// demand signing - which no unattended maintainer can satisfy.
		{"user.name", "automemory"},
		{"user.email", "automemory@localhost"},
		{"commit.gpgsign", "false"},
		// Byte-exact handling. Line endings are the merge engine's business, not git's:
		// git rewriting them would make a CRLF-only difference look like a real change on
		// one PC and not on another.
		{"core.autocrlf", "false"},
		{"core.safecrlf", "false"},
		{"core.quotepath", "false"},
		// Renames OFF so a judge migration - which is a deletion - is never paired with
		// an unrelated new fact file and silently turned into a rename, losing the
		// deletion.
		{"merge.renames", "false"},
		{"diff.renames", "false"},
	} {
		if _, err := gitx.Run(ctx, r.opts(), "config", kv[0], kv[1]); err != nil {
			return fmt.Errorf("sync: git config %s: %w", kv[0], err)
		}
	}
	info := filepath.Join(r.GitDir, "info")
	if err := os.MkdirAll(info, 0o755); err != nil {
		return fmt.Errorf("sync: create %s: %w", info, err)
	}
	if err := os.WriteFile(filepath.Join(info, "exclude"), []byte(excludeContent), 0o644); err != nil {
		return fmt.Errorf("sync: write info/exclude: %w", err)
	}
	return nil
}

// RelPath is a store's path relative to the work tree, in git's forward-slash spelling.
func RelPath(workspace string) string { return workspace + "/memory" }

// IndexRelPath is a store's MEMORY.md relative to the work tree.
func IndexRelPath(workspace string) string { return RelPath(workspace) + "/" + store.IndexName }

// Stage stages every fact file of a workspace, additions, modifications and deletions
// alike, and never MEMORY.md.
//
// The forced pathspec is what gets fact files past the blanket exclude; the explicit
// :(exclude) is what keeps the force from also dragging MEMORY.md in. Without it, `add
// -f` would override the exclude for the whole directory and the derived index would
// become a tracked, mergeable file again - the exact thing the design removed.
func (r Repo) Stage(ctx context.Context, workspace string) error {
	rel := RelPath(workspace)
	_, err := gitx.Run(ctx, r.opts(),
		"add", "-A", "-f", "--", rel, ":(exclude)"+IndexRelPath(workspace))
	if err != nil {
		return fmt.Errorf("sync: stage %s: %w", rel, err)
	}
	return nil
}

// HasStagedChanges reports whether anything is staged for a workspace.
func (r Repo) HasStagedChanges(ctx context.Context, workspace string) (bool, error) {
	res, err := gitx.Run(ctx, gitx.Options{
		GitDir: r.GitDir, WorkTree: r.WorkTree, OkExit: gitx.OkExitCodes(0, 1),
	}, "diff", "--cached", "--quiet", "--", RelPath(workspace))
	if err != nil {
		return false, fmt.Errorf("sync: diff --cached %s: %w", workspace, err)
	}
	return res.Code == 1, nil
}

// Commit commits the staged tree with trailers naming the machine and the kind of work.
// It returns "" when nothing was staged - git's own diff is the no-op guard, so a run
// that changed nothing leaves no commit and no noise in the log.
func (r Repo) Commit(ctx context.Context, message, machineID, kind string) (string, error) {
	res, err := gitx.Run(ctx, gitx.Options{
		GitDir: r.GitDir, WorkTree: r.WorkTree, OkExit: gitx.OkExitCodes(0, 1),
	}, "diff", "--cached", "--quiet")
	if err != nil {
		return "", fmt.Errorf("sync: diff --cached: %w", err)
	}
	if res.Code == 0 {
		return "", nil
	}
	full := message + "\n\nAms-Machine: " + machineID + "\nAms-Kind: " + kind + "\n"
	if _, err := gitx.Run(ctx, r.opts(), "commit", "-q", "-m", full); err != nil {
		return "", fmt.Errorf("sync: commit: %w", err)
	}
	return r.Head(ctx)
}

// Head is the current commit of the shared branch, or "" when there is none yet.
func (r Repo) Head(ctx context.Context) (string, error) {
	res, err := gitx.Run(ctx, gitx.Options{
		GitDir: r.GitDir, WorkTree: r.WorkTree, OkExit: gitx.OkExitCodes(0, 128),
	}, "rev-parse", "HEAD")
	if err != nil {
		return "", fmt.Errorf("sync: rev-parse HEAD: %w", err)
	}
	if res.Code != 0 {
		return "", nil
	}
	return strings.TrimSpace(res.Stdout), nil
}

// HasFile reports whether a path exists in a commit.
func (r Repo) HasFile(ctx context.Context, sha, relPath string) (bool, error) {
	res, err := gitx.Run(ctx, gitx.Options{
		GitDir: r.GitDir, WorkTree: r.WorkTree, OkExit: gitx.OkExitCodes(0, 1, 128),
	}, "cat-file", "-e", sha+":"+relPath)
	if err != nil {
		return false, err
	}
	return res.Code == 0, nil
}

// Tracked lists the paths the index tracks under a workspace's store.
func (r Repo) Tracked(ctx context.Context, workspace string) ([]string, error) {
	res, err := gitx.Run(ctx, r.opts(), "ls-files", "--", RelPath(workspace))
	if err != nil {
		return nil, fmt.Errorf("sync: ls-files %s: %w", workspace, err)
	}
	var out []string
	for _, l := range strings.Split(strings.ReplaceAll(res.Stdout, "\r\n", "\n"), "\n") {
		if l = strings.TrimSpace(l); l != "" {
			out = append(out, l)
		}
	}
	return out, nil
}

// RestoreFile restores ONE path from a commit into the work tree.
//
// Never a directory. A directory restore reverts every file under it, including the ones
// a live session wrote since the snapshot - the per-file rule is what makes a rollback
// safe to run while someone is working.
func (r Repo) RestoreFile(ctx context.Context, sha, relPath string) error {
	if _, err := gitx.Run(ctx, r.opts(), "checkout", sha, "--", relPath); err != nil {
		return fmt.Errorf("sync: restore %s from %s: %w", relPath, sha, err)
	}
	return nil
}

// Diff is the textual diff of one path between two commits, for the receipt artifact.
func (r Repo) Diff(ctx context.Context, from, to, relPath string) (string, error) {
	res, err := gitx.Run(ctx, r.opts(), "diff", "--no-color", from, to, "--", relPath)
	if err != nil {
		return "", fmt.Errorf("sync: diff %s..%s: %w", from, to, err)
	}
	return res.Stdout, nil
}

// Remotes lists the configured remote names.
func (r Repo) Remotes(ctx context.Context) ([]string, error) {
	if !r.Exists() {
		return nil, nil
	}
	res, err := gitx.Run(ctx, gitx.Options{GitDir: r.GitDir}, "remote")
	if err != nil {
		return nil, fmt.Errorf("sync: git remote: %w", err)
	}
	var out []string
	for _, l := range strings.Split(strings.ReplaceAll(res.Stdout, "\r\n", "\n"), "\n") {
		if l = strings.TrimSpace(l); l != "" {
			out = append(out, l)
		}
	}
	return out, nil
}

// RemoteURL is the fetch URL of a remote, or "" when it is not configured.
func (r Repo) RemoteURL(ctx context.Context, name string) (string, error) {
	res, err := gitx.Run(ctx, gitx.Options{
		GitDir: r.GitDir, OkExit: gitx.OkExitCodes(0, 1, 2, 128),
	}, "remote", "get-url", name)
	if err != nil {
		return "", err
	}
	if res.Code != 0 {
		return "", nil
	}
	return strings.TrimSpace(res.Stdout), nil
}
