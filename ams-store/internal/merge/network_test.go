package merge_test

// The merge engine's two network methods, against the guard in gitx.
//
// Engine.Fetch and Engine.Push used to build their git calls from plain options with no
// GIT_SSH_COMMAND on them, while sync and the watcher built their own hardened ones a few
// files away. Nothing could tell the two apart: both compiled, both ran, and the
// difference only showed up as a prompt on a changed host key or a minute-long dial on an
// unreachable hub. These tests pin the two halves of the repair - the engine carries the
// hardened environment, and an engine that cannot build one refuses instead of dialling.

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
)

func TestMerge_FetchAndPushRefuseWithoutTheHardenedEnvironment(t *testing.T) {
	f := newFleet(t, "a")
	a := f.pcs["a"]
	ctx := context.Background()

	// The same engine, minus the one field the hardened environment is built from.
	bare := &merge.Engine{GitDir: a.gitDir, WorkTree: a.projects, MachineID: a.machine}

	err := bare.Fetch(ctx, hubRemote)
	if err == nil {
		t.Fatal("Fetch with no state root reached the hub: a network call must never run unhardened")
	}
	if !strings.Contains(err.Error(), "StateRoot") {
		t.Fatalf("Fetch failed for the wrong reason: %v", err)
	}
	if _, err := bare.Push(ctx, hubRemote); err == nil {
		t.Fatal("Push with no state root reached the hub: a network call must never run unhardened")
	}
}

// TestMerge_FetchAndPushCarryTheHardenedEnvironment is the other direction: a properly
// built engine is NOT refused, and the proof is that the refusal is not what comes back
// when the remote itself is missing.
func TestMerge_FetchAndPushCarryTheHardenedEnvironment(t *testing.T) {
	f := newFleet(t, "a")
	a := f.pcs["a"]
	ctx := context.Background()

	a.write(ws, "seed.md", fact("Seed", "d", "h", "seed\n"))
	a.commit("seed", ws)

	// The hub is reachable: fetch and push both go through, which they can only do with
	// GIT_SSH_COMMAND on the call.
	if err := a.eng.Fetch(ctx, hubRemote); err != nil {
		t.Fatalf("a hardened fetch was refused: %v", err)
	}
	if res, err := a.eng.Push(ctx, hubRemote); err != nil || !res.OK {
		t.Fatalf("a hardened push failed: %v (%+v)", err, res)
	}

	// And a remote that does not exist fails as GIT's error, not as the guard's.
	err := a.eng.Fetch(ctx, "no-such-remote-configured")
	if err == nil {
		t.Fatal("fetching an undefined remote should have failed")
	}
	if errors.Is(err, gitx.ErrUnhardenedNetwork) {
		t.Fatalf("the engine's own fetch was refused as unhardened: %v", err)
	}
	if !strings.Contains(a.eng.StateRoot, filepath.Base(a.stateDir)) {
		t.Fatalf("the fixture engine does not carry the state root it claims: %q", a.eng.StateRoot)
	}
}
