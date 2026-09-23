package cli

import (
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// The transitional bare-name probe applies to the operator's DEFAULT store only - the one a
// pre-1.31.3 watcher served. A scratch or test state root never probes the real name.
func TestLegacyWatchName_OnlyForTheDefaultStateRoot(t *testing.T) {
	def, err := store.DefaultRoots()
	if err != nil {
		t.Fatal(err)
	}
	if got := legacyWatchNameFor(def.StateRoot); got != lock.WatchMutexName {
		t.Fatalf("default root: got %q, want the bare %q", got, lock.WatchMutexName)
	}
	if got := legacyWatchNameFor(t.TempDir()); got != "" {
		t.Fatalf("scratch root must not probe the bare name, got %q", got)
	}
}
