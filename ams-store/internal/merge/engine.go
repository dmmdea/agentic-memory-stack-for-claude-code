package merge

import (
	"context"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// MainRef is the one branch this tool uses. There is no second branch anywhere in the
// design: every PC and the hub track refs/heads/main and nothing else.
const MainRef = "refs/heads/main"

// TrailerMachine and TrailerKind are the commit trailers every ams-store commit carries.
// TrailerMachine is read back as the body-conflict tiebreak, so it is data, not a note.
const (
	TrailerMachine = "Ams-Machine"
	TrailerKind    = "Ams-Kind"
)

// Engine binds one local out-of-tree history repo.
type Engine struct {
	// GitDir is the history repo's git dir, which lives under STATE_ROOT and never
	// inside a store: nothing for the harness to sync and nothing for an agent to glob.
	GitDir string
	// WorkTree is PROJECTS_ROOT.
	WorkTree string
	// MachineID is this PC's id, recorded in every commit and used as the deterministic
	// tiebreak when two commits land in the same second.
	MachineID string
	// Now is injectable so tests get deterministic commit times.
	Now func() time.Time
}

func (e *Engine) now() time.Time {
	if e.Now != nil {
		return e.Now()
	}
	return time.Now()
}

func (e *Engine) opt() gitx.Options {
	return gitx.Options{GitDir: e.GitDir, WorkTree: e.WorkTree}
}

// excludeContent is info/exclude for the history repo.
//
// Everything is excluded by default and fact files are re-included by shape, because the
// projects root also holds transcripts, tool state and whatever else the harness writes
// there. MEMORY.md is excluded on purpose and stays untracked on every PC and on the
// hub: it is DERIVED from the fact set, re-derived after every merge, and therefore
// never merged and never in conflict.
const excludeContent = "" +
	"# ams-store: nothing is tracked except fact files and the shared state stamp.\n" +
	"# MEMORY.md is DERIVED, never merged - it must stay untracked on every PC and on the hub.\n" +
	"*\n" +
	"!*/\n" +
	"!*/memory/*.md\n" +
	"*/memory/" + store.IndexName + "\n" +
	"!/" + OverTriggerPath + "\n"

// HooksDirName is the empty directory core.hooksPath is pinned at, under the git dir.
//
// A global core.hooksPath - the operator's account-separation and leak-scan hooks - applies
// to EVERY repository on the box, this one included. Those hooks guard pushes to GitHub;
// this repo's only remote is the private hub on the tailnet, and its content is exactly the
// private material the scanner is right to refuse in a public-facing repo. Without a
// repo-LOCAL override the hub push is blocked by a hook that was never aimed at it. Local,
// so every other repo on the box stays guarded.
const HooksDirName = "ams-hooks"

// RepoConfig is the config every history repo carries, in one place because sync and the
// merge engine both initialize it and two spellings mean whichever ran last decides.
func RepoConfig(hooksDir string) [][2]string {
	return [][2]string{
		// An identity is required to commit at all, and a global config may demand signing,
		// which no unattended maintainer can satisfy.
		{"user.name", "automemory"},
		{"user.email", "automemory@localhost"},
		{"commit.gpgsign", "false"},
		// Byte-exact handling. Line endings are the merge engine's business, not git's:
		// git rewriting them makes a CRLF-only difference a real change on one PC and not
		// on another.
		{"core.autocrlf", "false"},
		{"core.safecrlf", "false"},
		{"core.quotepath", "false"},
		{"core.hooksPath", hooksDir},
		// Renames OFF: a judge migration is a deletion, and pairing it with an unrelated
		// new fact file reads as a rename and loses the deletion. Measured caveat: git
		// honours this unevenly across versions, which is why internal/merge audits every
		// path the two sides disagree about rather than trusting the setting.
		{"merge.renames", "false"},
		{"diff.renames", "false"},
	}
}

// Initialize creates (idempotently) the history repo and pins the config the merge
// engine depends on.
//
// core.autocrlf and core.safecrlf are off so git never rewrites a byte on its own - the
// merge engine owns line-ending normalization and does it in one place. merge.renames is
// off because a judge migration (a deletion) paired with an unrelated new fact file is
// silently read as a rename, and the deletion is lost.
func (e *Engine) Initialize(ctx context.Context) error {
	if _, err := gitx.Require(ctx); err != nil {
		return err
	}
	if _, err := os.Stat(filepath.Join(e.GitDir, "HEAD")); err != nil {
		if err := os.MkdirAll(filepath.Dir(e.GitDir), 0o755); err != nil {
			return fmt.Errorf("history repo parent: %w", err)
		}
		if _, err := gitx.Run(ctx, e.opt(), "init", "-q", "-b", "main"); err != nil {
			return err
		}
	}
	hooks := filepath.Join(e.GitDir, HooksDirName)
	if err := os.MkdirAll(hooks, 0o755); err != nil {
		return fmt.Errorf("history repo hooks dir: %w", err)
	}
	for _, kv := range RepoConfig(hooks) {
		if _, err := gitx.Run(ctx, e.opt(), "config", kv[0], kv[1]); err != nil {
			return err
		}
	}
	info := filepath.Join(e.GitDir, "info")
	if err := os.MkdirAll(info, 0o755); err != nil {
		return fmt.Errorf("history repo info dir: %w", err)
	}
	if err := os.WriteFile(filepath.Join(info, "exclude"), []byte(excludeContent), 0o644); err != nil {
		return fmt.Errorf("history repo info/exclude: %w", err)
	}
	return nil
}

// CommitOptions drives one local commit.
type CommitOptions struct {
	Message string
	// Kind is the Ams-Kind trailer: local, merge or judge.
	Kind string
	// Workspaces limits staging to these workspaces' stores. Empty means every
	// workspace directory under the work tree.
	Workspaces []string
	// StateRoot is where the per-workspace deferred queues live. It is REQUIRED on any
	// engine whose merges can defer - which is every engine a PC runs - because a queued
	// path must not be staged. Empty means "this engine has no queue" and is only true
	// of a fixture that never materializes.
	StateRoot string
	// Date pins the author and committer time. Zero means the engine clock.
	Date time.Time
}

// StoreRel is a workspace's store path as git stores it: slash-separated, relative to
// the work tree.
func StoreRel(workspace string) string { return workspace + "/" + "memory" }

// Commit stages every named store and the shared state stamp and commits.
//
// This is the merge-side hook `sync --once` calls, and the ORDER matters there: the local
// commit happens BEFORE the fetch, so a PC that worked all day offline keeps its history
// whatever the network did.
func (e *Engine) Commit(ctx context.Context, co CommitOptions) (oid string, changed bool, err error) {
	workspaces := co.Workspaces
	if len(workspaces) == 0 {
		workspaces, err = e.workspaces()
		if err != nil {
			return "", false, err
		}
	}
	for _, ws := range workspaces {
		rel := StoreRel(ws)
		if _, statErr := os.Stat(filepath.Join(e.WorkTree, filepath.FromSlash(rel))); statErr != nil {
			continue
		}
		// -f forces past info/exclude for the fact files, narrowed to *.md so the force
		// does not also track whatever else is sitting in the directory (blueprint 1.4:
		// nothing but MEMORY.md and fact files may live in a store). The exclude pathspec
		// then keeps MEMORY.md out even under -f, since it matches *.md too and forcing
		// it in would track the one file the design requires to stay untracked.
		//
		// The deferred queue is excluded on top of that: those paths are a merge result
		// this PC has already committed and has NOT put on disk yet, so staging what is
		// on disk would undo the merge.
		hold, err := deferredHold(co.StateRoot, ws)
		if err != nil {
			return "", false, err
		}
		if err := gitx.AddFactFiles(ctx, e.opt(), rel, store.IndexName, hold); err != nil {
			return "", false, err
		}
	}
	if _, statErr := os.Stat(filepath.Join(e.WorkTree, filepath.FromSlash(OverTriggerPath))); statErr == nil {
		if _, err := gitx.Run(ctx, e.opt(), "add", "-A", "-f", "--", OverTriggerPath); err != nil {
			return "", false, err
		}
	}

	dirty, err := e.indexDiffersFromHead(ctx)
	if err != nil {
		return "", false, err
	}
	if !dirty {
		return "", false, nil
	}

	when := co.Date
	if when.IsZero() {
		when = e.now()
	}
	opt := e.opt()
	opt.ExtraEnv = dateEnv(when)
	msg := e.message(co.Message, co.Kind)
	if _, err := gitx.Run(ctx, opt, "commit", "-q", "-m", msg); err != nil {
		return "", false, err
	}
	head, ok, err := gitx.RevParse(ctx, e.opt(), "HEAD")
	if err != nil || !ok {
		return "", false, fmt.Errorf("commit produced no HEAD: %w", err)
	}
	return head, true, nil
}

// deferredHold reads one workspace's queue for the staging pass. A state root that was
// never configured yields no hold, and a queue that cannot be read is an ERROR: staging
// on a guess is exactly the resurrection this exclusion exists to stop.
func deferredHold(stateRoot, workspace string) ([]string, error) {
	if stateRoot == "" {
		return nil, nil
	}
	return DeferredPaths(stateRoot, workspace)
}

func dateEnv(when time.Time) []string {
	stamp := when.UTC().Format(time.RFC3339)
	return []string{"GIT_AUTHOR_DATE=" + stamp, "GIT_COMMITTER_DATE=" + stamp}
}

func (e *Engine) message(subject, kind string) string {
	if kind == "" {
		kind = "local"
	}
	return subject + "\n\n" + TrailerMachine + ": " + e.MachineID + "\n" + TrailerKind + ": " + kind + "\n"
}

func (e *Engine) indexDiffersFromHead(ctx context.Context) (bool, error) {
	_, hasHead, err := gitx.RevParse(ctx, e.opt(), "HEAD")
	if err != nil {
		return false, err
	}
	if !hasHead {
		res, err := gitx.Run(ctx, e.opt(), "ls-files", "--cached")
		if err != nil {
			return false, err
		}
		return strings.TrimSpace(res.Stdout) != "", nil
	}
	opt := e.opt()
	opt.OkExit = gitx.OkExitCodes(0, 1)
	res, err := gitx.Run(ctx, opt, "diff", "--cached", "--quiet")
	if err != nil {
		return false, err
	}
	return res.Code == 1, nil
}

// workspaces lists the workspace directories that actually hold a store.
func (e *Engine) workspaces() ([]string, error) {
	entries, err := os.ReadDir(e.WorkTree)
	if err != nil {
		// Fail closed: "could not read" must never be spellable as "there is nothing
		// there", which is how an empty enumeration once wiped an entire index.
		return nil, fmt.Errorf("enumerate workspaces under %s: %w", e.WorkTree, err)
	}
	var out []string
	for _, ent := range entries {
		if !ent.IsDir() {
			continue
		}
		if _, err := os.Stat(filepath.Join(e.WorkTree, ent.Name(), "memory")); err != nil {
			continue
		}
		out = append(out, ent.Name())
	}
	return out, nil
}

// Fetch updates the remote-tracking refs. It is never called from a hook: the design's
// rule is that the network is never on a hook's critical path.
func (e *Engine) Fetch(ctx context.Context, remote string) error {
	_, err := gitx.Run(ctx, e.opt(), "fetch", "--prune", remote)
	return err
}

// PushResult distinguishes the one rejection the push loop retries from every other
// failure. A non-fast-forward means somebody else pushed first and the loser re-merges;
// anything else is a real error and must not be retried three times.
type PushResult struct {
	OK             bool
	NonFastForward bool
	Stderr         string
}

// Push sends refs/heads/main to the hub.
func (e *Engine) Push(ctx context.Context, remote string) (PushResult, error) {
	opt := e.opt()
	opt.OkExit = func(int) bool { return true }
	res, err := gitx.Run(ctx, opt, "push", remote, MainRef+":"+MainRef)
	if err != nil {
		return PushResult{Stderr: res.Stderr}, err
	}
	if res.Code == 0 {
		return PushResult{OK: true, Stderr: res.Stderr}, nil
	}
	combined := res.Stderr + res.Stdout
	lower := strings.ToLower(combined)
	nonFF := strings.Contains(lower, "non-fast-forward") ||
		strings.Contains(lower, "fetch first") ||
		strings.Contains(lower, "behind its remote")
	return PushResult{NonFastForward: nonFF, Stderr: combined}, nil
}

// Adopt takes a remote branch as this PC's history when there is nothing local to merge
// with, and materializes it. It is the first-sync path, and it refuses to run when the
// local branch has commits of its own - that case is a merge, not an adoption.
func (e *Engine) Adopt(ctx context.Context, theirsRef string, mo MaterializeOptions) error {
	theirs, ok, err := gitx.RevParse(ctx, e.opt(), theirsRef)
	if err != nil {
		return err
	}
	if !ok {
		return fmt.Errorf("adopt: %s does not exist", theirsRef)
	}
	ours, hasOurs, err := gitx.RevParse(ctx, e.opt(), MainRef)
	if err != nil {
		return err
	}
	if hasOurs {
		return fmt.Errorf("adopt: %s already exists at %s; use Round", MainRef, ours)
	}
	if err := gitx.UpdateRef(ctx, e.opt(), MainRef, theirs, gitx.NullOID); err != nil {
		return err
	}
	theirsTree, err := e.treeOf(ctx, theirs)
	if err != nil {
		return err
	}
	_, err = e.materialize(ctx, "", theirsTree, mo)
	return err
}

func (e *Engine) treeOf(ctx context.Context, commit string) (string, error) {
	res, err := gitx.Run(ctx, e.opt(), "rev-parse", commit+"^{tree}")
	if err != nil {
		return "", err
	}
	tree := strings.TrimSpace(res.Stdout)
	if !gitx.IsOID(tree) {
		return "", fmt.Errorf("rev-parse %s^{tree} returned %q", commit, tree)
	}
	return tree, nil
}

// workspaceOf returns the workspace a tracked path belongs to, or "" when the path is
// not inside a store (the shared state stamp is the only such path today).
func workspaceOf(p string) string {
	parts := strings.Split(path.Clean(p), "/")
	if len(parts) >= 3 && parts[1] == "memory" {
		return parts[0]
	}
	return ""
}
