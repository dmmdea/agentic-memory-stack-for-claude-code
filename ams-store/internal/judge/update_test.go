package judge

import (
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
	f.mem.Seed("seed-0001", "the stale text an earlier night migrated")

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
	f.mem.Seed("seed-0001", "stale")
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
