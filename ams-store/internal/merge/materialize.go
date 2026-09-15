package merge

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// MaterializeOptions configures the one part of the merge that touches the work tree.
type MaterializeOptions struct {
	// StateRoot is where the per-workspace deferred queue lives. Maintainer state is
	// OUTSIDE the store by rule.
	StateRoot string
	// Live is the liveness probe guarding a running session's files.
	Live Liveness
	// Derive re-renders a workspace's MEMORY.md. It runs LAST, after every fact file of
	// that workspace has landed, so no index ever points at a file not yet present.
	// Nil skips it (the merge engine does not own derive; task 3 does).
	Derive func(workspace string) error
	// OnWrite observes each work-tree write in order. It exists so the ordering rule is
	// testable rather than merely documented.
	OnWrite func(path string)
}

func (mo MaterializeOptions) workTreePath(e *Engine, rel string) string {
	return filepath.Join(e.WorkTree, filepath.FromSlash(rel))
}

// MaterializeReport is what one materialize did.
type MaterializeReport struct {
	Written  []string
	Deleted  []string
	Deferred []string
}

// materialize brings the work tree to the merged tree, file by file.
//
// Per-FILE atomic replace, never a directory swap: a directory restore reverts files a
// live session touched in the meantime, which is how a whole-store restore once undid a
// session's work. Fact files land first and the derived index last.
func (e *Engine) materialize(ctx context.Context, prevTree, newTree string, mo MaterializeOptions) (MaterializeReport, error) {
	var rep MaterializeReport

	want, err := gitx.LsTree(ctx, e.opt(), newTree)
	if err != nil {
		return rep, err
	}
	have, err := gitx.LsTree(ctx, e.opt(), prevTree)
	if err != nil {
		return rep, err
	}

	type change struct {
		path string
		op   Op
		oid  string
	}
	var changes []change
	for p, ent := range want {
		prev, existed := have[p]
		onDisk, readErr := os.ReadFile(mo.workTreePath(e, p))
		switch {
		case readErr != nil:
			changes = append(changes, change{path: p, op: OpReplace, oid: ent.OID})
		case existed && prev.OID == ent.OID:
			// The work tree is already supposed to match; only rewrite when it does not.
			blob, err := gitx.CatBlob(ctx, e.opt(), ent.OID)
			if err != nil {
				return rep, err
			}
			if !bytes.Equal(blob, onDisk) {
				changes = append(changes, change{path: p, op: OpReplace, oid: ent.OID})
			}
		default:
			blob, err := gitx.CatBlob(ctx, e.opt(), ent.OID)
			if err != nil {
				return rep, err
			}
			if !bytes.Equal(blob, onDisk) {
				changes = append(changes, change{path: p, op: OpReplace, oid: ent.OID})
			}
		}
	}
	for p := range have {
		if _, still := want[p]; !still {
			changes = append(changes, change{path: p, op: OpDelete})
		}
	}
	// Deterministic order so two PCs' receipts and the OnWrite observer agree.
	sort.Slice(changes, func(i, j int) bool {
		if changes[i].op != changes[j].op {
			return changes[i].op == OpReplace
		}
		return changes[i].path < changes[j].path
	})

	liveCache := map[string]struct {
		live  bool
		start time.Time
	}{}
	probe := func(ws string) (bool, time.Time) {
		if ws == "" {
			// Not inside a store: the shared state stamp has no session to protect.
			return false, time.Time{}
		}
		if v, ok := liveCache[ws]; ok {
			return v.live, v.start
		}
		live, start := mo.Live.Probe(ws)
		liveCache[ws] = struct {
			live  bool
			start time.Time
		}{live, start}
		return live, start
	}

	queued := map[string][]DeferredEntry{}
	touched := map[string]bool{}
	var deletions []change
	now := e.now().UTC().Format(time.RFC3339)

	for _, c := range changes {
		ws := workspaceOf(c.path)
		live, start := probe(ws)
		if blocked(DeferredEntry{Path: c.path, Op: c.op}, live, start, mo.workTreePath(e, c.path)) {
			queued[ws] = append(queued[ws], DeferredEntry{
				Path: c.path, Op: c.op, Blob: c.oid, QueuedAt: now,
			})
			rep.Deferred = append(rep.Deferred, c.path)
			continue
		}
		if c.op == OpDelete {
			deletions = append(deletions, c)
			continue
		}
		data, err := gitx.CatBlob(ctx, e.opt(), c.oid)
		if err != nil {
			return rep, err
		}
		if err := e.writeWorkTree(c.path, data, mo); err != nil {
			return rep, err
		}
		rep.Written = append(rep.Written, c.path)
		if ws != "" {
			touched[ws] = true
		}
	}

	for _, c := range deletions {
		full := mo.workTreePath(e, c.path)
		if err := os.Remove(full); err != nil && !os.IsNotExist(err) {
			return rep, fmt.Errorf("materialize deletion %s: %w", c.path, err)
		}
		rep.Deleted = append(rep.Deleted, c.path)
		if ws := workspaceOf(c.path); ws != "" {
			touched[ws] = true
		}
	}

	for ws, entries := range queued {
		if ws == "" {
			continue
		}
		if err := queueDeferred(mo.StateRoot, ws, newTree, entries); err != nil {
			return rep, err
		}
	}

	// MEMORY.md LAST, and only for workspaces something actually landed in.
	if mo.Derive != nil {
		names := make([]string, 0, len(touched))
		for ws := range touched {
			names = append(names, ws)
		}
		sort.Strings(names)
		for _, ws := range names {
			if err := mo.Derive(ws); err != nil {
				return rep, err
			}
		}
	}
	return rep, nil
}

// writeWorkTree writes one file through the atomic door, creating its store directory if
// the merge introduced a workspace this PC has never seen.
func (e *Engine) writeWorkTree(rel string, data []byte, mo MaterializeOptions) error {
	full := mo.workTreePath(e, rel)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		return fmt.Errorf("materialize %s: %w", rel, err)
	}
	if err := atomic.WriteBytes(full, data); err != nil {
		return err
	}
	if mo.OnWrite != nil {
		mo.OnWrite(rel)
	}
	return nil
}

// IndexName re-exports the one file in a store that is not a fact file, so callers of the
// merge engine do not have to reach into store for it.
const IndexName = store.IndexName
