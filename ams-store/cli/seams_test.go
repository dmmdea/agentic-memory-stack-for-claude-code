package cli_test

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/cli"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lint"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// Every engine in this module declares its neighbour as a one-method interface and is
// unit-tested against a fake. That is what keeps the packages independent, and it is also
// what makes a DISCONNECTED seam invisible: `Floor: nil` compiles, ships, passes every
// unit test in internal/gate, and silently turns the write gate into an advisory printer
// on every PC in the fleet.
//
// So these tests drive the CLI end to end with the real pair on both sides. Each one was
// seen RED against the unwired build before the adapter in cli/seams.go was connected.

func gitAt(t *testing.T, gitDir, workTree string, args ...string) string {
	t.Helper()
	full := append([]string{"--git-dir=" + gitDir, "--work-tree=" + workTree}, args...)
	out, err := exec.Command("git", full...).CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return string(out)
}

// ---------------------------------------------------------------------------
// gate -> derive's floor
// ---------------------------------------------------------------------------

// TestSeam_GateFloorsThroughDeriveNotAdvisoryOnly is the seam whose absence is hardest to
// notice: an unwired gate prints exactly the same advisory block it prints when wired, and
// only the file on disk tells the two apart.
func TestSeam_GateFloorsThroughDeriveNotAdvisoryOnly(t *testing.T) {
	sb := testutil.NewSandbox(t)
	// 70 long non-doctrine lines push the index past the 25,000 B sync limit, which is
	// where the gate is allowed to rewrite.
	dir := sb.AddStore("ws", testutil.BigIndex(70), testutil.BigIndexFacts(70))
	idx := filepath.Join(dir, store.IndexName)

	before, err := os.ReadFile(idx)
	if err != nil {
		t.Fatal(err)
	}
	if len(before) < store.SyncLimitBytes {
		t.Fatalf("fixture is %d B, need >= %d to reach the gate's rewrite path", len(before), store.SyncLimitBytes)
	}

	payload := `{"tool_input":{"file_path":` + mustJSON(t, idx) + `}}`
	code, stdout, _ := runStdin(t, payload, "gate",
		"--state-root", sb.StateRoot, "--projects-root", sb.ProjectsRoot,
		"--now", "2026-09-15T12:00:00Z")

	if code != cli.ExitOK {
		t.Fatalf("gate exit = %d, want 0 - the gate is fail-open by contract", code)
	}
	after, err := os.ReadFile(idx)
	if err != nil {
		t.Fatal(err)
	}
	if len(after) >= len(before) {
		t.Fatalf("the gate did not floor the index: %d B before, %d B after."+
			" A nil Floorer makes the gate advise and never mutate, which is exactly what"+
			" this seam exists to prevent", len(before), len(after))
	}
	if len(after) >= store.TriggerBytes {
		t.Errorf("floored to %d B, want under the trigger (%d B): the gate must leave the"+
			" store out of the nightly candidate set, not one edit inside it", len(after), store.TriggerBytes)
	}
	if !strings.Contains(stdout, "NORMALIZED") {
		t.Errorf("stdout did not report the normalization:\n%s", stdout)
	}
}

// ---------------------------------------------------------------------------
// derive -> the per-PC lock
// ---------------------------------------------------------------------------

// TestSeam_DeriveSkipsWhenTheLockIsHeld pins decision Q9's contender rule at the verb.
// derive.Options.Lock nil means derive runs UNLOCKED: two derives on one PC would then
// race, and the compare-and-swap would turn one of them into an abort instead of a skip.
func TestSeam_DeriveSkipsWhenTheLockIsHeld(t *testing.T) {
	sb := sandboxStore(t, plainIndex(), plainFacts())

	// Take the lock as this process. A contender sees a live PID whose start time
	// matches, which is exactly the "another process is working" state.
	held, err := lock.Acquire(lock.Options{
		Path:   filepath.Join(sb.StateRoot, lock.FileName),
		Reason: "test",
		Now:    time.Now(),
		// The named mutexes are per-process handles, so a second Acquire inside this same
		// process would pass them; disabling both leaves the PID+start-time file as the
		// one thing under test, which is the portable half of the rule anyway.
		MutexName:       "-",
		LegacyMutexName: "-",
	})
	if err != nil {
		t.Fatalf("staging the lock: %v", err)
	}
	defer func() { _ = held.Release() }()

	code, _, stderr := run(t, deriveArgs(sb)...)
	if code != cli.ExitLocked {
		t.Fatalf("derive exit = %d, want %d (lock held). Unwired, derive runs anyway and"+
			" the second writer's only protection is the compare-and-swap.\nstderr: %s",
			code, cli.ExitLocked, stderr)
	}
}

// ---------------------------------------------------------------------------
// derive -> the judge's Migrated: trailer (decision Q8)
// ---------------------------------------------------------------------------

// TestSeam_HarvestStampsMigratedFromTheJudgesTrailer drives the real lookup over a real
// deletion commit. Unwired (Migrated nil) the harvest simply never stamps, and every
// re-created slug becomes a fresh corpus record on the next nightly.
func TestSeam_HarvestStampsMigratedFromTheJudgesTrailer(t *testing.T) {
	testutil.RequireGit(t)
	sb := testutil.NewSandbox(t)
	dir := sb.AddStore("ws", []string{
		"# Memory Index",
		"",
		"- [Recreated](recreated.md) " + emDash + " the slug came back",
	}, map[string]string{
		"recreated.md": testutil.FactFile("recreated", "came back", "project", "body"),
	})
	gitDir := sb.InitHistory()

	// The judge's deletion commit is the only artifact that outlives the deleted file.
	seed := filepath.Join(sb.ProjectsRoot, "ws", "memory", "placeholder.md")
	if err := os.WriteFile(seed, []byte("seed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitAt(t, gitDir, sb.ProjectsRoot, "add", "-A", "-f", "--", "ws/memory")
	gitAt(t, gitDir, sb.ProjectsRoot, "commit", "-q", "-m", "seed")
	if err := os.Remove(seed); err != nil {
		t.Fatal(err)
	}
	gitAt(t, gitDir, sb.ProjectsRoot, "add", "-A", "-f", "--", "ws/memory")
	gitAt(t, gitDir, sb.ProjectsRoot, "commit", "-q", "-m",
		"judge ws: 1 migration\n\nMigrated: recreated.md mem0-abc123\n")

	code, _, stderr := run(t, "harvest", "--all",
		"--projects-root", sb.ProjectsRoot, "--state-root", sb.StateRoot,
		"--now", "2026-09-15T12:00:00Z")
	if code != cli.ExitOK {
		t.Fatalf("harvest exit = %d: %s", code, stderr)
	}

	got, err := os.ReadFile(filepath.Join(dir, "recreated.md"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), "migrated: mem0-abc123") {
		t.Fatalf("harvest did not stamp the id from the deletion commit's trailer:\n%s", got)
	}
}

// ---------------------------------------------------------------------------
// sync -> merge
// ---------------------------------------------------------------------------

// TestSeam_SyncMergesAnotherPCsFactThroughTheMergeEngine is the whole point of the fleet:
// two PCs, one hub, and a fact written on B appearing on A. With a nil Merger, sync
// refuses the moment a fetch finds new commits - correct, and useless.
func TestSeam_SyncMergesAnotherPCsFactThroughTheMergeEngine(t *testing.T) {
	testutil.RequireGit(t)
	hub := filepath.Join(t.TempDir(), "hub.git")
	out, err := exec.Command("git", "init", "-q", "--bare", "-b", "main", hub).CombinedOutput()
	if err != nil {
		t.Fatalf("init hub: %v\n%s", err, out)
	}

	// PC B writes a fact and pushes it.
	b := testutil.NewSandbox(t)
	b.AddStore("ws", []string{
		"# Memory Index",
		"",
		"- [FromB](fromb.md) " + emDash + " written on the other PC",
	}, map[string]string{"fromb.md": testutil.FactFile("fromb", "from B", "project", "b body")})
	syncArgs := func(sb *testutil.Sandbox) []string {
		return []string{"sync", "--once", "--allow-local-path",
			"--projects-root", sb.ProjectsRoot, "--state-root", sb.StateRoot,
			"--now", "2026-09-15T12:00:00Z", "--machine-id", filepath.Base(sb.Root)}
	}
	gitAt(t, filepath.Join(b.StateRoot, "history.git"), b.ProjectsRoot, "init", "-q", "-b", "main")
	gitAt(t, filepath.Join(b.StateRoot, "history.git"), b.ProjectsRoot, "remote", "add", "hub", hub)
	if code, _, stderr := run(t, syncArgs(b)...); code != cli.ExitOK {
		t.Fatalf("B sync exit = %d: %s", code, stderr)
	}

	// PC A has its own store and has never seen B.
	a := testutil.NewSandbox(t)
	a.AddStore("ws", []string{
		"# Memory Index",
		"",
		"- [FromA](froma.md) " + emDash + " written here",
	}, map[string]string{"froma.md": testutil.FactFile("froma", "from A", "project", "a body")})
	gitAt(t, filepath.Join(a.StateRoot, "history.git"), a.ProjectsRoot, "init", "-q", "-b", "main")
	gitAt(t, filepath.Join(a.StateRoot, "history.git"), a.ProjectsRoot, "remote", "add", "hub", hub)
	if code, _, stderr := run(t, syncArgs(a)...); code != cli.ExitOK {
		t.Fatalf("A sync exit = %d: %s", code, stderr)
	}

	bFactOnA := filepath.Join(a.ProjectsRoot, "ws", "memory", "fromb.md")
	if _, err := os.Stat(bFactOnA); err != nil {
		t.Fatalf("B's fact never reached A's work tree: %v."+
			" A nil Merger makes sync refuse a fetch that found new commits, so nothing"+
			" is ever materialized", err)
	}
	// MEMORY.md is DERIVED, never merged: the re-derive after the merge is what puts the
	// new pointer in the index, and it is the seam's other half.
	idx, err := os.ReadFile(filepath.Join(a.ProjectsRoot, "ws", "memory", store.IndexName))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(idx), "fromb.md") {
		t.Errorf("A's index was not re-derived after the merge:\n%s", idx)
	}
	if !strings.Contains(string(idx), "froma.md") {
		t.Errorf("A's own entry was lost by the merge:\n%s", idx)
	}
}

// ---------------------------------------------------------------------------
// judge -> derive's renderer
// ---------------------------------------------------------------------------

// TestSeam_JudgeApplyWritesTheDerivedIndex closes the judge builder's first open issue.
// Options.RenderIndex defaults to the VERBATIM regenerator; the judge's projected-index
// guard then measures bytes nobody will ever write, and the index it leaves behind is in
// a different order from the one the next derive produces - a whole-file diff on the next
// sync for every store the judge touched.
func TestSeam_JudgeApplyWritesTheDerivedIndex(t *testing.T) {
	sb := testutil.NewSandbox(t)
	// Doctrine is LAST in the file. A verbatim regenerate keeps it last; the derived
	// render puts it first, so the byte order of the file the judge writes is the whole
	// assertion. The plan carries one genuine shortening, because a judge with nothing to
	// apply writes nothing at all and would prove nothing either way.
	longHook := "the service listens on PORT 8080 and the detail here is deliberately long enough to be worth shortening"
	dir := sb.AddStore("ws", []string{
		"# Memory Index",
		"",
		"- [Plain](plain.md) " + emDash + " " + longHook,
		"- [Rule](rule.md) " + emDash + " NEVER do the thing",
	}, map[string]string{
		"plain.md": testutil.FactFile("plain", "ordinary", "project", "plain body"),
		"rule.md":  testutil.FactFile("rule", "a standing order", "feedback", "rule body"),
	})
	if err := os.WriteFile(filepath.Join(sb.StateRoot, "role"), []byte("hub\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	plan := writePlan(t, sb.Root, `{"version":1,"stores":[{"workspace":"ws","decisions":[`+
		`{"slug":"plain.md","verb":"SHORTEN","new_hook":"listens on PORT 8080"}]}]}`)

	code, _, stderr := run(t, "judge-apply", "--plan", plan, "--store", dir,
		"--state-root", sb.StateRoot, "--projects-root", sb.ProjectsRoot,
		"--now", "2026-09-15T12:00:00Z")
	if code != cli.ExitOK {
		t.Fatalf("judge-apply exit = %d: %s", code, stderr)
	}

	text, err := os.ReadFile(filepath.Join(dir, store.IndexName))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(text), "listens on PORT 8080") {
		t.Fatalf("the shortening was not applied, so nothing was rendered:\n%s", text)
	}
	plainAt := strings.Index(string(text), "plain.md")
	ruleAt := strings.Index(string(text), "rule.md")
	if ruleAt < 0 || plainAt < 0 {
		t.Fatalf("an entry disappeared:\n%s", text)
	}
	if ruleAt > plainAt {
		t.Fatalf("judge-apply wrote a VERBATIM index (doctrine still last):\n%s\n"+
			"It must render through derive's renderer, or its strict-decrease guard"+
			" measures a file derive will never write", text)
	}
}

// ---------------------------------------------------------------------------
// the maintenance path -> the G7 over-trigger clock (decision Q13)
// ---------------------------------------------------------------------------

// TestSeam_DeriveStampsTheOverTriggerClock.
//
// G7 is "hours over trigger without an applied decision", and it is the one number a skip
// cannot satisfy: receipt age says the maintainer RAN, this says the store got BETTER.
// It is computed from a stamp that lint only reads. Nothing on the maintenance path wrote
// it, so stores[].over_trigger_hours was null on every PC and the 24 h alarm could not
// fire in production at all.
func TestSeam_DeriveStampsTheOverTriggerClock(t *testing.T) {
	sb := testutil.NewSandbox(t)
	// Over the 20,000 B trigger but under the 25,000 B sync limit, so the Phase 3 floor
	// leaves it alone and the store STAYS over the trigger - which is the state the clock
	// is about.
	sb.AddStore("ws", testutil.BigIndex(62), testutil.BigIndexFacts(62))

	code, _, stderr := run(t, "derive", "--all",
		"--projects-root", sb.ProjectsRoot, "--state-root", sb.StateRoot,
		"--now", "2026-09-15T12:00:00Z")
	if code != cli.ExitOK {
		t.Fatalf("derive exit = %d: %s", code, stderr)
	}

	stamps, err := lint.ReadStamps(sb.ProjectsRoot)
	if err != nil {
		t.Fatal(err)
	}
	since, ok := stamps.OverTriggerSince["ws"]
	if !ok {
		t.Fatalf("no over-trigger stamp was written; G7 cannot fire on this PC")
	}
	if !since.Equal(time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)) {
		t.Errorf("stamp = %s, want the injected clock", since)
	}

	// A second run a day later must NOT reset the clock: the earliest crossing is the
	// truth, or a store that is checked daily isnever more than a day over trigger.
	code, _, stderr = run(t, "derive", "--all",
		"--projects-root", sb.ProjectsRoot, "--state-root", sb.StateRoot,
		"--now", "2026-09-16T12:00:00Z")
	if code != cli.ExitOK {
		t.Fatalf("second derive exit = %d: %s", code, stderr)
	}
	stamps, err = lint.ReadStamps(sb.ProjectsRoot)
	if err != nil {
		t.Fatal(err)
	}
	if got := stamps.OverTriggerSince["ws"]; !got.Equal(since) {
		t.Errorf("the clock was reset to %s; the earliest crossing is the truth", got)
	}

	// Once the store is back under the trigger the stamp goes, so the next crossing
	// starts a fresh clock rather than reporting a debt that was already paid.
	small := sb.AddStore("ws", []string{"# Memory Index", "", "- [A](a.md) " + emDash + " small"},
		map[string]string{"a.md": testutil.FactFile("a", "a", "project", "body")})
	_ = small
	code, _, stderr = run(t, "derive", "--all",
		"--projects-root", sb.ProjectsRoot, "--state-root", sb.StateRoot,
		"--now", "2026-09-17T12:00:00Z")
	if code != cli.ExitOK {
		t.Fatalf("third derive exit = %d: %s", code, stderr)
	}
	stamps, err = lint.ReadStamps(sb.ProjectsRoot)
	if err != nil {
		t.Fatal(err)
	}
	if _, still := stamps.OverTriggerSince["ws"]; still {
		t.Errorf("the stamp survived the store falling back under the trigger")
	}
}

func mustJSON(t *testing.T, s string) string {
	t.Helper()
	b, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}
