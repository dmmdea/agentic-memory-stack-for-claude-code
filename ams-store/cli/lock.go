package cli

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
)

// lockUsage is the --help block for `ams-store lock`, blueprint section 1.1.
const lockUsage = `usage: ams-store lock status
       ams-store lock acquire --for <dur> --reason <s>
       ams-store lock release
       ams-store lock break

Inspect and hold the per-PC cross-process lock (PID + process start time, stale after
10 minutes). A contender skips immediately rather than queueing - maintenance that
waits behind maintenance is maintenance that runs under a live session.

  status                 print the holder, its reason and its age
  acquire                take the lock for a bounded window
  release                release a lock this process holds
  break                  force-release a lock (stale holders only)

  --for <dur>            how long acquire holds it (default 1m; 0 takes and returns it)
  --reason <s>           what the lock is being taken for (gate|derive|sync|...)
  --json                 machine output on stdout

Exit: 0 acquired or released, 2 bad invocation, 4 held by another process.`

func lockCommand() command {
	return command{
		Name:    "lock",
		Summary: "inspect and hold the per-PC cross-process lock",
		Usage:   lockUsage,
		Run:     runLock,
	}
}

// LockPath is the per-PC lock file under a state root.
func LockPath(stateRoot string) string { return filepath.Join(stateRoot, lock.FileName) }

// lockNames is the pair of Windows named mutexes every verb in this package takes the
// per-PC lock with. Empty means the production names, which is what the binary always
// uses; only a test binary ever sets it.
//
// internal/lock already offers the seam - lock.Options.MutexName and LegacyMutexName
// exist, its own suite isolates itself through them - but the verbs BUILD their
// lock.Options here, so a cli test had no way to say "not the operator's real lock".
// The cost of that was measured: with the PowerShell compactor's own
// Local\ams-memory-compact held by any sibling process (a real nightly compaction, a
// second checkout's test binary), sixteen cli tests fail on exit 4, and the ones that
// assert about a verb's OUTPUT go vacuously green because the verb skipped as a
// contender before it did anything to assert about. That is worse than flakiness: the
// gate's no-network test reported PASS while the gate never ran.
//
// It is package-level state rather than a field on globalOpts on purpose: the flag set
// is the operator's surface, and a --mutex-name flag would be a way to point the
// shipped binary away from the lock it exists to respect.
var lockNames struct {
	mutex  string
	legacy string
}

// UseTestLockNames scopes every per-PC lock this package takes to the given mutex names
// and returns the restore function. It is exported for cli's external test package,
// which is the only caller: package cli_test cannot reach an unexported variable, and a
// test that has to spawn a whole binary to isolate a mutex will not be written.
//
// legacy may be "-" to disable the legacy mutex entirely, exactly as lock.Options
// documents.
func UseTestLockNames(mutexName, legacy string) func() {
	prev := lockNames
	lockNames.mutex, lockNames.legacy = mutexName, legacy
	return func() { lockNames = prev }
}

// lockOptions is the ONE construction of lock.Options in this package. Every verb goes
// through it, so an isolation seam cannot reach four call sites and miss the fifth.
func lockOptions(path, reason string, now time.Time) lock.Options {
	return lock.Options{
		Path:            path,
		Reason:          reason,
		Now:             now,
		MutexName:       lockNames.mutex,
		LegacyMutexName: lockNames.legacy,
	}
}

func runLock(env Env, args []string) int {
	var g globalOpts
	fs := newFlagSet("lock")
	g.bind(fs)
	var hold time.Duration
	var reason string
	fs.DurationVar(&hold, "for", time.Minute, "how long to hold the lock")
	fs.StringVar(&reason, "reason", "manual", "what the lock is taken for")

	positional, err := parseArgs(fs, args)
	if err != nil {
		return usageError(env, "lock", err)
	}
	if len(positional) != 1 {
		return usageError(env, "lock", errors.New("exactly one of status|acquire|release|break is required"))
	}
	roots, err := g.roots()
	if err != nil {
		return usageError(env, "lock", err)
	}
	now, err := g.now()
	if err != nil {
		return usageError(env, "lock", err)
	}
	path := LockPath(roots.StateRoot)

	switch positional[0] {
	case "status":
		return lockStatus(env, g, path, now)
	case "acquire":
		return lockAcquire(env, g, path, reason, hold, now)
	case "release":
		return lockRelease(env, path, now)
	case "break":
		return lockBreak(env, g, path)
	default:
		return usageError(env, "lock", fmt.Errorf("unknown subcommand %q; want status|acquire|release|break", positional[0]))
	}
}

// lockStatus REPORTS; it is never a contender, so it exits 0 whatever it finds. A status
// verb that exited non-zero on a healthy held lock would make every wrapper script treat
// the normal case as broken.
func lockStatus(env Env, g globalOpts, path string, now time.Time) int {
	st, err := lock.Inspect(path, now, lock.StaleAfter)
	if err != nil {
		// A lock file that exists but cannot be read is exactly the state `break` is for,
		// and saying so is more use than a stack trace.
		fmt.Fprintf(env.Stderr, "ams-store lock: %s is unreadable (%v); `ams-store lock break` clears it\n", path, err)
		if g.json {
			_ = writeJSON(env.Stdout, map[string]any{"present": true, "readable": false, "path": path})
		}
		return ExitOK
	}
	if g.json {
		_ = writeJSON(env.Stdout, st)
		return ExitOK
	}
	if !st.Present {
		fmt.Fprintf(env.Stdout, "lock free (%s)\n", path)
		return ExitOK
	}
	fmt.Fprintf(env.Stdout, "lock held by pid %d on %s for %q, age %ds, live=%v stale=%v\n",
		st.Holder.PID, st.Holder.Host, st.Holder.Reason, st.AgeSecs, st.Live, st.Stale)
	return ExitOK
}

// lockAcquire takes the lock and holds it for a bounded window.
//
// The window is what makes `acquire` honest from a shell: a process that wrote the lock
// file and exited would leave a holder whose PID is dead, which the next contender
// breaks at once - so the lock would have protected nothing.
func lockAcquire(env Env, g globalOpts, path, reason string, hold time.Duration, now time.Time) int {
	l, err := lock.Acquire(lockOptions(path, reason, now))
	if err != nil {
		if errors.Is(err, lock.ErrHeld) {
			// A contender SKIPS. No retry, no backoff, no timeout parameter.
			if h, rErr := lock.ReadHolder(path); rErr == nil && h != nil {
				fmt.Fprintf(env.Stderr, "ams-store lock: held by pid %d on %s for %q since %s; skipping\n",
					h.PID, h.Host, h.Reason, h.AcquiredAt.UTC().Format(time.RFC3339))
			} else {
				fmt.Fprintln(env.Stderr, "ams-store lock: held by another process; skipping")
			}
			return ExitLocked
		}
		fmt.Fprintf(env.Stderr, "ams-store lock: %v\n", err)
		return ExitRefused
	}
	defer l.Release()

	if g.json {
		_ = writeJSON(env.Stdout, l.Holder())
	} else {
		fmt.Fprintf(env.Stdout, "lock acquired by pid %d for %q\n", l.Holder().PID, reason)
	}
	if hold > 0 {
		time.Sleep(hold)
	}
	return ExitOK
}

// lockRelease drops a lock this process may legitimately drop.
//
// It is NOT break: quietly deleting a live process's lock is how two maintainers end up
// writing the same index in the same second, so a live foreign holder is refused and the
// operator is pointed at the verb that exists for that.
func lockRelease(env Env, path string, now time.Time) int {
	h, err := lock.ReadHolder(path)
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store lock: %s is unreadable (%v); use `ams-store lock break`\n", path, err)
		return ExitLocked
	}
	if h == nil {
		return ExitOK // nothing to release is a successful release
	}
	if h.PID != os.Getpid() && lock.IsLive(*h, now, lock.StaleAfter) {
		fmt.Fprintf(env.Stderr, "ams-store lock: pid %d still holds it for %q; use `ams-store lock break` if that is wrong\n", h.PID, h.Reason)
		return ExitLocked
	}
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		fmt.Fprintf(env.Stderr, "ams-store lock: %v\n", err)
		return ExitRefused
	}
	return ExitOK
}

// lockBreak is operator-only and never silent: the holder is printed BEFORE the file
// goes, so a break the operator regrets is at least a break they can describe.
func lockBreak(env Env, g globalOpts, path string) int {
	h, _ := lock.ReadHolder(path)
	if g.json {
		_ = writeJSON(env.Stdout, map[string]any{"broke": h != nil, "holder": h})
	} else if h == nil {
		fmt.Fprintln(env.Stdout, "lock free; nothing to break")
	} else {
		fmt.Fprintf(env.Stdout, "breaking lock held by pid %d on %s for %q since %s\n",
			h.PID, h.Host, h.Reason, h.AcquiredAt.UTC().Format(time.RFC3339))
	}
	if _, err := lock.Break(path); err != nil {
		fmt.Fprintf(env.Stderr, "ams-store lock: %v\n", err)
		return ExitRefused
	}
	return ExitOK
}
