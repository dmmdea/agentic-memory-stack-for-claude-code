package merge

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
)

// DeferredFile is the per-workspace queue file name under <STATE_ROOT>/<workspace>/.
const DeferredFile = "deferred.json"

// DeferredEntry is one change materialize would not apply under a live session.
type DeferredEntry struct {
	Path     string `json:"path"`
	Op       Op     `json:"op"`
	Blob     string `json:"blob,omitempty"`
	QueuedAt string `json:"queued_at"`
}

// Deferred is the queue: the merged tree it came from plus the entries still to apply.
type Deferred struct {
	Tree    string          `json:"tree"`
	Entries []DeferredEntry `json:"entries"`
}

// DeferredPath is the queue file for one workspace.
func DeferredPath(stateRoot, workspace string) string {
	return filepath.Join(stateRoot, workspace, DeferredFile)
}

// LoadDeferred reads the queue. A missing file is an empty queue; a file that exists but
// does not parse is an ERROR, because "absent" and "corrupt" must not collapse into the
// same answer - a corrupt queue silently read as empty drops every change a live session
// was protecting.
func LoadDeferred(stateRoot, workspace string) (Deferred, error) {
	var d Deferred
	found, err := atomic.ReadJSONFile(DeferredPath(stateRoot, workspace), &d)
	if err != nil {
		return Deferred{}, err
	}
	if !found {
		return Deferred{}, nil
	}
	return d, nil
}

// DeferredPaths is the queue as a pathspec-ready list: every path whose materialization
// is still pending for a workspace.
//
// It is what the staging pass must EXCLUDE. The error is never swallowed into an empty
// list by design - "I could not read the queue" and "nothing is queued" lead to opposite
// actions, and collapsing them is how a withheld deletion gets re-committed.
func DeferredPaths(stateRoot, workspace string) ([]string, error) {
	d, err := LoadDeferred(stateRoot, workspace)
	if err != nil {
		return nil, err
	}
	out := make([]string, 0, len(d.Entries))
	for _, e := range d.Entries {
		out = append(out, e.Path)
	}
	return out, nil
}

// SaveDeferred writes the queue atomically.
func SaveDeferred(stateRoot, workspace string, d Deferred) error {
	p := DeferredPath(stateRoot, workspace)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return fmt.Errorf("workspace state dir: %w", err)
	}
	return atomic.WriteJSONFile(p, d)
}

// queueDeferred appends entries, replacing any earlier entry for the same path: the
// newest merge is the one that should eventually land.
func queueDeferred(stateRoot, workspace, tree string, entries []DeferredEntry) error {
	if len(entries) == 0 {
		return nil
	}
	existing, err := LoadDeferred(stateRoot, workspace)
	if err != nil {
		return err
	}
	byPath := map[string]bool{}
	for _, e := range entries {
		byPath[e.Path] = true
	}
	merged := make([]DeferredEntry, 0, len(existing.Entries)+len(entries))
	for _, e := range existing.Entries {
		if byPath[e.Path] {
			continue
		}
		merged = append(merged, e)
	}
	merged = append(merged, entries...)
	return SaveDeferred(stateRoot, workspace, Deferred{Tree: tree, Entries: merged})
}

// ApplyDeferred drains a workspace's queue, re-checking the live/mtime condition for
// every entry. It is called at SessionEnd and at the next SessionStart.
//
// An entry that is still blocked STAYS QUEUED. Dropping it would silently discard the
// judge's deletion or another PC's edit, which is the failure the queue exists to
// prevent in the first place.
func (e *Engine) ApplyDeferred(ctx context.Context, workspace string, mo MaterializeOptions) (applied, stillQueued []string, err error) {
	d, err := LoadDeferred(mo.StateRoot, workspace)
	if err != nil {
		return nil, nil, err
	}
	if len(d.Entries) == 0 {
		return nil, nil, nil
	}
	live, sessionStart := mo.Live.Probe(workspace)

	var keep []DeferredEntry
	var deletions []DeferredEntry
	for _, ent := range d.Entries {
		if blocked(ent, live, sessionStart, mo.workTreePath(e, ent.Path)) {
			keep = append(keep, ent)
			stillQueued = append(stillQueued, ent.Path)
			continue
		}
		if ent.Op == OpDelete {
			deletions = append(deletions, ent)
			continue
		}
		data, readErr := gitx.CatBlob(ctx, e.opt(), ent.Blob)
		if readErr != nil {
			return applied, stillQueued, readErr
		}
		if writeErr := e.writeWorkTree(ent.Path, data, mo); writeErr != nil {
			return applied, stillQueued, writeErr
		}
		applied = append(applied, ent.Path)
	}
	// Deletions last, mirroring materialize: a reader that catches the store mid-apply
	// sees a superset of the fact set, never a pointer to a file already gone.
	for _, ent := range deletions {
		full := filepath.Join(e.WorkTree, filepath.FromSlash(ent.Path))
		if rmErr := os.Remove(full); rmErr != nil && !os.IsNotExist(rmErr) {
			return applied, stillQueued, fmt.Errorf("apply deferred deletion %s: %w", ent.Path, rmErr)
		}
		applied = append(applied, ent.Path)
	}

	if err := SaveDeferred(mo.StateRoot, workspace, Deferred{Tree: d.Tree, Entries: keep}); err != nil {
		return applied, stillQueued, err
	}
	if len(applied) > 0 && mo.Derive != nil {
		if err := mo.Derive(workspace); err != nil {
			return applied, stillQueued, err
		}
	}
	return applied, stillQueued, nil
}

// blocked re-applies the section 4.8 guard to one entry.
func blocked(ent DeferredEntry, live bool, sessionStart time.Time, fullPath string) bool {
	if !live {
		return false
	}
	if ent.Op == OpDelete {
		// No deletion is materialized while a session is live in the workspace - not
		// only the files it touched. A fact file vanishing under a running session is
		// the one change it cannot recover from.
		return true
	}
	fi, err := os.Stat(fullPath)
	if err != nil {
		// The file is not there, so nothing of the session's can be overwritten.
		return false
	}
	return !fi.ModTime().Before(sessionStart)
}
