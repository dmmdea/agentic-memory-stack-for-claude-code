package judge

import (
	"context"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// stampedFact is a fact file that carries the `migrated: <id>` stamp derive writes onto a
// slug the history says was migrated before: the same fact, written again.
func stampedFact(name, id string) string {
	return strings.Replace(factFile(name, "detail", "project", "body"),
		"description:", "migrated: "+id+"\ndescription:", 1)
}

// The stamp is consumed: a re-created slug updates the record it already has instead of
// adding a near-duplicate variant every night.
func TestMigrate_StampedSlugUpdatesTheExistingRecordByID(t *testing.T) {
	lines, facts := bigStore(60)
	facts["fact3.md"] = stampedFact("Fact 3", "seed-0001")
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	f.mem.SeedWithSource("seed-0001", "the stale text an earlier night migrated", SourceTag("ws", "fact3.md"))

	fm := frontmatter.ParseFile(filepath.Join(f.dir, "fact3.md"))
	want := MigrationText(fm.Description, fm.Body)

	res := f.apply([]Decision{migrate("fact3.md")}, nil)

	if res.Migrated != 1 || res.Updated != 1 {
		t.Fatalf("migrated=%d updated=%d, want 1 and 1 (note: %s, orphans: %v)", res.Migrated, res.Updated, res.Note, res.Mem0Orphan)
	}
	if puts := f.mem.Puts(); len(puts) != 1 || puts[0].ID != "seed-0001" {
		t.Fatalf("puts = %+v, want exactly one PUT to seed-0001", puts)
	}
	if posts := f.mem.Posts(); len(posts) != 0 {
		t.Fatalf("posts = %+v, want none: an add would fork a second record for the same slug", posts)
	}
	if got, _ := f.mem.Text("seed-0001"); got != want {
		t.Errorf("record text = %q, want the fact's migration text %q", got, want)
	}
	if f.exists("fact3.md") {
		t.Error("the fact file was not removed after a verified update")
	}
	if got := strings.Join(res.Mem0, "\n"); !strings.Contains(got, "seed-0001 | fact3.md") {
		t.Errorf("the receipt must map the slug to the updated id: %v", res.Mem0)
	}
	if len(f.mem.Deleted()) != 0 {
		t.Errorf("an updated record must never be deleted: %v", f.mem.Deleted())
	}
}

// A stamp whose record is gone (deleted by an operator, retired) falls back to Add.
func TestMigrate_StampedSlugWhoseRecordIsGoneIsAdded(t *testing.T) {
	lines, facts := bigStore(60)
	facts["fact3.md"] = stampedFact("Fact 3", "no-such-record")
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)

	res := f.apply([]Decision{migrate("fact3.md")}, nil)

	if res.Migrated != 1 || res.Updated != 0 {
		t.Fatalf("migrated=%d updated=%d, want 1 and 0", res.Migrated, res.Updated)
	}
	if len(f.mem.Puts()) != 0 || len(f.mem.Posts()) != 1 {
		t.Errorf("puts=%d posts=%d, want the Add path: 0 PUT and 1 POST", len(f.mem.Puts()), len(f.mem.Posts()))
	}
}

// An update the corpus refuses keeps the line and does NOT fall back to Add: a refused
// update followed by an add is the fork this change exists to stop.
func TestMigrate_RefusedUpdateKeepsTheLineAndDoesNotFork(t *testing.T) {
	lines, facts := bigStore(60)
	facts["fact3.md"] = stampedFact("Fact 3", "seed-0001")
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	f.mem.SeedWithSource("seed-0001", "stale", SourceTag("ws", "fact3.md"))
	f.mem.PutStatus = 500

	res := f.apply([]Decision{migrate("fact3.md")}, nil)

	if res.Migrated != 0 || res.AddFailed != 1 {
		t.Fatalf("migrated=%d add_failed=%d, want 0 and 1", res.Migrated, res.AddFailed)
	}
	if len(f.mem.Posts()) != 0 {
		t.Errorf("posts = %+v, want none after a refused update", f.mem.Posts())
	}
	if !f.exists("fact3.md") {
		t.Error("the file was deleted although the update did not land")
	}
	if !strings.Contains(strings.Join(res.Mem0Orphan, " "), "fact3.md") {
		t.Errorf("the failure must be reported: %v", res.Mem0Orphan)
	}
}

// The stamp is a claim, not proof: the id may be a pre-existing record the judge never
// created (a server-deduplicated Add keeps the older record's id, and derive stamps it).
// An update PUTs new text over whatever the id names, so the record must carry THIS
// slug's own source tag; anything else is left untouched and the fact is added as its own
// record.
func TestMigrate_StampedIDOfAnotherSourceIsNeverOverwritten(t *testing.T) {
	for name, source := range map[string]string{
		"an operator fact with no source":   "",
		"another slug's record":             SourceTag("ws", "other-slug.md"),
		"another workspace's same slug":     SourceTag("other-ws", "fact3.md"),
		"an unrelated writer's source tag":  "user-direct",
		"a tag that only extends this slug": "automemory:ws/fact3.md.bak",
	} {
		t.Run(name, func(t *testing.T) {
			lines, facts := bigStore(60)
			facts["fact3.md"] = stampedFact("Fact 3", "seed-0001")
			f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
			const foreign = "an evidence-tier operator fact the judge did not write"
			f.mem.SeedWithSource("seed-0001", foreign, source)

			res := f.apply([]Decision{migrate("fact3.md")}, nil)

			if puts := f.mem.Puts(); len(puts) != 0 {
				t.Fatalf("puts = %+v: a record of another source was overwritten", puts)
			}
			if got, _ := f.mem.Text("seed-0001"); got != foreign {
				t.Fatalf("the foreign record now reads %q", got)
			}
			if res.Migrated != 1 || res.Updated != 0 {
				t.Fatalf("migrated=%d updated=%d, want 1 and 0: the fact is added as its own record (note: %s, orphans: %v)",
					res.Migrated, res.Updated, res.Note, res.Mem0Orphan)
			}
			posts := f.mem.Posts()
			if len(posts) != 1 || posts[0].Source != SourceTag("ws", "fact3.md") {
				t.Errorf("posts = %+v, want one add carrying this slug's source tag", posts)
			}
			if len(f.mem.Deleted()) != 0 {
				t.Errorf("nothing may be deleted: %v", f.mem.Deleted())
			}
		})
	}
}

// A write the corpus accepted (it returned an id) but that failed its read-back is a
// failed write: a night in which every write does that must not read as a quiet night.
func TestMigrate_WriteThatFailsVerificationCountsAsAddFailed(t *testing.T) {
	cases := []struct {
		name  string
		mode  testutil.Mem0Mode
		setup func(*testutil.FakeMem0)
	}{
		{"mismatching read-back, record removed", testutil.Mem0Mismatch, nil},
		{"mismatching read-back, record not removable", testutil.Mem0Mismatch, func(m *testutil.FakeMem0) { m.DeleteStatus = 500 }},
		{"dedup record whose read-back failed", testutil.Mem0DedupMismatch, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			lines, facts := bigStore(60)
			f := newFixture(t, "ws", lines, facts, tc.mode)
			if tc.setup != nil {
				tc.setup(f.mem)
			}
			res := f.apply([]Decision{migrate("fact3.md")}, nil)
			if res.Migrated != 0 {
				t.Fatalf("migrated = %d, want 0", res.Migrated)
			}
			if res.AddFailed != 1 {
				t.Errorf("add_failed = %d, want 1: an unverified write must be counted (orphans: %v)", res.AddFailed, res.Mem0Orphan)
			}
			if !f.exists("fact3.md") {
				t.Error("the fact file was deleted against an unverified write")
			}
		})
	}
}

// Get reports the record's source tag, from the top-level field the server returns or,
// failing that, from the metadata map.
func TestHTTPMem0_GetReadsTheSourceTag(t *testing.T) {
	fake := testutil.NewFakeMem0(t, testutil.Mem0OK)
	fake.SeedWithSource("tagged", "text", "automemory:ws/a.md")
	fake.Seed("bare", "text")
	c := &HTTPMem0{BaseURL: fake.URL(), UserID: "u"}
	if rec, err := c.Get(context.Background(), "tagged"); err != nil || rec.Source != "automemory:ws/a.md" {
		t.Errorf("tagged: source=%q err=%v, want automemory:ws/a.md", rec.Source, err)
	}
	if rec, err := c.Get(context.Background(), "bare"); err != nil || rec.Source != "" {
		t.Errorf("bare: source=%q err=%v, want empty", rec.Source, err)
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"memory":"t","metadata":{"source":"automemory:ws/b.md"}}`))
	}))
	defer srv.Close()
	c2 := &HTTPMem0{BaseURL: srv.URL, UserID: "u"}
	if rec, err := c2.Get(context.Background(), "x"); err != nil || rec.Source != "automemory:ws/b.md" {
		t.Errorf("metadata fallback: source=%q err=%v", rec.Source, err)
	}
}
