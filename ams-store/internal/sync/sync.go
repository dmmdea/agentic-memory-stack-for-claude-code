package sync

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// MaxPushAttempts bounds the fetch-merge-push loop (DESIGN:187).
//
// Three, not "until it works": a non-fast-forward rejection means another PC pushed
// between this PC's fetch and its push, and the loser re-merges. That is normal and
// converges in one extra pass. An UNBOUNDED loop against a hub that rejects for any
// other reason is a process that never exits, on a box nobody is watching.
const MaxPushAttempts = 3

// KnownHostsFile is the pre-seeded known_hosts under STATE_ROOT. The file, like the ssh
// options that read it, is owned by gitx now: there is ONE hardened network environment in
// this module and every caller gets it from the same place.
const KnownHostsFile = gitx.KnownHostsFile

// Options configures one sync pass. The caller holds the per-PC lock; Once does not take
// it, because the lock covers derive AND sync together and the verb is what owns both.
type Options struct {
	Roots store.Roots
	// Deriver and Merger are the engines. A nil Deriver skips the derive steps (used by
	// the transport tests); a nil Merger makes a fetch that finds new commits an error,
	// because materializing them is not this package's job.
	Deriver Deriver
	Merger  Merger
	// Drainer applies the deferred queue at the top of the pass. A nil Drainer is only
	// legal while every queue is EMPTY: a pass that finds queued changes and has no
	// engine to apply them refuses, rather than staging over them.
	Drainer Drainer
	// MachineID is this PC's id; empty means read or create it under the state root.
	MachineID string
	// Policy is the remote policy. Zero value means shape rules only.
	Policy RemotePolicy
	// Now is the injected clock.
	Now time.Time
	// Timeout bounds each network git call.
	Timeout time.Duration
	// Workspaces limits the pass. Empty means every populated store.
	Workspaces []string
	// Version is stamped into the receipt so a fleet-wide behaviour change is
	// attributable to a binary.
	Version string
	// Log receives the human progress log. Nil discards it.
	Log io.Writer
}

// Result is what one pass did.
type Result struct {
	Receipt Receipt
	// ExitCode is the process exit code the verb should return.
	ExitCode int
	// Derived is the per-store derive results, in store order.
	Derived []DeriveResult
	Err     error
}

// Exit codes this package produces, mirroring blueprint section 1.2.
const (
	exitOK       = 0
	exitRefused  = 3
	exitNetwork  = 5
	exitConflict = 6
)

// Once runs the single sync pass of blueprint section 5.1.
//
// The order is the whole design in five lines:
//
//	derive -> COMMIT LOCALLY -> clear dirty -> (only then) fetch/merge/push
//
// The local commit happens BEFORE the fetch, so a PC with no connectivity still keeps
// its history. Every other ordering makes offline work invisible until the network comes
// back, and a box that is offline for a week is exactly the box whose history matters
// most when it returns.
func Once(ctx context.Context, opt Options) Result {
	now := opt.Now
	if now.IsZero() {
		now = time.Now()
	}
	logw := opt.Log
	if logw == nil {
		logw = io.Discard
	}
	host, _ := os.Hostname()

	res := Result{Receipt: Receipt{
		TS:      now.UTC(),
		Host:    host,
		Kind:    "once",
		Version: opt.Version,
		Status:  StatusUpToDate,
	}}

	machineID := opt.MachineID
	if machineID == "" {
		id, err := MachineID(opt.Roots.StateRoot)
		if err != nil {
			res.Err = err
			res.ExitCode = exitRefused
			return res
		}
		machineID = id
	}
	res.Receipt.Machine = machineID

	repo := NewRepo(opt.Roots)
	if err := repo.Initialize(ctx); err != nil {
		res.Err = err
		res.ExitCode = exitRefused
		res.Receipt.Status = StatusRefused
		res.Receipt.Note = err.Error()
		writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
		return res
	}

	workspaces, err := resolveWorkspaces(opt)
	if err != nil {
		res.Err = err
		res.ExitCode = exitRefused
		res.Receipt.Status = StatusRefused
		res.Receipt.Note = err.Error()
		writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
		return res
	}
	res.Receipt.Stores = len(workspaces)

	// 0. drain the deferred queue FIRST.
	//
	// Everything below it - derive, stage, commit - reads the work tree and writes
	// history from it, so a change the last merge withheld has to land before any of
	// them, or this pass commits the state the deferral was protecting and the merge
	// result is undone. This is blueprint 4.8's "applied at SessionEnd and at the next
	// SessionStart": both hooks run `sync --once`, and so does every watcher pass.
	if err := drainDeferred(ctx, opt, workspaces, &res, now, logw); err != nil {
		res.Err = err
		res.ExitCode = exitRefused
		res.Receipt.Status = StatusRefused
		res.Receipt.Note = err.Error()
		writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
		return res
	}

	// 1. derive, so the index is correct whether or not the hub is reachable.
	res.Derived = deriveAll(ctx, opt, workspaces, now, logw)

	// 2. stage and commit LOCALLY, before any network call.
	for _, ws := range workspaces {
		if err := repo.Stage(ctx, ws); err != nil {
			fmt.Fprintf(logw, "sync: %s: %v\n", ws, err)
		}
	}
	// The shared over-trigger stamp is the one tracked path outside every store.
	if err := repo.StageShared(ctx); err != nil {
		fmt.Fprintf(logw, "sync: %v\n", err)
	}
	// A store whose directory is gone stays tracked forever unless its deletion is
	// staged: it is never enumerated, so nothing ever notices its files are missing.
	if removed, err := repo.StageVanishedStores(ctx, workspaces); err != nil {
		fmt.Fprintf(logw, "sync: %v\n", err)
	} else if len(removed) > 0 {
		res.Receipt.Removed = removed
		fmt.Fprintf(logw, "sync: staged the removal of %d vanished store(s): %v\n", len(removed), removed)
	}
	msg := fmt.Sprintf("sync %s: %d store(s)", machineID, len(workspaces))
	localCommit, err := repo.Commit(ctx, msg, machineID, "local")
	if err != nil {
		res.Err = err
		res.ExitCode = exitRefused
		res.Receipt.Status = StatusRefused
		res.Receipt.Note = err.Error()
		writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
		return res
	}
	res.Receipt.LocalCommit = localCommit
	if localCommit != "" {
		fmt.Fprintf(logw, "sync: committed %s locally\n", short(localCommit))
	}

	// 3. the local commit is durable, so the dirty marker has done its job.
	if err := ClearDirty(opt.Roots.StateRoot); err != nil {
		fmt.Fprintf(logw, "sync: could not clear the dirty marker: %v\n", err)
	}

	// 4. no hub configured is a complete, successful, offline-only pass.
	hasHub, err := HasHub(ctx, repo)
	if err != nil {
		res.Err = err
		res.ExitCode = exitRefused
		res.Receipt.Status = StatusRefused
		res.Receipt.Note = err.Error()
		writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
		return res
	}
	if !hasHub {
		res.Receipt.Status = StatusLocal
		res.Receipt.Note = "no hub remote is configured; history is kept locally"
		writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
		return res
	}

	// 5. the remote policy is a refusal, not a warning.
	if err := opt.Policy.Validate(ctx, repo); err != nil {
		res.Err = err
		res.ExitCode = exitRefused
		res.Receipt.Status = StatusRefused
		res.Receipt.Note = err.Error()
		writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
		return res
	}

	// 6. the bounded fetch -> merge -> push loop.
	netOpts := gitx.Options{
		GitDir:   repo.GitDir,
		WorkTree: repo.WorkTree,
		Timeout:  opt.Timeout,
		ExtraEnv: gitx.NetworkEnv(opt.Roots.StateRoot),
	}

	for attempt := 1; attempt <= MaxPushAttempts; attempt++ {
		res.Receipt.Attempts = attempt

		if err := fetchHub(ctx, netOpts); err != nil {
			res.Err = err
			res.ExitCode = exitNetwork
			res.Receipt.Status = StatusOffline
			res.Receipt.Offline = true
			res.Receipt.Note = "fetch failed: " + oneLine(err.Error())
			writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
			return res
		}

		merged, err := mergeStep(ctx, opt, repo, machineID, now, workspaces)
		if err != nil {
			res.Err = err
			res.ExitCode = exitNetwork
			res.Receipt.Status = StatusOffline
			res.Receipt.Note = "merge failed: " + oneLine(err.Error())
			writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
			return res
		}
		if merged != nil {
			res.Receipt.MergeCommit = merged.Commit
			for _, r := range merged.Resurrected {
				res.Receipt.Resurrected = append(res.Receipt.Resurrected, r.Path)
			}
			res.Receipt.ConflictsInHistory = append(res.Receipt.ConflictsInHistory, merged.ConflictsInHistory...)
			for _, d := range merged.Deferred {
				res.Receipt.Deferred = append(res.Receipt.Deferred, DeferredRef{Path: d.Path, Op: d.Op})
			}
			// MEMORY.md is derived, never merged: re-derive every store the merge
			// touched so no index points at a file the merge just removed.
			touched := merged.TouchedWorkspaces
			if len(touched) == 0 && !merged.UpToDate {
				touched = workspaces
			}
			if len(touched) > 0 {
				res.Derived = append(res.Derived, deriveAll(ctx, opt, touched, now, logw)...)
				for _, ws := range touched {
					if err := repo.Stage(ctx, ws); err != nil {
						fmt.Fprintf(logw, "sync: %s: %v\n", ws, err)
					}
				}
				if c, err := repo.Commit(ctx, fmt.Sprintf("sync %s: post-merge derive", machineID), machineID, "merge"); err == nil && c != "" {
					fmt.Fprintf(logw, "sync: committed %s after the merge\n", short(c))
				}
			}
		}

		pushRes, pushErr := gitx.Run(ctx, gitx.Options{
			GitDir: netOpts.GitDir, WorkTree: netOpts.WorkTree, Timeout: netOpts.Timeout,
			ExtraEnv: netOpts.ExtraEnv, OkExit: gitx.OkExitCodes(0, 1),
		}, "push", HubRemote, Branch)
		if pushErr != nil {
			res.Err = pushErr
			res.ExitCode = exitNetwork
			res.Receipt.Status = StatusOffline
			res.Receipt.Offline = true
			res.Receipt.Note = "push failed: " + oneLine(pushErr.Error())
			writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
			return res
		}
		if pushRes.Code == 0 {
			res.Receipt.Pushed = true
			res.Receipt.Status = StatusPushed
			break
		}
		if !isNonFastForward(pushRes.Stderr + pushRes.Stdout) {
			res.Err = errors.New(oneLine(pushRes.Stderr))
			res.ExitCode = exitNetwork
			res.Receipt.Status = StatusOffline
			res.Receipt.Note = "push rejected: " + oneLine(pushRes.Stderr)
			writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
			return res
		}
		// A non-fast-forward is NOT an error. It is the loop's signal that another PC
		// pushed first; this one re-merges and tries again.
		fmt.Fprintf(logw, "sync: hub moved under attempt %d; re-merging\n", attempt)
		if attempt == MaxPushAttempts {
			res.ExitCode = exitNetwork
			res.Receipt.Status = StatusExhausted
			res.Receipt.Note = fmt.Sprintf("push loop exhausted after %d attempts", MaxPushAttempts)
			res.Err = errors.New(res.Receipt.Note)
			writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
			return res
		}
	}

	if len(res.Receipt.ConflictsInHistory) > 0 {
		// Advisory-loud: the work tree is correct and the push happened, but a loser is
		// sitting in history and a human has to know it is recoverable.
		res.Receipt.Status = StatusConflict
		res.ExitCode = exitConflict
	}
	writeReceipt(opt.Roots.StateRoot, res.Receipt, logw)
	return res
}

// drainDeferred applies every workspace's pending merge result before this pass touches
// anything else.
//
// It fails the WHOLE pass on an unreadable queue. That is deliberate: the queue names
// changes that are already in history and deliberately not on disk, so a pass that cannot
// read it cannot know what it is allowed to stage, and staging on a guess re-commits the
// stale file over the merged blob. "I could not tell" means "do not touch it".
func drainDeferred(ctx context.Context, opt Options, workspaces []string, res *Result, now time.Time, logw io.Writer) error {
	for _, ws := range workspaces {
		pending, err := merge.QueuedPaths(opt.Roots.StateRoot, ws)
		if err != nil {
			return fmt.Errorf("the deferred queue of %s cannot be read, so this pass cannot know what is pending: %w", ws, err)
		}
		if len(pending) == 0 {
			continue
		}
		if opt.Drainer == nil {
			return fmt.Errorf("%d change(s) are queued for %s and no drain engine is wired into this build", len(pending), ws)
		}
		out, err := opt.Drainer.ApplyDeferred(ctx, DrainOptions{Workspace: ws, Now: now})
		if err != nil {
			return fmt.Errorf("apply the deferred queue of %s: %w", ws, err)
		}
		res.Receipt.DeferredApplied = append(res.Receipt.DeferredApplied, out.Applied...)
		res.Receipt.Resurrected = append(res.Receipt.Resurrected, out.Resurrected...)
		if len(out.Applied) > 0 || len(out.Resurrected) > 0 {
			fmt.Fprintf(logw, "sync: %s: applied %d deferred change(s), %d still queued, %d resurrected\n",
				ws, len(out.Applied), len(out.StillQueued), len(out.Resurrected))
		}
	}
	return nil
}

// SSHCommand builds the GIT_SSH_COMMAND every network call runs under (DESIGN:184).
//
// The options themselves live in gitx.SSHCommand, beside the guard that refuses a network
// subcommand without them: a second hand-built copy of this string is exactly how the
// merge engine's Fetch and Push came to dial with the ambient ssh config.
func SSHCommand(stateRoot string) string { return gitx.SSHCommand(stateRoot) }

// fetchHub fetches the shared branch, tolerating the one failure that is not a failure:
// a hub that has no branch yet.
//
// The first PC to seed the fleet fetches from an empty bare repository, and git answers
// "couldn't find remote ref main" with exit 128. Treating that as a network error would
// make the seed procedure - the one described in blueprint section 9 - impossible to run.
func fetchHub(ctx context.Context, netOpts gitx.Options) error {
	o := netOpts
	o.OkExit = gitx.OkExitCodes(0, 128)
	res, err := gitx.Run(ctx, o, "fetch", "--prune", HubRemote, Branch)
	if err != nil {
		return err
	}
	if res.Code == 0 {
		return nil
	}
	if strings.Contains(strings.ToLower(res.Stderr), "couldn't find remote ref") {
		return nil // the hub is empty; this PC is seeding it
	}
	return errors.New(oneLine(res.Stderr))
}

func mergeStep(ctx context.Context, opt Options, repo Repo, machineID string, now time.Time, workspaces []string) (*MergeResult, error) {
	theirs := "refs/remotes/" + HubRemote + "/" + Branch
	if !refExists(ctx, repo, theirs) {
		return nil, nil // the hub has no branch yet; this PC is seeding it
	}
	if opt.Merger == nil {
		// Without an engine there is nothing that can safely bring their commits into
		// the work tree, so say so rather than push over them.
		if ahead, err := hasIncoming(ctx, repo, theirs); err == nil && ahead {
			return nil, errors.New("the hub has commits this build cannot merge (no merge engine wired)")
		}
		return nil, nil
	}
	out, err := opt.Merger.Merge(ctx, MergeOptions{
		GitDir:     repo.GitDir,
		WorkTree:   repo.WorkTree,
		Ours:       "refs/heads/" + Branch,
		Theirs:     theirs,
		MachineID:  machineID,
		Now:        now,
		Workspaces: workspaces,
	})
	if err != nil {
		return nil, err
	}
	return &out, nil
}

func refExists(ctx context.Context, repo Repo, ref string) bool {
	res, err := gitx.Run(ctx, gitx.Options{
		GitDir: repo.GitDir, OkExit: gitx.OkExitCodes(0, 1, 128),
	}, "rev-parse", "--verify", "--quiet", ref)
	return err == nil && res.Code == 0
}

func hasIncoming(ctx context.Context, repo Repo, theirs string) (bool, error) {
	res, err := gitx.Run(ctx, gitx.Options{
		GitDir: repo.GitDir, OkExit: gitx.OkExitCodes(0, 1, 128),
	}, "rev-list", "--count", "refs/heads/"+Branch+".."+theirs)
	if err != nil || res.Code != 0 {
		return false, err
	}
	return strings.TrimSpace(res.Stdout) != "0", nil
}

func deriveAll(ctx context.Context, opt Options, workspaces []string, now time.Time, logw io.Writer) []DeriveResult {
	if opt.Deriver == nil {
		return nil
	}
	out := make([]DeriveResult, 0, len(workspaces))
	for _, ws := range workspaces {
		r, err := opt.Deriver.Derive(ctx, DeriveOptions{
			Workspace: ws,
			StoreDir:  store.Dir(opt.Roots.ProjectsRoot, ws),
			Now:       now,
		})
		if err != nil {
			// One store's derive failing must not stop the others or abandon the sync:
			// the whole point of deriving before the fetch is that the pass still runs.
			fmt.Fprintf(logw, "sync: derive %s: %v\n", ws, err)
			continue
		}
		out = append(out, r)
	}
	return out
}

func resolveWorkspaces(opt Options) ([]string, error) {
	if len(opt.Workspaces) > 0 {
		return append([]string(nil), opt.Workspaces...), nil
	}
	stores, _, err := store.Enumerate(opt.Roots.ProjectsRoot)
	if err != nil {
		return nil, err
	}
	out := make([]string, 0, len(stores))
	for _, s := range stores {
		if s.IsAlias {
			continue // mutating through an alias mutates the real store twice
		}
		out = append(out, s.Workspace)
	}
	return out, nil
}

// isNonFastForward classifies a push rejection. Git spells it several ways across
// versions and hosting stacks, so the match is on all of them rather than on one.
func isNonFastForward(s string) bool {
	l := strings.ToLower(s)
	return strings.Contains(l, "non-fast-forward") ||
		strings.Contains(l, "fetch first") ||
		strings.Contains(l, "! [rejected]") ||
		strings.Contains(l, "updates were rejected")
}

func writeReceipt(stateRoot string, r Receipt, logw io.Writer) {
	if err := AppendReceipt(stateRoot, r); err != nil {
		fmt.Fprintf(logw, "sync: could not write the receipt: %v\n", err)
	}
}

func oneLine(s string) string {
	s = strings.ReplaceAll(strings.ReplaceAll(s, "\r\n", " "), "\n", " ")
	s = strings.Join(strings.Fields(s), " ")
	if len(s) > 400 {
		s = s[:400] + "..."
	}
	return s
}

func short(sha string) string {
	if len(sha) > 8 {
		return sha[:8]
	}
	return sha
}
