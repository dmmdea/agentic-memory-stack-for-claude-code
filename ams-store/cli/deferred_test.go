package cli_test

// `sync --once` is the only thing on a PC that runs at SessionStart and at SessionEnd, so
// it is where the deferred queue has to be drained. Without a drain on that path the queue
// is written and never read: every change a live session was protected from - the judge's
// deletions and every other PC's edits - stays in deferred.json forever.

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/cli"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

func TestCLI_SyncDrainsTheDeferredQueueOnceTheSessionIsGone(t *testing.T) {
	testutil.RequireGit(t)
	hub := initBareHub(t)

	sessionStart := time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)
	facts := map[string]string{
		"doomed.md": testutil.FactFile("doomed", "migrated by the judge", "project", "doomed body"),
		"shared.md": testutil.FactFile("shared", "edited on both PCs", "project", body12("first", "middle", "last")),
	}
	index := []string{"# Memory Index", "", "- [doomed](doomed.md)", "- [shared](shared.md)"}

	other := testutil.NewSandbox(t)
	other.AddStore("ws", index, facts)
	attachHub(t, other, hub)
	syncAt(t, other, sessionStart, "the other PC seeds the hub")

	pc := testutil.NewSandbox(t)
	pc.AddStore("ws", index, facts)
	attachHub(t, pc, hub)
	syncAt(t, pc, sessionStart, "this PC joins the fleet")

	// The other PC migrates one fact away and edits another.
	if err := os.Remove(filepath.Join(other.ProjectsRoot, "ws", "memory", "doomed.md")); err != nil {
		t.Fatal(err)
	}
	writeAt(t, filepath.Join(other.ProjectsRoot, "ws", "memory", "shared.md"),
		testutil.FactFile("shared", "edited on both PCs", "project", body12("FROM-THE-OTHER-PC", "middle", "last")), sessionStart)
	syncAt(t, other, sessionStart.Add(time.Minute), "the other PC deletes one and edits one")

	// A session is live here: a transcript a minute old, and a fact file it just wrote.
	transcript := filepath.Join(pc.ProjectsRoot, "ws", "session.jsonl")
	writeAt(t, transcript, "{}", sessionStart.Add(-time.Minute))
	held := filepath.Join(pc.ProjectsRoot, "ws", "memory", "shared.md")
	writeAt(t, held, testutil.FactFile("shared", "edited on both PCs", "project", body12("first", "middle", "FROM-THE-SESSION")),
		sessionStart.Add(-30*time.Second))

	syncAt(t, pc, sessionStart.Add(2*time.Minute), "this PC syncs under a live session")

	queue, err := merge.LoadDeferred(pc.StateRoot, "ws")
	if err != nil {
		t.Fatal(err)
	}
	if len(queue.Entries) != 2 {
		t.Fatalf("the scenario needs both changes deferred, got %+v", queue.Entries)
	}
	doomed := filepath.Join(pc.ProjectsRoot, "ws", "memory", "doomed.md")
	if _, err := os.Stat(doomed); err != nil {
		t.Fatalf("the deferral must leave the file alone while the session runs: %v", err)
	}

	// The session writes once more before it ends, so the queued replacement is no longer
	// what the merge deferred against.
	writeAt(t, held, testutil.FactFile("shared", "edited on both PCs", "project", body12("first", "LATER-EDIT", "FROM-THE-SESSION")),
		sessionStart.Add(3*time.Minute))

	// An hour later the transcript is well outside the liveness window: SessionEnd, or the
	// next SessionStart, whichever runs first - both run `sync --once`.
	syncAt(t, pc, sessionStart.Add(time.Hour), "the session is over")

	if _, err := os.Stat(doomed); err == nil {
		t.Error("the queued DELETION was never applied: ApplyDeferred has no caller on the" +
			" sync path, so deferred.json is written and never read")
	}
	got := readFile(t, held)
	if !strings.Contains(got, "LATER-EDIT") {
		t.Errorf("the drain overwrote an edit made after the merge:\n%s", got)
	}
	if !strings.Contains(got, "FROM-THE-OTHER-PC") {
		t.Errorf("the drain never applied the queued replacement:\n%s", got)
	}
	queue, err = merge.LoadDeferred(pc.StateRoot, "ws")
	if err != nil {
		t.Fatal(err)
	}
	if len(queue.Entries) != 0 {
		t.Errorf("the queue must be empty once every entry is decided: %+v", queue.Entries)
	}
}

// TestCLI_SyncRefusesWhenTheDeferredQueueCannotBeRead is the fail-closed half: a queue
// that does not parse is not an empty queue. Reading it as one would stage the workspace
// as if nothing were pending, which re-commits every change the queue was holding.
func TestCLI_SyncRefusesWhenTheDeferredQueueCannotBeRead(t *testing.T) {
	testutil.RequireGit(t)
	sb := testutil.NewSandbox(t)
	sb.AddStore("ws", []string{"# Memory Index", "", "- [a](a.md)"},
		map[string]string{"a.md": testutil.FactFile("a", "d", "project", "body")})
	gitAt(t, filepath.Join(sb.StateRoot, "history.git"), sb.ProjectsRoot, "init", "-q", "-b", "main")

	corrupt := filepath.Join(sb.StateRoot, "ws", "deferred.json")
	if err := os.MkdirAll(filepath.Dir(corrupt), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(corrupt, []byte("{not json at all"), 0o644); err != nil {
		t.Fatal(err)
	}

	code, _, stderr := run(t, "sync", "--once",
		"--projects-root", sb.ProjectsRoot, "--state-root", sb.StateRoot,
		"--now", "2026-09-15T12:00:00Z", "--machine-id", "pc")
	if code != cli.ExitRefused {
		t.Fatalf("exit = %d, want %d (refused): a corrupt queue must never be read as empty."+
			" stderr: %s", code, cli.ExitRefused, stderr)
	}
	out, err := runGit(filepath.Join(sb.StateRoot, "history.git"), sb.ProjectsRoot, "log", "--oneline")
	if err == nil && strings.TrimSpace(out) != "" {
		t.Errorf("the pass committed although it could not read what was pending:\n%s", out)
	}
}

// ---------------------------------------------------------------------------
// the fixture: a bare hub and two PCs that sync against it
// ---------------------------------------------------------------------------

func initBareHub(t *testing.T) string {
	t.Helper()
	hub := filepath.Join(t.TempDir(), "hub.git")
	if out, err := exec.Command("git", "init", "-q", "--bare", "-b", "main", hub).CombinedOutput(); err != nil {
		t.Fatalf("init hub: %v\n%s", err, out)
	}
	return hub
}

func attachHub(t *testing.T, sb *testutil.Sandbox, hub string) {
	t.Helper()
	gitDir := filepath.Join(sb.StateRoot, "history.git")
	gitAt(t, gitDir, sb.ProjectsRoot, "init", "-q", "-b", "main")
	gitAt(t, gitDir, sb.ProjectsRoot, "remote", "add", "hub", hub)
}

// syncAt runs one `sync --once` pass at an injected clock. The clock is what the liveness
// probe reads, so a test can put a session on either side of the window without waiting.
func syncAt(t *testing.T, sb *testutil.Sandbox, now time.Time, what string) {
	t.Helper()
	code, _, stderr := run(t, "sync", "--once", "--allow-local-path",
		"--projects-root", sb.ProjectsRoot, "--state-root", sb.StateRoot,
		"--now", now.UTC().Format(time.RFC3339), "--machine-id", filepath.Base(sb.Root))
	if code != cli.ExitOK {
		t.Fatalf("%s: sync exit = %d: %s", what, code, stderr)
	}
}

// writeAt writes a file and pins its mtime, which is the only signal the live-session
// guard has.
func writeAt(t *testing.T, path, content string, when time.Time) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, when, when); err != nil {
		t.Fatal(err)
	}
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// runGit is gitAt without the fatal: some assertions are about a command FAILING.
func runGit(gitDir, workTree string, args ...string) (string, error) {
	full := append([]string{"--git-dir=" + gitDir, "--work-tree=" + workTree}, args...)
	out, err := exec.Command("git", full...).CombinedOutput()
	return string(out), err
}

// body12 is a twelve-line body with three editable slots. Two PCs editing opposite ends of
// it merge CLEA\nY, so the test measures the deferral and the drain rather than the
// body-conflict winner rule.
func body12(first, mid, last string) string {
	lines := []string{first}
	for i := 2; i <= 11; i++ {
		if i == 6 {
			lines = append(lines, mid)
			continue
		}
		lines = append(lines, fmt.Sprintf("body line %d", i))
	}
	return strings.Join(append(lines, last), "\n")
}
