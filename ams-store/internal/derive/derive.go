package derive

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// Status values derive records. They are the compactor's own vocabulary (the statuses at
// COMPACT:333 and its abort sites, written to the receipts file named at COMPACT:78),
// narrowed to what a judge-less deterministic writer can actually reach: nothing here
// shortens for meaning, migrates, posts to a corpus or deletes a file.
const (
	StatusApplied            = "applied"
	StatusAppliedUnconverged = "applied-unconverged"
	StatusAppliedUnverified  = "applied-unverified"
	StatusNoOp               = "no-op"
	StatusUnconverged        = "unconverged"
	StatusDryRun             = "dry-run"
	StatusAbortedNoFactFiles = "aborted-no-fact-files"
	StatusAbortedGhostLinks  = "aborted-ghost-links"
	StatusAbortedBlastCap    = "aborted-blast-cap"
	StatusAbortedConcurrent  = "aborted-concurrent-write"
	StatusSkippedLockHeld    = "skipped-lock-held"
	StatusErrorStore         = "error-store"
)

// ReceiptFile and DirtyFile are the state-root files derive appends to and touches.
const (
	ReceiptFile = "compact-receipts.jsonl"
	DirtyFile   = "dirty"
)

// ErrLocked is returned when another process holds the per-PC lock. It is an error rather
// than a status so a caller can tell "skipped" from "done" without parsing text - the
// distinction the exit-code contract turns into exit 4.
var ErrLocked = errors.New("another process holds the per-PC lock")

// Lock is the per-PC cross-process lock that covers derive and sync (DESIGN:189-190).
//
// A contender SKIPS immediately: no retry, no backoff, no timeout parameter. Maintenance
// that queues behind maintenance is maintenance that runs under a live session, and the
// gate that calls derive on every Write|Edit must never wait. The sync task owns the
// implementation (PID + process start time, stale after 10 min); derive only needs to be
// able to ask.
type Lock interface {
	TryAcquire(reason string) (release func(), ok bool, err error)
}

// CommitTimes resolves the last commit time of every fact file in a store, in ONE pass.
//
// Commit time is the derived render's one non-local input, and it is a property of the
// SHARED history, so every PC agrees on the order after a sync. It must come from a single
// `git log --format=%ct --name-only` pass; one exec per file turns a 200-entry store into
// 200 process spawns on a hook's critical path.
type CommitTimes interface {
	CommitTimes(st store.Store) (map[string]int64, error)
}

// MigratedLookup answers decision Q8: has this slug been migrated before?
//
// The judge's deletion commit carries a trailer `Migrated: <slug> <mem0 id>`. When a
// session re-creates that slug, derive stamps the id back into the new file as
// `migrated: <id>` so the judge updates the corpus record by id instead of adding a
// nightly variant. The judge task implements the lookup over the history's trailers; no
// new transport, no ledger.
type MigratedLookup interface {
	MigratedID(st store.Store, slug string) (id string, ok bool, err error)
}

// Committer records the derived index in the local history. Optional: derive works
// offline and without a history repo, because an index that is correct on disk is worth
// more than one that waited for git.
type Committer interface {
	Commit(st store.Store, message string) (sha string, err error)
}

// Options configures one derive run over one store.
type Options struct {
	Roots store.Roots
	Store store.Store
	// DryRun reports what would change and writes NOTHING - not the index, and not the
	// frontmatter harvest either.
	DryRun bool
	// NoHarvest skips the whole harvest step (hook: and migrated:). The zero-hooks-lost
	// check uses it to prove derive alone reproduces the current index.
	NoHarvest bool
	// StopBelowBytes and EngageAtBytes override the floor's thresholds. Zero means the
	// Phase 3 defaults (decision Q2).
	StopBelowBytes int
	EngageAtBytes  int
	// InjectLimitLines stops the render at that many lines. Zero means the harness's
	// 200-line injection cap; a negative value disables the stop.
	InjectLimitLines int
	Now              time.Time
	Lock             Lock
	Commits          CommitTimes
	Migrated         MigratedLookup
	Committer        Committer
	Log              io.Writer
	ReceiptPath      string
	DirtyPath        string
}

// Result is one store's receipt row. The field names are the compactor's, so one reader
// and one lint rule cover receipts from both implementations during the Phase 4 overlap;
// the fields after JudgeCalled are derive's own additions.
type Result struct {
	TS          string `json:"ts"`
	Workspace   string `json:"workspace"`
	DryRun      bool   `json:"dry_run"`
	BeforeBytes int    `json:"before_bytes"`
	BeforeLines int    `json:"before_lines"`
	Status      string `json:"status"`
	// Shortened and Migrated are always zero for derive; they stay in the shape so a
	// receipt reader cannot tell the two writers apart by field set alone.
	Shortened        int      `json:"shortened"`
	Migrated         int      `json:"migrated"`
	Reindexed        int      `json:"reindexed"`
	Dedangled        int      `json:"dedangled"`
	DedupSlug        int      `json:"dedup_slug"`
	Floored          int      `json:"floored"`
	LineFloored      int      `json:"line_floored"`
	Mem0             []string `json:"mem0"`
	Mem0Orphan       []string `json:"mem0_orphan"`
	AfterBytes       *int     `json:"after_bytes"`
	AfterLines       *int     `json:"after_lines"`
	Commit           *string  `json:"commit"`
	Snapshot         *string  `json:"snapshot"`
	Note             string   `json:"note"`
	SkipStreak       int      `json:"skip_streak"`
	LivenessOverride bool     `json:"liveness_override"`
	JudgeCalled      bool     `json:"judge_called"`
	Harvested        int      `json:"harvested"`
	MigratedStamped  int      `json:"migrated_stamped"`
	// DedangledMigrated is how many of the dangling pointers hygiene dropped point at
	// a slug the history says the judge migrated. They are inside Dedangled, and they
	// are the removals the blast cap does not count.
	DedangledMigrated    int  `json:"dedangled_migrated"`
	OverInjectLimit      int  `json:"over_inject_limit"`
	ProtectedSetOverflow bool `json:"protected_set_overflow"`
	Unconverged          bool `json:"unconverged"`
	Changed              bool `json:"changed"`
}

// Run derives one store's MEMORY.md, following the step order of blueprint section 3.1.
//
// Every abort leaves the store exactly as it was. Nothing between the compare-and-swap and
// the write can delete anything, because derive deletes nothing at all: its only fact-file
// write is the harvest, and its only index write is the atomic replace at the end.
func Run(opt Options) (*Result, error) {
	now := opt.Now
	if now.IsZero() {
		now = time.Now()
	}
	res := &Result{
		TS:         now.UTC().Format(time.RFC3339Nano),
		Workspace:  opt.Store.Workspace,
		DryRun:     opt.DryRun,
		Status:     StatusErrorStore,
		Mem0:       []string{},
		Mem0Orphan: []string{},
	}
	logf := func(format string, a ...any) {
		if opt.Log == nil {
			return
		}
		fmt.Fprintf(opt.Log, opt.Store.Workspace+": "+format+"\n", a...)
	}

	// ---- the per-PC lock: a contender skips, it never waits --------------------------
	if opt.Lock != nil {
		release, ok, err := opt.Lock.TryAcquire("derive")
		if err != nil {
			res.Note = err.Error()
			return res, fmt.Errorf("acquire the per-PC lock: %w", err)
		}
		if !ok {
			res.Status = StatusSkippedLockHeld
			res.Note = "another process holds the per-PC lock; skipping immediately rather than queueing behind it"
			logf("%s", res.Note)
			return res, ErrLocked
		}
		if release != nil {
			defer release()
		}
	}

	dir := opt.Store.Dir
	indexPath := opt.Store.IndexPath
	if indexPath == "" {
		indexPath = filepath.Join(dir, store.IndexName)
	}

	// ---- 1. read the index and enumerate the fact files, fail-closed -----------------
	// A store with NO index is the fresh-checkout shape: the index is derived and never
	// tracked, so a PC (or the hub's own checkout) that has just materialized a store from
	// the hub holds fact files and nothing else. That is not a failure to read; it is an
	// empty index to render from the files (harvest finds nothing, every file is
	// re-indexed). Before this, the first sync of a fresh checkout materialized every store
	// and then failed on "read index: no such file", leaving no index at all. Any other
	// read error stays fail-closed.
	preBytes, err := os.ReadFile(indexPath)
	indexAbsent := false
	if err != nil {
		if !errors.Is(err, fs.ErrNotExist) {
			res.Note = err.Error()
			return res, fmt.Errorf("read index %s: %w", indexPath, err)
		}
		indexAbsent, preBytes = true, nil
	}
	preText := string(preBytes)
	preHash := atomic.Hash(preBytes)
	idx := index.Parse(preText)
	res.BeforeBytes = index.ByteCount(preText)
	res.BeforeLines = idx.LineCount()

	// "Could not read" must never be spellable as "nothing there": a caller comparing the
	// index against an empty set concludes every line is dangling, and every downstream
	// guard agrees because they all compare against the same empty set.
	factFiles, err := store.FactFiles(dir)
	if err != nil {
		res.Note = err.Error()
		return res, err
	}
	names := make([]string, 0, len(factFiles))
	onDisk := make(map[string]bool, len(factFiles))
	for _, f := range factFiles {
		names = append(names, f.Name)
		onDisk[f.Name] = true
	}
	entries := idx.Entries()

	// ---- 2. an index with entries over a store that enumerates nothing ---------------
	if len(entries) > 0 && len(names) == 0 {
		res.Status = StatusAbortedNoFactFiles
		res.Note = "the index has entries but the store enumerated no fact files; refusing to treat every line as dangling"
		logf("%s", res.Note)
		writeReceipt(opt, res, logf)
		return res, nil
	}

	// ---- 3. sweep orphaned *.am-tmp --------------------------------------------------
	sweep := store.SweepTempFiles(dir)
	for _, removed := range sweep.Removed {
		logf("swept a leftover temp file from a previously failed write: %s", removed)
	}
	for _, stuck := range sweep.Failed {
		// A full copy of the index stuck inside a synced, globbed directory - say so.
		logf("COULD NOT remove leftover temp file: %s", stuck)
		res.Note = addNote(res.Note, "a leftover .am-tmp could not be removed: "+stuck)
	}

	// ---- 4. harvest ------------------------------------------------------------------
	if !opt.NoHarvest && !opt.DryRun {
		harvest(opt, dir, entries, names, onDisk, res, logf)
	}

	// Frontmatter + doctrine classification, computed once and AFTER the harvest so the
	// hook: a file just gained is the one every later step reads.
	meta := make(map[string]*Meta, len(names)+len(entries))
	for _, name := range names {
		meta[name] = &Meta{FM: frontmatter.ParseFile(filepath.Join(dir, name))}
	}

	// hook: in the file is authoritative. The index's own hook text is the next fallback,
	// ahead of description: - without that step a derive run with --no-harvest would
	// replace every hook with its file's description and the zero-hooks-lost check could
	// never pass. description: and the synthesized hook cover files that have never
	// carried a hook at all.
	resolveHook := func(r *index.Record) string {
		var fm *frontmatter.Frontmatter
		if m := meta[r.Slug]; m != nil {
			fm = m.FM
		}
		if fm != nil && fm.Hook != "" {
			return fm.Hook
		}
		if r.Summary != "" {
			return r.Summary
		}
		if fm != nil && fm.Description != "" {
			return fm.Description
		}
		return SynthesizedHook(filepath.Join(dir, r.Slug), fm)
	}

	// ---- 5. hygiene ------------------------------------------------------------------
	in := HygieneInput{
		Records:     idx.Records,
		StoreDir:    dir,
		OnDisk:      onDisk,
		Files:       names,
		Meta:        meta,
		ResolveHook: resolveHook,
		// The headroom projection is the whole byte total of the derived render, with no
		// injection cap: an orphan pointer costs its bytes whether or not the cap would
		// later hide it.
		ProjectBytes: func(recs []*index.Record) int {
			return index.ByteCount(index.RenderDerived(recs, index.RenderOptions{}).Text)
		},
		Log: func(msg string) { logf("%s", msg) },
	}
	hy := Hygiene(in)
	ReindexOrphans(in, hy)
	// An orphan hygiene just re-indexed IS an index entry now, so the harvest rule applies
	// to it too. Doing it in the same run is what makes a second run a true no-op, and it
	// is what lets `harvest --all` finish a PC's store before its first push rather than
	// leaving one file per newly re-indexed orphan for the night after.
	if !opt.NoHarvest && !opt.DryRun {
		harvestReindexed(dir, hy.Keep, res, logf)
	}
	res.Dedangled = hy.Dedangled
	res.DedupSlug = hy.DedupSlug
	res.Reindexed = hy.Reindexed
	if hy.Note != "" {
		res.Note = addNote(res.Note, hy.Note)
	}

	// ---- 6. planned-ghost abort: before any write, with nothing touched ---------------
	if ghosts := index.EntryGhosts(hy.Keep, onDisk); len(ghosts) > 0 {
		res.Status = StatusAbortedGhostLinks
		res.Note = addNote(res.Note, "entry link(s) to missing files that hygiene cannot repair: "+
			strings.Join(ghosts, ", ")+" - fix by hand; nothing written")
		logf("%s", res.Note)
		writeReceipt(opt, res, logf)
		return res, nil
	}

	// ---- 7. blast cap over ALL removals ----------------------------------------------
	// A dangling pointer whose slug the history says the judge MIGRATED is not a wipe in
	// progress: the fact is in the corpus (write-then-verify) and its file left by a
	// commit every PC receives. The hub's own cap lets one night remove up to 20 % of a
	// store's entries; a PC then holds that many dangling pointers over a SMALLER entry
	// count, so counting them here refused the clean-up on every pass, forever (2026-09-17:
	// 16 pointers over a 14-line cap, and 2 over a 1-line cap on a five-line store). The
	// lookup fails closed - a slug with no trailer, or an unreadable history, still counts.
	res.DedangledMigrated = migratedDangling(opt, hy.Dangling, logf)
	cap := BlastCap(len(entries))
	if removals := res.Dedangled - res.DedangledMigrated + res.DedupSlug; removals > cap {
		res.Status = StatusAbortedBlastCap
		exempt := ""
		if res.DedangledMigrated > 0 {
			exempt = " (" + strconv.Itoa(res.DedangledMigrated) + " more point at migrated facts and are exempt)"
		}
		res.Note = addNote(res.Note, "hygiene wanted to remove "+strconv.Itoa(removals)+" line(s)"+exempt+", over the "+
			strconv.Itoa(cap)+"-line cap for this store; refusing and reporting instead")
		logf("%s", res.Note)
		writeReceipt(opt, res, logf)
		return res, nil
	}

	// ---- 8. render: order, floor, injection cap --------------------------------------
	doctrineOf := make(map[string]bool, len(hy.Keep))
	for _, r := range hy.Keep {
		if r.Kind != index.KindEntry {
			continue
		}
		var fm *frontmatter.Frontmatter
		if m := meta[r.Slug]; m != nil {
			fm = m.FM
		}
		doctrineOf[r.Slug] = frontmatter.IsDoctrine(r.Summary, fm)
	}

	commits := map[string]int64{}
	if opt.Commits != nil {
		got, err := opt.Commits.CommitTimes(opt.Store)
		if err != nil {
			// Order falls back to "everything is new", which is deterministic and still
			// correct; an index that is right on disk beats one that waited for git.
			logf("commit times unavailable (%v); ordering by slug", err)
		} else if got != nil {
			commits = got
		}
	}

	injectLimit := opt.InjectLimitLines
	switch {
	case injectLimit == 0:
		injectLimit = store.InjectLimitLines
	case injectLimit < 0:
		injectLimit = 0
	}
	renderOpt := index.RenderOptions{
		Doctrine:   func(r *index.Record) bool { return doctrineOf[r.Slug] },
		CommitTime: func(slug string) (int64, bool) { ct, ok := commits[slug]; return ct, ok },
		Now:        now.Unix(),
		// A deterministic zero Now would make RenderDerived reach for the wall clock; the
		// caller's --now flows through instead.
		InjectLimitLines: injectLimit,
	}
	project := func(recs []*index.Record) string { return index.RenderDerived(recs, renderOpt).Text }

	// The floor measures only what the render will emit. The order is a function of
	// doctrine, commit time and slug - never of hook length - so truncation cannot move a
	// line across the injection cap and the omitted set is stable.
	first := index.RenderDerived(hy.Keep, renderOpt)
	omitted := make(map[string]bool, len(first.Omitted))
	for _, slug := range first.Omitted {
		omitted[slug] = true
	}
	rendered := make([]*index.Record, 0, len(hy.Keep))
	for _, r := range hy.Keep {
		if r.Kind == index.KindEntry && omitted[r.Slug] {
			continue
		}
		rendered = append(rendered, r)
	}

	fl := Floor(rendered, FloorOptions{
		Doctrine:       func(r *index.Record) bool { return doctrineOf[r.Slug] },
		Project:        project,
		EngageAtBytes:  opt.EngageAtBytes,
		StopBelowBytes: opt.StopBelowBytes,
	})
	res.Floored = fl.Floored

	final := index.RenderDerived(hy.Keep, renderOpt)
	newText := final.Text
	res.OverInjectLimit = len(final.Omitted)
	res.ProtectedSetOverflow = final.ProtectedOverflow

	if res.OverInjectLimit > 0 {
		res.Note = addNote(res.Note, "over-inject-limit "+strconv.Itoa(res.OverInjectLimit)+
			": entries past the "+strconv.Itoa(injectLimit)+"-line cap are on disk but not injected")
		logf("over-inject-limit %d", res.OverInjectLimit)
	}
	if res.ProtectedSetOverflow {
		// Decision Q3: doctrine is never dropped. A standing order that disappears from
		// the index is a standing order nobody obeys, so the render goes past the cap and
		// says so instead.
		res.Note = addNote(res.Note, "protected-set-overflow: doctrine alone exceeds the "+
			strconv.Itoa(injectLimit)+"-line injection cap; rendered past it rather than drop a standing order - re-home doctrine detail into topic files by hand")
		logf("%s", "protected-set-overflow")
	}
	if index.ByteCount(newText) >= store.SyncLimitBytes {
		res.Unconverged = true
		res.Note = addNote(res.Note, "UNCONVERGED: "+strconv.Itoa(index.ByteCount(newText))+
			" B still >= the "+strconv.Itoa(store.SyncLimitBytes)+
			" B sync limit after the floor (doctrine lines are never shortened autonomously) - re-home doctrine into topic files by hand")
		logf("%s", res.Note)
	}

	afterBytes := index.ByteCount(newText)
	afterLines := index.Parse(newText).LineCount()

	// ---- nothing to write ------------------------------------------------------------
	if newText == preText {
		res.Status = StatusNoOp
		if res.Unconverged {
			res.Status = StatusUnconverged
		}
		res.AfterBytes = &afterBytes
		res.AfterLines = &afterLines
		// A clean store is the common case: no log line, no receipt. A receipt per store
		// per run would bury the ones that matter. A harvest, on the other hand, IS a
		// fact-file write and is receipted even when the index did not move.
		if res.Harvested > 0 || res.MigratedStamped > 0 || res.Unconverged {
			logf("%s (harvested %d, stamped %d)", res.Status, res.Harvested, res.MigratedStamped)
			writeReceipt(opt, res, logf)
		}
		return res, nil
	}

	// ---- a dry run reports and writes nothing ----------------------------------------
	if opt.DryRun {
		res.Status = StatusDryRun
		res.AfterBytes = &afterBytes
		res.AfterLines = &afterLines
		logf("DRY RUN %d -> %d B", res.BeforeBytes, afterBytes)
		writeReceipt(opt, res, logf)
		return res, nil
	}

	// ---- 9a. compare-and-swap: nothing has been written yet, so an abort is clean -----
	nowHash, err := atomic.FileHash(indexPath)
	if err != nil {
		if !(indexAbsent && errors.Is(err, fs.ErrNotExist)) {
			res.Note = addNote(res.Note, err.Error())
			return res, err
		}
		// Still absent: nothing appeared under the job, and the swap below CREATES the
		// index. An index that appeared meanwhile is a concurrent write and aborts.
		nowHash = preHash
	}
	nowFiles, err := store.FactFiles(dir)
	if err != nil {
		res.Note = addNote(res.Note, err.Error())
		return res, err
	}
	appeared, vanished := diffNames(names, nowFiles)
	if nowHash != preHash || len(appeared) > 0 || len(vanished) > 0 {
		res.Status = StatusAbortedConcurrent
		res.Note = addNote(res.Note, "the store changed under the job (index hash, +"+
			strconv.Itoa(len(appeared))+" / -"+strconv.Itoa(len(vanished))+
			" files); nothing written - abort, never roll back over a live session")
		logf("%s", res.Note)
		writeReceipt(opt, res, logf)
		return res, nil
	}

	// ---- 9b. the write ---------------------------------------------------------------
	if err := atomic.Write(indexPath, newText); err != nil {
		res.Note = addNote(res.Note, err.Error())
		return res, err
	}
	res.Changed = true
	res.AfterBytes = &afterBytes
	res.AfterLines = &afterLines
	res.Status = StatusApplied
	if res.Unconverged {
		res.Status = StatusAppliedUnconverged
	}

	// ---- 9c. post-write invariants ---------------------------------------------------
	// derive deletes nothing, so there is no ordering hazard here - but the check still
	// runs, because a concurrent writer that won the race after the swap is exactly the
	// case a readback cannot see and a session would read.
	if note, ok := verifyPostWrite(indexPath, dir, names, hy.LeftUnindexed, final.Omitted); !ok {
		res.Status = StatusAppliedUnverified
		res.Note = addNote(res.Note, note)
		logf("%s", note)
	}

	// ---- 10. dirty marker + local commit ---------------------------------------------
	touchDirty(opt, logf)
	if opt.Committer != nil {
		msg := fmt.Sprintf("derive %s: %d->%d B, %d->%d lines; reindexed=%d dedangled=%d dedup_slug=%d floored=%d harvested=%d",
			opt.Store.Workspace, res.BeforeBytes, afterBytes, res.BeforeLines, afterLines,
			res.Reindexed, res.Dedangled, res.DedupSlug, res.Floored, res.Harvested)
		if sha, err := opt.Committer.Commit(opt.Store, msg); err != nil {
			res.Note = addNote(res.Note, "post-commit FAILED ("+err.Error()+"); this run is applied but not in history")
			logf("%s", res.Note)
		} else if sha != "" {
			res.Commit = &sha
		}
	}

	logf("%s %d -> %d B, %d -> %d lines", res.Status, res.BeforeBytes, afterBytes, res.BeforeLines, afterLines)
	writeReceipt(opt, res, logf)
	return res, nil
}

// Harvest runs the harvest step alone: it copies each index entry's hook text into that
// entry's fact file as hook:, and stamps `migrated: <id>` onto a re-created slug the
// history says was migrated. It writes no index and renders nothing.
//
// The step is part of derive; the standalone entry point exists because harvest must run
// on every PC BEFORE that PC's first push (DESIGN:365-366). Without it, PC A's index hook
// and PC B's index hook for the same slug arrive at the hub as two competing `hook:`
// additions; with it, both sides already carry the key and the merge sees a `hook:`-only
// difference, which the deletion table's normalized comparison treats as no difference.
func Harvest(opt Options) (*Result, error) {
	now := opt.Now
	if now.IsZero() {
		now = time.Now()
	}
	res := &Result{
		TS:         now.UTC().Format(time.RFC3339Nano),
		Workspace:  opt.Store.Workspace,
		DryRun:     opt.DryRun,
		Status:     StatusErrorStore,
		Mem0:       []string{},
		Mem0Orphan: []string{},
	}
	logf := func(format string, a ...any) {
		if opt.Log == nil {
			return
		}
		fmt.Fprintf(opt.Log, opt.Store.Workspace+": "+format+"\n", a...)
	}

	if opt.Lock != nil {
		release, ok, err := opt.Lock.TryAcquire("harvest")
		if err != nil {
			res.Note = err.Error()
			return res, fmt.Errorf("acquire the per-PC lock: %w", err)
		}
		if !ok {
			res.Status = StatusSkippedLockHeld
			res.Note = "another process holds the per-PC lock; skipping immediately rather than queueing behind it"
			return res, ErrLocked
		}
		if release != nil {
			defer release()
		}
	}

	dir := opt.Store.Dir
	indexPath := opt.Store.IndexPath
	if indexPath == "" {
		indexPath = filepath.Join(dir, store.IndexName)
	}
	preBytes, err := os.ReadFile(indexPath)
	if err != nil {
		if !errors.Is(err, fs.ErrNotExist) {
			res.Note = err.Error()
			return res, fmt.Errorf("read index %s: %w", indexPath, err)
		}
		// No index, nothing to harvest: the fresh-checkout shape (see Run).
		preBytes = nil
	}
	idx := index.Parse(string(preBytes))
	res.BeforeBytes = index.ByteCount(string(preBytes))
	res.BeforeLines = idx.LineCount()

	factFiles, err := store.FactFiles(dir)
	if err != nil {
		res.Note = err.Error()
		return res, err
	}
	names := make([]string, 0, len(factFiles))
	onDisk := make(map[string]bool, len(factFiles))
	for _, f := range factFiles {
		names = append(names, f.Name)
		onDisk[f.Name] = true
	}

	res.Status = StatusNoOp
	if opt.NoHarvest || opt.DryRun {
		return res, nil
	}
	harvest(opt, dir, idx.Entries(), names, onDisk, res, logf)
	if res.Harvested > 0 || res.MigratedStamped > 0 {
		res.Status = StatusApplied
		res.Changed = true
		logf("harvested %d hook(s), stamped %d migrated id(s)", res.Harvested, res.MigratedStamped)
	}
	return res, nil
}

// harvest is the only fact-file write derive makes (DESIGN:193-195, decision Q8).
//
// It must run on every PC before that PC's first push: without it PC A's index hook and
// PC B's index hook for the same slug arrive as two competing `hook:` additions, and with
// it the merge sees a `hook:`-only difference, which the deletion table's normalized
// comparison treats as no difference at all.
func harvest(opt Options, dir string, entries []*index.Record, names []string, onDisk map[string]bool, res *Result, logf func(string, ...any)) {
	for _, e := range entries {
		if !onDisk[e.Slug] || e.Summary == "" {
			continue
		}
		changed, err := frontmatter.Harvest(filepath.Join(dir, e.Slug), e.Summary)
		if err != nil {
			logf("harvest %s: %v", e.Slug, err)
			continue
		}
		if changed {
			res.Harvested++
		}
	}

	stampMigrated(opt, dir, names, res, logf)
}

// harvestReindexed harvests the entries hygiene constructed this run. They carry Index=-1,
// which is how a record the job built is told apart from one it parsed.
func harvestReindexed(dir string, keep []*index.Record, res *Result, logf func(string, ...any)) {
	for _, r := range keep {
		if r.Kind != index.KindEntry || r.Index != -1 || r.Summary == "" {
			continue
		}
		changed, err := frontmatter.Harvest(filepath.Join(dir, r.Slug), r.Summary)
		if err != nil {
			logf("harvest %s: %v", r.Slug, err)
			continue
		}
		if changed {
			res.Harvested++
		}
	}
}

// migratedDangling counts the dangling pointers whose slug carries a migration trailer in
// the history: removals that consume a decision the judge already made rather than
// evidence of a store being gutted. Without a lookup nothing is explained, and a lookup
// error explains nothing either - the cap must fail closed.
func migratedDangling(opt Options, slugs []string, logf func(string, ...any)) int {
	if opt.Migrated == nil || len(slugs) == 0 {
		return 0
	}
	n := 0
	for _, slug := range slugs {
		id, ok, err := opt.Migrated.MigratedID(opt.Store, slug)
		if err != nil {
			logf("migrated lookup %s: %v", slug, err)
			continue
		}
		if ok && id != "" {
			n++
		}
	}
	if n > 0 {
		logf("%d dangling pointer(s) name migrated facts; not counted against the blast cap", n)
	}
	return n
}

// stampMigrated is decision Q8's consumer.
func stampMigrated(opt Options, dir string, names []string, res *Result, logf func(string, ...any)) {
	if opt.Migrated == nil {
		return
	}
	for _, name := range names {
		path := filepath.Join(dir, name)
		b, err := os.ReadFile(path)
		if err != nil {
			logf("migrated stamp %s: %v", name, err)
			continue
		}
		text := string(b)
		if fm := frontmatter.ParseText(text); fm == nil || fm.Migrated != "" {
			continue
		}
		id, ok, err := opt.Migrated.MigratedID(opt.Store, name)
		if err != nil {
			logf("migrated lookup %s: %v", name, err)
			continue
		}
		if !ok || id == "" {
			continue
		}
		value := id
		if !reSafeID.MatchString(id) {
			value = frontmatter.QuoteYAML(id)
		}
		out, changed := frontmatter.InsertKey(text, "migrated", value)
		if !changed {
			continue
		}
		if err := atomic.Write(path, out); err != nil {
			logf("migrated stamp %s: %v", name, err)
			continue
		}
		logf("stamped migrated: %s on the re-created slug %s", id, name)
		res.MigratedStamped++
	}
}

// verifyPostWrite re-reads and re-parses the index derive just wrote and re-enumerates the
// store, requiring zero entry ghosts, zero unexplained orphans and zero lost files
// (COMPACT:903-938). Files the injection cap omitted and orphans left unindexed for want
// of byte headroom are expected to be unlinked and are excluded by name.
func verifyPostWrite(indexPath, dir string, before, leftUnindexed, omitted []string) (string, bool) {
	postBytes, err := os.ReadFile(indexPath)
	if err != nil {
		return "index written but post-write verification failed: " + err.Error(), false
	}
	postFiles, err := store.FactFiles(dir)
	if err != nil {
		return "index written but post-write verification failed: " + err.Error(), false
	}
	postOnDisk := make(map[string]bool, len(postFiles))
	for _, f := range postFiles {
		postOnDisk[f.Name] = true
	}
	postIdx := index.Parse(string(postBytes))
	expected := make(map[string]bool, len(leftUnindexed)+len(omitted))
	for _, n := range leftUnindexed {
		expected[n] = true
	}
	for _, n := range omitted {
		expected[n] = true
	}
	linked := index.LinkedSlugs(postIdx.Records)

	ghosts := index.EntryGhosts(postIdx.Records, postOnDisk)
	var orphans, lost []string
	for _, f := range postFiles {
		if !linked[f.Name] && !expected[f.Name] {
			orphans = append(orphans, f.Name)
		}
	}
	for _, n := range before {
		if !postOnDisk[n] {
			lost = append(lost, n)
		}
	}
	if len(ghosts) == 0 && len(orphans) == 0 && len(lost) == 0 {
		return "", true
	}
	sort.Strings(orphans)
	sort.Strings(lost)
	return fmt.Sprintf("index written but the post-write invariants do not hold (ghosts=%s orphans=%s lost=%s); nothing was deleted - a concurrent writer won the race after the swap",
		strings.Join(ghosts, ","), strings.Join(orphans, ","), strings.Join(lost, ",")), false
}

func touchDirty(opt Options, logf func(string, ...any)) {
	path := opt.DirtyPath
	if path == "" {
		if opt.Roots.StateRoot == "" {
			return
		}
		path = filepath.Join(opt.Roots.StateRoot, DirtyFile)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		logf("dirty marker: %v", err)
		return
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		logf("dirty marker: %v", err)
		return
	}
	_ = f.Close()
	_ = os.Chtimes(path, time.Now(), time.Now())
}

// writeReceipt appends one JSON row to compact-receipts.jsonl. A receipt failure is logged,
// never fatal and never silent: this file IS the audit trail, and both watchdogs read its
// mtime, so a failure here otherwise masquerades as "the maintainer is dead" and sends the
// operator to the wrong subsystem.
func writeReceipt(opt Options, res *Result, logf func(string, ...any)) {
	path := opt.ReceiptPath
	if path == "" {
		if opt.Roots.StateRoot == "" {
			return
		}
		path = filepath.Join(opt.Roots.StateRoot, ReceiptFile)
	}
	b, err := json.Marshal(res)
	if err != nil {
		logf("RECEIPT ENCODE FAILED: %v", err)
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		logf("RECEIPT WRITE FAILED (%s): %v", path, err)
		return
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		logf("RECEIPT WRITE FAILED (%s): %v", path, err)
		return
	}
	defer f.Close()
	if _, err := f.Write(append(b, '\n')); err != nil {
		logf("RECEIPT WRITE FAILED (%s): %v", path, err)
	}
}

func addNote(note, add string) string {
	if note == "" {
		return add
	}
	return note + "; " + add
}

func diffNames(before []string, now []store.FactFile) (appeared, vanished []string) {
	was := make(map[string]bool, len(before))
	for _, n := range before {
		was[n] = true
	}
	is := make(map[string]bool, len(now))
	for _, f := range now {
		is[f.Name] = true
		if !was[f.Name] {
			appeared = append(appeared, f.Name)
		}
	}
	for _, n := range before {
		if !is[n] {
			vanished = append(vanished, n)
		}
	}
	return appeared, vanished
}

// ---------------------------------------------------------------- commit times

// HistoryCommitTimes reads the last commit time of every fact file in a store from the
// local history repo, in ONE `git log --format=%ct --name-only` pass.
type HistoryCommitTimes struct {
	GitDir   string
	WorkTree string
	Timeout  time.Duration
}

// NewHistoryCommitTimes builds the reader for a set of roots.
func NewHistoryCommitTimes(roots store.Roots) HistoryCommitTimes {
	return HistoryCommitTimes{GitDir: roots.HistoryGitDir(), WorkTree: roots.ProjectsRoot}
}

// CommitTimes returns unix seconds keyed by slug. A store with no history repo yet returns
// an empty map and no error: derive is offline-resilient by contract, and "no commit yet"
// simply sorts as newest.
func (h HistoryCommitTimes) CommitTimes(st store.Store) (map[string]int64, error) {
	out := map[string]int64{}
	if h.GitDir == "" {
		return out, nil
	}
	if _, err := os.Stat(h.GitDir); err != nil {
		return out, nil
	}
	ctx := context.Background()
	prefix := st.Workspace + "/memory/"
	res, err := gitx.Run(ctx, gitx.Options{GitDir: h.GitDir, WorkTree: h.WorkTree, Timeout: h.Timeout},
		"log", "--format=%ct", "--name-only", "--", prefix)
	if err != nil {
		return out, err
	}
	var current int64
	for _, line := range strings.Split(strings.ReplaceAll(res.Stdout, "\r\n", "\n"), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if ts, convErr := strconv.ParseInt(line, 10, 64); convErr == nil && !strings.Contains(line, "/") {
			current = ts
			continue
		}
		if !strings.HasPrefix(line, prefix) {
			continue
		}
		slug := strings.TrimPrefix(line, prefix)
		// First mention wins: git log walks newest first.
		if _, seen := out[slug]; !seen {
			out[slug] = current
		}
	}
	return out, nil
}
