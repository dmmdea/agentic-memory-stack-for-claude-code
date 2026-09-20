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
	"sort"
	"strings"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// HubRemote is the one remote name the policy permits (DESIGN:170).
const HubRemote = "hub"

// Branch is the single branch the fleet shares.
const Branch = "main"

// Repo is the local history repository.
type Repo struct {
	GitDir   string
	WorkTree string
	// StateRoot is where the per-workspace deferred queues live. Staging consults them,
	// so a Repo built without it would re-commit every change a live session is being
	// protected from.
	StateRoot string
}

// NewRepo builds a Repo from the resolved roots.
func NewRepo(roots store.Roots) Repo {
	return Repo{
		GitDir:    roots.HistoryGitDir(),
		WorkTree:  roots.ProjectsRoot,
		StateRoot: roots.StateRoot,
	}
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
//
// The SHAPE - the config pairs, the empty repo-local hooks directory and info/exclude -
// belongs to internal/merge and is applied from there, not written a second time here.
// Both packages initialize this repo, sync on every pass and the engine on every round;
// two spellings of "what is tracked" means whichever ran last wins, and the loser's rule
// is undone without a word. That is not hypothetical: the two spellings disagreed about
// the shared over-trigger stamp, and sync's would have made decision Q13's transport
// untrackable on every PC.
func (r Repo) Initialize(ctx context.Context) error {
	if err := os.MkdirAll(filepath.Dir(r.GitDir), 0o755); err != nil {
		return fmt.Errorf("sync: create state dir: %w", err)
	}
	eng := &merge.Engine{GitDir: r.GitDir, WorkTree: r.WorkTree}
	if err := eng.Initialize(ctx); err != nil {
		return fmt.Errorf("sync: initialize the history repo: %w", err)
	}
	return nil
}

// StageShared stages the one tracked path outside every store: the shared over-trigger
// stamp of decision Q13. It is a no-op until some PC first crosses the trigger.
func (r Repo) StageShared(ctx context.Context) error {
	p := filepath.Join(r.WorkTree, filepath.FromSlash(merge.OverTriggerPath))
	if _, err := os.Stat(p); err != nil {
		return nil
	}
	if _, err := gitx.Run(ctx, r.opts(), "add", "-A", "-f", "--", merge.OverTriggerPath); err != nil {
		return fmt.Errorf("sync: stage %s: %w", merge.OverTriggerPath, err)
	}
	return nil
}

// StageVanishedStores stages the deletion of every tracked store whose WORKSPACE
// directory is gone, and returns the workspaces it removed.
//
// Without it a store that was deleted on this PC stays tracked forever: staging is
// per-enumerated-workspace, and a workspace that no longer exists is never enumerated, so
// its files are never seen as deleted. A live sync on 2026-09-15 reported "5 store(s)"
// and left 61 deletions of exactly such a store unstaged.
//
// The guard is the STORE directory, because the store is what is tracked. Guarding the
// whole workspace directory instead missed the ordinary shape of a removal: a project
// folder outlives its store - the transcripts stay behind - and enumeration recognises a
// store by its index, so such a workspace is never enumerated and its files stayed
// tracked with stale bytes forever, pushed on every sync and materialized back onto every
// other PC.
//
// The original caution stands and is spelled out instead of approximated: only a stat
// that says NOT FOUND is a removal. Any other stat error - a permission fault, a
// disconnected share - leaves the store tracked, because propagating a fleet-wide removal
// off one bad read is the failure worth being careful about. The caller passes the
// workspaces enumeration actually found, so a failed enumeration (which fails closed
// upstream) never reaches this.
func (r Repo) StageVanishedStores(ctx context.Context, live []string) ([]string, error) {
	res, err := gitx.Run(ctx, r.opts(), "ls-files")
	if err != nil {
		return nil, fmt.Errorf("sync: ls-files: %w", err)
	}
	liveSet := make(map[string]bool, len(live))
	for _, ws := range live {
		liveSet[ws] = true
	}
	seen := map[string]bool{}
	var gone []string
	for _, line := range strings.Split(strings.ReplaceAll(res.Stdout, "\r\n", "\n"), "\n") {
		line = strings.TrimSpace(line)
		parts := strings.Split(line, "/")
		if len(parts) < 3 || parts[1] != "memory" {
			continue
		}
		ws := parts[0]
		if liveSet[ws] || seen[ws] {
			continue
		}
		seen[ws] = true
		if _, statErr := os.Stat(filepath.Join(r.WorkTree, filepath.FromSlash(RelPath(ws)))); !os.IsNotExist(statErr) {
			continue // the store is there, or we cannot tell: either way, not a removal
		}
		if _, err := gitx.Run(ctx, r.opts(), "rm", "-r", "-q", "--cached", "--ignore-unmatch",
			"--", RelPath(ws)); err != nil {
			return gone, fmt.Errorf("sync: stage the removal of %s: %w", ws, err)
		}
		gone = append(gone, ws)
	}
	sort.Strings(gone)
	return gone, nil
}

// RelPath is a store's path relative to the work tree, in git's forward-slash spelling.
func RelPath(workspace string) string { return workspace + "/memory" }

// IndexRelPath is a store's MEMORY.md relative to the work tree.
func IndexRelPath(workspace string) string { return RelPath(workspace) + "/" + store.IndexName }

// Stage stages every fact file of a workspace, additions, modifications and deletions
// alike, and never MEMORY.md and never anything that is not a fact file.
//
// The forced pathspec is what gets fact files past the blanket exclude. It is narrowed to
// *.md for the same reason info/exclude is a deny-list with re-includes: the force
// overrides the exclude, so a force over the whole DIRECTORY tracks whatever happens to be
// sitting in it. Something always is - the PowerShell compactor leaves .bak-<date>-<kind>
// files beside the index - and blueprint 1.4 is explicit that nothing but MEMORY.md and
// fact files may live in a store, because the store is globbed by agents on every PC that
// receives it. The explicit :(exclude) then keeps the index itself out, since MEMORY.md
// matches *.md and is DERIVED, never merged.
//
// Every path on the workspace's DEFERRED QUEUE is excluded: the merge committed those
// changes to history and withheld them from disk because a session is live, so what is on
// disk is deliberately older than what is committed. Staging it would resurrect a deleted
// fact on every PC and revert another PC's edit. A queue that cannot be read stages
// NOTHING - see LoadDeferred's fail-closed contract.
func (r Repo) Stage(ctx context.Context, workspace string) error {
	rel := RelPath(workspace)
	hold, err := r.deferredHold(workspace)
	if err != nil {
		return err
	}
	if err := gitx.AddFactFiles(ctx, r.opts(), rel, store.IndexName, hold); err != nil {
		return fmt.Errorf("sync: stage %s: %w", rel, err)
	}
	return nil
}

// deferredHold is the workspace's pending merge result, which staging must not touch.
func (r Repo) deferredHold(workspace string) ([]string, error) {
	if r.StateRoot == "" {
		return nil, nil
	}
	hold, err := merge.QueuedPaths(r.StateRoot, workspace)
	if err != nil {
		return nil, fmt.Errorf("sync: the deferred queue of %s cannot be read, so nothing of it may be staged: %w", workspace, err)
	}
	return hold, nil
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
//
// Before the diff it puts HEAD's entry back into the index for every path any workspace's
// deferred queue holds. Stage already excludes those paths, but a stage taken BEFORE a
// concurrent merge wrote the queue carries the on-disk bytes that merge withheld, and the
// commit that followed re-added three hub deletions on top of the merge (P5-10,
// 2026-09-19). The queue is honoured at the moment of commit, whatever admitted the
// concurrency, so no verb's commit can carry a queued path.
func (r Repo) Commit(ctx context.Context, message, machineID, kind string) (string, error) {
	if err := r.unstageQueued(ctx); err != nil {
		return "", err
	}
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

// unstageQueued resets the index entry of every queued path to HEAD's: the merge result
// for a path a live session is still holding the old bytes of. A path HEAD does not have
// leaves the index (git reset on an unborn branch has no HEAD, so rm --cached does the
// same there); a queued path that matches nothing is not an error. A queue that cannot
// be read fails the commit, for the reason LoadDeferred gives: "I could not tell" means
// "do not touch it".
func (r Repo) unstageQueued(ctx context.Context) error {
	if r.StateRoot == "" {
		return nil
	}
	held, err := merge.AllQueuedPaths(r.StateRoot)
	if err != nil {
		return fmt.Errorf("sync: the deferred queues cannot be read, so nothing may be committed: %w", err)
	}
	if len(held) == 0 {
		return nil
	}
	head, err := r.Head(ctx)
	if err != nil {
		return err
	}
	var args []string
	if head == "" {
		args = append([]string{"rm", "-q", "-r", "--cached", "--ignore-unmatch", "--"}, held...)
	} else {
		args = append([]string{"reset", "-q", "--"}, held...)
	}
	if _, err := gitx.Run(ctx, r.opts(), args...); err != nil {
		return fmt.Errorf("sync: unstage the deferred queue before committing: %w", err)
	}
	return nil
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
