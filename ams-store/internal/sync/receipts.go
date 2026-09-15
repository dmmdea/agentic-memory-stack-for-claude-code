package sync

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// ReceiptFile is the sync receipts JSONL under STATE_ROOT.
const ReceiptFile = "sync-receipts.jsonl"

// DirtyMarker is the file the gate touches and the watcher watches for.
const DirtyMarker = "dirty"

// Status values a sync receipt can carry.
const (
	StatusPushed    = "pushed"      // fetched, merged, pushed
	StatusLocal     = "local-only"  // committed locally; no hub configured
	StatusUpToDate  = "up-to-date"  // nothing to push and nothing new
	StatusOffline   = "hub-offline" // the hub could not be reached
	StatusExhausted = "push-loop-exhausted"
	StatusRefused   = "refused" // the remote policy said no
	StatusConflict  = "conflict-in-history"
)

// ConflictRef names a body conflict's losing commit, so the loser is recoverable.
type ConflictRef struct {
	Path   string `json:"path"`
	Commit string `json:"commit"`
	Detail string `json:"detail,omitempty"`
}

// Receipt is one row of sync-receipts.jsonl.
//
// The receipt is the audit trail a human reads at 9am to answer "what did the fleet do
// last night". It records the DECISION as well as the outcome: attempts, whether a push
// happened, which files a modify/delete rule resurrected and which body conflicts left a
// loser in history. A receipt that only said "ok" would be worth nothing.
type Receipt struct {
	TS          time.Time `json:"ts"`
	Machine     string    `json:"machine"`
	Host        string    `json:"host,omitempty"`
	Kind        string    `json:"kind"` // "once" or "watch"
	Status      string    `json:"status"`
	Stores      int       `json:"stores"`
	Attempts    int       `json:"attempts"`
	Pushed      bool      `json:"pushed"`
	Offline     bool      `json:"offline"`
	LocalCommit string    `json:"local_commit,omitempty"`
	MergeCommit string    `json:"merge_commit,omitempty"`
	// Resurrected lists paths the modify/delete rule kept. Actionable: a deliberate
	// deletion that came back is something a human decides about, not the tool.
	Resurrected []string `json:"resurrected,omitempty"`
	// ConflictsInHistory carries one entry per real body conflict.
	ConflictsInHistory []ConflictRef `json:"conflict_in_history,omitempty"`
	// Deferred lists paths materialization queued because a session was live.
	Deferred []string `json:"deferred,omitempty"`
	// DeferredApplied lists paths a previously queued change landed on in this pass. It
	// is the other end of Deferred: without it the audit trail shows changes going into
	// the queue and nothing ever coming out.
	DeferredApplied []string `json:"deferred_applied,omitempty"`
	// Removed lists workspaces whose whole directory is gone and whose tracked files
	// this pass staged for deletion. It is a receipt field rather than a log line
	// because a store leaving the fleet is the kind of change a human reads back later.
	Removed []string `json:"removed,omitempty"`
	Note    string   `json:"note,omitempty"`
	Version string   `json:"ams_store_version,omitempty"`
}

// ReceiptPath is the receipts file under a state root.
func ReceiptPath(stateRoot string) string { return filepath.Join(stateRoot, ReceiptFile) }

// AppendReceipt appends one row.
//
// Appending is deliberately not routed through the atomic writer: a JSONL row is
// append-only and a temp-and-swap would rewrite the whole file on every sync. A failure
// here is logged, never fatal - losing the audit line is bad, losing the sync because
// the audit line could not be written is worse.
func AppendReceipt(stateRoot string, r Receipt) error {
	if err := os.MkdirAll(stateRoot, 0o755); err != nil {
		return fmt.Errorf("sync: create state root: %w", err)
	}
	b, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("sync: encode receipt: %w", err)
	}
	f, err := os.OpenFile(ReceiptPath(stateRoot), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return fmt.Errorf("sync: open receipts: %w", err)
	}
	defer f.Close()
	if _, err := f.Write(append(b, '\n')); err != nil {
		return fmt.Errorf("sync: append receipt: %w", err)
	}
	return nil
}

// ReadReceipts reads the last `tail` rows of a receipts file. A missing file is not an
// error: a fleet that has never synced has no receipts, which is different from a fleet
// whose receipts cannot be read.
func ReadReceipts(path string, tail int) ([]Receipt, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("sync: read receipts %s: %w", path, err)
	}
	defer f.Close()

	var lines []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		l := strings.TrimSpace(sc.Text())
		if l == "" {
			continue
		}
		lines = append(lines, l)
		if tail > 0 && len(lines) > tail {
			lines = lines[1:]
		}
	}
	if err := sc.Err(); err != nil {
		return nil, fmt.Errorf("sync: scan receipts %s: %w", path, err)
	}
	out := make([]Receipt, 0, len(lines))
	for _, l := range lines {
		var r Receipt
		// A row that does not parse is skipped, not fatal: one torn append must not
		// blind every reader to the rest of the history.
		if err := json.Unmarshal([]byte(l), &r); err != nil {
			continue
		}
		out = append(out, r)
	}
	return out, nil
}

// MarkDirty touches the dirty marker the watcher wakes on.
func MarkDirty(stateRoot string) error {
	if err := os.MkdirAll(stateRoot, 0o755); err != nil {
		return err
	}
	path := filepath.Join(stateRoot, DirtyMarker)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	now := time.Now()
	// The content does not matter; the mtime is the signal. Writing a byte guarantees
	// the mtime moves even on a filesystem with coarse timestamps.
	if _, err := f.WriteString("1"); err != nil {
		return err
	}
	f.Close()
	return os.Chtimes(path, now, now)
}

// ClearDirty removes the dirty marker. Absent is success.
func ClearDirty(stateRoot string) error {
	err := os.Remove(filepath.Join(stateRoot, DirtyMarker))
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// IsDirty reports whether the marker is present.
func IsDirty(stateRoot string) bool {
	_, err := os.Stat(filepath.Join(stateRoot, DirtyMarker))
	return err == nil
}
