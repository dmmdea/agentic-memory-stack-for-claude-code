package sync

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/live"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// The cooperative stop (1.31.3 review): the installer writes <state root>/watch.stop before it
// swaps the exe, and the watcher exits BETWEEN passes. A TerminateProcess skips gitx's
// killTree, so a git child mid-commit on history.git could be orphaned holding index.lock.

func TestWatch_StopRequestEndsTheLoopOnlyAfterTheRunningPass(t *testing.T) {
	c := &fakeClock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	d := baseDeps(c)
	ev := make(chan struct{}, 1)
	stop := make(chan struct{}, 1)
	d.events = ev
	d.stop = stop
	d.isDirty = func() bool { return true }
	finished := false
	d.runSync = func(context.Context) Result {
		stop <- struct{}{} // the stop request lands while this pass is running
		time.Sleep(20 * time.Millisecond)
		finished = true
		return Result{}
	}
	d.after = func(time.Duration) <-chan time.Time { return make(chan time.Time) } // never fires
	ev <- struct{}{}
	type out struct {
		sum WatchSummary
		err error
	}
	ch := make(chan out, 1)
	go func() {
		s, e := runWatch(context.Background(), WatchOptions{}, d)
		ch <- out{s, e}
	}()
	var sum WatchSummary
	var err error
	select {
	case o := <-ch:
		sum, err = o.sum, o.err
	case <-time.After(5 * time.Second):
		t.Fatal("the watcher did not honour the stop request")
	}
	if err != nil {
		t.Fatalf("runWatch: %v", err)
	}
	if !finished || sum.Syncs != 1 {
		t.Fatalf("the running pass must complete before the stop is honoured: finished=%v %+v", finished, sum)
	}
	if sum.Reason != ExitReasonStopRequested {
		t.Fatalf("reason %q, want %q", sum.Reason, ExitReasonStopRequested)
	}
}

func TestWatch_EndToEndStopFileEndsTheWatcher(t *testing.T) {
	sb := testutil.NewSandbox(t)
	// a live session, so the watcher stays resident
	ws := filepath.Join(sb.ProjectsRoot, "-ws")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ws, "session.jsonl"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	// a leftover request from an earlier install must not stop a fresh watcher at once
	stopPath := filepath.Join(sb.StateRoot, StopFile)
	if err := os.MkdirAll(sb.StateRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(stopPath, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	type out struct {
		sum WatchSummary
		err error
	}
	ch := make(chan out, 1)
	go func() {
		s, e := Watch(ctx, WatchOptions{
			Options:        Options{Roots: store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot}},
			SingletonName:  `Local\ams-store-watch-test-` + t.Name(),
			LivenessWithin: live.DefaultWithin,
		})
		ch <- out{s, e}
	}()
	time.Sleep(500 * time.Millisecond)
	select {
	case o := <-ch:
		t.Fatalf("the watcher exited before any stop was requested: %+v %v", o.sum, o.err)
	default:
	}
	if _, err := os.Stat(stopPath); !os.IsNotExist(err) {
		t.Fatalf("a leftover stop file must be cleared at start (stat err=%v)", err)
	}
	if err := os.WriteFile(stopPath, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	select {
	case o := <-ch:
		if o.err != nil || o.sum.Reason != ExitReasonStopRequested {
			t.Fatalf("want a clean stop-requested exit, got %+v %v", o.sum, o.err)
		}
	case <-ctx.Done():
		t.Fatal("the watcher did not honour the stop file")
	}
}
