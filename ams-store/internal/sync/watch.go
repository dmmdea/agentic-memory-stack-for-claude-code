package sync

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/live"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
	"github.com/fsnotify/fsnotify"
)

// Watcher timings, DESIGN:176-184.
const (
	// IdleTimeout is how long the watcher stays resident with nothing dirty and nothing
	// new before it exits. A watcher is not a daemon: it is started by whichever
	// SessionStart finds none running, and it goes away again.
	IdleTimeout = 10 * time.Minute
	// RemoteCheckEvery bounds how often the hub is asked whether it has moved. The check
	// is a single `ls-remote`, and it runs ONLY while a session is live: a PC with
	// nobody working on it must produce no network traffic at all.
	RemoteCheckEvery = 5 * time.Minute
	// RetryHeldAfter is how soon a pass skipped because the per-PC lock was held is tried
	// again. Acquire never waits, so a held lock costs one failed create and a short timer;
	// the dirty marker the pass was answering is still there.
	RetryHeldAfter = 3 * time.Second
)

// Exit reasons a watch run ends with.
const (
	ExitReasonNoSession = "no-session"
	ExitReasonIdle      = "idle"
	ExitReasonCancelled = "cancelled"
	ExitReasonError     = "error"
	// ExitReasonLegacyWatcher: a pre-1.31.3 watcher still holds the bare singleton name.
	ExitReasonLegacyWatcher = "legacy-watcher"
)

// LegacyWatcherLog is the file in the state root a refused start appends its line to. The
// watcher is spawned hidden with no console, so stderr alone would reach nobody.
const LegacyWatcherLog = "watch-refused.log"

// ErrLegacyWatcher is returned when a watcher from before the per-store mutex names is still
// alive on this desktop. Starting beside it would put two watchers on one store.
var ErrLegacyWatcher = errors.New("an older ams-store watcher (bare Local\\ams-store-watch) is still running")

// WatchOptions configures the singleton watcher.
type WatchOptions struct {
	Options
	// IdleTimeout and RemoteCheckEvery override the defaults; zero means the default.
	IdleTimeout      time.Duration
	RemoteCheckEvery time.Duration
	// LivenessWithin is the transcript window that counts as a live session.
	LivenessWithin time.Duration
	// SingletonName overrides the named mutex, for tests.
	SingletonName string
	// LegacyWatchName is the one-release transitional probe (1.31.3): when set and a mutex of
	// that name is open, the watcher refuses to start. The CLI sets the bare pre-1.31.3 name
	// for the operator's default store only; it is only ever OPENED, never created or taken,
	// so a test or scratch root never touches the real name. Remove with the next release.
	LegacyWatchName string
	// AcquirePassLock takes the per-PC lock for ONE pass and returns its release, or
	// ok=false when another process holds it. Nil means the lock package's production
	// names and the state root's lock file. The CLI wires its own (the test-name seam).
	//
	// P5-10 (2026-09-19): a watcher pass used to call Once with no lock at all and with
	// the watcher's START time as the pass clock. A hook-driven `sync --once` (which
	// holds the lock for its whole pass) merged the hub and queued three deletions while
	// the watcher's pass was staging the pre-merge work tree; the watcher then committed
	// that stage on top of the merge and pushed it - the deletions came back on every
	// PC. Every pass now takes the same lock every other verb takes, with a fresh clock.
	AcquirePassLock func(now time.Time) (release func(), ok bool, err error)
}

// WatchSummary is what a watch run did before exiting.
type WatchSummary struct {
	// Started is false when another watcher already held the singleton: a second
	// instance exits 0 SILENTLY, because a session starting while the watcher runs is
	// the normal case, not a fault.
	Started bool
	Reason  string
	Syncs   int
	// RemoteChecks counts ls-remote calls, so a test can assert the "only while live,
	// only every 5 min" rule rather than trusting it.
	RemoteChecks int
	// Skipped counts passes that found the per-PC lock held and were retried.
	Skipped int
}

// Watch runs the singleton watcher until no session is live or the idle window expires.
func Watch(ctx context.Context, opt WatchOptions) (WatchSummary, error) {
	logw := opt.Log
	if logw == nil {
		logw = io.Discard
	}
	if opt.LegacyWatchName != "" {
		if held, pErr := lock.MutexExists(opt.LegacyWatchName); pErr == nil && held {
			line := fmt.Sprintf("%s ams-store sync --watch REFUSED: an older ams-store watcher still holds %s (a pre-1.31.3 image running from ams-store.exe.prev); two watchers would run on one store. Stop it (re-run the installer, or end that ams-store.exe) and the next SessionStart starts this one.\n",
				time.Now().UTC().Format(time.RFC3339), opt.LegacyWatchName)
			fmt.Fprint(logw, line)
			if mErr := os.MkdirAll(opt.Roots.StateRoot, 0o755); mErr == nil {
				if f, fErr := os.OpenFile(filepath.Join(opt.Roots.StateRoot, LegacyWatcherLog), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644); fErr == nil {
					_, _ = f.WriteString(line)
					_ = f.Close()
				}
			}
			return WatchSummary{Reason: ExitReasonLegacyWatcher}, ErrLegacyWatcher
		}
	}
	single, ok, err := lock.AcquireSingleton(lock.SingletonOptions{
		Name: opt.SingletonName,
		Path: filepath.Join(opt.Roots.StateRoot, lock.WatchFileName),
	})
	if err != nil {
		return WatchSummary{Reason: ExitReasonError}, err
	}
	if !ok {
		return WatchSummary{Started: false, Reason: "already-running"}, nil
	}
	defer single.Release()

	if err := os.MkdirAll(opt.Roots.StateRoot, 0o755); err != nil {
		return WatchSummary{Reason: ExitReasonError}, err
	}

	w, err := fsnotify.NewWatcher()
	if err != nil {
		return WatchSummary{Reason: ExitReasonError}, fmt.Errorf("sync: fsnotify: %w", err)
	}
	defer w.Close()
	// Watch the DIRECTORY, not the marker file. The marker is created, removed and
	// re-created on every cycle, and a watch on a path that stops existing is a watch
	// that silently stops firing.
	if err := w.Add(opt.Roots.StateRoot); err != nil {
		return WatchSummary{Reason: ExitReasonError}, fmt.Errorf("sync: watch %s: %w", opt.Roots.StateRoot, err)
	}

	events := make(chan struct{}, 1)
	done := make(chan struct{})
	defer close(done)
	go func() {
		marker := filepath.Join(opt.Roots.StateRoot, DirtyMarker)
		for {
			select {
			case <-done:
				return
			case ev, open := <-w.Events:
				if !open {
					return
				}
				if !sameFile(ev.Name, marker) {
					continue
				}
				select {
				case events <- struct{}{}:
				default: // a pending wake-up already says what this one would
				}
			case _, open := <-w.Errors:
				if !open {
					return
				}
			}
		}
	}()

	within := opt.LivenessWithin
	if within <= 0 {
		within = live.DefaultWithin
	}
	repo := NewRepo(opt.Roots)
	deps := watchDeps{
		events:  events,
		anyLive: func() bool { return live.AnyClaudeSession(opt.Roots.ProjectsRoot, within, time.Now()) },
		isDirty: func() bool { return IsDirty(opt.Roots.StateRoot) },
		lsRemote: func(c context.Context) (string, error) {
			return LsRemoteHead(c, repo, opt.Roots.StateRoot, opt.Timeout)
		},
		runSync: watchPass(opt.Options, opt.AcquirePassLock, time.Now, logw),
		now:     time.Now,
		after:   time.After,
		log:     logw,
	}
	sum, err := runWatch(ctx, opt, deps)
	sum.Started = true
	return sum, err
}

// watchDeps is everything the loop touches that is not pure logic. It exists so the exit
// conditions and the "only while live, only every 5 min" network rule are testable
// without a real filesystem, a real clock or a real hub.
type watchDeps struct {
	events   <-chan struct{}
	anyLive  func() bool
	isDirty  func() bool
	lsRemote func(context.Context) (string, error)
	runSync  func(context.Context) Result
	now      func() time.Time
	after    func(time.Duration) <-chan time.Time
	log      io.Writer
}

// watchPass is one sync pass as the watcher runs it: the per-PC lock taken first, a
// clock read for THIS pass, Once, release. The lock is the same file and mutexes every
// other verb takes, so a pass and a hook-driven `sync --once` can never interleave their
// stage, merge and commit steps (P5-10). A held lock is not an error: the pass reports
// LockHeld, writes nothing, and the loop retries after RetryHeldAfter while the dirty
// marker stands.
func watchPass(base Options, acquire func(time.Time) (func(), bool, error), clock func() time.Time, logw io.Writer) func(context.Context) Result {
	if acquire == nil {
		acquire = func(now time.Time) (func(), bool, error) {
			l, err := lock.Acquire(lock.Options{
				Path:   filepath.Join(base.Roots.StateRoot, lock.FileName),
				Reason: "sync",
				Now:    now,
			})
			if err != nil {
				if errors.Is(err, lock.ErrHeld) {
					return nil, false, nil
				}
				return nil, false, err
			}
			return func() { _ = l.Release() }, true, nil
		}
	}
	if clock == nil {
		clock = time.Now
	}
	if logw == nil {
		logw = io.Discard
	}
	return func(ctx context.Context) Result {
		o := base
		o.Now = clock()
		release, ok, err := acquire(o.Now)
		if err != nil {
			return Result{ExitCode: exitRefused, Err: fmt.Errorf("watch: per-PC lock: %w", err)}
		}
		if !ok {
			fmt.Fprintf(logw, "watch: the per-PC lock is held by another process; pass skipped, retry in %s\n", RetryHeldAfter)
			return Result{ExitCode: exitLocked, LockHeld: true}
		}
		defer release()
		return Once(ctx, o)
	}
}

// runWatch is the event loop of DESIGN:179-184.
//
// There is no ticker anywhere in it. A ticker would wake the process on a schedule
// whether or not anything had happened, which on a laptop is a wakeup budget spent for
// nothing; the loop instead blocks on the filesystem event and arms a SINGLE timer,
// and only while a session is live. With no session live it does not arm anything - it
// exits.
func runWatch(ctx context.Context, opt WatchOptions, d watchDeps) (WatchSummary, error) {
	idle := opt.IdleTimeout
	if idle <= 0 {
		idle = IdleTimeout
	}
	every := opt.RemoteCheckEvery
	if every <= 0 {
		every = RemoteCheckEvery
	}
	logw := d.log
	if logw == nil {
		logw = io.Discard
	}

	sum := WatchSummary{}
	start := d.now()
	idleDeadline := start.Add(idle)
	nextRemote := start.Add(every)
	lastHead := ""
	// retryAt is set when a pass found the per-PC lock held: the marker is still dirty
	// and nothing else will wake the loop for it, so a short timer does.
	var retryAt time.Time
	pass := func(ctx context.Context) {
		res := d.runSync(ctx)
		if res.LockHeld {
			sum.Skipped++
			retryAt = d.now().Add(RetryHeldAfter)
			return
		}
		retryAt = time.Time{}
		sum.Syncs++
		if res.Err != nil {
			fmt.Fprintf(logw, "watch: sync: %v\n", res.Err)
		}
		idleDeadline = d.now().Add(idle)
	}

	for {
		if !d.anyLive() {
			sum.Reason = ExitReasonNoSession
			return sum, nil
		}

		now := d.now()
		if !now.Before(idleDeadline) {
			sum.Reason = ExitReasonIdle
			return sum, nil
		}
		wait := idleDeadline.Sub(now)
		if r := nextRemote.Sub(now); r < wait {
			wait = r
		}
		if !retryAt.IsZero() {
			if r := retryAt.Sub(now); r < wait {
				wait = r
			}
		}
		if wait < 0 {
			wait = 0
		}

		select {
		case <-ctx.Done():
			sum.Reason = ExitReasonCancelled
			return sum, nil

		case _, open := <-d.events:
			if !open {
				sum.Reason = ExitReasonError
				return sum, nil
			}
			if !d.isDirty() {
				// The marker was removed rather than written: that is a sync clearing
				// it, not new work.
				continue
			}
			pass(ctx)

		case <-d.after(wait):
			now := d.now()
			if !d.anyLive() {
				sum.Reason = ExitReasonNoSession
				return sum, nil
			}
			if !retryAt.IsZero() && !now.Before(retryAt) {
				if d.isDirty() {
					pass(ctx)
				} else {
					retryAt = time.Time{} // the holder's own pass cleared the marker
				}
			}
			if !now.Before(nextRemote) {
				nextRemote = now.Add(every)
				sum.RemoteChecks++
				head, err := d.lsRemote(ctx)
				if err != nil {
					// The hub being unreachable is not the watcher's problem to solve.
					// It keeps watching; the next dirty marker will try again.
					fmt.Fprintf(logw, "watch: ls-remote: %v\n", err)
				} else if head != "" && head != lastHead {
					if lastHead != "" {
						pass(ctx)
					}
					lastHead = head
				}
			}
			if !d.now().Before(idleDeadline) {
				sum.Reason = ExitReasonIdle
				return sum, nil
			}
		}
	}
}

// LsRemoteHead is the cheap hub-side change check: one `ls-remote` for one ref.
func LsRemoteHead(ctx context.Context, r Repo, stateRoot string, timeout time.Duration) (string, error) {
	res, err := gitx.Run(ctx, gitx.Options{
		GitDir:   r.GitDir,
		Timeout:  timeout,
		ExtraEnv: gitx.NetworkEnv(stateRoot),
	}, "ls-remote", "--heads", HubRemote, Branch)
	if err != nil {
		return "", err
	}
	line := strings.TrimSpace(res.Stdout)
	if line == "" {
		return "", nil
	}
	return strings.Fields(line)[0], nil
}

func sameFile(a, b string) bool {
	return strings.EqualFold(filepath.Clean(a), filepath.Clean(b))
}
