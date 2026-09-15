package sync

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/live"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// fakeClock makes the watcher's timers instantaneous and deterministic. Waiting for a
// real 10-minute idle window in a test would mean either a 10-minute test or a timing
// assertion that flakes on a loaded box; here "arm a timer for d" simply advances the
// clock by d and fires.
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) after(d time.Duration) <-chan time.Time {
	c.mu.Lock()
	c.t = c.t.Add(d)
	fire := c.t
	c.mu.Unlock()
	ch := make(chan time.Time, 1)
	ch <- fire
	return ch
}

func baseDeps(c *fakeClock) watchDeps {
	return watchDeps{
		events:   make(chan struct{}),
		anyLive:  func() bool { return true },
		isDirty:  func() bool { return false },
		lsRemote: func(context.Context) (string, error) { return "", nil },
		runSync:  func(context.Context) Result { return Result{} },
		now:      c.now,
		after:    c.after,
	}
}

// TestWatch_ExitsWhenNoClaudeSessionIsLive is DESIGN:182: the watcher is not a daemon.
// With nobody working on the PC it exits, and it exits BEFORE arming anything, so an
// idle laptop pays nothing at all.
func TestWatch_ExitsWhenNoClaudeSessionIsLive(t *testing.T) {
	c := &fakeClock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	d := baseDeps(c)
	d.anyLive = func() bool { return false }
	d.after = func(time.Duration) <-chan time.Time {
		t.Fatal("the watcher armed a timer with no session live")
		return nil
	}
	d.lsRemote = func(context.Context) (string, error) {
		t.Fatal("the watcher reached the network with no session live")
		return "", nil
	}

	sum, err := runWatch(context.Background(), WatchOptions{}, d)
	if err != nil {
		t.Fatalf("runWatch: %v", err)
	}
	if sum.Reason != ExitReasonNoSession {
		t.Fatalf("exit reason %q, want %q", sum.Reason, ExitReasonNoSession)
	}
	if sum.RemoteChecks != 0 || sum.Syncs != 0 {
		t.Fatalf("an immediately-exiting watcher did work: %+v", sum)
	}
}

// TestWatch_ExitsAfterTenIdleMinutes is DESIGN:183: ten minutes with nothing dirty and
// nothing new and the watcher is done.
func TestWatch_ExitsAfterTenIdleMinutes(t *testing.T) {
	c := &fakeClock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	d := baseDeps(c)

	sum, err := runWatch(context.Background(), WatchOptions{
		IdleTimeout:      10 * time.Minute,
		RemoteCheckEvery: 5 * time.Minute,
	}, d)
	if err != nil {
		t.Fatalf("runWatch: %v", err)
	}
	if sum.Reason != ExitReasonIdle {
		t.Fatalf("exit reason %q, want %q", sum.Reason, ExitReasonIdle)
	}
	if elapsed := c.now().Sub(time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)); elapsed != 10*time.Minute {
		t.Fatalf("the idle window lasted %v, want 10m", elapsed)
	}
	if sum.Syncs != 0 {
		t.Fatalf("an idle watcher synced %d times", sum.Syncs)
	}
}

// TestWatch_ChecksTheHubAtMostEveryFiveMinutes is DESIGN:180-181. The hub check is the
// only network the watcher does, and it is rate-limited: over a ten-minute idle window it
// happens twice, not on every wake-up.
func TestWatch_ChecksTheHubAtMostEveryFiveMinutes(t *testing.T) {
	c := &fakeClock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	d := baseDeps(c)
	sum, err := runWatch(context.Background(), WatchOptions{
		IdleTimeout:      10 * time.Minute,
		RemoteCheckEvery: 5 * time.Minute,
	}, d)
	if err != nil {
		t.Fatalf("runWatch: %v", err)
	}
	if sum.RemoteChecks != 2 {
		t.Fatalf("%d hub checks in a 10-minute window with a 5-minute interval, want 2", sum.RemoteChecks)
	}

	// And with an idle window shorter than the interval, the hub is never asked at all.
	c2 := &fakeClock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	d2 := baseDeps(c2)
	sum2, err := runWatch(context.Background(), WatchOptions{
		IdleTimeout:      4 * time.Minute,
		RemoteCheckEvery: 5 * time.Minute,
	}, d2)
	if err != nil {
		t.Fatalf("runWatch: %v", err)
	}
	if sum2.RemoteChecks != 0 {
		t.Fatalf("the hub was asked %d times before the interval elapsed", sum2.RemoteChecks)
	}
	if sum2.Reason != ExitReasonIdle {
		t.Fatalf("exit reason %q, want %q", sum2.Reason, ExitReasonIdle)
	}
}

// TestWatch_SyncsOnTheDirtyMarker: the marker is the wake-up. A filesystem event that is
// the marker being CLEARED - which is what a sync itself does - must not trigger another
// sync, or the watcher chases its own tail.
func TestWatch_SyncsOnTheDirtyMarker(t *testing.T) {
	c := &fakeClock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	events := make(chan struct{}, 2)
	events <- struct{}{} // the gate wrote the marker
	events <- struct{}{} // the sync cleared it again

	seen := 0
	d := baseDeps(c)
	d.events = events
	// The marker is present for the first event and gone for the second, which is what a
	// completed sync leaves behind.
	d.isDirty = func() bool { seen++; return seen == 1 }
	// Two events, then nobody is working any more: a deterministic end with no timer.
	d.anyLive = func() bool { return seen < 2 }
	// Never fires, so the filesystem event is the only ready case and the assertion
	// cannot be decided by which of two ready channels select happened to pick.
	d.after = func(time.Duration) <-chan time.Time { return make(chan time.Time) }

	sum, err := runWatch(context.Background(), WatchOptions{
		IdleTimeout: 10 * time.Minute, RemoteCheckEvery: 5 * time.Minute,
	}, d)
	if err != nil {
		t.Fatalf("runWatch: %v", err)
	}
	if sum.Syncs != 1 {
		t.Fatalf("%d syncs, want exactly 1: the clearing event must not re-trigger", sum.Syncs)
	}
	if seen != 2 {
		t.Fatalf("the watcher consumed %d events, want 2", seen)
	}
}

// TestWatch_HubMovingTriggersASync: a change on the hub while a session is live is the
// second wake-up condition.
func TestWatch_HubMovingTriggersASync(t *testing.T) {
	c := &fakeClock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	heads := []string{"aaa", "aaa", "bbb"}
	calls := 0
	synced := 0
	d := baseDeps(c)
	d.lsRemote = func(context.Context) (string, error) {
		h := heads[min(calls, len(heads)-1)]
		calls++
		return h, nil
	}
	d.runSync = func(context.Context) Result { synced++; return Result{} }

	sum, err := runWatch(context.Background(), WatchOptions{
		IdleTimeout: 15 * time.Minute, RemoteCheckEvery: 5 * time.Minute,
	}, d)
	if err != nil {
		t.Fatalf("runWatch: %v", err)
	}
	if synced != 1 {
		t.Fatalf("%d syncs, want 1: only the head CHANGING is news", synced)
	}
	if sum.Reason != ExitReasonIdle {
		t.Fatalf("exit reason %q", sum.Reason)
	}
}

// TestWatch_HubUnreachableKeepsWatching: the hub being down is not the watcher's problem
// to solve. It logs and keeps watching; the next dirty marker will try again.
func TestWatch_HubUnreachableKeepsWatching(t *testing.T) {
	c := &fakeClock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	d := baseDeps(c)
	d.lsRemote = func(context.Context) (string, error) { return "", os.ErrDeadlineExceeded }

	sum, err := runWatch(context.Background(), WatchOptions{
		IdleTimeout: 10 * time.Minute, RemoteCheckEvery: 5 * time.Minute,
	}, d)
	if err != nil {
		t.Fatalf("an unreachable hub must not fail the watcher: %v", err)
	}
	if sum.Reason != ExitReasonIdle {
		t.Fatalf("exit reason %q, want %q", sum.Reason, ExitReasonIdle)
	}
	if sum.RemoteChecks != 2 {
		t.Fatalf("%d hub checks, want 2", sum.RemoteChecks)
	}
}

// TestWatch_CancelledContextStops keeps the verb interruptible.
func TestWatch_CancelledContextStops(t *testing.T) {
	c := &fakeClock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	d := baseDeps(c)
	// Never fires, so the only ready case is the cancelled context.
	d.after = func(time.Duration) <-chan time.Time { return make(chan time.Time) }
	sum, err := runWatch(ctx, WatchOptions{IdleTimeout: time.Hour, RemoteCheckEvery: time.Hour}, d)
	if err != nil {
		t.Fatalf("runWatch: %v", err)
	}
	if sum.Reason != ExitReasonCancelled {
		t.Fatalf("exit reason %q, want %q", sum.Reason, ExitReasonCancelled)
	}
}

// TestWatch_SecondInstanceExitsSilently is DESIGN:178: one watcher per PC. A session
// starting while the watcher already runs is the NORMAL case, so the second instance
// must exit 0 with nothing on stdout - anything louder would print on every session start.
func TestWatch_SecondInstanceExitsSilently(t *testing.T) {
	sb := testutil.NewSandbox(t)
	name := `Local\ams-store-watch-test-` + t.Name()
	held, ok, err := lock.AcquireSingleton(lock.SingletonOptions{
		Name: name,
		Path: filepath.Join(sb.StateRoot, lock.WatchFileName),
	})
	if err != nil || !ok {
		t.Fatalf("could not hold the singleton first: ok=%v err=%v", ok, err)
	}
	defer held.Release()

	sum, err := Watch(context.Background(), WatchOptions{
		Options: Options{Roots: store.Roots{
			ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot,
		}},
		SingletonName: name,
	})
	if err != nil {
		t.Fatalf("the second watcher errored: %v", err)
	}
	if sum.Started {
		t.Fatal("two watchers ran at once")
	}
	if sum.Reason != "already-running" {
		t.Fatalf("reason %q, want already-running", sum.Reason)
	}
}

// TestWatch_EndToEndExitsWithNoSession drives the real Watch - real fsnotify, real
// filesystem - and asserts it exits immediately on a PC where nobody is working.
func TestWatch_EndToEndExitsWithNoSession(t *testing.T) {
	sb := testutil.NewSandbox(t)
	sum, err := Watch(context.Background(), WatchOptions{
		Options: Options{Roots: store.Roots{
			ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot,
		}},
		SingletonName:  `Local\ams-store-watch-test-` + t.Name(),
		LivenessWithin: live.DefaultWithin,
	})
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	if !sum.Started {
		t.Fatal("the watcher did not start")
	}
	if sum.Reason != ExitReasonNoSession {
		t.Fatalf("reason %q, want %q", sum.Reason, ExitReasonNoSession)
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
