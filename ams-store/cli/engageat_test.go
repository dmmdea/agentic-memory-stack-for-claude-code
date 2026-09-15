package cli_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/cli"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// Decision Q2: the convergence floor has TWO thresholds. It ENGAGES at the sync limit
// and STOPS below the trigger - the legacy hysteresis - and Phase 4 lowers the engage
// threshold to the trigger once the zero-hooks-lost check has run.
//
// Only `--stop-below` was reachable from the command line, so that flip would have been
// an edit to floor.go rather than a decision anyone could rehearse, measure or roll back
// on one PC. `--engage-at` makes it a flag on the two verbs that run the floor, and the
// Phase 4 change becomes a change of this flag's default.
//
// The window between the trigger and the sync limit is where the whole decision lives:
// an index in it is untouched today and floored after the flip. Both tests below build a
// store in that window and assert the default leaves it alone while the flag reaches the
// floor.

// engageWindowStore builds a store whose index sits strictly between the trigger and the
// sync limit, and fails loudly if the fixture drifts out of that window - outside it
// neither assertion below would mean anything.
func engageWindowStore(t *testing.T, sb *testutil.Sandbox, ws string) (dir, indexPath string, size int) {
	t.Helper()
	dir = sb.AddStore(ws, testutil.BigIndex(60), testutil.BigIndexFacts(60))
	indexPath = filepath.Join(dir, store.IndexName)
	raw, err := os.ReadFile(indexPath)
	if err != nil {
		t.Fatalf("read the fixture index: %v", err)
	}
	size = len(raw)
	if size <= store.TriggerBytes || size >= store.SyncLimitBytes {
		t.Fatalf("the fixture index is %d B; this test is only about the window between the trigger (%d B)"+
			" and the sync limit (%d B)", size, store.TriggerBytes, store.SyncLimitBytes)
	}
	return dir, indexPath, size
}

func derivedFloored(t *testing.T, out string) float64 {
	t.Helper()
	var report struct {
		Stores []struct {
			Floored int `json:"floored"`
		} `json:"stores"`
	}
	if err := json.Unmarshal([]byte(out), &report); err != nil {
		t.Fatalf("derive --json did not produce a report: %v (stdout %q)", err, out)
	}
	if len(report.Stores) != 1 {
		t.Fatalf("derive reported %d stores, want 1 (stdout %q)", len(report.Stores), out)
	}
	return float64(report.Stores[0].Floored)
}

func TestCLI_DeriveEngageAtReachesTheFloor(t *testing.T) {
	sb := testutil.NewSandbox(t)
	dir, _, size := engageWindowStore(t, sb, "ws")

	code, out, errOut := runIn(t, sb, "", "derive", "--store", dir, "--dry-run", "--json")
	if code != cli.ExitOK {
		t.Fatalf("derive at the default engage threshold: exit = %d (%s)", code, errOut)
	}
	if n := derivedFloored(t, out); n != 0 {
		t.Fatalf("the default floored %v lines on a %d B index; the legacy hysteresis engages at the sync"+
			" limit (%d B) and must leave this window alone", n, size, store.SyncLimitBytes)
	}

	code, out, errOut = runIn(t, sb, "", "derive", "--store", dir, "--dry-run", "--json",
		"--engage-at", strconv.Itoa(store.TriggerBytes))
	if code != cli.ExitOK {
		t.Fatalf("derive --engage-at %d: exit = %d (%s)", store.TriggerBytes, code, errOut)
	}
	if n := derivedFloored(t, out); n == 0 {
		t.Fatalf("--engage-at %d never reached the floor: a %d B index was left untouched, so the Phase 4"+
			" flip is still an edit to floor.go rather than a flag", store.TriggerBytes, size)
	}
}

func TestCLI_GateEngageAtReachesTheFloor(t *testing.T) {
	sb := testutil.NewSandbox(t)
	_, indexPath, size := engageWindowStore(t, sb, "ws")
	before, err := os.ReadFile(indexPath)
	if err != nil {
		t.Fatal(err)
	}

	if code, _, _ := runIn(t, sb, hookPayload(indexPath), "gate"); code != cli.ExitOK {
		t.Fatalf("gate exit = %d, want 0", code)
	}
	after, err := os.ReadFile(indexPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(after) != len(before) {
		t.Fatalf("the gate rewrote a %d B index at the default engage threshold; below the sync limit"+
			" (%d B) it advises and never mutates", size, store.SyncLimitBytes)
	}

	if code, _, _ := runIn(t, sb, hookPayload(indexPath), "gate",
		"--engage-at", strconv.Itoa(store.TriggerBytes)); code != cli.ExitOK {
		t.Fatalf("gate --engage-at exit = %d, want 0", code)
	}
	after, err = os.ReadFile(indexPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(after) >= len(before) {
		t.Fatalf("--engage-at %d never reached the gate's floor: the index is still %d B (was %d B)",
			store.TriggerBytes, len(after), len(before))
	}
	if len(after) >= store.TriggerBytes {
		t.Fatalf("the gate floored to %d B; the stop threshold is still the trigger (%d B), and only the"+
			" ENGAGE threshold moved", len(after), store.TriggerBytes)
	}
}
