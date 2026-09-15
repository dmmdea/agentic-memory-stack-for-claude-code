// Net-new guard coverage the Pester suite has no scenario for.
//
// The three guards below were each found UNCOVERED by a mutation run over this package
// (2026-09-15): the implementation was correct and every ported scenario stayed green
// with the guard deleted, which means the guard was shipped untested. A guard no test can
// kill is a guard the next refactor removes.
package judge

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// The strict decrease is asserted TWICE, on two different things, and only one of them
// has a Pester counterpart. Per line, a rewrite must be shorter than the line it replaces
// (TestJudge_RejectsNoShrinkAndAnchorLoss). Per RUN, the whole projected index must be
// smaller than the one on disk - and that is a separate question, because the projection
// is pluggable: the hub wires derive's renderer in here so the judge measures its guards
// against exactly the bytes derive will write, and derive re-renders every line, not only
// the edited ones. A run that shortens two hooks and grows the index by re-rendering
// forty others has done net damage, and is discarded whole.
//
// The fixture makes the projection grow on purpose, because a renderer that can only
// shrink cannot test a guard against growth.
func TestJudge_ProjectedIndexMustStrictlyDecrease(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	before := f.indexText()

	res := f.apply(
		[]Decision{
			shorten("fact1.md", "detail number 1 kept short"),
			migrate("fact3.md"), // a verified corpus write, so the undo is exercised too
		},
		func(o *Options) {
			o.RenderIndex = func(records []*index.Record, newline string) string {
				// A projection that is different from, and no smaller than, what is on
				// disk: the shape a re-rendering derive can produce.
				return index.RenderVerbatim(records, newline) + "\n<!-- a renderer that grew the file " + strings.Repeat("x", 4000) + " -->\n"
			}
		})

	if res.Status != StatusRejectedNoShrink {
		t.Fatalf("status = %q, want %q (note %s)", res.Status, StatusRejectedNoShrink, res.Note)
	}
	if got := f.indexText(); got != before {
		t.Error("the index was written although the projected index did not shrink")
	}
	if !f.exists("fact3.md") {
		t.Error("a fact file was deleted by a run that was discarded")
	}
	if got := f.mem.Deleted(); len(got) != 1 || got[0] != "stub-id-0001" {
		t.Errorf("the migration write was not undone: deleted=%v", got)
	}
	if res.Migrated != 0 || res.Shortened == 0 {
		t.Errorf("a discarded run reported migrated=%d shortened=%d; the migration must not be counted and the shortening must still show what was attempted",
			res.Migrated, res.Shortened)
	}
}

// MaxMigrationsPerRun is the JUDGE's quota, and it is not the blast cap: the cap is a
// fraction of the index and bounds every removal a run makes, while this bounds how much
// of one night's plan is acted on. It exists because a judge that decides to migrate
// forty facts in one answer is a judge whose answer nobody has reviewed yet - five a
// night is a rate at which a wrong call is noticed before the store is gone.
//
// The deterministic line floor deliberately bypasses this bound (it has its own, the line
// debt) and never the blast cap, so the fixture is kept under the line trigger: otherwise
// the floor would top the run back up to the debt and hide the quota entirely.
func TestMigrate_BlastCapMaxMigrationsPerRun(t *testing.T) {
	plan := []Decision{}
	for i := 1; i <= 8; i++ {
		plan = append(plan, migrate("fact"+itoa(i)+".md"))
	}

	t.Run("default is five", func(t *testing.T) {
		// The number itself is the contract (COMPACT:765-774), so it is pinned as a
		// literal: asserting the run against the constant would keep passing if the
		// constant moved, which is the one change this test exists to catch.
		if DefaultMaxMigrations != 5 {
			t.Fatalf("DefaultMaxMigrations = %d, want 5", DefaultMaxMigrations)
		}
		lines, facts := bigStore(60)
		f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
		if lines := index.Parse(f.indexText()).LineCount(); lines > store.TriggerLines {
			t.Fatalf("the fixture is %d lines, over the %d-line trigger: the floor would mask the quota", lines, store.TriggerLines)
		}
		res := f.apply(plan, nil)
		if res.Migrated != DefaultMaxMigrations {
			t.Fatalf("migrated = %d, want the default quota of %d", res.Migrated, DefaultMaxMigrations)
		}
		if res.LineFloored != 0 {
			t.Fatalf("line_floored = %d: the floor ran, so this run says nothing about the judge's quota", res.LineFloored)
		}
		for i := 1; i <= DefaultMaxMigrations; i++ {
			if f.exists("fact" + itoa(i) + ".md") {
				t.Errorf("fact%d.md is inside the quota and should have migrated", i)
			}
		}
		for i := DefaultMaxMigrations + 1; i <= 8; i++ {
			if !f.exists("fact" + itoa(i) + ".md") {
				t.Errorf("fact%d.md is past the quota and must be left for the next run", i)
			}
			if !strings.Contains(f.indexText(), "(fact"+itoa(i)+".md)") {
				t.Errorf("fact%d.md lost its pointer without being migrated", i)
			}
		}
	})

	t.Run("the flag lowers it", func(t *testing.T) {
		lines, facts := bigStore(60)
		f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
		res := f.apply(plan, func(o *Options) { o.MaxMigrations = 2 })
		if res.Migrated != 2 {
			t.Fatalf("migrated = %d, want 2", res.Migrated)
		}
		if n := len(f.mem.Posts()); n != 2 {
			t.Errorf("%d record(s) reached the corpus for a quota of 2: the quota must stop the WRITE, not only the count", n)
		}
	})
}

// The undo has its own dedup rule, separate from the read-back path's.
//
// A migration can verify against a record the server says is DEDUPLICATED: the same fact
// migrated on an earlier night, or an L1a extraction of it, whose text happens to match
// byte for byte. Migrating against it is legitimate - the fact is in the corpus, which is
// all the file's deletion needs. But the record is not this run's to remove, so when a
// later guard discards the run, the undo must leave it alone and report it. Deleting it
// would destroy a fact no store points at any more.
func TestMigrate_UndoNeverDeletesADeduplicatedRecord(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0DedupOK)
	f.mem.OnAdd = func(testutil.Mem0Post) {
		appendLine(t, filepath.Join(f.dir, store.IndexName),
			"- [Appended by a live session](fact1.md) "+testutil.EmDash+" written mid-run")
	}

	res := f.apply([]Decision{migrate("fact3.md")}, nil)

	if res.Status != StatusAbortedConcurrent {
		t.Fatalf("status = %q, want %q (note %s)", res.Status, StatusAbortedConcurrent, res.Note)
	}
	if got := f.mem.Deleted(); len(got) != 0 {
		t.Errorf("the undo deleted a pre-existing (dedup) record: %v", got)
	}
	if !f.exists("fact3.md") {
		t.Error("a file was deleted on an aborted run")
	}
	if !strings.Contains(strings.Join(res.Mem0Orphan, " "), "pre-existing") {
		t.Errorf("the record left in place must be reported: %v", res.Mem0Orphan)
	}
}
