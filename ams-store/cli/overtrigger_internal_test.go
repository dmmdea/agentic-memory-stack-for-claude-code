package cli

import (
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lint"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// The G7 clock trips on bytes OR lines, exactly as lint and the compactor do. Testing bytes
// alone left a store at 176 lines and 19 KB - past the line trigger, under the byte one -
// with no clock at all, so the 24 h alarm could never see it.
func TestRecordOverTrigger_TripsOnLinesAsWellAsBytes(t *testing.T) {
	now := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name         string
		bytes, lines int
		wantStamped  bool
	}{
		{"under both", store.TriggerBytes - 1, store.TriggerLines - 1, false},
		{"over the byte trigger only", store.TriggerBytes, 10, true},
		{"over the line trigger only", 5000, store.TriggerLines, true},
		{"over both", store.TriggerBytes + 1, store.TriggerLines + 1, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			roots := store.Roots{ProjectsRoot: t.TempDir(), StateRoot: t.TempDir()}
			recordOverTrigger(roots, "ws", tc.bytes, tc.lines, false, now, nil)
			stamps, err := lint.ReadStamps(roots.ProjectsRoot)
			if err != nil {
				t.Fatal(err)
			}
			if got := stamps.IsLive("ws"); got != tc.wantStamped {
				t.Errorf("stamped = %v, want %v (bytes %d, lines %d)", got, tc.wantStamped, tc.bytes, tc.lines)
			}
		})
	}
}

func TestRecordOverTrigger_ADryRunNeverStamps(t *testing.T) {
	roots := store.Roots{ProjectsRoot: t.TempDir(), StateRoot: t.TempDir()}
	recordOverTrigger(roots, "ws", 30000, 300, true, time.Now(), nil)
	if stamps, err := lint.ReadStamps(roots.ProjectsRoot); err != nil || stamps.IsLive("ws") {
		t.Errorf("a dry run started the clock: %+v %v", stamps, err)
	}
}
