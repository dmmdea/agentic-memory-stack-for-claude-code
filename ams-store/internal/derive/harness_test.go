package derive

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// emDash is the index separator, from the one place it is defined.
const emDash = store.EmDash

// env is the Go form of MemoryCompact.Fixture.ps1's New-Sandbox + Add-SandboxStore: a
// throwaway PROJECTS_ROOT/STATE_ROOT pair holding one store. Everything runs in-process,
// so a scenario costs milliseconds instead of the ~7 s a child-pwsh Pester scenario paid.
type env struct {
	t   *testing.T
	sb  *testutil.Sandbox
	st  store.Store
	dir string
	log bytes.Buffer
}

func newEnv(t *testing.T, ws string, lines []string, facts map[string]string) *env {
	t.Helper()
	return newEnvNL(t, ws, lines, facts, "\n")
}

func newEnvNL(t *testing.T, ws string, lines []string, facts map[string]string, nl string) *env {
	t.Helper()
	sb := testutil.NewSandbox(t)
	dir := sb.AddStoreNL(ws, lines, facts, nl)
	return &env{
		t:   t,
		sb:  sb,
		dir: dir,
		st: store.Store{
			Workspace:    ws,
			Dir:          dir,
			IndexPath:    filepath.Join(dir, store.IndexName),
			CanonicalDir: dir,
			WorkspaceDir: filepath.Dir(dir),
			ProbeDirs:    []string{filepath.Dir(dir)},
		},
	}
}

// run derives the store. Every option the scenarios vary is applied by the mutators, so
// the default shape stays one line at every call site.
func (e *env) run(mut ...func(*Options)) (*Result, error) {
	e.t.Helper()
	opt := Options{
		Roots: store.Roots{ProjectsRoot: e.sb.ProjectsRoot, StateRoot: e.sb.StateRoot},
		Store: e.st,
		Now:   time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC),
		Log:   &e.log,
	}
	for _, m := range mut {
		m(&opt)
	}
	return Run(opt)
}

func (e *env) indexBytes() []byte {
	e.t.Helper()
	b, err := os.ReadFile(e.st.IndexPath)
	if err != nil {
		e.t.Fatalf("read index: %v", err)
	}
	return b
}

func (e *env) indexText() string { return string(e.indexBytes()) }

func (e *env) fact(name string) string {
	e.t.Helper()
	b, err := os.ReadFile(filepath.Join(e.dir, name))
	if err != nil {
		e.t.Fatalf("read %s: %v", name, err)
	}
	return string(b)
}

func (e *env) exists(name string) bool {
	_, err := os.Stat(filepath.Join(e.dir, name))
	return err == nil
}

// receipts reads compact-receipts.jsonl the way the Pester fixture's Invoke-Compactor
// does: one decoded object per non-blank line.
func (e *env) receipts() []map[string]any {
	e.t.Helper()
	b, err := os.ReadFile(filepath.Join(e.sb.StateRoot, "compact-receipts.jsonl"))
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		e.t.Fatalf("read receipts: %v", err)
	}
	var out []map[string]any
	for _, ln := range strings.Split(string(b), "\n") {
		if strings.TrimSpace(ln) == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(ln), &m); err != nil {
			e.t.Fatalf("receipt line %q: %v", ln, err)
		}
		out = append(out, m)
	}
	return out
}

// ---------------------------------------------------------------- index fixtures

// bigIndexLines is New-BigIndexLines: a heading, a blank, then n pointers whose hooks are
// far over the 130 B line cap, so 60 of them clear the 20,000 B trigger.
func bigIndexLines(n int) []string {
	lines := []string{"# Memory Index", ""}
	for i := 1; i <= n; i++ {
		lines = append(lines, fmt.Sprintf("- [Fact %d](fact%d.md) %s %s",
			i, i, emDash, strings.Repeat(fmt.Sprintf("detail number %d ", i), 22)))
	}
	return lines
}

// bigIndexFacts builds the fact files bigIndexLines points at. Each carries hook: already,
// so a scenario that is not about harvesting is not perturbed by it.
func bigIndexFacts(n int) map[string]string {
	facts := make(map[string]string, n)
	for i := 1; i <= n; i++ {
		facts[fmt.Sprintf("fact%d.md", i)] = factWithHook(
			fmt.Sprintf("Fact %d", i), "d",
			strings.TrimRight(strings.Repeat(fmt.Sprintf("detail number %d ", i), 22), " "))
	}
	return facts
}

// fact is New-FactFile: --- , name, a quoted description, metadata with a NESTED type and
// modified, ---, a blank line, the body, a trailing newline.
func fact(name, desc string) string {
	return testutil.FactFile(name, desc, "project", "the body of the fact")
}

// factWithHook is a fact file that already carries hook:, i.e. one harvest has run over
// it. Most floor and hygiene scenarios want this, so the run under test is not also the
// first harvest.
func factWithHook(name, desc, hook string) string {
	return "---\nname: " + name + "\ndescription: \"" + desc + "\"\nhook: \"" +
		strings.ReplaceAll(hook, `"`, `\"`) + "\"\nmetadata: \n  node_type: memory\n  type: project\n" +
		"  modified: 2026-08-01\n---\n\nthe body of the fact\n"
}

// ---------------------------------------------------------------- fakes

// fakeCommits is the injected commit-time source. Slugs it does not name have "no commit
// yet" and sort as now.
type fakeCommits struct {
	times map[string]int64
	// onCall runs before the map is returned. It is the natural mid-run seam: derive asks
	// for commit times while rendering, which is after it took the pre-run hash and before
	// it writes - exactly where the Pester fixture mutates the index "while Codex thinks".
	onCall func()
}

func (f *fakeCommits) CommitTimes(store.Store) (map[string]int64, error) {
	if f.onCall != nil {
		f.onCall()
	}
	return f.times, nil
}

// fakeMigrated is the judge-side lookup of decision Q8. The judge task implements the real
// one over the `Migrated: <slug> <id>` commit trailers.
type fakeMigrated struct{ ids map[string]string }

func (f *fakeMigrated) MigratedID(_ store.Store, slug string) (string, bool, error) {
	id, ok := f.ids[slug]
	return id, ok, nil
}

// heldLock is a lock another process already owns.
type heldLock struct{ asked int }

func (l *heldLock) TryAcquire(string) (func(), bool, error) { l.asked++; return nil, false, nil }

// freeLock is a lock this process can take, recording that it was released.
type freeLock struct{ acquired, released int }

func (l *freeLock) TryAcquire(string) (func(), bool, error) {
	l.acquired++
	return func() { l.released++ }, true, nil
}
