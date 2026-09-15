package judge

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// ReceiptFileName is the shared receipts ledger (COMPACT:78). Both watchdogs read this
// file's mtime, so an append failure is reported loudly rather than swallowed.
const ReceiptFileName = "compact-receipts.jsonl"

// UsageFileName is the judge's own usage ledger.
const UsageFileName = "judge-usage.jsonl"

// JudgeWindowHours is how long one judge ATTEMPT per store holds off the next
// (COMPACT:80-82). Twenty, not twenty-four, so a nightly that runs a few minutes earlier
// than yesterday's does not find yesterday's attempt still inside "today".
//
// The window is computed from RECEIPTS, never from a timer or a stamp file: a receipt is
// the only record that survives a reboot, a crash mid-run and a machine that was asleep
// at 05:00.
const JudgeWindowHours = 20

// Receipt is one store's row for one run. The field set is the port of COMPACT:331-338;
// fields this verb cannot produce (hygiene counters) are kept so the ledger stays one
// shape across the PowerShell compactor and this binary during the Phase 4 overlap.
type Receipt struct {
	TS          string   `json:"ts"`
	Workspace   string   `json:"workspace"`
	DryRun      bool     `json:"dry_run"`
	BeforeBytes int      `json:"before_bytes"`
	BeforeLines int      `json:"before_lines"`
	Status      string   `json:"status"`
	Shortened   int      `json:"shortened"`
	Migrated    int      `json:"migrated"`
	LineFloored int      `json:"line_floored"`
	Mem0        []string `json:"mem0"`
	Mem0Orphan  []string `json:"mem0_orphan"`
	AfterBytes  *int     `json:"after_bytes"`
	AfterLines  *int     `json:"after_lines"`
	Commit      string   `json:"commit,omitempty"`
	Note        string   `json:"note"`
	JudgeCalled bool     `json:"judge_called"`
}

// UsageRow is one row of the judge's usage ledger.
type UsageRow struct {
	TS        string `json:"ts"`
	Component string `json:"component"`
	Workspace string `json:"workspace"`
	Status    string `json:"status"`
	Outcome   string `json:"outcome"`
}

// UsageComponent names this verb in the usage ledger.
const UsageComponent = "judge-apply"

// ReceiptPath is the receipts ledger under a state root.
func ReceiptPath(stateRoot string) string { return filepath.Join(stateRoot, ReceiptFileName) }

// UsagePath is the usage ledger under a state root.
func UsagePath(stateRoot string) string { return filepath.Join(stateRoot, UsageFileName) }

// AppendJSONL appends one JSON document as a line. The write is O_APPEND so two
// processes cannot interleave a partial line, and it is UTF-8 without a BOM.
func AppendJSONL(path string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("encode row for %s: %w", path, err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create %s: %w", filepath.Dir(path), err)
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()
	if _, err := f.Write(append(b, '\n')); err != nil {
		return fmt.Errorf("append to %s: %w", path, err)
	}
	return nil
}

// WriteReceipt appends a receipt row.
func WriteReceipt(stateRoot string, r Receipt) error { return AppendJSONL(ReceiptPath(stateRoot), r) }

// WriteUsage appends a usage row. A judge call that succeeded and returned nothing is an
// OUTCOME, not a non-event: the row is what makes it countable.
func WriteUsage(stateRoot string, row UsageRow) error { return AppendJSONL(UsagePath(stateRoot), row) }

// readTail returns the last n lines of a file. A missing file is no lines and no error;
// an unreadable one is an error, because "I could not read the ledger" must never be
// spelled the same way as "the judge has never run".
func readTail(path string, n int) ([]string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	lines := strings.Split(strings.ReplaceAll(string(b), "\r\n", "\n"), "\n")
	out := make([]string, 0, len(lines))
	for _, l := range lines {
		if strings.TrimSpace(l) != "" {
			out = append(out, l)
		}
	}
	if len(out) > n {
		out = out[len(out)-n:]
	}
	return out, nil
}

// LastJudgeAttempt is the newest receipt for a workspace whose judge_called is true.
//
// It is an ATTEMPT, not an outcome: a rejected plan, an unavailable judge and a clean
// apply all count, which is the whole point - 32 judge calls on one store in a day, every
// result rejected, is what the window exists to stop.
//
// Dry runs are excluded (LIB:522-528): a rehearsal must not consume the night's attempt.
// A malformed line is skipped rather than fatal - the ledger is append-only from several
// producers and one torn line must not disarm the window.
func LastJudgeAttempt(stateRoot, workspace string) (time.Time, bool, error) {
	lines, err := readTail(ReceiptPath(stateRoot), store.ReceiptTailLines)
	if err != nil {
		return time.Time{}, false, err
	}
	var newest time.Time
	found := false
	for _, l := range lines {
		var r Receipt
		if json.Unmarshal([]byte(l), &r) != nil {
			continue
		}
		if r.Workspace != workspace || r.DryRun || !r.JudgeCalled {
			continue
		}
		ts, err := time.Parse(time.RFC3339, r.TS)
		if err != nil {
			continue
		}
		ts = ts.UTC()
		if !found || ts.After(newest) {
			newest, found = ts, true
		}
	}
	return newest, found, nil
}

// WithinJudgeWindow reports whether a judge attempt for this store is still inside the
// 20 h window, and the instant of that attempt.
func WithinJudgeWindow(stateRoot, workspace string, now time.Time) (time.Time, bool, error) {
	last, found, err := LastJudgeAttempt(stateRoot, workspace)
	if err != nil || !found {
		return time.Time{}, false, err
	}
	return last, last.After(now.UTC().Add(-JudgeWindowHours * time.Hour)), nil
}
