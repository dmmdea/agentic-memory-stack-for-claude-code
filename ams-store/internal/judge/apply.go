package judge

import (
	"context"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// DefaultMaxMigrations is how many migrations one JUDGE plan may perform in a run
// (COMPACT:765-774). The line floor has its own bound - the line debt - and bypasses this
// one, but never the blast cap.
const DefaultMaxMigrations = 5

// Statuses a run can end in. They are the receipt's vocabulary and two watchdogs read
// them, so they are constants rather than literals scattered through the flow.
const (
	StatusApplied           = "applied"
	StatusAppliedUnrecorded = "applied-unrecorded"
	StatusNoOp              = "no-op"
	StatusDryRun            = "dry-run"
	StatusRejectedNoShrink  = "rejected-no-shrink"
	StatusAbortedConcurrent = "aborted-concurrent-write"
	StatusAbortedBlastCap   = "aborted-blast-cap"
	StatusAbortedNoFacts    = "aborted-no-fact-files"
	StatusProtectedOverflow = "protected-set-overflow"
	StatusSkippedWindow     = "skipped-judge-attempted-today"
	StatusSkippedNoJudge    = "skipped-judge-unavailable"
	StatusRevertedInvariant = "reverted-invariant-failure"
	StatusRestoreFailed     = "invariant-failure-RESTORE-FAILED"
)

// productive lists the statuses that count as a decision reached (COMPACT:1019-1021).
// A run that skipped everything must be RETRIED, not counted as done, which is why the
// skipped-* statuses are deliberately absent.
var productive = map[string]bool{
	StatusApplied:           true,
	StatusAppliedUnrecorded: true,
	StatusNoOp:              true,
	StatusDryRun:            true,
	StatusProtectedOverflow: true,
}

// Options drives one store's apply.
type Options struct {
	// Roots supplies STATE_ROOT (receipts, seals) and PROJECTS_ROOT (the work tree).
	Roots store.Roots
	// Dir is the store's memory directory.
	Dir string
	// Workspace is the harness slug.
	Workspace string
	// Plan is this store's slice of the plan file. Nil means "the plan named no
	// decisions for this store", which still lets the deterministic line floor run.
	Plan *StorePlan
	// DryRun reports what would be applied and writes nothing, posts nothing.
	DryRun bool
	// Force is the operator saying "judge it now": it bypasses the 20 h window and
	// nothing else. No guard is ever disarmed by a flag.
	Force bool
	// MaxMigrations bounds the judge's migrations. Zero means DefaultMaxMigrations.
	MaxMigrations int
	// Now is the clock. Zero means time.Now().
	Now time.Time
	// Mem0 is the corpus client. Nil makes migrations impossible - they are reported as
	// unmigrated, never silently counted.
	Mem0 Mem0Client
	// History is the local out-of-tree repo the deletion commit and its Migrated:
	// trailers go into. An invalid repo skips the commit and says so in the receipt.
	History HistoryRepo
	// Log receives the human progress log. Nil discards it.
	Log io.Writer
	// RenderIndex overrides how the projected index is rendered from records.
	//
	// The default is the verbatim regenerator: rebuild the Dirty entries, emit every
	// other line from Raw. Once derive lands, the hub wires derive's renderer in here so
	// the judge measures its guards against exactly the bytes derive will write.
	RenderIndex func(records []*index.Record, newline string) string
}

// Result is what one store's apply did.
type Result struct {
	Workspace   string
	Status      string
	Note        string
	Shortened   int
	Migrated    int
	LineFloored int
	// Mem0 carries "<id> | <slug> | <the line as it stood in the index>" per migrated
	// fact. The third field is never empty: it is the only record of what the index said
	// before the pointer was removed.
	Mem0 []string
	// Mem0Orphan carries every corpus record this run could not account for.
	Mem0Orphan  []string
	BeforeBytes int
	BeforeLines int
	AfterBytes  int
	AfterLines  int
	JudgeCalled bool
	Productive  bool
	Commit      string
	// IndexText is the index as this run left it (or would leave it, under --dry-run).
	IndexText string
	// Candidates is the offer surface this run computed.
	Candidates CandidateSet
}

type pendingDelete struct {
	Slug         string
	ID           string
	Raw          string
	Deduplicated bool
}

type pendingHook struct {
	Slug string
	Hook string
}

func (o Options) now() time.Time {
	if o.Now.IsZero() {
		return time.Now().UTC()
	}
	return o.Now.UTC()
}

func (o Options) maxMigrations() int {
	if o.MaxMigrations <= 0 {
		return DefaultMaxMigrations
	}
	return o.MaxMigrations
}

func (o Options) logf(format string, args ...any) {
	if o.Log == nil {
		return
	}
	fmt.Fprintf(o.Log, format+"\n", args...)
}

func (o Options) render(records []*index.Record, newline string) string {
	if o.RenderIndex != nil {
		return o.RenderIndex(records, newline)
	}
	return index.RenderVerbatim(records, newline)
}

// Apply applies one store's plan under every guard and writes its receipt.
//
// The order of the guards is load-bearing and is the order the compactor learned the
// hard way: decide everything that can abort BEFORE anything is posted to the corpus,
// write the index BEFORE anything is deleted from disk, and verify the written index
// BEFORE the deletions are carried out. Each inversion of that order has a live incident
// behind it.
//
// An error return means the run could not be evaluated at all (an unreadable store, a
// corrupt seal). Everything else - including every refusal - is a Result with a status
// and a receipt.
func Apply(ctx context.Context, opt Options) (Result, error) {
	now := opt.now()
	res := Result{Workspace: opt.Workspace}

	wsStateDir, err := opt.Roots.WorkspaceStateDir(opt.Workspace)
	if err != nil {
		return res, fmt.Errorf("workspace state dir: %w", err)
	}
	sealPath := SealPath(wsStateDir)
	// A corrupt seal file stops the run. Proceeding seal-less re-arms the judge on every
	// already-shortened line, and the save at the end would then overwrite the file with
	// only this run's seals, discarding the history permanently.
	seal, err := LoadSeal(sealPath)
	if err != nil {
		return res, err
	}

	st, err := Load(opt.Dir, opt.Workspace)
	if err != nil {
		return res, err
	}
	res.Candidates = st.Candidates(seal)

	preText := st.Text
	preFiles := st.FileNames()
	preHash := atomic.Hash([]byte(preText))
	res.BeforeBytes = index.ByteCount(preText)
	res.BeforeLines = st.Index.LineCount()
	res.IndexText = preText

	entries := st.Index.Entries()
	if len(entries) > 0 && len(st.Files) == 0 {
		// Every line would read as dangling. That is what an unreadable directory looks
		// like from here, and wiping the index on it is the failure this aborts on.
		res.Status = StatusAbortedNoFacts
		res.Note = "the index has entries but the store enumerated zero fact files; refusing to treat that as 'everything is dangling'"
		return finish(opt, res, now)
	}

	decisions := []Decision{}
	outcome := OutcomeOK
	if opt.Plan != nil {
		decisions = opt.Plan.Decisions
		if opt.Plan.Outcome != "" {
			outcome = opt.Plan.Outcome
		}
		if opt.Plan.Note != "" {
			res.Note = opt.Plan.Note
		}
	}

	// judgeNeeded is a property of the STORE, not of the plan: it is "was there anything
	// for a judge to decide here", which is what the once-per-window rule is about.
	judgeNeeded := len(res.Candidates.Shorten) > 0 || len(res.Candidates.Migrate) > 0
	judgeOK := outcome == OutcomeOK
	judgeAttemptedToday := false

	if judgeNeeded && !opt.Force {
		last, within, err := WithinJudgeWindow(opt.Roots.StateRoot, opt.Workspace, now)
		if err != nil {
			return res, err
		}
		if within {
			// A second attempt inside the window is dropped, not applied: 32 judge calls
			// on one store in a day, every result rejected, is what this stops. The
			// deterministic work below still runs.
			judgeAttemptedToday = true
			decisions = nil
			res.Note = "judge already attempted at " + last.Format(time.RFC3339) +
				"; next attempt after " + fmt.Sprint(JudgeWindowHours) + "h"
			opt.logf("%s: judge attempted inside the %dh window; plan not applied (deterministic work only)", opt.Workspace, JudgeWindowHours)
		}
	}

	// judge_called records an ATTEMPT, whatever the outcome: a rejected plan, an
	// unavailable judge and a clean apply all count, because the window exists to stop
	// repeated ATTEMPTS. A plan dropped inside the window is not an attempt - it is the
	// same attempt arriving twice.
	res.JudgeCalled = opt.Plan != nil && !judgeAttemptedToday

	if !judgeAttemptedToday && outcome != OutcomeOK {
		// The judge was called and did not answer usefully. There is NO local fallback
		// judge: the deterministic work runs, the judge-only work waits, and the run is
		// not productive. The usage row is written for every such outcome - including
		// "succeeded and returned nothing", which wrote no row at all in the original
		// and was therefore invisible to the failure count.
		decisions = nil
		status := "error"
		if outcome == OutcomeEmpty {
			status = "ok"
			res.Note = "judge returned empty; deterministic work only"
		} else if res.Note == "" {
			res.Note = "judge " + string(outcome) + "; deterministic work only"
		}
		if err := WriteUsage(opt.Roots.StateRoot, UsageRow{
			TS: now.Format(time.RFC3339Nano), Component: UsageComponent,
			Workspace: opt.Workspace, Status: status, Outcome: string(outcome),
		}); err != nil {
			opt.logf("%s: usage ledger append failed: %v", opt.Workspace, err)
		}
	}

	// ---- feasibility: can the protected set alone even fit? ------------------------
	// The hard rule "doctrine is untouchable" is never loosened autonomously. When
	// doctrine alone will not fit the budget, the run says so and stops; it does not
	// start shortening standing orders to make room.
	protectedLines, protectedFloor := 0, 0
	for _, r := range entries {
		if m := st.Meta[r.Slug]; m != nil && m.Doctrine {
			protectedLines++
			protectedFloor += min(r.Bytes, store.LineByteCap) + 1
		}
	}
	if protectedLines > store.TargetLines || protectedFloor > store.TargetBytes {
		res.Status = StatusProtectedOverflow
		res.Note = fmt.Sprintf("doctrine lines alone (%d lines, ~%d B) exceed the target budget; the hard rule is never loosened autonomously - re-home doctrine into a topic file by hand",
			protectedLines, protectedFloor)
		opt.logf("%s: %s", opt.Workspace, res.Note)
		return finish(opt, res, now)
	}

	// ---- the blast cap over every removal this run may make -------------------------
	blastCap := max(1, int(math.Floor(float64(len(entries))*store.BlastCapFraction)))
	removals := 0
	migrationsDone := 0

	keep := append([]*index.Record(nil), st.Index.Records...)
	byslug := map[string]*index.Record{}
	for _, r := range keep {
		if r.Kind == index.KindEntry {
			if _, dup := byslug[r.Slug]; !dup {
				byslug[r.Slug] = r
			}
		}
	}

	newlySealed := map[string]bool{}
	var pendingDeletes []pendingDelete
	var pendingHooks []pendingHook

	plan := append([]Decision(nil), decisions...)
	plan = append(plan, lineFloorPlan(opt, st, decisions, keep, byslug)...)
	if n := countFloor(plan); n > 0 {
		opt.logf("%s: line floor: migrating the %d oldest pullable fact(s) toward the %d-line target", opt.Workspace, n, store.TargetLines)
	}

	for _, d := range plan {
		rec, ok := byslug[d.Slug]
		if !ok {
			continue
		}
		m := st.Meta[d.Slug]
		if m == nil || m.Doctrine {
			// Re-checked at apply time, not trusted from the offer: the plan is written
			// by another program and doctrine is the one thing no plan may touch.
			continue
		}
		switch d.Verb {
		case VerbKeep:
			continue

		case VerbShorten:
			hook := strings.TrimSpace(d.NewHook)
			if hook == "" {
				continue
			}
			candidate := index.EntryLine(rec.Title, rec.Slug, hook, rec.Indent)
			newBytes := index.ByteCount(candidate)
			if newBytes >= rec.Bytes {
				continue // strict decrease, or the edit is not a shortening
			}
			if !index.LineRoundTrips(candidate, rec.Slug, rec.ExtraSlugs) {
				// A hook carrying a markdown link injects a phantom second slug: a ghost
				// hygiene can never remove, so the invariant check fails, the index is
				// restored, and the run is discarded - every night, forever.
				continue
			}
			if !index.KeepsAnAnchor(rec.Summary, hook) {
				// The rewrite kept the topic and lost the trigger detail.
				continue
			}
			rec.Summary = hook
			rec.Bytes = newBytes
			rec.Dirty = true
			newlySealed[d.Slug] = true
			res.Shortened++
			if m.FM != nil && m.FM.Present {
				// The hook is authoritative in the FILE from v2 on; the index is derived
				// from it. The write is deferred to after the invariants pass so an
				// abort really is "nothing written".
				pendingHooks = append(pendingHooks, pendingHook{Slug: d.Slug, Hook: hook})
			}

		case VerbMigrate:
			isFloor := d.Metadata != nil && d.Metadata[floorKey] == "1"
			if migrationsDone >= opt.maxMigrations() && !isFloor {
				continue
			}
			if removals >= blastCap {
				continue
			}
			if m.FM == nil || m.FM.Body == "" {
				continue
			}
			text := d.Mem0Text
			if text == "" {
				text = MigrationText(m.FM.Description, m.FM.Body)
			}
			if TooLargeToMigrate(text) {
				continue // re-checked here: the server would refuse it, every night
			}
			if opt.DryRun {
				keep = removeRecord(keep, rec)
				migrationsDone++
				removals++
				res.Migrated++
				if isFloor {
					res.LineFloored++
				}
				continue
			}
			if opt.Mem0 == nil {
				res.Mem0Orphan = append(res.Mem0Orphan, "(no id) | "+d.Slug+" | no corpus client configured; line kept")
				continue
			}
			meta := map[string]string{"tier": "evidence", "origin_slug": d.Slug, "workspace": opt.Workspace}
			for k, v := range d.Metadata {
				if k == floorKey {
					continue
				}
				meta[k] = v
			}
			w, err := opt.Mem0.Add(ctx, text, SourceTag(opt.Workspace, d.Slug), meta)
			if err != nil || w.ID == "" {
				// A record MAY still have landed, so nothing is undone: the retry is
				// hash-idempotent and will deduplicate.
				msg := "write returned no id"
				if err != nil {
					msg = err.Error()
				}
				res.Mem0Orphan = append(res.Mem0Orphan, "(no id) | "+d.Slug+" | line kept: "+msg)
				opt.logf("%s: migration %s returned no id; line kept (%s)", opt.Workspace, d.Slug, msg)
				continue
			}
			rec0, getErr := opt.Mem0.Get(ctx, w.ID)
			if getErr == nil && Landed(rec0, text) {
				keep = removeRecord(keep, rec)
				pendingDeletes = append(pendingDeletes, pendingDelete{
					Slug: d.Slug, ID: w.ID, Raw: recordLine(rec), Deduplicated: w.Deduplicated,
				})
				migrationsDone++
				removals++
				res.Migrated++
				if isFloor {
					res.LineFloored++
				}
				continue
			}
			if w.Deduplicated {
				// The id belongs to a PRE-EXISTING record this run did not create -
				// an L1a fact, or an earlier migration. Never delete it.
				res.Mem0Orphan = append(res.Mem0Orphan, w.ID+" | "+d.Slug+" | pre-existing (dedup) record, read-back failed; line kept, record untouched")
				opt.logf("%s: migration %s hit an existing record %s whose read-back failed; line kept", opt.Workspace, d.Slug, w.ID)
				continue
			}
			if delErr := opt.Mem0.Delete(ctx, w.ID); delErr == nil {
				opt.logf("%s: migration %s unverifiable; the record was removed and the line kept", opt.Workspace, d.Slug)
			} else {
				res.Mem0Orphan = append(res.Mem0Orphan, w.ID+" | "+d.Slug+" | unverified write that could not be removed")
				opt.logf("%s: migration %s unverifiable AND its record could not be removed; id %s recorded in the receipt", opt.Workspace, d.Slug, w.ID)
			}
		}
	}

	newText := opt.render(keep, "\n")
	newBytesTotal := index.ByteCount(newText)
	res.IndexText = newText

	undo := func(why string) {
		for _, pd := range pendingDeletes {
			if pd.Deduplicated {
				res.Mem0Orphan = append(res.Mem0Orphan, pd.ID+" | "+pd.Slug+" | pre-existing (dedup) record left in place after "+why)
				continue
			}
			if opt.Mem0 == nil {
				continue
			}
			if err := opt.Mem0.Delete(ctx, pd.ID); err != nil {
				res.Mem0Orphan = append(res.Mem0Orphan, pd.ID+" | "+pd.Slug+" | verified write left in corpus after "+why+" (delete failed)")
				continue
			}
			opt.logf("%s: undid migration write %s for %s after %s", opt.Workspace, pd.ID, pd.Slug, why)
		}
		res.Migrated = 0
		res.LineFloored = 0
	}

	// A run with verified migrations pending is NEVER a no-op even when the index text
	// is unchanged: the file must still be deleted and its corpus id must reach the
	// receipt, or the audit trail loses the only mapping from the deleted fact to the
	// record that now holds it.
	if newText == preText && len(pendingDeletes) == 0 && len(pendingHooks) == 0 {
		switch {
		case judgeNeeded && judgeAttemptedToday:
			res.Status = StatusSkippedWindow
		case judgeNeeded && !judgeOK:
			res.Status = StatusSkippedNoJudge
		default:
			res.Status = StatusNoOp
		}
		res.AfterBytes, res.AfterLines = res.BeforeBytes, res.BeforeLines
		return finish(opt, res, now)
	}

	if newBytesTotal >= res.BeforeBytes {
		undo("rejected-no-shrink")
		res.Status = StatusRejectedNoShrink
		res.Note = "the judge-driven edits did not shrink the index; discarded"
		res.IndexText = preText
		opt.logf("%s: %s", opt.Workspace, res.Note)
		return finish(opt, res, now)
	}

	if opt.DryRun {
		res.Status = StatusDryRun
		res.AfterBytes = newBytesTotal
		res.AfterLines = index.Parse(newText).LineCount()
		opt.logf("%s: DRY RUN %d -> %d B", opt.Workspace, res.BeforeBytes, newBytesTotal)
		return finish(opt, res, now)
	}

	// ---- compare-and-swap: nothing has been removed from disk yet -------------------
	nowHash, err := atomic.FileHash(st.IndexPath)
	nowFiles, ferr := store.FactFiles(opt.Dir)
	if err != nil || ferr != nil || nowHash != preHash || changed(preFiles, names(nowFiles)) {
		undo("concurrent-write abort")
		res.Status = StatusAbortedConcurrent
		res.Note = "the store changed under the job; nothing written, nothing deleted, migration writes undone - abort, never roll back over a live session"
		res.IndexText = preText
		opt.logf("%s: %s", opt.Workspace, res.Note)
		return finish(opt, res, now)
	}

	if err := atomic.Write(st.IndexPath, newText); err != nil {
		undo("index write failure")
		return res, fmt.Errorf("write index: %w", err)
	}

	// ---- post-write invariants, computed BEFORE any file is deleted ------------------
	// Deleting first and checking second is how a revert restored an index that still
	// pointed at files already gone.
	migratedSlugs := map[string]bool{}
	for _, pd := range pendingDeletes {
		migratedSlugs[pd.Slug] = true
	}
	post, verr := Load(opt.Dir, opt.Workspace)
	var ghosts, orphans, lost []string
	if verr == nil {
		ghosts = index.EntryGhosts(post.Index.Records, post.OnDisk)
		linked := index.LinkedSlugs(post.Index.Records)
		for _, f := range post.FileNames() {
			if !linked[f] && !migratedSlugs[f] {
				orphans = append(orphans, f)
			}
		}
		for _, f := range preFiles {
			if !post.OnDisk[f] {
				lost = append(lost, f)
			}
		}
	}
	if verr != nil || len(ghosts) > 0 || len(orphans) > 0 || len(lost) > 0 {
		undo("invariant failure")
		// Restore only the index - the one file this run wrote. Nothing has been
		// deleted, so a successful restore returns the store to exactly its pre-run
		// state. The restore's own success is checked: a silent failure would leave a
		// mutated index behind a receipt claiming it was reverted.
		if rerr := atomic.Write(st.IndexPath, preText); rerr == nil {
			res.Status = StatusRevertedInvariant
			res.Note = fmt.Sprintf("ghosts=%s orphans=%d lost=%d; index restored; no file deleted",
				strings.Join(ghosts, ","), len(orphans), len(lost))
		} else {
			res.Status = StatusRestoreFailed
			res.Note = fmt.Sprintf("ghosts=%d orphans=%d lost=%d; THE INDEX IS MUTATED AND WAS NOT RESTORED (no file deleted): %v",
				len(ghosts), len(orphans), len(lost), rerr)
		}
		res.IndexText = preText
		opt.logf("%s: INVARIANT FAILURE: %s", opt.Workspace, res.Note)
		return finish(opt, res, now)
	}

	// ---- the deferred writes: the index is on disk and verified ----------------------
	var deleteFailed []string
	var migrations []Migration
	var relPaths []string
	rel := storeRelPath(opt)
	for _, pd := range pendingDeletes {
		if err := os.Remove(filepath.Join(opt.Dir, pd.Slug)); err != nil {
			// The fact is safe in the corpus but the file lingers unreferenced; the next
			// run re-indexes it as an orphan. Say so rather than count a clean migration.
			deleteFailed = append(deleteFailed, pd.Slug)
			res.Mem0 = append(res.Mem0, pd.ID+" | "+pd.Slug+" | MIGRATED but file delete FAILED: "+err.Error())
			continue
		}
		res.Mem0 = append(res.Mem0, pd.ID+" | "+pd.Slug+" | "+pd.Raw)
		migrations = append(migrations, Migration{Slug: pd.Slug, Mem0ID: pd.ID})
		relPaths = append(relPaths, rel+"/"+pd.Slug)
	}

	for _, ph := range pendingHooks {
		if _, err := frontmatter.WriteHook(filepath.Join(opt.Dir, ph.Slug), ph.Hook); err != nil {
			opt.logf("%s: hook write failed for %s: %v", opt.Workspace, ph.Slug, err)
		}
	}

	for slug := range newlySealed {
		seal.Stamp(slug, now)
	}
	if len(newlySealed) > 0 {
		if err := SaveSeal(sealPath, seal); err != nil {
			// An unsaved seal silently re-arms the judge on those lines next run.
			res.Note = appendNote(res.Note, "seal save FAILED ("+err.Error()+"); those lines may be re-offered next run")
			opt.logf("%s: %s", opt.Workspace, res.Note)
		}
	}

	commitFailed := false
	if len(migrations) > 0 && opt.History.Valid() {
		subject := fmt.Sprintf("judge %s: %d migrated, %d shortened", opt.Workspace, len(migrations), res.Shortened)
		commit, err := CommitDeletions(ctx, opt.History, relPaths, subject, migrations)
		if err != nil {
			commitFailed = true
			res.Note = appendNote(res.Note, "deletion commit FAILED ("+err.Error()+"); this run is applied but not in history")
			opt.logf("%s: %s", opt.Workspace, res.Note)
		} else {
			res.Commit = commit
		}
	}

	postText, rerr := os.ReadFile(st.IndexPath)
	if rerr == nil {
		res.IndexText = string(postText)
		res.AfterBytes = len(postText)
		res.AfterLines = index.Parse(string(postText)).LineCount()
	} else {
		res.AfterBytes = newBytesTotal
		res.AfterLines = index.Parse(newText).LineCount()
	}
	// applied-unrecorded is "the work happened but history does not know", which is only
	// possible when there IS a history repo. A fixture with none is not an unrecorded run.
	if commitFailed {
		res.Status = StatusAppliedUnrecorded
	} else {
		res.Status = StatusApplied
	}
	if len(deleteFailed) > 0 {
		res.Note = appendNote(res.Note, fmt.Sprintf("migrated, but %d fact file(s) could not be deleted: %s", len(deleteFailed), strings.Join(deleteFailed, ", ")))
	}
	opt.logf("judge %s: %d->%d B, %d->%d lines; shortened=%d migrated=%d line_floored=%d",
		opt.Workspace, res.BeforeBytes, res.AfterBytes, res.BeforeLines, res.AfterLines, res.Shortened, res.Migrated, res.LineFloored)
	return finish(opt, res, now)
}

// floorKey marks a decision the deterministic line floor added rather than the judge.
const floorKey = "ams_line_floor"

// lineFloorPlan is the deterministic line floor (COMPACT:715-732).
//
// Bytes converge through the convergence floor; LINES only fall through migration, and a
// judge keeps by default - one store climbed to 174 lines against the 200-line injection
// cutoff while every nightly migrated nothing. When the index is over its line trigger,
// the OLDEST pullable facts the judge did not decide are migrated deterministically -
// same write-then-verify path, same blast cap - until the store is back at its target.
//
// It is appended AFTER the judge's decisions so the judge's own MIGRATEs count first,
// and the debt is reduced by them: a judge migration that fails to land leaves the store
// one line over target tonight and the floor takes it tomorrow. Self-healing, never
// over-migrating.
func lineFloorPlan(opt Options, st *State, decisions []Decision, keep []*index.Record, byslug map[string]*index.Record) []Decision {
	lines := index.Parse(opt.render(keep, "\n")).LineCount()
	if lines <= store.TriggerLines {
		return nil
	}
	debt := lines - store.TargetLines
	decided := map[string]bool{}
	judgeMigrates := 0
	for _, d := range decisions {
		decided[d.Slug] = true
		if d.Verb == VerbMigrate {
			judgeMigrates++
		}
	}
	left := debt - min(judgeMigrates, opt.maxMigrations())
	if left <= 0 {
		return nil
	}

	type aged struct {
		slug string
		mod  time.Time
	}
	var pool []aged
	for _, r := range st.Index.Entries() {
		if decided[r.Slug] {
			continue
		}
		if _, live := byslug[r.Slug]; !live {
			continue
		}
		if !IsMigratable(st.Meta[r.Slug]) {
			continue
		}
		// An unreadable file sorts LAST: "I cannot tell how old it is" must never make
		// something the first thing removed.
		mod := time.Unix(1<<62, 0)
		if fi, err := os.Stat(filepath.Join(st.Dir, r.Slug)); err == nil {
			mod = fi.ModTime()
		}
		pool = append(pool, aged{slug: r.Slug, mod: mod})
	}
	sort.SliceStable(pool, func(i, j int) bool {
		if !pool[i].mod.Equal(pool[j].mod) {
			return pool[i].mod.Before(pool[j].mod)
		}
		return pool[i].slug < pool[j].slug
	})
	if len(pool) > left {
		pool = pool[:left]
	}
	out := make([]Decision, 0, len(pool))
	for _, a := range pool {
		out = append(out, Decision{Slug: a.slug, Verb: VerbMigrate, Metadata: map[string]string{floorKey: "1"}})
	}
	return out
}

func countFloor(plan []Decision) int {
	n := 0
	for _, d := range plan {
		if d.Metadata != nil && d.Metadata[floorKey] == "1" {
			n++
		}
	}
	return n
}

// recordLine is the line as it stood in the index.
//
// A record the job itself constructed (a re-indexed orphan) has no "original" Raw, so
// the constructed line is used instead - never an empty string, which would leave the
// receipt unable to say what the index looked like before the pointer was removed.
func recordLine(r *index.Record) string {
	if r.Raw != "" {
		return r.Raw
	}
	return index.EntryLine(r.Title, r.Slug, r.Summary, r.Indent)
}

// finish writes the receipt and returns the result. Every exit from Apply that reached a
// store goes through here: a run whose receipt is missing is a run that did not happen as
// far as both watchdogs are concerned.
func finish(opt Options, res Result, now time.Time) (Result, error) {
	res.Productive = productive[res.Status]
	if res.Mem0 == nil {
		res.Mem0 = []string{}
	}
	if res.Mem0Orphan == nil {
		res.Mem0Orphan = []string{}
	}
	var after, afterLines *int
	if res.AfterBytes > 0 || res.AfterLines > 0 {
		b, l := res.AfterBytes, res.AfterLines
		after, afterLines = &b, &l
	}
	r := Receipt{
		TS:          now.Format(time.RFC3339Nano),
		Workspace:   res.Workspace,
		DryRun:      opt.DryRun,
		BeforeBytes: res.BeforeBytes,
		BeforeLines: res.BeforeLines,
		Status:      res.Status,
		Shortened:   res.Shortened,
		Migrated:    res.Migrated,
		LineFloored: res.LineFloored,
		Mem0:        res.Mem0,
		Mem0Orphan:  res.Mem0Orphan,
		AfterBytes:  after,
		AfterLines:  afterLines,
		Commit:      res.Commit,
		Note:        res.Note,
		JudgeCalled: res.JudgeCalled,
	}
	if err := WriteReceipt(opt.Roots.StateRoot, r); err != nil {
		// Both watchdogs read this file's mtime; a lost receipt is a silent run.
		opt.logf("%s: receipt append FAILED: %v", res.Workspace, err)
	}
	return res, nil
}

// storeRelPath is the store's path relative to the work tree, in git's forward-slash
// spelling. It falls back to the conventional <workspace>/memory when the store is not
// under the projects root - which only happens in a fixture.
func storeRelPath(opt Options) string {
	if opt.Roots.ProjectsRoot != "" {
		if r, err := filepath.Rel(opt.Roots.ProjectsRoot, opt.Dir); err == nil && !strings.HasPrefix(r, "..") {
			return filepath.ToSlash(r)
		}
	}
	return opt.Workspace + "/memory"
}

func appendNote(note, add string) string {
	if note == "" {
		return add
	}
	return note + "; " + add
}

func removeRecord(records []*index.Record, target *index.Record) []*index.Record {
	out := make([]*index.Record, 0, len(records))
	for _, r := range records {
		if r == target {
			continue
		}
		out = append(out, r)
	}
	return out
}

func names(files []store.FactFile) []string {
	out := make([]string, 0, len(files))
	for _, f := range files {
		out = append(out, f.Name)
	}
	return out
}

func changed(before, after []string) bool {
	if len(before) != len(after) {
		return true
	}
	a := append([]string(nil), before...)
	b := append([]string(nil), after...)
	sort.Strings(a)
	sort.Strings(b)
	for i := range a {
		if a[i] != b[i] {
			return true
		}
	}
	return false
}
