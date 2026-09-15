// Counterparts of MemoryCompactRobustness.Tests.ps1 - the apply-guard half. The store-
// shape and hygiene scenarios in that file belong to derive and are ported there.
package judge

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// MemoryCompactRobustness.Tests.ps1:73 - a judge that could not be reached is reported as
// skipped-judge-unavailable, not no-op, and the run is NOT productive.
//
// The distinction is the whole of "no local fallback judge": a run that accomplished
// nothing must be retried, and a throttle marked on it is a night lost.
func TestJudge_UnavailableIsNotProductive(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	before := f.indexText()

	opt := f.options()
	opt.Plan = &StorePlan{Workspace: f.ws, Outcome: OutcomeUnavailable, Note: "codex timed out"}
	res := mustApply(t, opt)

	if res.Status != StatusSkippedNoJudge {
		t.Fatalf("status = %q, want %q", res.Status, StatusSkippedNoJudge)
	}
	if res.Productive {
		t.Error("an unreachable judge is not a decision: the judge-only work waits and is retried")
	}
	if f.indexText() != before {
		t.Error("nothing may be applied when the judge never answered")
	}
	if !res.JudgeCalled {
		t.Error("judge_called records the ATTEMPT, whatever its outcome")
	}
	rows := f.usageRows()
	if len(rows) != 1 || rows[0].Outcome != string(OutcomeUnavailable) {
		t.Errorf("the usage ledger must carry the failed attempt: %+v", rows)
	}
}

// MemoryCompactRobustness.Tests.ps1:84 - a DEDUPLICATED id is never deleted.
//
// add() with infer=false returns an EXISTING id on a hash hit. Deleting that id on a
// read-back failure destroys a record this run did not create: an L1a fact, or an earlier
// migration. The line is kept, the record is left untouched, and the id is reported.
func TestMigrate_NeverDeletesDeduplicatedID(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0DedupMismatch)

	res := f.apply([]Decision{migrate("fact3.md")}, nil)

	if res.Migrated != 0 {
		t.Fatalf("migrated = %d, want 0", res.Migrated)
	}
	if !f.exists("fact3.md") {
		t.Error("the fact file was deleted against an unverified write")
	}
	if len(f.mem.Deleted()) != 0 {
		t.Errorf("a pre-existing (dedup) record was deleted: %v", f.mem.Deleted())
	}
	if !strings.Contains(strings.Join(res.Mem0Orphan, " "), "pre-existing") {
		t.Errorf("the dedup hit must be reported: %v", res.Mem0Orphan)
	}
}

// MemoryCompactRobustness.Tests.ps1:95 - a concurrent-write abort after a verified
// migration UNDOES the corpus write.
//
// Without the undo the abort leaves a verified, dedup-protected record that no receipt
// names and that only heals if the judge happens to say MIGRATE again. The concurrent
// write is real here: the fake corpus writes to the index while the migration is in
// flight, exactly as a live session would, rather than a seam into the guard itself.
func TestMigrate_UndoOnConcurrentAbort(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	f.mem.OnAdd = func(testutil.Mem0Post) {
		appendLine(t, filepath.Join(f.dir, store.IndexName),
			"- [Appended by a live session](fact1.md) "+testutil.EmDash+" written mid-run")
	}

	res := f.apply([]Decision{migrate("fact3.md")}, nil)

	if res.Status != StatusAbortedConcurrent {
		t.Fatalf("status = %q, want %q", res.Status, StatusAbortedConcurrent)
	}
	if res.Migrated != 0 {
		t.Errorf("migrated = %d, want 0 after an abort", res.Migrated)
	}
	if got := f.mem.Deleted(); len(got) != 1 || got[0] != "stub-id-0001" {
		t.Errorf("the corpus write was not undone: deleted=%v", got)
	}
	if !f.exists("fact3.md") {
		t.Error("a file was deleted on an aborted run")
	}
	if !strings.Contains(f.indexText(), "Appended by a live session") {
		t.Error("the concurrent write must survive untouched - abort, never roll back over a live session")
	}
}

// MemoryCompactRobustness.Tests.ps1:111 - an unverifiable write is undone rather than
// left as an orphan record.
func TestMigrate_UndoUnverifiedWrite(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0Mismatch)

	res := f.apply([]Decision{migrate("fact3.md")}, nil)

	if res.Migrated != 0 {
		t.Fatalf("migrated = %d, want 0", res.Migrated)
	}
	if !f.exists("fact3.md") {
		t.Error("the fact file was deleted against an unverified write")
	}
	if got := f.mem.Deleted(); len(got) != 1 || got[0] != "stub-id-0001" {
		t.Errorf("the unverified record must be removed, not left dangling in the corpus: %v", got)
	}
}

// MemoryCompactRobustness.Tests.ps1:208 - the receipt row carries the line as it stood in
// the index, never an empty third field.
//
// The row is "id | slug | line". A line the job itself constructed - a re-indexed orphan,
// which derive produces - has no "original", so the constructed line must be used; an
// empty string leaves the audit trail unable to say what the index looked like before the
// pointer was removed. Both halves are pinned: the end-to-end row, and the rule for a
// record whose Raw is empty.
func TestReceipt_ReindexedThenMigratedCarriesLine(t *testing.T) {
	lines, facts := bigStore(60)
	lines = append(lines, entryLine("orphan", "orphan.md", "an unindexed reference fact"))
	facts["orphan.md"] = factFile("orphan", "an unindexed reference fact", "reference", "the body")
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)

	res := f.apply([]Decision{migrate("orphan.md")}, nil)

	if res.Migrated != 1 {
		t.Fatalf("migrated = %d, want 1 (orphans %v)", res.Migrated, res.Mem0Orphan)
	}
	row := strings.Join(res.Mem0, "\n")
	if !strings.Contains(row, "orphan.md | - [orphan](orphan.md)") {
		t.Errorf("the receipt must show the line as it stood in the index: %q", row)
	}
	if strings.HasSuffix(strings.TrimSpace(row), "|") {
		t.Errorf("the receipt row's line field is empty: %q", row)
	}

	constructed := &index.Record{Kind: index.KindEntry, Title: "orphan", Slug: "orphan.md", Summary: "a hook"}
	if got := recordLine(constructed); got == "" || !strings.Contains(got, "(orphan.md)") {
		t.Errorf("a record with no Raw must yield its constructed line, never an empty string: %q", got)
	}
}

// MemoryCompactRobustness.Tests.ps1:344 - a body over the server's storage cap is never
// offered and never migrated.
//
// It 413s on every attempt, nightly, forever. The check runs twice on purpose: once when
// the offer set is built, and again at apply time against the text about to be sent.
func TestMigrate_NeverOffersOverCapBody(t *testing.T) {
	lines, facts := bigStore(60)
	lines = append(lines, entryLine("Big", "big.md", "a pullable lookup with an enormous body"))
	facts["big.md"] = factFile("big", "d", "project", strings.Repeat("lorem ipsum ", 500))
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)

	// fact3 is the CONTROL: a migratable fact in the same plan, which must land, so an
	// apply that migrated nothing at all cannot pass this scenario.
	res := f.apply([]Decision{migrate("big.md"), migrate("fact3.md")}, nil)

	if res.Migrated != 1 {
		t.Fatalf("the control migration did not land: migrated=%d orphans=%v", res.Migrated, res.Mem0Orphan)
	}
	if got := len(f.mem.Posts()); got != 1 {
		t.Fatalf("exactly the control may be posted: posts=%d", got)
	}
	if f.exists("big.md") != true {
		t.Fatal("the over-cap fact must stay on disk")
	}
	if !f.exists("big.md") {
		t.Error("the file was removed for a migration that cannot land")
	}
	if !strings.Contains(f.indexText(), "(big.md)") {
		t.Error("the index line was removed for a migration that cannot land")
	}
	if hasCandidate(res.Candidates.Migrate, "big.md") {
		t.Error("an unmigratable body must not even be offered")
	}
	for _, p := range f.mem.Posts() {
		if strings.Contains(p.Source, "big.md") {
			t.Errorf("nothing may be posted for an over-cap body: %v", p)
		}
	}
}

// MemoryCompactRobustness.Tests.ps1:391 - the line floor migrates the OLDEST pullable
// facts until the store is back at its line target, and never doctrine.
//
// Bytes converge through the convergence floor; LINES only fall through migration, and a
// judge keeps by default. One store climbed to 174 lines against the 200-line injection
// cutoff while every nightly migrated nothing.
func TestLineFloor_MigratesOldestToTarget(t *testing.T) {
	lines := []string{"# Memory Index", ""}
	facts := map[string]string{}
	for i := 1; i <= 169; i++ {
		slug := "fact" + itoa(i) + ".md"
		lines = append(lines, entryLine("Fact "+itoa(i), slug, "hook "+itoa(i)))
		facts[slug] = factFile("fact"+itoa(i), "desc "+itoa(i), "reference", "body "+itoa(i))
	}
	for i := 1; i <= 3; i++ {
		slug := "rule" + itoa(i) + ".md"
		lines = append(lines, entryLine("Rule "+itoa(i), slug, "Daniel: never do thing "+itoa(i)))
		facts[slug] = factFile("rule"+itoa(i), "Daniel: never do thing "+itoa(i), "feedback", "the rule "+itoa(i))
	}
	f := newFixture(t, "tall", lines, facts, testutil.Mem0OK)
	// fact1..fact40 are the oldest, ascending; everything else keeps its fresh mtime.
	for i := 1; i <= 40; i++ {
		age(t, filepath.Join(f.dir, "fact"+itoa(i)+".md"), f.now.AddDate(0, 0, -100+i))
	}

	res := f.apply(nil, nil)

	if res.Status != StatusApplied {
		t.Fatalf("status = %q, want %q (note %s)", res.Status, StatusApplied, res.Note)
	}
	// 174 index lines -> the 140-line target is 34 migrations, and the blast cap over 172
	// entries is floor(0.2*172) = 34. Both bounds are exactly met, which is what makes
	// this fixture worth its length.
	if res.LineFloored != 34 {
		t.Fatalf("line_floored = %d, want 34", res.LineFloored)
	}
	if res.Migrated != 34 {
		t.Fatalf("migrated = %d, want 34", res.Migrated)
	}
	if res.AfterLines > store.TargetLines+2 {
		t.Errorf("after_lines = %d, want at most %d", res.AfterLines, store.TargetLines+2)
	}
	for i := 1; i <= 34; i++ {
		if f.exists("fact" + itoa(i) + ".md") {
			t.Errorf("fact%d.md is among the 34 oldest and should have been migrated", i)
		}
	}
	if !f.exists("fact35.md") || !f.exists("fact169.md") {
		t.Error("the floor migrated past its debt")
	}
	for i := 1; i <= 3; i++ {
		if !f.exists("rule" + itoa(i) + ".md") {
			t.Errorf("rule%d.md is doctrine: the floor must never migrate it", i)
		}
	}
	if !strings.Contains(f.indexText(), "(rule1.md)") {
		t.Error("a doctrine pointer was dropped from the index")
	}
}

// MemoryCompactRobustness.Tests.ps1:435 - the line floor never migrates an ATTRIBUTED
// statement, even when it is typed project.
//
// The live run migrated "Owner: X's box = first-class, ABSOLUTE": no imperative verb,
// type project, and the oldest file in the store. An attributed statement is a standing
// order whatever verb follows it.
func TestLineFloor_NeverMigratesAttributed(t *testing.T) {
	lines := []string{"# Memory Index", ""}
	facts := map[string]string{}
	for i := 1; i <= 165; i++ {
		slug := "fact" + itoa(i) + ".md"
		lines = append(lines, entryLine("Fact "+itoa(i), slug, "hook "+itoa(i)))
		facts[slug] = factFile("fact"+itoa(i), "desc "+itoa(i), "reference", "body "+itoa(i))
	}
	lines = append(lines, entryLine("Owner priority", "owner-priority.md",
		"Owner: the small box = first-class, ABSOLUTE #1 queue priority"))
	facts["owner-priority.md"] = factFile("owner-priority",
		"Owner 2026-07-23: the small box = first-class, ABSOLUTE #1 priority", "project", "the standing order")
	f := newFixture(t, "attr", lines, facts, testutil.Mem0OK)
	age(t, filepath.Join(f.dir, "owner-priority.md"), f.now.AddDate(0, 0, -400)) // the oldest of all

	res := f.apply(nil, nil)

	if res.Status != StatusApplied {
		t.Fatalf("status = %q, want %q (note %s)", res.Status, StatusApplied, res.Note)
	}
	// 168 index lines against the 140-line target is a debt of 28, and the floor must
	// migrate exactly that: one short would mean the attributed statement was PICKED and
	// then refused at apply time, which is a pool that still offers doctrine.
	if res.LineFloored != 28 {
		t.Fatalf("line_floored = %d, want 28 (the whole debt, from non-doctrine facts only)", res.LineFloored)
	}
	if hasCandidate(res.Candidates.Migrate, "owner-priority.md") {
		t.Error("an attributed standing order was in the migratable pool at all")
	}
	if IsMigratable(&Meta{Slug: "owner-priority.md", Doctrine: true,
		FM: &frontmatter.Frontmatter{Type: "project", Body: "the standing order"}}) {
		t.Error("IsMigratable admitted a doctrine fact; the floor reads this directly")
	}
	if !f.exists("owner-priority.md") {
		t.Error("the oldest file is an attributed standing order; the floor must skip it")
	}
	if !strings.Contains(f.indexText(), "(owner-priority.md)") {
		t.Error("the attributed statement was dropped from the index")
	}
}

// MemoryCompactRobustness.Tests.ps1:470 - a judge call that SUCCEEDS and returns nothing
// writes outcome=empty.
//
// An empty string is falsy in PowerShell, so this path wrote no ledger row at all and was
// invisible to the usage report's failed column. A failure the ledger cannot count is a
// failure nobody fixes.
func TestUsageLedger_EmptyJudgeOutputIsAnOutcome(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)

	opt := f.options()
	opt.Plan = &StorePlan{Workspace: f.ws, Outcome: OutcomeEmpty}
	res := mustApply(t, opt)

	rows := f.usageRows()
	found := false
	for _, r := range rows {
		if r.Component == UsageComponent && r.Outcome == string(OutcomeEmpty) {
			found = true
		}
	}
	if !found {
		t.Fatalf("a successful call that returned nothing is an outcome, not a non-event: %+v", rows)
	}
	if res.Productive {
		t.Error("an empty answer accomplished nothing; the run must be retried")
	}
}

// --- helpers ------------------------------------------------------------------------

func mustApply(t *testing.T, opt Options) Result {
	t.Helper()
	res, err := Apply(t.Context(), opt)
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	return res
}

func appendLine(t *testing.T, path, line string) {
	t.Helper()
	fh, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatalf("open %s: %v", path, err)
	}
	defer fh.Close()
	if _, err := fh.WriteString(line + "\n"); err != nil {
		t.Fatalf("append to %s: %v", path, err)
	}
}

func age(t *testing.T, path string, when time.Time) {
	t.Helper()
	if err := os.Chtimes(path, when, when); err != nil {
		t.Fatalf("age %s: %v", path, err)
	}
}

// The blast cap bounds the ONE removal loop that can empty an index. The line floor's own
// bound is its line debt; when the debt is larger than the cap, the cap wins and the store
// converges over several nights instead of losing a fifth of itself in one.
//
// 202 index lines is a debt of 62 against a cap of floor(0.2*200) = 40, so the two bounds
// are genuinely different here - which is what makes this test able to see the cap at all.
func TestLineFloor_StopsAtTheBlastCap(t *testing.T) {
	lines := []string{"# Memory Index", ""}
	facts := map[string]string{}
	for i := 1; i <= 200; i++ {
		slug := "fact" + itoa(i) + ".md"
		lines = append(lines, entryLine("Fact "+itoa(i), slug, "hook "+itoa(i)))
		facts[slug] = factFile("fact"+itoa(i), "desc "+itoa(i), "reference", "body "+itoa(i))
	}
	f := newFixture(t, "wide", lines, facts, testutil.Mem0OK)

	res := f.apply(nil, nil)

	if res.Status != StatusApplied {
		t.Fatalf("status = %q, want %q (note %s)", res.Status, StatusApplied, res.Note)
	}
	if res.LineFloored != 40 {
		t.Fatalf("line_floored = %d, want 40: the debt is 62 but the blast cap over 200 entries is 40", res.LineFloored)
	}
	if res.AfterLines != 162 {
		t.Errorf("after_lines = %d, want 162 - the store converges over several nights, not in one", res.AfterLines)
	}
}
