package sync

import (
	"context"
	"time"
)

// Merger is the merge engine, declared here as an interface because sync and merge are
// built in parallel: sync drives the engine, it does not contain it.
//
// The contract, from blueprint section 4: Merge computes the three-way merge OUT OF
// TREE (`git merge-tree --write-tree`), resolves every conflicted path by the deletion
// table and the field-aware frontmatter rules, commits the result with
// `commit-tree`/`update-ref`, and materializes it with per-file atomic replace - fact
// files first, deletions and session-touched files deferred. It never runs `git merge`,
// `git checkout` or `git stash`, and it never leaves a conflict marker or a .orig file
// in a store.
//
// What sync does with the result: re-derives every touched store (MEMORY.md is derived,
// never merged) and then pushes. What sync does NOT do: interpret conflicts. A conflict
// recorded in history is reported and pushed like anything else - the work tree is
// correct, and lint is what tells the operator a loser is recoverable.
type Merger interface {
	Merge(ctx context.Context, opts MergeOptions) (MergeResult, error)
}

// MergeOptions is one merge request.
type MergeOptions struct {
	// GitDir and WorkTree address the local history repository.
	GitDir   string
	WorkTree string
	// Ours and Theirs are the two refs. sync passes refs/heads/main and
	// refs/remotes/hub/main.
	Ours   string
	Theirs string
	// MachineID is the deterministic tiebreak when two sides committed in the same
	// second. Never the model-written `modified:` stamp.
	MachineID string
	// Now is the clock, injected so a test can pin it.
	Now time.Time
	// DryRun computes and reports without committing, materializing or deleting.
	DryRun bool
	// Workspaces limits the merge to these workspace slugs. Empty means every store.
	Workspaces []string
}

// Resurrection is one file the modify/delete rule kept alive.
type Resurrection struct {
	// Path is the work-tree-relative path, e.g. "ws/memory/fact.md".
	Path string
	// Side is which side's content survived: "ours" or "theirs".
	Side string
	// Reason is the rule that kept it, for the receipt.
	Reason string
}

// DeferredEntry is one materialization the live-session guard queued.
type DeferredEntry struct {
	Path     string
	Op       string // "replace" or "delete"
	Blob     string
	QueuedAt time.Time
}

// MergeResult is the shape blueprint section 4.2 produces.
type MergeResult struct {
	// UpToDate is true when theirs was already an ancestor of ours: nothing merged,
	// nothing committed, and no materialization happened.
	UpToDate bool
	// Tree is the merged tree oid. Empty when UpToDate.
	Tree string
	// Commit is the merge commit oid written by commit-tree. Empty when UpToDate or
	// DryRun.
	Commit string
	// Parents are the commit's parents, ours first. The SECOND parent is what keeps a
	// conflict's loser reachable in history.
	Parents []string
	// ConflictedPaths is what merge-tree reported, before resolution. A path here is not
	// a failure - it is the set the deletion table and the field-aware merge resolved.
	ConflictedPaths []string
	// Resurrected reports the modify/delete decisions.
	Resurrected []Resurrection
	// ConflictsInHistory reports real body conflicts whose loser stayed in history.
	ConflictsInHistory []ConflictRef
	// Materialized and Deferred are what reached the work tree and what did not.
	Materialized []string
	Deferred     []DeferredEntry
	// TouchedWorkspaces is what sync must re-derive.
	TouchedWorkspaces []string
}

// Drainer applies a workspace's deferred queue, declared here for the same reason as
// Merger: sync drives the merge engine, it does not contain it.
//
// The queue is a PENDING MERGE RESULT - changes another PC (or the judge) already
// committed to history, which this PC withheld from disk because a session was live. It
// is drained at the top of every pass: blueprint 4.8 says "applied at SessionEnd and at
// the next SessionStart", and both of those hooks run `sync --once`.
//
// The drain is a re-check, not a replay: an entry whose session is still live stays
// queued, and one whose file was edited after the merge is reconciled rather than
// overwritten. That judgement belongs to the engine; sync only reports what it decided.
type Drainer interface {
	ApplyDeferred(ctx context.Context, opts DrainOptions) (DrainResult, error)
}

// DrainOptions is one workspace's drain.
type DrainOptions struct {
	Workspace string
	// Now is the injected clock. It is what the liveness probe reads, so it decides
	// whether the session that queued the entries is still running.
	Now time.Time
}

// DrainResult is what the drain decided, path by path.
type DrainResult struct {
	// Applied is what reached the work tree.
	Applied []string
	// Merged is the subset of Applied that was reconciled with a later edit instead of
	// overwriting it.
	Merged []string
	// Resurrected is a queued deletion abandoned because the file was edited after the
	// merge - the modify/delete rule, a pass late.
	Resurrected []string
	// StillQueued is what a live session still blocks.
	StillQueued []string
}

// Deriver is the derive engine, declared here for the same reason as Merger: derive is
// built in parallel and sync drives it.
//
// sync calls Derive BEFORE the fetch, so the index is correct even with no connectivity,
// and again after every merge, because MEMORY.md is derived rather than merged.
type Deriver interface {
	Derive(ctx context.Context, opts DeriveOptions) (DeriveResult, error)
}

// DeriveOptions is one derive request.
type DeriveOptions struct {
	// StoreDir is the store (memory) directory. Empty with Workspace set lets the engine
	// resolve it from its own roots.
	StoreDir string
	// Workspace is the slug.
	Workspace string
	// NoHarvest skips the frontmatter harvest step.
	NoHarvest bool
	// DryRun reports without writing.
	DryRun bool
	// StopBelowBytes is the floor's stop threshold. Zero means the engine default.
	StopBelowBytes int
	// Now is the injected clock.
	Now time.Time
}

// DeriveResult is what one store's derive reports.
type DeriveResult struct {
	Workspace string
	// Status is the receipt status: "no-op", "applied", "aborted-no-fact-files",
	// "aborted-blast-cap", "aborted-concurrent-write", ...
	Status string
	// Changed is whether anything on disk moved.
	Changed     bool
	BeforeBytes int
	AfterBytes  int
	// Floored is how many hooks the convergence floor truncated.
	Floored int
	// Converged is AfterBytes < the sync limit.
	Converged bool
	// OverInjectLimit is how many entries the 200-line render stop omitted.
	OverInjectLimit int
	// ProtectedOverflow is set when doctrine alone exceeded the line cap and the render
	// went past it rather than drop a standing order.
	ProtectedOverflow bool
	// HarvestedHooks is how many fact files gained a hook: key.
	HarvestedHooks int
}
