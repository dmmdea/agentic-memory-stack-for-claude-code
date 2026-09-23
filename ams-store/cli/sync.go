package cli

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	amsync "github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/sync"
)

// syncUsage is the --help block for `ams-store sync`, blueprint section 1.1.
const syncUsage = `usage: ams-store sync [--once] [--watch] [--timeout <dur>] [--hub-host <name>]
                      [--workspace <slug>] [--json]

Commit every store locally, then fetch, merge and push against the hub. --watch is the
singleton watcher: one per PC, holding watch.lock, waking on the dirty marker.

  --once                 one pass, then exit (the default)
  --watch                stay resident and sync on every dirty marker
  --timeout <dur>        bound each network git call (default 30s)
  --hub-host <name>      the MagicDNS name the remote policy pins the hub to
  --workspace <slug>     restrict the pass to a workspace slug; repeatable
  --allow-local-path     permit a filesystem path as the hub URL (the hub's own checkout)
  --json                 one JSON document on stdout, nothing else

The LOCAL COMMIT happens BEFORE the fetch, so a PC with no connectivity still keeps its
history. Having no hub configured is a fully successful pass, not a failure.

Exit: 0 normal, 2 bad invocation, 3 refused (no history repo, or a remote that is not
the hub), 4 lock held, 5 hub unreachable, 6 a conflict was recorded in history.`

func syncCommand() command {
	return command{
		Name:    "sync",
		Summary: "commit, fetch, merge and push against the hub",
		Usage:   syncUsage,
		Run:     runSync,
	}
}

func runSync(env Env, args []string) int {
	var g globalOpts
	fs := newFlagSet("sync")
	g.bind(fs)
	var once, watch, allowLocal bool
	var timeout time.Duration
	var hubHost string
	var workspaces stringList
	fs.BoolVar(&once, "once", false, "one pass, then exit")
	fs.BoolVar(&watch, "watch", false, "stay resident")
	fs.BoolVar(&allowLocal, "allow-local-path", false, "permit a filesystem path as the hub URL")
	fs.DurationVar(&timeout, "timeout", 30*time.Second, "bound each network git call")
	fs.StringVar(&hubHost, "hub-host", "", "the MagicDNS name of the hub")
	fs.Var(&workspaces, "workspace", "restrict the pass to a workspace slug")
	if _, err := parseArgs(fs, args); err != nil {
		return usageError(env, "sync", err)
	}
	if once && watch {
		return usageError(env, "sync", errors.New("--once and --watch are alternatives, not a pair"))
	}

	roots, err := g.roots()
	if err != nil {
		return usageError(env, "sync", err)
	}
	now, err := g.now()
	if err != nil {
		return usageError(env, "sync", err)
	}

	opt := amsync.Options{
		Roots:      roots,
		MachineID:  g.machineID,
		Policy:     amsync.RemotePolicy{ExpectedHost: hubHost, AllowLocalPath: allowLocal},
		Now:        now,
		Timeout:    timeout,
		Workspaces: workspaces,
		Version:    BuildInfo.Version,
		Log:        g.logWriter(env),
	}

	// The engines (cli/seams.go). A nil Deriver silently skips every derive step and a
	// nil Merger turns "the hub has new commits" into a refusal, so both are wired here
	// and nowhere else.
	logw := g.logWriter(env)
	opt.Deriver = deriverAdapter{roots: roots, machineID: g.machineID, log: logw}
	opt.Merger = mergerAdapter{roots: roots, machineID: g.machineID, log: logw}
	// The queue drain. A nil Drainer makes a pass with queued changes REFUSE, so this
	// wiring is not optional decoration: it is the only production caller of
	// ApplyDeferred, and without it the deferred queue is written and never read.
	opt.Drainer = drainerAdapter{roots: roots, machineID: g.machineID, log: logw}

	if watch {
		return runSyncWatch(env, g, opt)
	}
	return runSyncOnce(env, g, opt)
}

// runSyncOnce takes the per-PC lock for the whole pass. sync.Once deliberately does NOT
// take it: the lock covers derive AND sync together, and the verb is what owns both.
func runSyncOnce(env Env, g globalOpts, opt amsync.Options) int {
	l, err := lock.Acquire(lockOptions(LockPath(opt.Roots.StateRoot), "sync", opt.Now))
	if err != nil {
		if errors.Is(err, lock.ErrHeld) {
			if h, rErr := lock.ReadHolder(LockPath(opt.Roots.StateRoot)); rErr == nil && h != nil {
				fmt.Fprintf(env.Stderr, "ams-store sync: held by pid %d for %q; skipping\n", h.PID, h.Reason)
			} else {
				fmt.Fprintln(env.Stderr, "ams-store sync: the per-PC lock is held; skipping")
			}
			return ExitLocked
		}
		fmt.Fprintf(env.Stderr, "ams-store sync: %v\n", err)
		return ExitRefused
	}
	defer func() { _ = l.Release() }()

	res := amsync.Once(context.Background(), opt)
	// The G7 clock, from the same maintenance path that derived the index. sync cannot
	// write it itself: internal/lint imports internal/sync for the remote policy, so the
	// stamp is recorded here, where every package may be imported.
	for _, d := range res.Derived {
		recordOverTrigger(opt.Roots, d.Workspace, d.AfterBytes, false, opt.Now, env.Stderr)
	}
	if res.Err != nil {
		fmt.Fprintf(env.Stderr, "ams-store sync: %v\n", res.Err)
	}
	if g.json {
		if err := writeJSON(env.Stdout, res.Receipt); err != nil {
			fmt.Fprintf(env.Stderr, "ams-store sync: %v\n", err)
		}
	}
	return res.ExitCode
}

// runSyncWatch does NOT hold the per-PC lock for its lifetime - it outlives the 10-minute
// staleness window, so holding it would make every other verb on the PC look like a
// contender behind a lock that is, by its own rule, dead. Each pass the watcher runs
// takes and drops the lock itself.
func runSyncWatch(env Env, g globalOpts, opt amsync.Options) int {
	// Each pass takes the per-PC lock through the same seam every other verb uses, so a
	// test binary's lock names reach the watcher too (P5-10).
	wo := amsync.WatchOptions{Options: opt}
	wo.LegacyWatchName = legacyWatchNameFor(opt.Roots.StateRoot)
	wo.AcquirePassLock = func(now time.Time) (func(), bool, error) {
		return deriveLock{path: LockPath(opt.Roots.StateRoot), now: now}.TryAcquire("sync")
	}
	sum, err := amsync.Watch(context.Background(), wo)
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store sync --watch: %v\n", err)
		return ExitRefused
	}
	if g.json {
		if wErr := writeJSON(env.Stdout, sum); wErr != nil {
			fmt.Fprintf(env.Stderr, "ams-store sync: %v\n", wErr)
		}
	}
	// A second instance exits 0 SILENTLY: a session starting while the watcher already
	// runs is the normal case, not a fault, and a message here would print on every
	// session start on every PC.
	return ExitOK
}

// legacyWatchNameFor is the bare pre-1.31.3 watcher name for the operator's DEFAULT store and
// "" for any other root. Only the default store ever had a bare-named watcher, and keeping the
// probe off scratch roots means a test never looks at the real name (1.31.3 transitional;
// remove with the next release).
func legacyWatchNameFor(stateRoot string) string {
	def, err := store.DefaultRoots()
	if err != nil {
		return ""
	}
	if lock.StateRootKey(stateRoot) != lock.StateRootKey(def.StateRoot) {
		return ""
	}
	return lock.WatchMutexName
}
