package cli_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/cli"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// The engine has its own suite. These exercise the WIRING: a flag that is parsed but never
// handed to the engine, a scope that resolves to the wrong set, or a JSON document that is
// not on stdout is invisible to an engine test and breaks every caller.

const emDash = store.EmDash

func sandboxStore(t *testing.T, lines []string, facts map[string]string) *testutil.Sandbox {
	t.Helper()
	sb := testutil.NewSandbox(t)
	sb.AddStore("ws1", lines, facts)
	return sb
}

func plainFacts() map[string]string {
	return map[string]string{
		"alpha.md": testutil.FactFile("alpha", "the alpha fact", "project", "alpha body"),
		"beta.md":  testutil.FactFile("beta", "the beta fact", "feedback", "beta body"),
	}
}

func plainIndex() []string {
	return []string{
		"# Memory Index",
		"",
		"- [Alpha](alpha.md) " + emDash + " alpha hook text",
		"- [Beta](beta.md) " + emDash + " NEVER do the thing",
	}
}

func deriveArgs(sb *testutil.Sandbox, extra ...string) []string {
	return append([]string{
		"derive", "--all",
		"--projects-root", sb.ProjectsRoot,
		"--state-root", sb.StateRoot,
		"--now", "2026-09-15T12:00:00Z",
	}, extra...)
}

func decodeReport(t *testing.T, stdout string) map[string]any {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("--json stdout is not one JSON document (%v):\n%s", err, stdout)
	}
	return doc
}

func TestCLI_DeriveWritesTheIndexAndReportsJSONOnStdout(t *testing.T) {
	sb := sandboxStore(t, plainIndex(), plainFacts())

	code, stdout, _ := run(t, deriveArgs(sb, "--json")...)
	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0", code)
	}
	doc := decodeReport(t, stdout)
	if doc["verb"] != "derive" {
		t.Errorf("verb = %v, want derive", doc["verb"])
	}
	stores, _ := doc["stores"].([]any)
	if len(stores) != 1 {
		t.Fatalf("stores = %d, want 1", len(stores))
	}
	row, _ := stores[0].(map[string]any)
	if row["workspace"] != "ws1" {
		t.Errorf("workspace = %v, want ws1", row["workspace"])
	}
	if row["harvested"].(float64) != 2 {
		t.Errorf("harvested = %v, want 2", row["harvested"])
	}

	b, err := os.ReadFile(filepath.Join(sb.ProjectsRoot, "ws1", "memory", store.IndexName))
	if err != nil {
		t.Fatalf("read index: %v", err)
	}
	text := string(b)
	// Doctrine first: beta is metadata.type feedback AND its hook is imperative.
	if !strings.HasPrefix(text, "# Memory Index\n\n- [Beta](beta.md)") {
		t.Errorf("derived order is wrong:\n%s", text)
	}
	if strings.Contains(text, "\r\n") {
		t.Error("the derived index carries CRLF; derive writes LF only")
	}
}

func TestCLI_DeriveDryRunWritesNothingAndStillReports(t *testing.T) {
	sb := sandboxStore(t, plainIndex(), plainFacts())
	indexPath := filepath.Join(sb.ProjectsRoot, "ws1", "memory", store.IndexName)
	before, err := os.ReadFile(indexPath)
	if err != nil {
		t.Fatalf("read index: %v", err)
	}
	alphaBefore, err := os.ReadFile(filepath.Join(sb.ProjectsRoot, "ws1", "memory", "alpha.md"))
	if err != nil {
		t.Fatalf("read alpha: %v", err)
	}

	code, stdout, _ := run(t, deriveArgs(sb, "--json", "--dry-run")...)
	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0", code)
	}
	doc := decodeReport(t, stdout)
	row := doc["stores"].([]any)[0].(map[string]any)
	if row["status"] != "dry-run" {
		t.Errorf("status = %v, want dry-run", row["status"])
	}

	after, _ := os.ReadFile(indexPath)
	if string(after) != string(before) {
		t.Error("--dry-run rewrote the index")
	}
	alphaAfter, _ := os.ReadFile(filepath.Join(sb.ProjectsRoot, "ws1", "memory", "alpha.md"))
	if string(alphaAfter) != string(alphaBefore) {
		t.Error("--dry-run harvested into a fact file; a dry run writes NOTHING, not even frontmatter")
	}
}

func TestCLI_DeriveNoHarvestLeavesFactFilesAlone(t *testing.T) {
	sb := sandboxStore(t, plainIndex(), plainFacts())
	alphaPath := filepath.Join(sb.ProjectsRoot, "ws1", "memory", "alpha.md")
	before, _ := os.ReadFile(alphaPath)

	code, stdout, _ := run(t, deriveArgs(sb, "--json", "--no-harvest")...)
	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0", code)
	}
	row := decodeReport(t, stdout)["stores"].([]any)[0].(map[string]any)
	if row["harvested"].(float64) != 0 {
		t.Errorf("harvested = %v under --no-harvest, want 0", row["harvested"])
	}
	after, _ := os.ReadFile(alphaPath)
	if string(after) != string(before) {
		t.Error("--no-harvest still wrote hook: into a fact file")
	}
	// The index hook must SURVIVE a --no-harvest render: this is the zero-hooks-lost
	// check's mode, and a render that fell back to description: would silently rewrite
	// every hook in the fleet on the first run.
	b, _ := os.ReadFile(filepath.Join(sb.ProjectsRoot, "ws1", "memory", store.IndexName))
	if !strings.Contains(string(b), "alpha hook text") {
		t.Errorf("the index hook did not survive --no-harvest:\n%s", b)
	}
}

// --stop-below is the floor's one knob on the command line (decision Q2). A flag that is
// parsed and then not handed to the engine reads exactly like a flag that works.
func TestCLI_DeriveStopBelowReachesTheFloor(t *testing.T) {
	lines := testutil.BigIndex(80)
	sb := sandboxStore(t, lines, testutil.BigIndexFacts(80))
	indexPath := filepath.Join(sb.ProjectsRoot, "ws1", "memory", store.IndexName)
	b, err := os.ReadFile(indexPath)
	if err != nil {
		t.Fatalf("read index: %v", err)
	}
	if len(b) <= store.SyncLimitBytes {
		t.Fatalf("fixture is %d B; it must start over the %d B sync limit", len(b), store.SyncLimitBytes)
	}

	code, stdout, _ := run(t, deriveArgs(sb, "--json", "--stop-below", "24000")...)
	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0 (stderr had the detail)", code)
	}
	row := decodeReport(t, stdout)["stores"].([]any)[0].(map[string]any)
	floored := int(row["floored"].(float64))
	if floored == 0 {
		t.Fatal("floored = 0; the floor never engaged")
	}
	after, _ := os.ReadFile(indexPath)
	if len(after) >= store.SyncLimitBytes {
		t.Errorf("index is %d B, still at or over the sync limit", len(after))
	}
	// The whole point of the flag: it stopped at 24,000 rather than carrying on down to
	// the 20,000 B default, so strictly fewer lines were truncated than the default would.
	if len(after) < 20000 {
		t.Errorf("index is %d B; --stop-below 24000 truncated past its own stop threshold", len(after))
	}
}

func TestCLI_DeriveUnknownWorkspaceIsAUsageError(t *testing.T) {
	sb := sandboxStore(t, plainIndex(), plainFacts())

	code, stdout, stderr := run(t, "derive",
		"--workspace", "no-such-workspace",
		"--projects-root", sb.ProjectsRoot,
		"--state-root", sb.StateRoot)
	if code != cli.ExitUsage {
		t.Errorf("exit = %d, want %d", code, cli.ExitUsage)
	}
	if stdout != "" {
		t.Errorf("stdout = %q, want empty", stdout)
	}
	if !strings.Contains(stderr, "no-such-workspace") {
		t.Errorf("stderr = %q, want it to name the workspace", stderr)
	}
}

func TestCLI_DeriveStoreAndAllAreMutuallyExclusive(t *testing.T) {
	sb := sandboxStore(t, plainIndex(), plainFacts())

	code, _, stderr := run(t, "derive", "--all",
		"--store", filepath.Join(sb.ProjectsRoot, "ws1", "memory"),
		"--projects-root", sb.ProjectsRoot)
	if code != cli.ExitUsage {
		t.Errorf("exit = %d, want %d", code, cli.ExitUsage)
	}
	if !strings.Contains(stderr, "mutually exclusive") {
		t.Errorf("stderr = %q, want it to say the two flags are mutually exclusive", stderr)
	}
}

// Exit 1 is the unconverged contract: an index still at or above the sync limit after the
// floor, because every remaining over-cap line is doctrine and doctrine is never
// shortened autonomously.
func TestCLI_DeriveDoctrineOnlyOverflowExitsUnconverged(t *testing.T) {
	lines := []string{"# Memory Index", ""}
	facts := map[string]string{}
	for i := 0; i < 80; i++ {
		slug := "fact" + string(rune('a'+i/26)) + string(rune('a'+i%26)) + ".md"
		hook := "NEVER do the thing " + strings.Repeat("with a long standing order ", 12)
		lines = append(lines, "- [Fact]("+slug+") "+emDash+" "+hook)
		facts[slug] = testutil.FactFile("Fact", "d", "feedback", hook)
	}
	sb := sandboxStore(t, lines, facts)
	b, _ := os.ReadFile(filepath.Join(sb.ProjectsRoot, "ws1", "memory", store.IndexName))
	if len(b) <= store.SyncLimitBytes {
		t.Fatalf("fixture is %d B; it must start over the %d B sync limit", len(b), store.SyncLimitBytes)
	}

	code, stdout, _ := run(t, deriveArgs(sb, "--json")...)
	if code != cli.ExitUnconverged {
		t.Fatalf("exit = %d, want %d (unconverged)", code, cli.ExitUnconverged)
	}
	doc := decodeReport(t, stdout)
	if doc["unconverged"].(float64) != 1 {
		t.Errorf("unconverged = %v, want 1", doc["unconverged"])
	}
	row := doc["stores"].([]any)[0].(map[string]any)
	if row["floored"].(float64) != 0 {
		t.Errorf("floored = %v; doctrine must never be truncated", row["floored"])
	}
}

// A receipt row is the audit trail both watchdogs read. The CLI must append it in the
// compactor's own file and vocabulary, or the Phase 4 overlap has two writers and one
// reader that understands only half of them.
func TestCLI_DeriveAppendsAReceiptInTheCompactorsFile(t *testing.T) {
	sb := sandboxStore(t, plainIndex(), plainFacts())

	if code, _, _ := run(t, deriveArgs(sb)...); code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0", code)
	}
	b, err := os.ReadFile(filepath.Join(sb.StateRoot, "compact-receipts.jsonl"))
	if err != nil {
		t.Fatalf("read compact-receipts.jsonl: %v", err)
	}
	line := strings.TrimSpace(strings.Split(strings.TrimSpace(string(b)), "\n")[0])
	var row map[string]any
	if err := json.Unmarshal([]byte(line), &row); err != nil {
		t.Fatalf("receipt is not JSON (%v): %s", err, line)
	}
	for _, key := range []string{"ts", "workspace", "dry_run", "before_bytes", "status",
		"reindexed", "dedangled", "dedup_slug", "floored", "after_bytes", "judge_called"} {
		if _, ok := row[key]; !ok {
			t.Errorf("receipt row has no %q field; a reader of the PowerShell receipts cannot parse it", key)
		}
	}
	if row["workspace"] != "ws1" {
		t.Errorf("workspace = %v, want ws1", row["workspace"])
	}
}

// Nothing the CLI writes may land outside the roots it was given. The incident that
// produced this test was a bare `derive` defaulting to the real projects root; the guard
// above refuses that, and this one proves the overrides are honoured when they are given.
func TestCLI_DeriveTouchesNothingOutsideTheInjectedRoots(t *testing.T) {
	sb := sandboxStore(t, plainIndex(), plainFacts())
	outside := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.MkdirAll(outside, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	if code, _, _ := run(t, deriveArgs(sb)...); code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0", code)
	}
	entries, err := os.ReadDir(outside)
	if err != nil {
		t.Fatalf("read outside: %v", err)
	}
	if len(entries) != 0 {
		t.Errorf("derive wrote %d entries outside its roots", len(entries))
	}
	for _, rel := range []string{"compact-receipts.jsonl", "dirty"} {
		if _, err := os.Stat(filepath.Join(sb.StateRoot, rel)); err != nil {
			t.Errorf("%s was not written into the injected state root: %v", rel, err)
		}
	}
}

func TestCLI_HarvestScopedToOneStoreWritesHooksAndNoIndex(t *testing.T) {
	sb := sandboxStore(t, plainIndex(), plainFacts())
	dir := filepath.Join(sb.ProjectsRoot, "ws1", "memory")
	indexBefore, _ := os.ReadFile(filepath.Join(dir, store.IndexName))

	code, stdout, _ := run(t, "harvest", "--store", dir,
		"--projects-root", sb.ProjectsRoot,
		"--state-root", sb.StateRoot,
		"--now", "2026-09-15T12:00:00Z", "--json")
	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0", code)
	}
	doc := decodeReport(t, stdout)
	if doc["harvested"].(float64) != 2 {
		t.Errorf("harvested = %v, want 2", doc["harvested"])
	}
	alpha, _ := os.ReadFile(filepath.Join(dir, "alpha.md"))
	if !strings.Contains(string(alpha), `hook: "alpha hook text"`) {
		t.Errorf("hook: was not harvested into alpha.md:\n%s", alpha)
	}
	indexAfter, _ := os.ReadFile(filepath.Join(dir, store.IndexName))
	if string(indexAfter) != string(indexBefore) {
		t.Error("harvest rewrote the index; it writes fact files only")
	}
}

// A dry run reports and writes NOTHING - the help text promises it. The receipts ledger is
// what lint's compactor-silent and starved rules read, so a dry-run row there would let a
// rehearsal pass for a real run (2026-09-15: a --dry-run against a live store appended a
// row to the operator's compact-receipts.jsonl). The dirty marker is a write too: it wakes
// the watcher into a sync pass the dry run never earned.
func TestCLI_DeriveDryRunWritesNoReceiptAndNoDirtyMarker(t *testing.T) {
	sb := sandboxStore(t, plainIndex(), plainFacts())

	args := append(deriveArgs(sb), "--dry-run")
	if code, _, _ := run(t, args...); code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0", code)
	}
	for _, rel := range []string{"compact-receipts.jsonl", "dirty"} {
		if _, err := os.Stat(filepath.Join(sb.StateRoot, rel)); err == nil {
			t.Errorf("a dry run wrote %s; --dry-run promises to write nothing", rel)
		}
	}
}
