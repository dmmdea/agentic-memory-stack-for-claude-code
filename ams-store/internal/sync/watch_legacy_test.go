package sync

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/live"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// 1.31.3 upgrade window: the installer renames the running exe aside, and a pre-1.31.3
// watcher keeps running from the .prev image on the BARE Local\ams-store-watch. The new
// watcher takes the SCOPED name, so without this check two watchers ran on one store until
// reboot. The legacy name here is a per-test name standing in for the bare one: the check
// only ever OPENS it (it never creates or takes it), and tests never name the real one.

func TestWatch_RefusesWhileALegacyWatcherHoldsTheBareName(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("named mutexes exist on Windows only")
	}
	sb := testutil.NewSandbox(t)
	legacy := `Local\ams-store-watch-legacy-test-` + t.Name()
	old, ok, err := lock.AcquireSingleton(lock.SingletonOptions{Name: legacy})
	if err != nil || !ok {
		t.Fatalf("fake old watcher: ok=%v err=%v", ok, err)
	}
	defer old.Release()

	sum, err := Watch(context.Background(), WatchOptions{
		Options:         Options{Roots: store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot}},
		SingletonName:   `Local\ams-store-watch-test-` + t.Name(),
		LegacyWatchName: legacy,
		LivenessWithin:  live.DefaultWithin,
	})
	if !errors.Is(err, ErrLegacyWatcher) {
		t.Fatalf("want ErrLegacyWatcher while an old watcher is alive, got %v", err)
	}
	if sum.Started || sum.Reason != ExitReasonLegacyWatcher {
		t.Fatalf("summary %+v: the new watcher must not start", sum)
	}
	b, rErr := os.ReadFile(filepath.Join(sb.StateRoot, LegacyWatcherLog))
	if rErr != nil || !strings.Contains(string(b), "REFUSED") {
		t.Fatalf("the refusal must be logged in the state root: %q %v", b, rErr)
	}
}

func TestWatch_StartsWhenNoLegacyWatcherIsAlive(t *testing.T) {
	sb := testutil.NewSandbox(t)
	sum, err := Watch(context.Background(), WatchOptions{
		Options:         Options{Roots: store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot}},
		SingletonName:   `Local\ams-store-watch-test-` + t.Name(),
		LegacyWatchName: `Local\ams-store-watch-legacy-test-` + t.Name(),
		LivenessWithin:  live.DefaultWithin,
	})
	if err != nil || !sum.Started {
		t.Fatalf("no old watcher: the new one must start (err=%v, %+v)", err, sum)
	}
	// the probe must not have created the name it checks
	if held, _ := lock.MutexExists(`Local\ams-store-watch-legacy-test-` + t.Name()); held {
		t.Fatal("the legacy probe created the bare name it only reads")
	}
}
