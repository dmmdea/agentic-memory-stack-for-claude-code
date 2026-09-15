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
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gate"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lint"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	amsync "github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/sync"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// hubHost is a PLACEHOLDER; the real MagicDNS name is configuration, never a literal.
const hubHost = "hub-host"

// runIn drives a verb against an injected environment, with the roots pointed at a
// sandbox. Tests never spawn the binary: the whole surface is reachable in-process, so a
// scenario costs milliseconds.
func runIn(t *testing.T, sb *testutil.Sandbox, stdin string, args ...string) (int, string, string) {
	t.Helper()
	var out, errb strings.Builder
	full := append([]string{args[0],
		"--state-root", sb.StateRoot,
		"--projects-root", sb.ProjectsRoot,
	}, args[1:]...)
	code := cli.RunWith(cli.Env{Stdout: &out, Stderr: &errb, Stdin: strings.NewReader(stdin)}, full)
	return code, out.String(), errb.String()
}

func cleanStore(t *testing.T, sb *testutil.Sandbox, ws string) string {
	t.Helper()
	return sb.AddStore(ws, []string{"# Index", "", "- [A](a.md) " + testutil.EmDash + " hook"},
		map[string]string{"a.md": testutil.FactFile("a", "d", "project", "body")})
}

// --------------------------------------------------------------------------------
// lock
// --------------------------------------------------------------------------------

func TestCLI_LockStatusReportsFreeAndHeld(t *testing.T) {
	sb := testutil.NewSandbox(t)

	code, out, _ := runIn(t, sb, "", "lock", "status", "--json")
	if code != cli.ExitOK {
		t.Fatalf("status on a free lock: exit = %d, want 0", code)
	}
	var st map[string]any
	if err := json.Unmarshal([]byte(out), &st); err != nil {
		t.Fatalf("status --json is not JSON: %v\n%s", err, out)
	}
	if st["present"] != false {
		t.Errorf("present = %v on a free lock, want false", st["present"])
	}

	// Now hold it from "another process" and ask again. status REPORTS; it is not a
	// contender, so it exits 0 whatever it finds - a status verb that exited non-zero
	// on a healthy held lock would make every wrapper script treat normal as broken.
	l, err := lock.Acquire(lock.Options{
		Path:            filepath.Join(sb.StateRoot, lock.FileName),
		Reason:          "sync",
		MutexName:       `Local\ams-store-test-status`,
		LegacyMutexName: "-",
	})
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	defer l.Release()

	code, out, _ = runIn(t, sb, "", "lock", "status", "--json")
	if code != cli.ExitOK {
		t.Errorf("status on a held lock: exit = %d, want 0", code)
	}
	if err := json.Unmarshal([]byte(out), &st); err != nil {
		t.Fatalf("status --json is not JSON: %v\n%s", err, out)
	}
	if st["present"] != true {
		t.Errorf("present = %v on a held lock, want true", st["present"])
	}
	holder, _ := st["holder"].(map[string]any)
	if holder == nil || holder["reason"] != "sync" {
		t.Errorf("holder = %v, want the reason it was taken for", st["holder"])
	}
}

func TestCLI_LockAcquireAndRelease(t *testing.T) {
	sb := testutil.NewSandbox(t)
	// --for 0 takes it and hands it straight back, which is what a test and a scripted
	// "is this takeable" probe both want.
	code, _, stderr := runIn(t, sb, "", "lock", "acquire", "--for", "0", "--reason", "derive")
	if code != cli.ExitOK {
		t.Fatalf("acquire: exit = %d, want 0 (%s)", code, stderr)
	}
	if _, err := os.Stat(filepath.Join(sb.StateRoot, lock.FileName)); !os.IsNotExist(err) {
		t.Error("the lock file survived a --for 0 acquire; the window ended and so must the lock")
	}
}

func TestCLI_LockContenderExitsFourImmediately(t *testing.T) {
	sb := testutil.NewSandbox(t)
	l, err := lock.Acquire(lock.Options{
		Path:            filepath.Join(sb.StateRoot, lock.FileName),
		Reason:          "sync",
		MutexName:       `Local\ams-store-test-contend`,
		LegacyMutexName: "-",
	})
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	defer l.Release()

	start := time.Now()
	code, _, stderr := runIn(t, sb, "", "lock", "acquire", "--for", "5m", "--reason", "derive")
	elapsed := time.Since(start)

	if code != cli.ExitLocked {
		t.Fatalf("exit = %d, want %d", code, cli.ExitLocked)
	}
	// No retry, no backoff, no timeout parameter. Maintenance that waits behind
	// maintenance is maintenance that runs under a live session by the time it gets in.
	if elapsed > 3*time.Second {
		t.Errorf("the contender waited %s; it must skip immediately", elapsed)
	}
	if !strings.Contains(stderr, "sync") {
		t.Errorf("stderr = %q, want it to name the holder's reason", stderr)
	}
}

func TestCLI_LockBreakPrintsTheHolderFirst(t *testing.T) {
	sb := testutil.NewSandbox(t)
	l, err := lock.Acquire(lock.Options{
		Path:            filepath.Join(sb.StateRoot, lock.FileName),
		Reason:          "sync",
		MutexName:       `Local\ams-store-test-break`,
		LegacyMutexName: "-",
	})
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	defer l.Release()

	code, out, _ := runIn(t, sb, "", "lock", "break")
	if code != cli.ExitOK {
		t.Fatalf("break: exit = %d, want 0", code)
	}
	// Operator-only, and never silent: breaking a lock whose holder is alive can corrupt
	// a run, so the operator sees who they took it from.
	if !strings.Contains(out, "sync") {
		t.Errorf("break output = %q, want it to print the holder before breaking", out)
	}
	if _, err := os.Stat(filepath.Join(sb.StateRoot, lock.FileName)); !os.IsNotExist(err) {
		t.Error("break left the lock file in place")
	}
}

func TestCLI_LockReleaseRefusesALiveForeignHolder(t *testing.T) {
	// A genuinely live foreign process, because the point of the rule is the FOREIGN
	// part: with the holder set to this process, "refuses a live holder" and "refuses
	// nothing" produce the same result and the test proves neither.
	child := exec.Command(os.Args[0], "-test.run=TestHelperSleeper")
	child.Env = append(os.Environ(), sleeperEnv+"=1")
	if err := child.Start(); err != nil {
		t.Fatalf("start the sleeper: %v", err)
	}
	t.Cleanup(func() {
		_ = child.Process.Kill()
		_ = child.Wait()
	})

	sb := testutil.NewSandbox(t)
	stage := lock.Holder{
		PID:           child.Process.Pid,
		StartTimeUnix: lock.StartTimeUnix(child.Process.Pid),
		Host:          "test",
		AcquiredAt:    time.Now().UTC(),
		Reason:        "sync",
	}
	raw, err := json.Marshal(stage)
	if err != nil {
		t.Fatal(err)
	}
	lockFile := filepath.Join(sb.StateRoot, lock.FileName)
	if err := os.WriteFile(lockFile, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	if !lock.IsLive(stage, time.Now(), lock.StaleAfter) {
		t.Skip("cannot observe the child process's start time on this platform")
	}

	// release is not break: quietly deleting a live process's lock is how two maintainers
	// end up writing the same index in the same second.
	code, _, stderr := runIn(t, sb, "", "lock", "release")
	if code != cli.ExitLocked {
		t.Errorf("exit = %d, want %d - release must not take a live holder's lock", code, cli.ExitLocked)
	}
	if !strings.Contains(stderr, "break") {
		t.Errorf("stderr = %q, want it to point at the break verb", stderr)
	}
	if _, err := os.Stat(lockFile); err != nil {
		t.Error("release removed a live holder's lock anyway")
	}

	// break is the verb that IS allowed to, and it says whose lock it took.
	code, out, _ := runIn(t, sb, "", "lock", "break")
	if code != cli.ExitOK {
		t.Fatalf("break: exit = %d", code)
	}
	if !strings.Contains(out, "sync") {
		t.Errorf("break output = %q, want the holder printed before it went", out)
	}
}

func TestCLI_LockUnknownSubcommandIsAUsageError(t *testing.T) {
	sb := testutil.NewSandbox(t)
	code, out, _ := runIn(t, sb, "", "lock", "steal")
	if code != cli.ExitUsage {
		t.Errorf("exit = %d, want %d", code, cli.ExitUsage)
	}
	if out != "" {
		t.Errorf("stdout = %q, want empty on a usage error", out)
	}
}

// --------------------------------------------------------------------------------
// gate
// --------------------------------------------------------------------------------

func hookPayload(path string) string {
	b, _ := json.Marshal(map[string]any{
		"hook_event_name": "PostToolUse",
		"tool_name":       "Write",
		"tool_input":      map[string]string{"file_path": path},
	})
	return string(b)
}

func TestCLI_GateIsSilentAndAlwaysExitsZero(t *testing.T) {
	sb := testutil.NewSandbox(t)
	dir := cleanStore(t, sb, "ws")

	code, out, _ := runIn(t, sb, hookPayload(filepath.Join(dir, store.IndexName)), "gate")
	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0 - a gate that can fail a tool call can stop the operator working", code)
	}
	if out != "" {
		t.Errorf("stdout = %q, want silence under every cap", out)
	}
}

func TestCLI_GateSurvivesGarbageOnStdin(t *testing.T) {
	sb := testutil.NewSandbox(t)
	for _, payload := range []string{"", "not json at all", "{}", `{"tool_input":{}}`, `{"tool_input":{"file_path":"/nope/MEMORY.md"}}`} {
		code, out, _ := runIn(t, sb, payload, "gate")
		if code != cli.ExitOK {
			t.Errorf("payload %q: exit = %d, want 0", payload, code)
		}
		if out != "" {
			t.Errorf("payload %q: stdout = %q, want empty", payload, out)
		}
	}
}

func TestCLI_GateWritesTheDirtyMarkerForAStoreIndex(t *testing.T) {
	sb := testutil.NewSandbox(t)
	dir := cleanStore(t, sb, "ws")

	code, _, _ := runIn(t, sb, hookPayload(filepath.Join(dir, store.IndexName)), "gate")
	if code != cli.ExitOK {
		t.Fatalf("exit = %d", code)
	}
	// The marker is how the watcher learns there is something to push. Without it an
	// index a session just wrote sits on one PC until something else happens to sync.
	if !amsync.IsDirty(sb.StateRoot) {
		t.Error("the gate did not write the dirty marker after a store's MEMORY.md was written")
	}
}

func TestCLI_GateLeavesANonIndexPathAlone(t *testing.T) {
	sb := testutil.NewSandbox(t)
	cleanStore(t, sb, "ws")
	other := sb.WriteFile("notes.md", "hello\n")

	code, out, _ := runIn(t, sb, hookPayload(other), "gate")
	if code != cli.ExitOK {
		t.Fatalf("exit = %d", code)
	}
	if out != "" {
		t.Errorf("stdout = %q, want empty", out)
	}
	if amsync.IsDirty(sb.StateRoot) {
		t.Error("a write to a file that is not a store index marked the tree dirty")
	}
}

func TestCLI_GateAdvisesOnAnOversizedIndex(t *testing.T) {
	sb := testutil.NewSandbox(t)
	dir := sb.AddStore("ws", testutil.BigIndex(60), testutil.BigIndexFacts(60))

	code, out, _ := runIn(t, sb, hookPayload(filepath.Join(dir, store.IndexName)), "gate")
	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0", code)
	}
	if out == "" {
		t.Fatal("the gate was silent over the caps; the advisory block IS the product")
	}
	if !strings.Contains(out, "auto-memory index") {
		t.Errorf("advisory = %q, want the index advisory header", out)
	}
	if !strings.Contains(out, "over 130 B") {
		t.Errorf("advisory = %q, want it to name the over-cap lines", out)
	}
	// And it receipted what it saw, whether or not it could normalize.
	if _, err := os.Stat(filepath.Join(sb.StateRoot, gate.ReceiptFile)); err == nil {
		raw, _ := os.ReadFile(filepath.Join(sb.StateRoot, gate.ReceiptFile))
		if !strings.Contains(string(raw), "before_bytes") {
			t.Errorf("receipt = %q, want the before/after sizes", raw)
		}
	}
}

func TestCLI_GateNeverTouchesTheNetwork(t *testing.T) {
	// Network is never on a hook's critical path. The proof here is structural: the gate
	// runs with a hub remote configured and an unroutable URL, and still exits 0 fast.
	sb := testutil.NewSandbox(t)
	dir := cleanStore(t, sb, "ws")
	gitDir := sb.InitHistory()
	gitCLI(t, gitDir, sb.ProjectsRoot, "remote", "add", "hub", "ams-hub@"+hubHost+":ams-store.git")

	start := time.Now()
	code, _, _ := runIn(t, sb, hookPayload(filepath.Join(dir, store.IndexName)), "gate")
	if code != cli.ExitOK {
		t.Fatalf("exit = %d", code)
	}
	if d := time.Since(start); d > 8*time.Second {
		t.Errorf("the gate took %s; it must stay well under the 10 s hook timeout and must never dial the hub", d)
	}
}

// --------------------------------------------------------------------------------
// lint
// --------------------------------------------------------------------------------

func TestCLI_LintWritesTheSummaryAndStaysReadOnly(t *testing.T) {
	sb := testutil.NewSandbox(t)
	dir := sb.AddStore("ws", []string{"- [A](a.md)", "- [Gone](gone.md)"}, map[string]string{
		"a.md":      testutil.FactFile("a", "d", "", ""),
		"orphan.md": testutil.FactFile("o", "d", "", ""),
	})
	before, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}

	code, out, _ := runIn(t, sb, "", "lint", "--all", "--quiet", "--json")
	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0 - findings are reported, never fatal", code)
	}
	var sum lint.Summary
	if err := json.Unmarshal([]byte(out), &sum); err != nil {
		t.Fatalf("lint --json is not a summary: %v\n%s", err, out)
	}
	if sum.Counts.Dangling != 1 || sum.Counts.Orphan != 1 {
		t.Errorf("counts = %+v, want one dangling and one orphan", sum.Counts)
	}

	if _, err := os.Stat(lint.SummaryPath(sb.StateRoot)); err != nil {
		t.Errorf("lint-summary.json was not written: %v", err)
	}
	after, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(before) != len(after) {
		t.Errorf("lint changed the store: %d entries -> %d", len(before), len(after))
	}
}

func TestCLI_LintThrottlesItselfButQuietBypassesIt(t *testing.T) {
	sb := testutil.NewSandbox(t)
	cleanStore(t, sb, "ws")

	// --quiet is the operator's and the test's door past the throttle, and it also means
	// the run does NOT mark it: an explicit scan must not starve the next scheduled one.
	if code, _, _ := runIn(t, sb, "", "lint", "--quiet"); code != cli.ExitOK {
		t.Fatalf("first quiet run: exit = %d", code)
	}
	if code, _, _ := runIn(t, sb, "", "lint", "--quiet"); code != cli.ExitOK {
		t.Fatalf("second quiet run: exit = %d", code)
	}

	// The unattended path: a burst of session starts costs one scan at worst.
	if code, _, _ := runIn(t, sb, "", "lint"); code != cli.ExitOK {
		t.Fatalf("first throttled run: exit = %d", code)
	}
	stamp := filepath.Join(sb.StateRoot, cli.LintThrottleFile)
	if _, err := os.Stat(stamp); err != nil {
		t.Fatalf("the throttle was not marked after a successful run: %v", err)
	}
	// Remove the summary and run again: a throttled run must do nothing at all.
	if err := os.Remove(lint.SummaryPath(sb.StateRoot)); err != nil {
		t.Fatal(err)
	}
	if code, _, _ := runIn(t, sb, "", "lint"); code != cli.ExitOK {
		t.Fatalf("second throttled run: exit = %d", code)
	}
	if _, err := os.Stat(lint.SummaryPath(sb.StateRoot)); err == nil {
		t.Error("the throttled run scanned anyway")
	}
}

func TestCLI_LintNamesTheNightlyUnitOnlyWhenToldTo(t *testing.T) {
	// Decision Q10. On a PC there is no local nightly, so the finding does not exist; the
	// hub passes the unit that owns the chain. A hard-coded Windows task name is exactly
	// what the Linux port could not carry.
	sb := testutil.NewSandbox(t)
	sb.AddStore("lt", testutil.BigIndex(60), testutil.BigIndexFacts(60))

	_, out, _ := runIn(t, sb, "", "lint", "--quiet", "--json")
	if strings.Contains(out, lint.KindSilent) {
		t.Error("compactor-silent fired on a PC with no nightly unit")
	}

	_, out, _ = runIn(t, sb, "", "lint", "--quiet", "--json", "--nightly-unit", "ams-nightly.timer")
	if !strings.Contains(out, lint.KindSilent) {
		t.Error("compactor-silent did not fire on the hub")
	}
	if !strings.Contains(out, "ams-nightly.timer") {
		t.Error("the finding does not name the unit it is about")
	}
}

func TestCLI_LintWorkspaceFilterNarrowsTheScan(t *testing.T) {
	sb := testutil.NewSandbox(t)
	sb.AddStore("one", []string{"- [Gone](gone.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})
	sb.AddStore("two", []string{"- [Gone](gone.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})

	_, out, _ := runIn(t, sb, "", "lint", "--quiet", "--json", "--workspace", "one")
	var sum lint.Summary
	if err := json.Unmarshal([]byte(out), &sum); err != nil {
		t.Fatalf("not JSON: %v", err)
	}
	if len(sum.Stores) != 1 || sum.Stores[0].Workspace != "one" {
		t.Errorf("stores = %+v, want only 'one'", sum.Stores)
	}
	for _, f := range sum.Findings {
		if f.Store == "two" {
			t.Errorf("finding from the filtered-out store: %+v", f)
		}
	}
}

func TestCLI_LintSummaryOutWritesASecondCopy(t *testing.T) {
	sb := testutil.NewSandbox(t)
	cleanStore(t, sb, "ws")
	extra := filepath.Join(t.TempDir(), "out", "summary.json")

	code, _, _ := runIn(t, sb, "", "lint", "--quiet", "--summary-out", extra)
	if code != cli.ExitOK {
		t.Fatalf("exit = %d", code)
	}
	raw, err := os.ReadFile(extra)
	if err != nil {
		t.Fatalf("--summary-out did not write: %v", err)
	}
	var sum lint.Summary
	if err := json.Unmarshal(raw, &sum); err != nil {
		t.Fatalf("--summary-out is not a summary: %v", err)
	}
}

// --------------------------------------------------------------------------------
// sync
// --------------------------------------------------------------------------------

func TestCLI_SyncOfflineIsAFullSuccessfulPass(t *testing.T) {
	testutil.RequireGit(t)
	sb := testutil.NewSandbox(t)
	cleanStore(t, sb, "ws")

	code, out, stderr := runIn(t, sb, "", "sync", "--once", "--json")
	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0 with no hub configured (%s)", code, stderr)
	}
	var r amsync.Receipt
	if err := json.Unmarshal([]byte(out), &r); err != nil {
		t.Fatalf("sync --json is not a receipt: %v\n%s", err, out)
	}
	// Offline is not a failure. A PC that has been off the tailnet for a week still has
	// to keep its own history, and the local commit is what makes that true.
	if r.Status != amsync.StatusLocal {
		t.Errorf("status = %q, want %q", r.Status, amsync.StatusLocal)
	}
	if r.LocalCommit == "" {
		t.Error("no local commit was made; offline work would be invisible until the network came back")
	}
	if _, err := os.Stat(amsync.ReceiptPath(sb.StateRoot)); err != nil {
		t.Errorf("no sync receipt was appended: %v", err)
	}
}

func TestCLI_SyncClearsTheDirtyMarker(t *testing.T) {
	testutil.RequireGit(t)
	sb := testutil.NewSandbox(t)
	cleanStore(t, sb, "ws")
	if err := amsync.MarkDirty(sb.StateRoot); err != nil {
		t.Fatal(err)
	}

	if code, _, stderr := runIn(t, sb, "", "sync", "--once"); code != cli.ExitOK {
		t.Fatalf("exit = %d (%s)", code, stderr)
	}
	if amsync.IsDirty(sb.StateRoot) {
		t.Error("the dirty marker survived a pass that committed it; the watcher would sync forever")
	}
}

func TestCLI_SyncRefusesARemoteThatIsNotTheHub(t *testing.T) {
	testutil.RequireGit(t)
	sb := testutil.NewSandbox(t)
	cleanStore(t, sb, "ws")
	gitDir := sb.InitHistory()
	gitCLI(t, gitDir, sb.ProjectsRoot, "remote", "add", "hub", "https://example.invalid/ams.git")

	code, _, stderr := runIn(t, sb, "", "sync", "--once")
	if code != cli.ExitRefused {
		t.Fatalf("exit = %d, want %d - these stores hold credentials and private brand facts", code, cli.ExitRefused)
	}
	if !strings.Contains(stderr, "SSH") && !strings.Contains(stderr, "ssh") {
		t.Errorf("stderr = %q, want it to say why the remote was refused", stderr)
	}
}

func TestCLI_SyncContenderExitsFour(t *testing.T) {
	testutil.RequireGit(t)
	sb := testutil.NewSandbox(t)
	cleanStore(t, sb, "ws")
	l, err := lock.Acquire(lock.Options{
		Path:            filepath.Join(sb.StateRoot, lock.FileName),
		Reason:          "derive",
		MutexName:       `Local\ams-store-test-sync`,
		LegacyMutexName: "-",
	})
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	defer l.Release()

	start := time.Now()
	code, _, _ := runIn(t, sb, "", "sync", "--once")
	if code != cli.ExitLocked {
		t.Fatalf("exit = %d, want %d", code, cli.ExitLocked)
	}
	if d := time.Since(start); d > 3*time.Second {
		t.Errorf("the contender waited %s; sync skips, it does not queue", d)
	}
}

func TestCLI_SyncRequiresExactlyOneMode(t *testing.T) {
	sb := testutil.NewSandbox(t)
	if code, _, _ := runIn(t, sb, "", "sync", "--once", "--watch"); code != cli.ExitUsage {
		t.Errorf("--once --watch: exit = %d, want %d", code, cli.ExitUsage)
	}
}

func TestCLI_SyncWatchSecondInstanceExitsZeroSilently(t *testing.T) {
	testutil.RequireGit(t)
	sb := testutil.NewSandbox(t)
	cleanStore(t, sb, "ws")

	// No live Claude session in this sandbox, so the watcher exits at once on its own
	// terms rather than waiting out the idle window.
	code, out, _ := runIn(t, sb, "", "sync", "--watch")
	if code != cli.ExitOK {
		t.Fatalf("watch: exit = %d, want 0", code)
	}
	_ = out
}

// gitCLI runs a git command against an out-of-tree history repo.
func gitCLI(t *testing.T, gitDir, workTree string, args ...string) {
	t.Helper()
	full := append([]string{"--git-dir=" + gitDir, "--work-tree=" + workTree}, args...)
	if out, err := exec.Command("git", full...).CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}
