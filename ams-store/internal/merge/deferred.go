package merge

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
)

// DeferredFile is the per-workspace queue file name under <STATE_ROOT>/<workspace>/.
const DeferredFile = "deferred.json"

// DeferredEntry is one change materialize would not apply under a live session.
//
// Blob is the MERGED blob - what the work tree would have got. OursBlob is what was on
// disk when the entry was queued, and it is what makes the drain a RE-CHECK instead of a
// blind write: if the file still holds those bytes, nothing of the session's is at risk
// and the merged result lands; if it does not, the session edited the file AFTER the
// merge, and the later edit is the one thing the queue exists to protect.
type DeferredEntry struct {
	Path     string `json:"path"`
	Op       Op     `json:"op"`
	Blob     string `json:"blob,omitempty"`
	OursBlob string `json:"ours_blob,omitempty"`
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

// QueuedPaths is the queue as a pathspec-ready list: every path whose materialization is
// still pending for a workspace.
//
// It is what the staging pass must EXCLUDE. The error is never swallowed into an empty
// list by design - "I could not read the queue" and "nothing is queued" lead to opposite
// actions, and collapsing them is how a withheld deletion gets re-committed.
func QueuedPaths(stateRoot, workspace string) ([]string, error) {
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

// AllQueuedPaths is the union of every workspace's queue under a state root, for a caller
// that must not commit any of it. A queue that cannot be read is an error for the same
// reason it is in LoadDeferred: "absent" and "corrupt" must not collapse into "nothing
// pending". A state root that does not exist yet has no queues.
func AllQueuedPaths(stateRoot string) ([]string, error) {
	entries, err := os.ReadDir(stateRoot)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("read the state root for deferred queues: %w", err)
	}
	var out []string
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		if _, sErr := os.Stat(DeferredPath(stateRoot, e.Name())); sErr != nil {
			if os.IsNotExist(sErr) {
				continue
			}
			return nil, fmt.Errorf("stat the deferred queue of %s: %w", e.Name(), sErr)
		}
		paths, qErr := QueuedPaths(stateRoot, e.Name())
		if qErr != nil {
			return nil, qErr
		}
		out = append(out, paths...)
	}
	sort.Strings(out)
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

// DrainReport is what one drain did. Every path lands in exactly one list, so a receipt
// can say what happened to each queued change rather than only how many there were.
type DrainReport struct {
	// Applied is what reached the work tree: a replacement written or a file removed.
	Applied []string
	// Merged is the subset of Applied whose file had been edited AFTER the merge and was
	// reconciled three-way rather than overwritten.
	Merged []string
	// Resurrected is a queued DELETION whose file the session edited afterwards. The
	// edit is the later decision and the file stays - deletion table row 3, one pass
	// late. The deleted side is still reachable in history.
	Resurrected []string
	// StillQueued is what a live session still blocks.
	StillQueued []string
}

// ApplyDeferred drains a workspace's queue, re-checking BOTH guards for every entry: the
// live/mtime condition that queued it, and whether the file on disk is still the file the
// merge deferred against.
//
// It is a re-check and not a replay. Between the merge and the drain the session went on
// working, and the entry carries what the file looked like when it was queued. If the
// bytes still match, the merged result lands - that is the ordinary case. If they do not,
// the session wrote something later than the merge, and a blind write would silently
// revert it: a replacement is merged three-way against the queued bytes with the DISK
// side winning any real body conflict (it is the later edit, and the merged side is
// already reachable in history), and a deletion is abandoned and reported `resurrected`,
// which is exactly the modify-vs-delete row of the deletion table arriving a pass late.
//
// An entry that is still blocked STAYS QUEUED. Dropping it would silently discard the
// judge's deletion or another PC's edit, which is the failure the queue exists to
// prevent in the first place.
func (e *Engine) ApplyDeferred(ctx context.Context, workspace string, mo MaterializeOptions) (DrainReport, error) {
	var rep DrainReport
	d, err := LoadDeferred(mo.StateRoot, workspace)
	if err != nil {
		return rep, err
	}
	if len(d.Entries) == 0 {
		return rep, nil
	}
	live, sessionStart := mo.Live.Probe(workspace)

	var keep []DeferredEntry
	var deletions []DeferredEntry
	for _, ent := range d.Entries {
		full := mo.workTreePath(e, ent.Path)
		if blocked(ent, live, sessionStart, full) {
			keep = append(keep, ent)
			rep.StillQueued = append(rep.StillQueued, ent.Path)
			continue
		}
		disk, diskErr := os.ReadFile(full)
		untouched, err := e.diskStillHoldsQueuedBytes(ctx, ent, disk, diskErr)
		if err != nil {
			return rep, err
		}
		if ent.Op == OpDelete {
			switch {
			case diskErr != nil:
				rep.Applied = append(rep.Applied, ent.Path) // already gone
			case untouched:
				deletions = append(deletions, ent)
			default:
				rep.Resurrected = append(rep.Resurrected, ent.Path)
			}
			continue
		}
		data, readErr := gitx.CatBlob(ctx, e.opt(), ent.Blob)
		if readErr != nil {
			return rep, readErr
		}
		if !untouched && diskErr == nil {
			data, err = e.reconcileLaterEdit(ctx, ent, disk, data)
			if err != nil {
				return rep, err
			}
			rep.Merged = append(rep.Merged, ent.Path)
		}
		if writeErr := e.writeWorkTree(ent.Path, data, mo); writeErr != nil {
			return rep, writeErr
		}
		rep.Applied = append(rep.Applied, ent.Path)
	}
	// Deletions last, mirroring materialize: a reader that catches the store mid-apply
	// sees a superset of the fact set, never a pointer to a file already gone.
	for _, ent := range deletions {
		full := filepath.Join(e.WorkTree, filepath.FromSlash(ent.Path))
		if rmErr := os.Remove(full); rmErr != nil && !os.IsNotExist(rmErr) {
			return rep, fmt.Errorf("apply deferred deletion %s: %w", ent.Path, rmErr)
		}
		rep.Applied = append(rep.Applied, ent.Path)
	}

	if err := SaveDeferred(mo.StateRoot, workspace, Deferred{Tree: d.Tree, Entries: keep}); err != nil {
		return rep, err
	}
	if (len(rep.Applied) > 0 || len(rep.Resurrected) > 0) && mo.Derive != nil {
		if err := mo.Derive(workspace); err != nil {
			return rep, err
		}
	}
	return rep, nil
}

// diskStillHoldsQueuedBytes answers "is this the file the merge deferred against".
//
// It fails CLOSED in both directions that matter: an entry with no recorded ours-blob
// (queued by an older build, or queued for a path that was not on disk) is treated as
// CHANGED, because "I do not know what was there" must never authorise an overwrite.
//
// The comparison is the deletion table's NORMALIZED one, not byte equality. Between the
// merge and the drain the PC's own derive runs, and derive writes fact files: it harvests
// `hook:` and stamps `migrated:` (the id arrives in the very merge that queued the
// deletion). Neither is a session's edit, and a byte comparison read them as one - every
// queued deletion came back "resurrected" and the judge's migrations were undone on the
// next push (2026-09-17, nineteen files on one PC). Only a difference Canon keeps counts.
func (e *Engine) diskStillHoldsQueuedBytes(ctx context.Context, ent DeferredEntry, disk []byte, diskErr error) (bool, error) {
	if diskErr != nil || ent.OursBlob == "" {
		return false, nil
	}
	queued, err := gitx.CatBlob(ctx, e.opt(), ent.OursBlob)
	if err != nil {
		return false, err
	}
	return NormalizedEqual(queued, disk), nil
}

// reconcileLaterEdit merges the session's later edit with the merged blob the queue was
// holding: base = what was on disk when it was queued, ours = what is on disk now,
// theirs = the merged result. The DISK side wins a real body conflict.
func (e *Engine) reconcileLaterEdit(ctx context.Context, ent DeferredEntry, disk, merged []byte) ([]byte, error) {
	var base []byte
	basePresent := false
	if ent.OursBlob != "" {
		b, err := gitx.CatBlob(ctx, e.opt(), ent.OursBlob)
		if err != nil {
			return nil, err
		}
		base, basePresent = b, true
	}
	// hasOurs without hasTheirs is the deterministic "ours is newer" answer: there is no
	// commit on either side to compare here, and the disk IS the later write by
	// construction - the merged side was computed before it.
	out, _, err := e.mergeContent(ctx, ent.Path,
		sideBlob{present: basePresent, data: base},
		sideBlob{present: true, data: disk},
		sideBlob{present: true, data: merged},
		resolveContext{hasOurs: true})
	return out, err
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
