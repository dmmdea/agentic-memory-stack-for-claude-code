package cli_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/cli"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// judgeSandbox builds a store with one over-cap line and returns the roots to inject.
// Every test here passes --state-root and --projects-root: a CLI test that resolved the
// real roots would read, and one day write, the operator's own stores.
func judgeSandbox(t *testing.T) (sb *testutil.Sandbox, dir string) {
	t.Helper()
	sb = testutil.NewSandbox(t)
	lines := testutil.BigIndex(3)
	dir = sb.AddStore("ws", lines, testutil.BigIndexFacts(3))
	return sb, dir
}

func writePlan(t *testing.T, dir, doc string) string {
	t.Helper()
	p := filepath.Join(dir, "plan.json")
	if err := os.WriteFile(p, []byte(doc), 0o644); err != nil {
		t.Fatalf("write plan: %v", err)
	}
	return p
}

// The hub guard runs before the plan is even read: a PC must not be able to learn what
// the nightly decided by pointing this verb at a plan file.
func TestCLI_JudgeApplyRefusesOffTheHub(t *testing.T) {
	sb, dir := judgeSandbox(t)
	plan := writePlan(t, sb.Root, `{"version":1,"stores":[{"workspace":"ws","decisions":[]}]}`)

	code, stdout, stderr := run(t, "judge-apply", "--state-root", sb.StateRoot,
		"--projects-root", sb.ProjectsRoot, "--store", dir, "--plan", plan)

	if code != cli.ExitRefused {
		t.Fatalf("exit = %d, want %d (refused)", code, cli.ExitRefused)
	}
	if stdout != "" {
		t.Errorf("stdout = %q, want empty", stdout)
	}
	if !strings.Contains(stderr, "hub-only") {
		t.Errorf("stderr = %q, want it to say the verb is hub-only", stderr)
	}

	if err := os.WriteFile(filepath.Join(sb.StateRoot, "role"), []byte("pc\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	code, _, stderr = run(t, "judge-apply", "--state-root", sb.StateRoot,
		"--projects-root", sb.ProjectsRoot, "--store", dir, "--plan", plan)
	if code != cli.ExitRefused {
		t.Fatalf("a PC applied a plan: exit = %d", code)
	}
	if !strings.Contains(stderr, "pc") {
		t.Errorf("stderr = %q, want it to name the role it found", stderr)
	}
}

func TestCLI_JudgeApplyRejectsABadInvocation(t *testing.T) {
	sb, dir := judgeSandbox(t)
	base := []string{"judge-apply", "--hub", "--state-root", sb.StateRoot, "--projects-root", sb.ProjectsRoot}

	cases := map[string][]string{
		"no store":       append(base, "--plan", writePlan(t, sb.Root, `{"version":1,"stores":[{"workspace":"ws","decisions":[]}]}`)),
		"no plan":        append(base, "--store", dir),
		"malformed plan": append(base, "--store", dir, "--plan", writePlan(t, sb.Root, `{"version":1,"stores":[{"workspace":"ws","decisions":[{"slug":"a.md","verb":"DELETE"}]}]}`)),
		"missing plan":   append(base, "--store", dir, "--plan", filepath.Join(sb.Root, "absent.json")),
		// The corpus key is environment-only: a key on a command line reaches the process
		// list and the shell history, so the flag must not exist at all.
		"key as a flag": append(base, "--store", dir, "--mem0-key", "secret"),
	}
	for name, args := range cases {
		t.Run(name, func(t *testing.T) {
			code, stdout, _ := run(t, args...)
			if code != cli.ExitUsage {
				t.Errorf("exit = %d, want %d", code, cli.ExitUsage)
			}
			if stdout != "" {
				t.Errorf("stdout = %q, want empty", stdout)
			}
		})
	}
}

func TestCLI_JudgeApplyAppliesAndReportsJSON(t *testing.T) {
	sb, dir := judgeSandbox(t)
	plan := writePlan(t, sb.Root,
		`{"version":1,"stores":[{"workspace":"ws","decisions":[{"slug":"fact1.md","verb":"SHORTEN","new_hook":"detail number 1 kept short"}]}]}`)

	code, stdout, stderr := run(t, "judge-apply", "--hub", "--json",
		"--state-root", sb.StateRoot, "--projects-root", sb.ProjectsRoot,
		"--store", dir, "--plan", plan)

	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0 (stderr %s)", code, stderr)
	}
	var out struct {
		Workspace string `json:"workspace"`
		Status    string `json:"status"`
		Shortened int    `json:"shortened"`
	}
	if err := json.Unmarshal([]byte(stdout), &out); err != nil {
		t.Fatalf("stdout is not one JSON document: %v\n%s", err, stdout)
	}
	if out.Workspace != "ws" || out.Status != "applied" || out.Shortened != 1 {
		t.Errorf("result = %+v, want the shortening applied to ws", out)
	}

	idx, err := os.ReadFile(filepath.Join(dir, "MEMORY.md"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(idx), "detail number 1 kept short") {
		t.Error("the index on disk did not receive the rewrite")
	}
}

func TestCLI_JudgeApplyCandidatesPrintsTheOfferSet(t *testing.T) {
	sb, dir := judgeSandbox(t)

	code, stdout, stderr := run(t, "judge-apply", "--hub", "--candidates", "--json",
		"--state-root", sb.StateRoot, "--projects-root", sb.ProjectsRoot, "--store", dir)

	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0 (stderr %s)", code, stderr)
	}
	var out struct {
		Workspace string `json:"workspace"`
		Shorten   []struct {
			Slug  string `json:"Slug"`
			Bytes int    `json:"Bytes"`
		} `json:"shorten"`
	}
	if err := json.Unmarshal([]byte(stdout), &out); err != nil {
		t.Fatalf("stdout is not one JSON document: %v\n%s", err, stdout)
	}
	if out.Workspace != "ws" || len(out.Shorten) != 3 {
		t.Fatalf("offer set = %+v, want the three over-cap lines of ws", out)
	}
	// Nothing may be written by an offer-set read: it is the prompt-building path.
	if _, err := os.Stat(filepath.Join(sb.StateRoot, "compact-receipts.jsonl")); err == nil {
		t.Error("--candidates wrote a receipt; it applies nothing and decides nothing")
	}
}

// migratePlan is one MIGRATE decision against the sandbox's first fact.
func migratePlan(t *testing.T, root string) string {
	t.Helper()
	return writePlan(t, root,
		`{"version":1,"stores":[{"workspace":"ws","decisions":[{"slug":"fact1.md","verb":"MIGRATE"}]}]}`)
}

// A migration needs a corpus PARTITION as much as it needs an authority. The server answers an
// empty user_id with a 500 per request, so a client built without one turns every migration into
// its own failure while the store is left untouched and the night still exits 0 - which is what
// the deployed chain did: its unit carries the credential and neither the authority nor the user.
// The verb must report the misconfiguration once and attempt nothing.
func TestCLI_JudgeApplyWithoutACorpusUserAttemptsNothing(t *testing.T) {
	sb, dir := judgeSandbox(t)
	var mu sync.Mutex
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		calls++
		mu.Unlock()
		http.Error(w, `{"detail":"Invalid user_id: cannot be empty"}`, http.StatusInternalServerError)
	}))
	defer srv.Close()
	t.Setenv("AMS_MEM0_URL", "")
	t.Setenv("MEM0_URL", srv.URL)
	t.Setenv("MEM0_USER_ID", "")
	t.Setenv("AMS_MEM0_USER", "")

	code, stdout, stderr := run(t, "judge-apply", "--hub", "--json",
		"--state-root", sb.StateRoot, "--projects-root", sb.ProjectsRoot,
		"--store", dir, "--plan", migratePlan(t, sb.Root))

	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0 (stderr %s)", code, stderr)
	}
	if !strings.Contains(stderr, "no corpus user configured") {
		t.Errorf("stderr did not name the missing corpus user: %s", stderr)
	}
	mu.Lock()
	got := calls
	mu.Unlock()
	if got != 0 {
		t.Errorf("the authority was called %d time(s) with no user configured; want 0 attempts", got)
	}
	var out struct {
		Migrated   int      `json:"migrated"`
		Mem0Orphan []string `json:"mem0_orphan"`
	}
	if err := json.Unmarshal([]byte(stdout), &out); err != nil {
		t.Fatalf("stdout is not one JSON document: %v\n%s", err, stdout)
	}
	if out.Migrated != 0 {
		t.Errorf("migrated = %d, want 0", out.Migrated)
	}
	if len(out.Mem0Orphan) != 1 || !strings.Contains(out.Mem0Orphan[0], "no corpus client configured") {
		t.Errorf("the receipt did not report the kept line: %+v", out.Mem0Orphan)
	}
	if _, err := os.Stat(filepath.Join(dir, "fact1.md")); err != nil {
		t.Error("the fact was removed although nothing was written to the corpus")
	}
}

// ...and when the partition IS configured it must reach the wire, because the environment is the
// only way the deployed chain can supply it.
func TestCLI_JudgeApplyMigrationCarriesTheConfiguredCorpusUser(t *testing.T) {
	sb, dir := judgeSandbox(t)
	var mu sync.Mutex
	var postedUser, postedText string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.Method == http.MethodPost {
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			postedUser, _ = body["user_id"].(string)
			postedText, _ = body["messages"].(string)
			if strings.TrimSpace(postedUser) == "" {
				http.Error(w, `{"detail":"Invalid user_id"}`, http.StatusInternalServerError)
				return
			}
			_, _ = w.Write([]byte(`{"results":[{"id":"m1"}]}`))
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"memory": postedText, "retrievable": true})
	}))
	defer srv.Close()
	t.Setenv("AMS_MEM0_URL", "")
	t.Setenv("MEM0_URL", srv.URL)
	t.Setenv("AMS_MEM0_USER", "")
	t.Setenv("MEM0_USER_ID", "probe-partition")

	code, stdout, stderr := run(t, "judge-apply", "--hub", "--json",
		"--state-root", sb.StateRoot, "--projects-root", sb.ProjectsRoot,
		"--store", dir, "--plan", migratePlan(t, sb.Root))

	if code != cli.ExitOK {
		t.Fatalf("exit = %d, want 0 (stderr %s)", code, stderr)
	}
	var out struct {
		Migrated int `json:"migrated"`
	}
	if err := json.Unmarshal([]byte(stdout), &out); err != nil {
		t.Fatalf("stdout is not one JSON document: %v\n%s", err, stdout)
	}
	if out.Migrated != 1 {
		t.Fatalf("migrated = %d, want 1 (stderr %s)", out.Migrated, stderr)
	}
	mu.Lock()
	gotUser := postedUser
	mu.Unlock()
	if gotUser != "probe-partition" {
		t.Errorf("the write carried user_id %q, want the configured partition", gotUser)
	}
}
