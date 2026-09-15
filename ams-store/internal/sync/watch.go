package sync

import (
	"context"
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
)

// Exit reasons a watch run ends with.
const (
	ExitReasonNoSession = "no-session"
	ExitReasonIdle      = "idle"
	ExitReasonCancelled = "cancelled"
	ExitReasonError     = "error"
)

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
}

// Watch runs the singleton watcher until no session is live or the idle window expires.
func Watch(ctx context.Context, opt WatchOptions) (WatchSummary, error) {
	logw := opt.Log
	if logw == nil {
		logw = io.Discard
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
		runSync: func(c context.Context) Result { return Once(c, opt.Options) },
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
			res := d.runSync(ctx)
			sum.Syncs++
			if res.Err != nil {
				fmt.Fprintf(logw, "watch: sync: %v\n", res.Err)
			}
			idleDeadline = d.now().Add(idle)

		case <-d.after(wait):
			now := d.now()
			if !d.anyLive() {
				sum.Reason = ExitReasonNoSession
				return sum, nil
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
						res := d.runSync(ctx)
						sum.Syncs++
						if res.Err != nil {
							fmt.Fprintf(logw, "watch: sync: %v\n", res.Err)
						}
						idleDeadline = d.now().Add(idle)
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
