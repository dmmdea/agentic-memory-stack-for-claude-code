package merge

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
)

// RoundOptions drives one fetch-merge-materialize round. The FETCH is the caller's: sync
// owns the network, the lock and the receipts, and this is the merge-side hook it calls.
type RoundOptions struct {
	// OursRef defaults to refs/heads/main.
	OursRef string
	// TheirsRef is the post-fetch remote-tracking ref, e.g. refs/remotes/hub/main.
	TheirsRef string
	// Message overrides the merge commit subject.
	Message string
	// Date pins the merge commit's time. Zero means the engine clock.
	Date time.Time
	// Materialize configures the work-tree half.
	Materialize MaterializeOptions
}

// Report is the outcome of one round, and the source of every sync receipt field.
type Report struct {
	// Commit is the merge commit, empty when there was nothing to merge.
	Commit string `json:"commit,omitempty"`
	// MergedTree is the tree the work tree was brought to.
	MergedTree string `json:"merged_tree,omitempty"`
	// Clean is false when merge-tree itself reported conflicted paths. With renames off
	// and a field-aware frontmatter merge, a routine two-PC round is CLEAN.
	Clean bool `json:"clean"`
	// FastForward is true when there was nothing of ours to merge in.
	FastForward bool `json:"fast_forward"`
	// Resurrected names files kept because one side modified what the other deleted.
	Resurrected []string `json:"resurrected,omitempty"`
	// Conflicts are the conflict-in-history findings, each with the loser's commit id.
	Conflicts []Conflict `json:"conflicts,omitempty"`
	// Deferred, Materialized and Deleted mirror the materialize report.
	Deferred     []string `json:"deferred,omitempty"`
	Materialized []string `json:"materialized,omitempty"`
	Deleted      []string `json:"deleted,omitempty"`
}

// Round merges the hub's branch into ours and materializes the result.
//
// Nothing here writes the work tree until the merge COMMIT exists: the merge is computed
// with merge-tree, resolved in memory, staged through a scratch index and committed, and
// only then does materialize touch a file. A process killed at any point before the
// update-ref leaves the work tree exactly as it was.
func (e *Engine) Round(ctx context.Context, ro RoundOptions) (*Report, error) {
	oursRef := ro.OursRef
	if oursRef == "" {
		oursRef = MainRef
	}
	rep := &Report{Clean: true}

	theirs, hasTheirs, err := gitx.RevParse(ctx, e.opt(), ro.TheirsRef)
	if err != nil {
		return nil, err
	}
	ours, hasOurs, err := gitx.RevParse(ctx, e.opt(), oursRef)
	if err != nil {
		return nil, err
	}
	switch {
	case !hasTheirs:
		// Nothing has ever been pushed to the hub: offline-resilient by construction.
		return rep, nil
	case !hasOurs:
		if err := gitx.UpdateRef(ctx, e.opt(), oursRef, theirs, gitx.NullOID); err != nil {
			return nil, err
		}
		tree, err := e.treeOf(ctx, theirs)
		if err != nil {
			return nil, err
		}
		rep.Commit, rep.MergedTree, rep.FastForward = theirs, tree, true
		return rep, e.applyMaterialize(ctx, rep, "", tree, ro.Materialize)
	}

	if ours == theirs {
		return rep, nil
	}
	ahead, err := e.isAncestor(ctx, theirs, ours)
	if err != nil {
		return nil, err
	}
	if ahead {
		// We already contain the hub's history: the push is all that is left.
		return rep, nil
	}
	behind, err := e.isAncestor(ctx, ours, theirs)
	if err != nil {
		return nil, err
	}
	oursTree, err := e.treeOf(ctx, ours)
	if err != nil {
		return nil, err
	}
	if behind {
		if err := gitx.UpdateRef(ctx, e.opt(), oursRef, theirs, ours); err != nil {
			return nil, err
		}
		theirsTree, err := e.treeOf(ctx, theirs)
		if err != nil {
			return nil, err
		}
		rep.Commit, rep.MergedTree, rep.FastForward = theirs, theirsTree, true
		return rep, e.applyMaterialize(ctx, rep, oursTree, theirsTree, ro.Materialize)
	}

	// A real merge.
	base, hasBase, err := gitx.MergeBase(ctx, e.opt(), ours, theirs)
	if err != nil {
		return nil, err
	}
	baseTreeish := base
	if !hasBase {
		// Unrelated histories: two PCs that both initialized before either pushed. The
		// empty tree is the honest base - everything on both sides is an addition.
		baseTreeish, err = gitx.EmptyTree(ctx, e.opt())
		if err != nil {
			return nil, err
		}
	}

	mt, err := gitx.MergeTree(ctx, e.opt(), baseTreeish, ours, theirs)
	if err != nil {
		return nil, err
	}
	rep.Clean = mt.Clean

	baseTree := baseTreeish
	if hasBase {
		baseTree, err = e.treeOf(ctx, base)
		if err != nil {
			return nil, err
		}
	}
	theirsTree, err := e.treeOf(ctx, theirs)
	if err != nil {
		return nil, err
	}

	trees := map[string]map[string]gitx.TreeEntry{}
	for _, t := range []string{baseTree, oursTree, theirsTree, mt.Tree} {
		if _, done := trees[t]; done {
			continue
		}
		listing, err := gitx.LsTree(ctx, e.opt(), t)
		if err != nil {
			return nil, err
		}
		trees[t] = listing
	}

	// ONE log pass per side, not one exec per conflicted file.
	oursTimes, err := gitx.CommitTimesByPath(ctx, e.opt(), ours)
	if err != nil {
		return nil, err
	}
	theirsTimes, err := gitx.CommitTimesByPath(ctx, e.opt(), theirs)
	if err != nil {
		return nil, err
	}

	paths := append([]string(nil), mt.Conflicted...)
	// The shared state stamp is reduced with MIN whatever git made of it: git's own
	// three-way would happily take "the side that changed it", which is not the same
	// answer as "the earliest crossing".
	if _, inOurs := trees[oursTree][OverTriggerPath]; inOurs {
		if _, inTheirs := trees[theirsTree][OverTriggerPath]; inTheirs {
			paths = appendUnique(paths, OverTriggerPath)
		}
	}
	sort.Strings(paths)

	load := func(tree, p string) (sideBlob, error) {
		ent, ok := trees[tree][p]
		if !ok {
			return sideBlob{}, nil
		}
		data, err := gitx.CatBlob(ctx, e.opt(), ent.OID)
		if err != nil {
			return sideBlob{}, err
		}
		return sideBlob{present: true, oid: ent.OID, data: data}, nil
	}

	var changes []gitx.IndexChange
	for _, p := range paths {
		baseSide, err := load(baseTree, p)
		if err != nil {
			return nil, err
		}
		ourSide, err := load(oursTree, p)
		if err != nil {
			return nil, err
		}
		theirSide, err := load(theirsTree, p)
		if err != nil {
			return nil, err
		}
		oc, hasOC := oursTimes[p]
		tc, hasTC := theirsTimes[p]
		rc := resolveContext{oursCommit: oc, theirsCommit: tc, hasOurs: hasOC, hasTheirs: hasTC}

		r, err := e.resolvePath(ctx, p, baseSide, ourSide, theirSide, rc)
		if err != nil {
			return nil, err
		}
		if r.resurrected {
			rep.Resurrected = append(rep.Resurrected, p)
		}
		rep.Conflicts = append(rep.Conflicts, r.conflicts...)

		switch r.op {
		case OpDelete:
			if _, inMerged := trees[mt.Tree][p]; inMerged {
				changes = append(changes, gitx.IndexChange{Path: p})
			}
		case OpReplace:
			blob, err := gitx.HashObject(ctx, e.opt(), r.content)
			if err != nil {
				return nil, err
			}
			if ent, inMerged := trees[mt.Tree][p]; !inMerged || ent.OID != blob {
				changes = append(changes, gitx.IndexChange{Path: p, OID: blob})
			}
		}
	}

	newTree := mt.Tree
	if len(changes) > 0 {
		newTree, err = gitx.StageInto(ctx, e.opt(), mt.Tree, changes)
		if err != nil {
			return nil, err
		}
	}

	subject := ro.Message
	if subject == "" {
		subject = fmt.Sprintf("merge hub: %d resolved, %d conflict-in-history", len(paths), len(rep.Conflicts))
	}
	when := ro.Date
	if when.IsZero() {
		when = e.now()
	}
	opt := e.opt()
	opt.ExtraEnv = dateEnv(when)
	// BOTH parents, always: the loser of a body conflict stays reachable only because
	// the merge commit names the side it came from.
	commit, err := gitx.CommitTree(ctx, opt, newTree, []string{ours, theirs}, e.message(subject, "merge"))
	if err != nil {
		return nil, err
	}
	// The old-value guard: if the gate derived and committed while this merge was being
	// computed, the ref move fails and the round is retried instead of discarding it.
	if err := gitx.UpdateRef(ctx, e.opt(), oursRef, commit, ours); err != nil {
		return nil, fmt.Errorf("update-ref %s: the local branch moved while the merge was computed (retry the round): %w", oursRef, err)
	}
	rep.Commit, rep.MergedTree = commit, newTree
	return rep, e.applyMaterialize(ctx, rep, oursTree, newTree, ro.Materialize)
}

func (e *Engine) applyMaterialize(ctx context.Context, rep *Report, prevTree, newTree string, mo MaterializeOptions) error {
	mr, err := e.materialize(ctx, prevTree, newTree, mo)
	if err != nil {
		return err
	}
	rep.Materialized = mr.Written
	rep.Deleted = mr.Deleted
	rep.Deferred = mr.Deferred
	return nil
}

func (e *Engine) isAncestor(ctx context.Context, maybeAncestor, descendant string) (bool, error) {
	opt := e.opt()
	opt.OkExit = gitx.OkExitCodes(0, 1)
	res, err := gitx.Run(ctx, opt, "merge-base", "--is-ancestor", maybeAncestor, descendant)
	if err != nil {
		return false, err
	}
	return res.Code == 0, nil
}

func appendUnique(list []string, s string) []string {
	for _, v := range list {
		if v == s {
			return list
		}
	}
	return append(list, s)
}
