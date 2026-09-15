package cli

import (
	"context"
	"fmt"
	"io"
	"path/filepath"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/derive"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gate"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/judge"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lint"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	amsync "github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/sync"
)

// This file is the ONLY place the engines meet.
//
// Each engine package declares the neighbour it drives as a one-method interface and is
// tested against a fake, so none of them imports another: sync does not know merge
// exists, gate does not know derive exists, derive does not know git exists. That keeps
// each package's test suite honest, and it moves one obligation here - the adapters
// below are the wiring, and a seam that is only declared and never connected is a verb
// that silently degrades in production while every unit test stays green.
//
// So every adapter here carries a test in cli/seams_test.go that drives the real pair,
// not the fake.

// ---------------------------------------------------------------------------
// derive -> the per-PC lock (blueprint 5.5, decision Q9)
// ---------------------------------------------------------------------------

// deriveLock adapts internal/lock to derive.Lock.
//
// A contender SKIPS. There is no wait, no retry and no timeout anywhere on this path:
// derive runs from the PostToolUse gate, and a lock that blocks puts another process's
// worst case inside the operator's edit loop.
type deriveLock struct {
	path string
	now  time.Time
}

func (d deriveLock) TryAcquire(reason string) (func(), bool, error) {
	l, err := lock.Acquire(lock.Options{Path: d.path, Reason: reason, Now: d.now})
	if err != nil {
		if isHeld(err) {
			return nil, false, nil
		}
		return nil, false, err
	}
	return func() { _ = l.Release() }, true, nil
}

func isHeld(err error) bool {
	return err == lock.ErrHeld || strings.Contains(err.Error(), lock.ErrHeld.Error())
}

// ---------------------------------------------------------------------------
// derive -> the local history commit
// ---------------------------------------------------------------------------

// deriveCommitter adapts the history repo to derive.Committer. It is best-effort by
// contract: derive records a commit failure in the receipt note and still keeps the index
// it just wrote, because an index that is correct on disk is worth more than one that
// waited for git.
type deriveCommitter struct {
	roots     store.Roots
	machineID string
}

func (c deriveCommitter) Commit(st store.Store, message string) (string, error) {
	ctx := context.Background()
	repo := amsync.NewRepo(c.roots)
	if err := repo.Initialize(ctx); err != nil {
		return "", err
	}
	if err := repo.Stage(ctx, st.Workspace); err != nil {
		return "", err
	}
	return repo.Commit(ctx, message, c.machineID, "local")
}

// ---------------------------------------------------------------------------
// derive -> the judge's Migrated: trailer (decision Q8)
// ---------------------------------------------------------------------------

// migratedLookup adapts judge.HistoryMigrated to derive.MigratedLookup.
//
// The judge's deletion commit is the only artifact that outlives a migrated fact file, so
// it is where the mem0 id lives. When a session re-creates the slug, derive's harvest
// stamps `migrated: <id>` back onto the new file and the judge updates the existing
// record by id instead of adding a near-duplicate every night. The lookup fails CLOSED:
// any error is "no id", which costs one duplicate record and never a wrong one.
type migratedLookup struct {
	h judge.HistoryMigrated
}

func (m migratedLookup) MigratedID(_ store.Store, slug string) (string, bool, error) {
	id, ok := m.h.MigratedFor(slug)
	return id, ok, nil
}

func newMigratedLookup(roots store.Roots) migratedLookup {
	return migratedLookup{h: judge.HistoryMigrated{Repo: judge.HistoryRepo{
		GitDir: roots.HistoryGitDir(), WorkTree: roots.ProjectsRoot,
	}}}
}

// ---------------------------------------------------------------------------
// gate -> derive's floor (blueprint 12.1: ONE floor in this binary)
// ---------------------------------------------------------------------------

// floorAdapter adapts derive.Floor to gate.Floorer.
//
// There is exactly one floor implementation in the binary. What differs between the two
// callers is the PROJECTION, not the rule: derive measures against the derived render it
// is about to write, the gate against a verbatim re-render of the file the harness just
// wrote, because the gate normalizes in place and never re-orders. Passing the wrong
// projection would make the gate converge a file that the nightly derive then re-floors.
type floorAdapter struct{}

func (floorAdapter) Floor(records []*index.Record, storeDir, newline string, engageAt, stopBelow int) (gate.FloorResult, error) {
	doctrine := doctrineSet(records, storeDir)
	res := derive.Floor(records, derive.FloorOptions{
		Doctrine:       func(r *index.Record) bool { return doctrine[r.Slug] },
		Project:        func(recs []*index.Record) string { return index.RenderVerbatim(recs, newline) },
		EngageAtBytes:  engageAt,
		StopBelowBytes: stopBelow,
	})
	return gate.FloorResult{Floored: res.Floored, Bytes: res.Bytes}, nil
}

// doctrineSet reads each entry's fact file and classifies it with the ONE doctrine rule
// in internal/frontmatter. A file that cannot be read is not doctrine: the floor's
// guard is "never truncate a standing order", and an unreadable file is not evidence of
// one. It is also never the last word - hygiene has already removed dangling entries by
// the time derive floors, and the gate only truncates, never deletes.
func doctrineSet(records []*index.Record, storeDir string) map[string]bool {
	out := make(map[string]bool, len(records))
	for _, r := range records {
		if r.Kind != index.KindEntry || r.Slug == "" {
			continue
		}
		out[r.Slug] = isDoctrineSlug(storeDir, r)
	}
	return out
}

// ---------------------------------------------------------------------------
// sync -> derive
// ---------------------------------------------------------------------------

// deriverAdapter adapts derive.Run to sync.Deriver.
type deriverAdapter struct {
	roots     store.Roots
	machineID string
	log       io.Writer
}

func (d deriverAdapter) Derive(_ context.Context, opts amsync.DeriveOptions) (amsync.DeriveResult, error) {
	st, err := d.storeFor(opts)
	if err != nil {
		return amsync.DeriveResult{Workspace: opts.Workspace, Status: "aborted-unresolvable-store"}, err
	}
	now := opts.Now
	if now.IsZero() {
		now = time.Now()
	}
	res, err := derive.Run(derive.Options{
		Roots:          d.roots,
		Store:          st,
		DryRun:         opts.DryRun,
		NoHarvest:      opts.NoHarvest,
		StopBelowBytes: opts.StopBelowBytes,
		Now:            now,
		// No lock here: sync already holds the per-PC lock for the whole pass, and derive
		// taking it again would make the pass a contender against itself.
		Commits:   derive.NewHistoryCommitTimes(d.roots),
		Migrated:  newMigratedLookup(d.roots),
		Committer: nil, // sync commits the whole pass itself, once, after every store.
		Log:       d.log,
	})
	out := amsync.DeriveResult{Workspace: st.Workspace}
	if res != nil {
		out.Status = res.Status
		out.Changed = res.Changed
		out.BeforeBytes = res.BeforeBytes
		if res.AfterBytes != nil {
			out.AfterBytes = *res.AfterBytes
		}
		out.Floored = res.Floored
		out.Converged = !res.Unconverged
		out.OverInjectLimit = res.OverInjectLimit
		out.ProtectedOverflow = res.ProtectedSetOverflow
		out.HarvestedHooks = res.Harvested
	}
	return out, err
}

func (d deriverAdapter) storeFor(opts amsync.DeriveOptions) (store.Store, error) {
	if opts.StoreDir != "" {
		return storeAt(opts.StoreDir), nil
	}
	if opts.Workspace == "" {
		return store.Store{}, fmt.Errorf("derive: neither a store directory nor a workspace was named")
	}
	return storeAt(store.Dir(d.roots.ProjectsRoot, opts.Workspace)), nil
}

// ---------------------------------------------------------------------------
// sync -> merge
// ---------------------------------------------------------------------------

// mergerAdapter adapts merge.Engine to sync.Merger.
//
// The derive seam inside MaterializeOptions is the ordering rule made executable: it runs
// LAST for a workspace, after every fact file of that workspace has landed, so no index
// ever points at a file the merge has not written yet.
type mergerAdapter struct {
	roots     store.Roots
	machineID string
	log       io.Writer
}

func (m mergerAdapter) Merge(ctx context.Context, opts amsync.MergeOptions) (amsync.MergeResult, error) {
	now := opts.Now
	if now.IsZero() {
		now = time.Now()
	}
	eng := &merge.Engine{
		GitDir:    opts.GitDir,
		WorkTree:  opts.WorkTree,
		MachineID: opts.MachineID,
		Now:       func() time.Time { return now },
	}
	deriver := deriverAdapter{roots: m.roots, machineID: m.machineID, log: m.log}
	mo := merge.MaterializeOptions{
		StateRoot: m.roots.StateRoot,
		Live: merge.Liveness{
			ProjectsRoot: opts.WorkTree,
			Now:          func() time.Time { return now },
			ProbeDirs:    probeDirsFor(m.roots.ProjectsRoot),
		},
	}
	if !opts.DryRun {
		mo.Derive = func(workspace string) error {
			_, err := deriver.Derive(ctx, amsync.DeriveOptions{Workspace: workspace, Now: now})
			return err
		}
	}
	rep, err := eng.Round(ctx, merge.RoundOptions{
		OursRef: opts.Ours, TheirsRef: opts.Theirs, Date: now, Materialize: mo,
	})
	if err != nil {
		return amsync.MergeResult{}, err
	}
	return reportToResult(rep), nil
}

// ---------------------------------------------------------------------------
// sync -> the deferred queue (blueprint 4.8)
// ---------------------------------------------------------------------------

// drainerAdapter adapts merge.Engine.ApplyDeferred to sync.Drainer.
//
// It is the seam the queue was missing: ApplyDeferred had no caller outside its own
// tests, so every change withheld from a live session stayed in deferred.json forever.
// The materialize options are the same ones the merge round uses - the same liveness
// probe, including alias directories, and the same derive hook - because a drain IS a
// materialize, arriving late.
type drainerAdapter struct {
	roots     store.Roots
	machineID string
	log       io.Writer
}

func (d drainerAdapter) ApplyDeferred(ctx context.Context, opts amsync.DrainOptions) (amsync.DrainResult, error) {
	now := opts.Now
	if now.IsZero() {
		now = time.Now()
	}
	eng := &merge.Engine{
		GitDir:    d.roots.HistoryGitDir(),
		WorkTree:  d.roots.ProjectsRoot,
		MachineID: d.machineID,
		Now:       func() time.Time { return now },
	}
	deriver := deriverAdapter{roots: d.roots, machineID: d.machineID, log: d.log}
	rep, err := eng.ApplyDeferred(ctx, opts.Workspace, merge.MaterializeOptions{
		StateRoot: d.roots.StateRoot,
		Live: merge.Liveness{
			ProjectsRoot: d.roots.ProjectsRoot,
			Now:          func() time.Time { return now },
			ProbeDirs:    probeDirsFor(d.roots.ProjectsRoot),
		},
		Derive: func(workspace string) error {
			_, dErr := deriver.Derive(ctx, amsync.DeriveOptions{Workspace: workspace, Now: now})
			return dErr
		},
	})
	if err != nil {
		return amsync.DrainResult{}, err
	}
	return amsync.DrainResult{
		Applied:     rep.Applied,
		Merged:      rep.Merged,
		Resurrected: rep.Resurrected,
		StillQueued: rep.StillQueued,
	}, nil
}

// reportToResult maps the merge engine's report onto the shape sync writes into its
// receipt. Nothing is invented here: UpToDate is the engine's own "no commit and no
// fast-forward", and the touched set is read off the paths that actually moved, so a
// workspace is re-derived because a file of it changed and never because it was named.
func reportToResult(rep *merge.Report) amsync.MergeResult {
	out := amsync.MergeResult{
		Tree:         rep.MergedTree,
		Commit:       rep.Commit,
		UpToDate:     rep.Commit == "" && !rep.FastForward,
		Materialized: append(append([]string(nil), rep.Materialized...), rep.Deleted...),
	}
	for _, p := range rep.Resurrected {
		out.Resurrected = append(out.Resurrected, amsync.Resurrection{
			Path: p, Side: "ours", Reason: "modified here, deleted there",
		})
	}
	for _, c := range rep.Conflicts {
		out.ConflictsInHistory = append(out.ConflictsInHistory, amsync.ConflictRef{
			Path: c.Path, Commit: c.LoserCommit,
			Detail: c.Kind + "; winner " + short12(c.WinnerCommit) + "; tiebreak " + c.Tiebreak,
		})
		out.ConflictedPaths = append(out.ConflictedPaths, c.Path)
	}
	for _, e := range rep.Deferred {
		// The op is carried, never assumed. A withheld deletion reported as a
		// "replace" is the one line in the receipt an operator would misread.
		queuedAt, _ := time.Parse(time.RFC3339, e.QueuedAt)
		out.Deferred = append(out.Deferred, amsync.DeferredEntry{
			Path: e.Path, Op: string(e.Op), Blob: e.Blob, QueuedAt: queuedAt,
		})
	}
	seen := map[string]bool{}
	for _, p := range concat(rep.Materialized, rep.Deleted, rep.DeferredPaths(), rep.Resurrected) {
		ws := workspaceOfRel(p)
		if ws != "" && !seen[ws] {
			seen[ws] = true
			out.TouchedWorkspaces = append(out.TouchedWorkspaces, ws)
		}
	}
	return out
}

func concat(lists ...[]string) []string {
	var out []string
	for _, l := range lists {
		out = append(out, l...)
	}
	return out
}

// workspaceOfRel maps "ws/memory/fact.md" back to "ws". Anything else is not a store
// path and belongs to no workspace - the shared state stamp under .ams/ is the one
// tracked path that hits this and must not be read as a store.
func workspaceOfRel(rel string) string {
	parts := strings.Split(filepath.ToSlash(rel), "/")
	if len(parts) < 3 || parts[1] != "memory" {
		return ""
	}
	return parts[0]
}

func short12(s string) string {
	if len(s) > 12 {
		return s[:12]
	}
	return s
}

// probeDirsFor returns the liveness probe directories for a workspace: its own directory
// plus every ALIAS directory the enumerator found for the same physical store.
//
// A session running under a junction writes its transcript into the alias directory. A
// probe on the canonical name alone reports "nobody is working here" and the merge
// overwrites a file the operator is editing, which is the exact failure the deferral
// exists to prevent. Enumeration failure means we cannot prove the store is quiet, so
// the probe falls back to the workspace directory and the liveness rule itself stays
// fail-closed.
func probeDirsFor(projectsRoot string) func(string) []string {
	return func(workspace string) []string {
		def := []string{filepath.Join(projectsRoot, workspace)}
		rows, _, err := store.Enumerate(projectsRoot)
		if err != nil {
			return def
		}
		for _, r := range rows {
			if r.Workspace != workspace {
				continue
			}
			if len(r.ProbeDirs) > 0 {
				return r.ProbeDirs
			}
		}
		return def
	}
}

// ---------------------------------------------------------------------------
// judge -> derive's renderer
// ---------------------------------------------------------------------------

// deriveRenderIndex is what judge.Options.RenderIndex must be wired to at the hub call
// site.
//
// The judge's projected-index guard asserts the index STRICTLY SHRINKS. It has to measure
// the bytes derive will actually write, not a verbatim re-render: the derived order and
// the 200-line stop can make an index grow while every individual hook got shorter, and a
// guard measuring the wrong file would pass a run that makes the store worse.
func deriveRenderIndex(doctrine func(*index.Record) bool, commitTime func(string) (int64, bool), now time.Time) func([]*index.Record, string) string {
	return func(records []*index.Record, _ string) string {
		return index.RenderDerived(records, index.RenderOptions{
			Doctrine:         doctrine,
			CommitTime:       commitTime,
			Now:              now.Unix(),
			InjectLimitLines: store.InjectLimitLines,
		}).Text
	}
}

// isDoctrineSlug classifies one index entry by reading its fact file's frontmatter and
// applying the ONE doctrine rule in internal/frontmatter. Never a second copy of that
// rule: the floor, the render order and the judge's refusal all have to agree on what a
// standing order is, or one of them protects a line the others do not.
func isDoctrineSlug(storeDir string, r *index.Record) bool {
	fm := frontmatter.ParseFile(filepath.Join(storeDir, r.Slug))
	return frontmatter.IsDoctrine(r.Summary, fm)
}

// judgeRenderIndex builds the renderer judge.Options.RenderIndex is wired to: derive's
// own, over this store's doctrine set and this store's commit times.
//
// Commit times are read ONCE per run, not once per render: the guard renders the
// projection several times and a per-call git log would put a process spawn per candidate
// on the nightly's critical path. An unreadable history is not fatal - the order falls
// back to "everything is new", which is still deterministic and still the same order
// derive itself would fall back to.
func judgeRenderIndex(roots store.Roots, storeDir string, now time.Time) func([]*index.Record, string) string {
	st := storeAt(storeDir)
	commits := map[string]int64{}
	if got, err := derive.NewHistoryCommitTimes(roots).CommitTimes(st); err == nil && got != nil {
		commits = got
	}
	return func(records []*index.Record, newline string) string {
		doctrine := doctrineSet(records, storeDir)
		return deriveRenderIndex(
			func(r *index.Record) bool { return doctrine[r.Slug] },
			func(slug string) (int64, bool) { ct, ok := commits[slug]; return ct, ok },
			now,
		)(records, newline)
	}
}

// ---------------------------------------------------------------------------
// the maintenance path -> the G7 over-trigger clock (decision Q13)
// ---------------------------------------------------------------------------

// recordOverTrigger stamps, or clears, one store's over-trigger clock.
//
// The stamp is written by the MAINTENANCE path and only read by lint, which is read-only
// by contract. It is the input to G7 - "hours over trigger without an applied decision" -
// which is the one metric a skip cannot satisfy: receipt age proves the maintainer ran,
// this proves the store got better. Nothing wrote it before the engines met here, so
// stores[].over_trigger_hours was null on every PC and the 24 h alarm could not fire.
//
// A dry run never stamps: a rehearsal that started the clock would report a debt the
// operator never incurred.
func recordOverTrigger(roots store.Roots, workspace string, bytes int, dryRun bool, now time.Time, log io.Writer) {
	if dryRun || workspace == "" {
		return
	}
	if _, err := lint.RecordOverTrigger(roots.ProjectsRoot, workspace, bytes >= store.TriggerBytes, now); err != nil && log != nil {
		fmt.Fprintf(log, "ams-store: over-trigger stamp for %s: %v\n", workspace, err)
	}
}

// resultBytes is the size the clock is judged on: what the index IS after the run, or what
// it was when the run wrote nothing.
func resultBytes(before int, after *int) int {
	if after != nil {
		return *after
	}
	return before
}
