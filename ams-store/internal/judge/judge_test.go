package judge

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// fixture is the Go port of MemoryCompact.Fixture.ps1's New-Sandbox + Add-SandboxStore,
// in process. The PowerShell suite paid about seven seconds per scenario to a child
// process and had to be split in two to stay under a 180 s per-file bound; nothing here
// spawns anything, so the guards can be exercised as often as they need to be.
type fixture struct {
	t     *testing.T
	sb    *testutil.Sandbox
	roots store.Roots
	dir   string
	ws    string
	mem   *testutil.FakeMem0
	now   time.Time
}

// newFixture builds a store and a fake corpus. The clock is fixed so the 20 h window is
// a computation over seeded receipts, never a race with the wall clock.
func newFixture(t *testing.T, ws string, lines []string, facts map[string]string, mode testutil.Mem0Mode) *fixture {
	t.Helper()
	sb := testutil.NewSandbox(t)
	dir := sb.AddStore(ws, lines, facts)
	return &fixture{
		t:     t,
		sb:    sb,
		roots: store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot},
		dir:   dir,
		ws:    ws,
		mem:   testutil.NewFakeMem0(t, mode),
		now:   time.Date(2026, 9, 15, 5, 0, 0, 0, time.UTC),
	}
}

// bigStore is New-BigIndexLines plus its fact files: n entries, every line far past the
// 130 B cap, the store just under the sync limit.
func bigStore(n int) ([]string, map[string]string) {
	return testutil.BigIndex(n), testutil.BigIndexFacts(n)
}

func (f *fixture) options() Options {
	return Options{
		Roots:     f.roots,
		Dir:       f.dir,
		Workspace: f.ws,
		Now:       f.now,
		Mem0:      &HTTPMem0{BaseURL: f.mem.URL(), APIKey: "test-key", UserID: "tester"},
	}
}

// apply runs one store's plan. decisions==nil with outcome ok is "the judge kept
// everything", which is a real and common plan, not an absent one.
func (f *fixture) apply(decisions []Decision, tweak func(*Options)) Result {
	f.t.Helper()
	opt := f.options()
	opt.Plan = &StorePlan{Workspace: f.ws, Outcome: OutcomeOK, Decisions: decisions}
	if tweak != nil {
		tweak(&opt)
	}
	res, err := Apply(context.Background(), opt)
	if err != nil {
		f.t.Fatalf("apply: %v", err)
	}
	return res
}

func (f *fixture) indexText() string {
	f.t.Helper()
	b, err := os.ReadFile(filepath.Join(f.dir, store.IndexName))
	if err != nil {
		f.t.Fatalf("read index: %v", err)
	}
	return string(b)
}

func (f *fixture) indexBytes() int { return len(f.indexText()) }

func (f *fixture) exists(name string) bool {
	_, err := os.Stat(filepath.Join(f.dir, name))
	return err == nil
}

// line returns the index line pointing at a slug.
func (f *fixture) line(slug string) string {
	f.t.Helper()
	re := regexp.MustCompile(`\(` + regexp.QuoteMeta(slug) + `\)`)
	for _, l := range strings.Split(f.indexText(), "\n") {
		if re.MatchString(l) {
			return l
		}
	}
	return ""
}

// receipts reads every receipt row written so far.
func (f *fixture) receipts() []Receipt {
	f.t.Helper()
	b, err := os.ReadFile(ReceiptPath(f.roots.StateRoot))
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		f.t.Fatalf("read receipts: %v", err)
	}
	var out []Receipt
	for _, l := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if strings.TrimSpace(l) == "" {
			continue
		}
		var r Receipt
		if err := json.Unmarshal([]byte(l), &r); err != nil {
			f.t.Fatalf("receipt row %q: %v", l, err)
		}
		out = append(out, r)
	}
	return out
}

func (f *fixture) lastReceipt() Receipt {
	f.t.Helper()
	rs := f.receipts()
	if len(rs) == 0 {
		f.t.Fatal("no receipt was written")
	}
	return rs[len(rs)-1]
}

// seedJudgeReceipt writes a receipt that says the judge was attempted hoursAgo hours
// before the fixture's clock. It is the port of the Pester fixture's hand-written
// receipt row: the window is receipt state, so seeding a receipt is the only honest way
// to put a store inside or outside it.
func (f *fixture) seedJudgeReceipt(hoursAgo float64) {
	f.t.Helper()
	ts := f.now.Add(-time.Duration(hoursAgo * float64(time.Hour)))
	r := Receipt{TS: ts.Format(time.RFC3339Nano), Workspace: f.ws, Status: StatusRejectedNoShrink, JudgeCalled: true}
	if err := WriteReceipt(f.roots.StateRoot, r); err != nil {
		f.t.Fatalf("seed receipt: %v", err)
	}
}

// ageReceipts rewrites every receipt's timestamp to hoursAgo before the clock, the way
// the Pester suite ages its JSONL so a second run re-enters the judge path. Without it
// the second run silently degrades into "the judge was withheld" and the scenario tests
// nothing it claims to.
func (f *fixture) ageReceipts(hoursAgo float64) {
	f.t.Helper()
	path := ReceiptPath(f.roots.StateRoot)
	b, err := os.ReadFile(path)
	if err != nil {
		f.t.Fatalf("age receipts: %v", err)
	}
	ts := f.now.Add(-time.Duration(hoursAgo * float64(time.Hour))).Format(time.RFC3339Nano)
	out := regexp.MustCompile(`"ts":"[^"]+"`).ReplaceAllString(string(b), `"ts":"`+ts+`"`)
	if err := os.WriteFile(path, []byte(out), 0o644); err != nil {
		f.t.Fatalf("age receipts: %v", err)
	}
}

func (f *fixture) usageRows() []UsageRow {
	f.t.Helper()
	b, err := os.ReadFile(UsagePath(f.roots.StateRoot))
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		f.t.Fatalf("read usage ledger: %v", err)
	}
	var out []UsageRow
	for _, l := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if strings.TrimSpace(l) == "" {
			continue
		}
		var r UsageRow
		if err := json.Unmarshal([]byte(l), &r); err != nil {
			f.t.Fatalf("usage row %q: %v", l, err)
		}
		out = append(out, r)
	}
	return out
}

func hasCandidate(list []Candidate, slug string) bool {
	for _, c := range list {
		if c.Slug == slug {
			return true
		}
	}
	return false
}

func shorten(slug, hook string) Decision {
	return Decision{Slug: slug, Verb: VerbShorten, NewHook: hook}
}

func migrate(slug string) Decision {
	return Decision{Slug: slug, Verb: VerbMigrate}
}

// factFile is New-FactFile: the exact frontmatter shape a real fact file carries, with
// type NESTED under metadata.
func factFile(name, desc, typ, body string) string {
	return testutil.FactFile(name, desc, typ, body)
}

func entryLine(title, slug, hook string) string {
	return fmt.Sprintf("- [%s](%s) %s %s", title, slug, testutil.EmDash, hook)
}
