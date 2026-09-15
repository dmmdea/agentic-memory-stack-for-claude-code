package judge

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// The trailer is the Q8 producer: the mapping slug -> mem0 id has to survive the deletion
// of the file it belongs to, and the deletion COMMIT is the one artifact that does and
// that every PC already has after a sync.
func TestMigrated_TrailerRoundTripsThroughHistory(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	gitDir := f.sb.InitHistory()
	repo := HistoryRepo{GitDir: gitDir, WorkTree: f.sb.ProjectsRoot}

	// A first commit so the store's files are in history before one of them is removed.
	gitRun(t, gitDir, f.sb.ProjectsRoot, "add", "-A", "-f", "--", "ws/memory")
	gitRun(t, gitDir, f.sb.ProjectsRoot, "commit", "-q", "-m", "seed")

	res := f.apply([]Decision{migrate("fact3.md")}, func(o *Options) { o.History = repo })
	if res.Migrated != 1 {
		t.Fatalf("migrated = %d, want 1 (%v)", res.Migrated, res.Mem0Orphan)
	}
	if res.Commit == "" {
		t.Fatal("the deletion was not committed, so the mapping did not survive the file")
	}

	id, ok := MigratedFor(repo, "fact3.md")
	if !ok {
		t.Fatal("the migration is not findable by slug; a re-created slug would gain a nightly variant")
	}
	if id != "stub-id-0001" {
		t.Errorf("MigratedFor = %q, want the id the migration used", id)
	}
	if _, ok := MigratedFor(repo, "fact4.md"); ok {
		t.Error("a slug that was never migrated must not resolve")
	}

	// The concrete type derive's harvest step takes.
	var lookup interface {
		MigratedFor(string) (string, bool)
	} = HistoryMigrated{Repo: repo}
	if got, ok := lookup.MigratedFor("fact3.md"); !ok || got != id {
		t.Errorf("HistoryMigrated.MigratedFor = %q,%v; want %q,true", got, ok, id)
	}

	// The commit body must still read as a commit, not only as a trailer block.
	body := gitRun(t, gitDir, f.sb.ProjectsRoot, "log", "-1", "--format=%B")
	if !strings.Contains(body, "judge ws:") {
		t.Errorf("the commit lost its subject line:\n%s", body)
	}
	if !strings.Contains(body, MigratedTrailer("fact3.md", "stub-id-0001")) {
		t.Errorf("the trailer is not in the commit body:\n%s", body)
	}
}

// Fails closed: no repo, a malformed slug, or a repo with no such commit all answer "no
// known migration". A wrong id written into a fact file would make the judge update
// somebody else's record.
func TestMigrated_LookupFailsClosed(t *testing.T) {
	if _, ok := MigratedFor(HistoryRepo{}, "a.md"); ok {
		t.Error("an unconfigured repo answered a lookup")
	}
	if _, ok := MigratedFor(HistoryRepo{GitDir: filepath.Join(t.TempDir(), "nope.git")}, "a.md"); ok {
		t.Error("a missing repo answered a lookup")
	}
	if _, ok := MigratedFor(HistoryRepo{GitDir: t.TempDir()}, "not a slug"); ok {
		t.Error("a malformed slug answered a lookup")
	}
}

func TestMigrated_CommitMessageShape(t *testing.T) {
	got := CommitMessage("judge ws: 2 migrated", []Migration{
		{Slug: "a.md", Mem0ID: "id-a"}, {Slug: "b.md", Mem0ID: "id-b"},
	})
	want := "judge ws: 2 migrated\n\nMigrated: a.md id-a\nMigrated: b.md id-b\n"
	if got != want {
		t.Errorf("commit message =\n%q\nwant\n%q", got, want)
	}
	if bare := CommitMessage("subject", nil); bare != "subject\n" {
		t.Errorf("a commit with no migrations = %q", bare)
	}
}

// The client speaks the wire protocol the authority really serves: infer=false, the
// results[0].id shape, the X-API-Key header, and retrievable=false as a refusal.
func TestMem0Client_WriteReadBackAndRefusals(t *testing.T) {
	ctx := context.Background()

	fake := testutil.NewFakeMem0(t, testutil.Mem0OK)
	c := &HTTPMem0{BaseURL: fake.URL(), APIKey: "k", UserID: "u"}
	add, err := c.Add(ctx, "the fact", SourceTag("ws", "a.md"), map[string]string{"workspace": "ws"})
	if err != nil {
		t.Fatalf("Add: %v", err)
	}
	if add.ID == "" || add.Deduplicated {
		t.Fatalf("Add = %+v", add)
	}
	rec, err := c.Get(ctx, add.ID)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if !Landed(rec, "the fact") {
		t.Errorf("a byte-equal read-back did not land: %+v", rec)
	}
	if Landed(rec, "the fact ") {
		t.Error("a read-back that differs by one byte must not land")
	}
	if missing, err := c.Get(ctx, "no-such-id"); err != nil || missing.Found {
		t.Errorf("a missing record must be found=false and no error: %+v %v", missing, err)
	}
	if err := c.Delete(ctx, add.ID); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	if got := fake.Deleted(); len(got) != 1 {
		t.Errorf("deleted = %v", got)
	}

	unret := testutil.NewFakeMem0(t, testutil.Mem0NotRetrievable)
	c2 := &HTTPMem0{BaseURL: unret.URL(), UserID: "u"}
	a2, err := c2.Add(ctx, "the fact", SourceTag("ws", "a.md"), nil)
	if err != nil {
		t.Fatal(err)
	}
	r2, err := c2.Get(ctx, a2.ID)
	if err != nil {
		t.Fatal(err)
	}
	if Landed(r2, "the fact") {
		t.Error("a record the server calls unreachable is not a place a fact may be moved to")
	}

	noid := testutil.NewFakeMem0(t, testutil.Mem0NoID)
	c3 := &HTTPMem0{BaseURL: noid.URL(), UserID: "u"}
	if _, err := c3.Add(ctx, "the fact", SourceTag("ws", "a.md"), nil); err == nil {
		t.Error("an answer with no id must be an error, never a silent success")
	}
}

func TestMem0Client_MigrationTextIsVerbatim(t *testing.T) {
	got := MigrationText("  the description  ", "the body\nmore body\n")
	want := "the description  \n\nthe body\nmore body"
	if got != want {
		t.Errorf("MigrationText = %q, want %q - a paraphrase defeats hash dedup on retry", got, want)
	}
	if TooLargeToMigrate(strings.Repeat("a", 4000)) {
		t.Error("a body exactly at the cap must be migratable")
	}
	if !TooLargeToMigrate(strings.Repeat("a", 4001)) {
		t.Error("a body over the cap must be refused locally rather than 413 nightly")
	}
}

func gitRun(t *testing.T, gitDir, workTree string, args ...string) string {
	t.Helper()
	full := append([]string{"--git-dir=" + gitDir, "--work-tree=" + workTree}, args...)
	cmd := exec.Command("git", full...)
	cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return string(out)
}
