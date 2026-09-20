package sync

import (
	"context"
	"io"
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

// TestWatch_PassTakesThePerPCLockWithAFreshClock is P5-10 (2026-09-19): a watcher pass
// used to run Once with no lock and with the watcher's start time as its clock, so a
// hook-driven sync could merge and queue deletions in the middle of the pass's stage, and
// every receipt of a long-lived watcher carried one identical timestamp. A pass now asks
// for the lock with a clock read for THAT pass, runs nothing when refused, and stamps its
// receipt with the same instant the lock recorded.
func TestWatch_PassTakesThePerPCLockWithAFreshClock(t *testing.T) {
	sb, _, opt := pcFixture(t, "ws", map[string]string{"a.md": "---\nname: a\n---\n\nbody\n"})
	var nows []time.Time
	held := true
	acquire := func(now time.Time) (func(), bool, error) {
		nows = append(nows, now)
		if held {
			return nil, false, nil
		}
		return func() {}, true, nil
	}
	tick := time.Date(2026, 9, 19, 17, 52, 8, 0, time.UTC)
	clock := func() time.Time { tick = tick.Add(time.Second); return tick }
	run := watchPass(opt, acquire, clock, io.Discard)

	res := run(context.Background())
	if !res.LockHeld || res.ExitCode != exitLocked || res.Err != nil {
		t.Fatalf("held lock: LockHeld=%v exit=%d err=%v, want a skipped pass with exit 4", res.LockHeld, res.ExitCode, res.Err)
	}
	if _, err := os.Stat(filepath.Join(sb.StateRoot, "sync-receipts.jsonl")); !os.IsNotExist(err) {
		t.Fatal("a pass that did not run must write no receipt")
	}

	held = false
	res = run(context.Background())
	if res.LockHeld || res.Err != nil {
		t.Fatalf("free lock: LockHeld=%v err=%v, want the pass to run", res.LockHeld, res.Err)
	}
	if len(nows) != 2 || !nows[1].After(nows[0]) {
		t.Fatalf("each pass must read its own clock; the lock saw %v", nows)
	}
	if !res.Receipt.TS.Equal(nows[1]) {
		t.Fatalf("the receipt is stamped %v but the lock was taken at %v: one clock per pass", res.Receipt.TS, nows[1])
	}
}

// TestWatch_HeldPassIsRetriedWhileTheMarkerStands: a refused pass leaves the dirty marker
// where it was and nothing else would wake the loop for it, so the loop arms a short
// retry instead of waiting for the next remote check.
func TestWatch_HeldPassIsRetriedWhileTheMarkerStands(t *testing.T) {
	start := time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)
	c := &fakeClock{t: start}
	events := make(chan struct{}, 1)
	events <- struct{}{}
	calls := 0
	d := baseDeps(c)
	d.events = events
	d.isDirty = func() bool { return true }
	d.runSync = func(context.Context) Result {
		calls++
		if calls == 1 {
			return Result{ExitCode: exitLocked, LockHeld: true}
		}
		return Result{}
	}
	d.anyLive = func() bool { return calls < 2 }
	// The loop arms ONE timer per iteration with the shortest pending wait. Only the retry
	// wait may fire here: the idle and remote waits get a channel that never does, so the
	// first iteration is decided by the event alone and the second by the retry timer.
	var waits []time.Duration
	d.after = func(w time.Duration) <-chan time.Time {
		waits = append(waits, w)
		if w == RetryHeldAfter {
			return c.after(w)
		}
		return make(chan time.Time)
	}

	sum, err := runWatch(context.Background(), WatchOptions{
		IdleTimeout: 10 * time.Minute, RemoteCheckEvery: 5 * time.Minute,
	}, d)
	if err != nil {
		t.Fatalf("runWatch: %v", err)
	}
	if sum.Skipped != 1 || sum.Syncs != 1 {
		t.Fatalf("skipped=%d syncs=%d, want exactly one skipped pass and one that ran", sum.Skipped, sum.Syncs)
	}
	if got := c.now().Sub(start); got != RetryHeldAfter {
		t.Fatalf("the retry fired %v after the refusal, want %v (timer waits asked: %v)", got, RetryHeldAfter, waits)
	}
}

// TestWatch_EndToEndPassGoesThroughThePerPCLock pins the wiring: the real Watch, a live
// session and a dirty marker, and the pass must ask the per-PC lock before doing
// anything. With the lock refused, no pass runs.
func TestWatch_EndToEndPassGoesThroughThePerPCLock(t *testing.T) {
	sb, _, opt := pcFixture(t, "ws", map[string]string{"a.md": "---\nname: a\n---\n\nbody\n"})
	if err := os.WriteFile(filepath.Join(sb.ProjectsRoot, "ws", "session.jsonl"), []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var mu sync.Mutex
	asked := 0
	wo := WatchOptions{
		Options: opt, IdleTimeout: 5 * time.Second, RemoteCheckEvery: time.Hour,
		SingletonName:  `Local\ams-store-watch-test-` + t.Name(),
		LivenessWithin: live.DefaultWithin,
	}
	wo.AcquirePassLock = func(now time.Time) (func(), bool, error) {
		mu.Lock()
		asked++
		mu.Unlock()
		cancel()
		return nil, false, nil
	}
	go func() {
		time.Sleep(300 * time.Millisecond)
		_ = MarkDirty(sb.StateRoot)
	}()
	sum, err := Watch(ctx, wo)
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if asked == 0 {
		t.Fatalf("the watcher ran a pass without asking for the per-PC lock (reason %q)", sum.Reason)
	}
	if sum.Syncs != 0 || sum.Skipped == 0 {
		t.Fatalf("syncs=%d skipped=%d: a refused lock must skip the pass", sum.Syncs, sum.Skipped)
	}
}
