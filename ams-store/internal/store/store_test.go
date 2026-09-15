package store_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// makeAlias creates a directory alias of target at link: a junction on Windows (the
// shape actually observed in a real projects root), a symlink elsewhere. It reports
// false when the environment cannot create one, which is a skip, not a failure - the
// Pester original self-skips the same way.
func makeAlias(t *testing.T, link, target string) bool {
	t.Helper()
	if runtime.GOOS == "windows" {
		if err := exec.Command("cmd", "/c", "mklink", "/J", link, target).Run(); err != nil {
			return false
		}
		return true
	}
	return os.Symlink(target, link) == nil
}

// MemoryStoreLib.Tests.ps1:198
func TestStores_EnumerateDedupAliasProbeDirs(t *testing.T) {
	s := testutil.NewSandbox(t)
	s.AddStore("ws-a", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})
	s.AddEmptyWorkspace("empty")
	if !makeAlias(t, filepath.Join(s.ProjectsRoot, "ws a alias"), filepath.Join(s.ProjectsRoot, "ws-a")) {
		t.Skip("this environment cannot create a directory alias (the alias half of the test is unverifiable here)")
	}

	stores, _, err := store.Enumerate(s.ProjectsRoot)
	if err != nil {
		t.Fatalf("Enumerate: %v", err)
	}
	var canonical, aliases []store.Store
	for _, st := range stores {
		if st.IsAlias {
			aliases = append(aliases, st)
		} else {
			canonical = append(canonical, st)
		}
	}
	if len(canonical) != 1 {
		t.Fatalf("canonical stores = %d, want 1 (the alias must not be processed twice)", len(canonical))
	}
	for _, st := range stores {
		if st.Workspace == "empty" {
			t.Error("a workspace with no MEMORY.md is an empty scaffold and must not be enumerated")
		}
	}
	if len(aliases) != 1 || aliases[0].Workspace != "ws a alias" {
		t.Fatalf("aliases = %v, want exactly [ws a alias]", aliases)
	}
	if aliases[0].AliasOf != "ws-a" {
		t.Errorf("AliasOf = %q, want ws-a", aliases[0].AliasOf)
	}
	if len(canonical[0].ProbeDirs) != 2 {
		t.Errorf("ProbeDirs = %v, want both directories so liveness covers the alias path", canonical[0].ProbeDirs)
	}
}

// MemoryStoreLib.Tests.ps1:218. Sorting by name alone once crowned the alias (spaces
// sort before dashes), so the job would have mutated the store THROUGH the link.
func TestStores_PhysicalWinsOverAlias(t *testing.T) {
	s := testutil.NewSandbox(t)
	s.AddStore("zzz-real", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})
	if !makeAlias(t, filepath.Join(s.ProjectsRoot, "aaa alias"), filepath.Join(s.ProjectsRoot, "zzz-real")) {
		t.Skip("cannot create a directory alias here")
	}
	stores, _, err := store.Enumerate(s.ProjectsRoot)
	if err != nil {
		t.Fatalf("Enumerate: %v", err)
	}
	for _, st := range stores {
		if !st.IsAlias && st.Workspace != "zzz-real" {
			t.Fatalf("canonical store = %q, want zzz-real", st.Workspace)
		}
	}
}

func TestStores_UnresolvableAliasIsNeverMutatedThrough(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("a dangling junction cannot be built portably on Windows")
	}
	s := testutil.NewSandbox(t)
	s.AddStore("real", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})
	// A symlink to a directory that does not exist: the link resolves to nothing, so it
	// must be reported as an alias of nothing rather than crowned its own store.
	broken := filepath.Join(s.ProjectsRoot, "broken")
	if err := os.Symlink(filepath.Join(s.ProjectsRoot, "does-not-exist"), broken); err != nil {
		t.Skip("cannot create a symlink here")
	}
	if got := store.ResolveReparseTarget(broken); got != "" {
		t.Errorf("ResolveReparseTarget(unresolvable) = %q, want empty: resolution fails CLOSED", got)
	}
}

func TestStores_ResolveOfAMissingPathFailsClosed(t *testing.T) {
	if got := store.ResolveReparseTarget(filepath.Join(t.TempDir(), "nope")); got != "" {
		t.Errorf("ResolveReparseTarget(missing) = %q, want empty", got)
	}
}

// filepath.EvalSymlinks does NOT follow a Windows junction: it returns the link path
// unchanged, with no error. That is the exact fail-open the alias dedup exists to
// prevent, so this asserts the resolver actually reaches the physical directory.
func TestStores_ReparseTargetResolvesAJunctionToItsTarget(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "real")
	link := filepath.Join(root, "a link")
	if err := os.MkdirAll(target, 0o755); err != nil {
		t.Fatal(err)
	}
	if !makeAlias(t, link, target) {
		t.Skip("cannot create a directory alias here")
	}
	got := store.ResolveReparseTarget(link)
	if got != filepath.Clean(target) {
		t.Errorf("ResolveReparseTarget(alias) = %q, want %q", got, filepath.Clean(target))
	}
}

func TestStore_FactFilesExcludeTheIndexAndNeverRecurse(t *testing.T) {
	s := testutil.NewSandbox(t)
	dir := s.AddStore("ws", []string{"- [A](a.md)"}, map[string]string{
		"a.md":  testutil.FactFile("a", "d", "", ""),
		"b.md":  testutil.FactFile("b", "d", "", ""),
		"c.txt": "not markdown",
	})
	if err := os.MkdirAll(filepath.Join(dir, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "sub", "deep.md"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	files, err := store.FactFiles(dir)
	if err != nil {
		t.Fatalf("FactFiles: %v", err)
	}
	var names []string
	for _, f := range files {
		names = append(names, f.Name)
	}
	if len(names) != 2 || names[0] != "a.md" || names[1] != "b.md" {
		t.Errorf("FactFiles = %v, want [a.md b.md] sorted, index and subdirectories excluded", names)
	}
}

// "Could not read" must never be spellable as "nothing there": a caller comparing the
// index against an empty set concludes every line is dangling and wipes the index.
func TestStore_FactFilesFailClosedOnAnUnreadableDirectory(t *testing.T) {
	_, err := store.FactFiles(filepath.Join(t.TempDir(), "does-not-exist"))
	if err == nil {
		t.Fatal("FactFiles on an unreadable directory must return an error, never an empty slice")
	}
}

// MemoryStoreLib.Tests.ps1:128
func TestSweep_ReportsRemovedAndFailed(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "sweep")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "MEMORY.md.am-tmp"), []byte("leftover"), 0o644); err != nil {
		t.Fatal(err)
	}
	r := store.SweepTempFiles(dir)
	if len(r.Removed) != 1 || r.Removed[0] != "MEMORY.md.am-tmp" {
		t.Errorf("Removed = %v, want [MEMORY.md.am-tmp]", r.Removed)
	}
	if len(r.Failed) != 0 {
		t.Errorf("Failed = %v, want none", r.Failed)
	}

	locked := filepath.Join(dir, "locked.am-tmp")
	unlock, ok := lockTempFile(t, locked)
	if !ok {
		t.Skip("this environment cannot hold a file against deletion; the failed half is unverifiable here")
	}
	defer unlock()
	r2 := store.SweepTempFiles(dir)
	if len(r2.Failed) != 1 {
		t.Errorf("Failed = %v, want exactly one: a temp file that could NOT be removed is a full copy of the index in a synced, globbed directory - the case to shout about, never one to swallow", r2.Failed)
	}
}

func TestStore_ConstantsMatchTheShippedLibrary(t *testing.T) {
	for _, tc := range []struct {
		name string
		got  int
		want int
	}{
		{"LineByteCap", store.LineByteCap, 130},
		{"TriggerBytes", store.TriggerBytes, 20000},
		{"TriggerLines", store.TriggerLines, 160},
		{"TargetBytes", store.TargetBytes, 17000},
		{"TargetLines", store.TargetLines, 140},
		{"SyncLimitBytes", store.SyncLimitBytes, 25000},
		{"InjectLimitLines", store.InjectLimitLines, 200},
		{"OversizedFactBytes", store.OversizedFactBytes, 10000},
		{"Mem0MaxChars", store.Mem0MaxChars, 4000},
		{"MinHookBudget", store.MinHookBudget, 24},
		{"TruncDefaultMax", store.TruncDefaultMax, 100},
		{"OrphanHookBudget", store.OrphanHookBudget, 90},
		{"ReceiptTailLines", store.ReceiptTailLines, 600},
	} {
		if tc.got != tc.want {
			t.Errorf("%s = %d, want %d", tc.name, tc.got, tc.want)
		}
	}
	if store.EmDash != "—" || len(store.EmDash) != 3 {
		t.Errorf("EmDash = %q (%d bytes), want U+2014 as 3 UTF-8 bytes", store.EmDash, len(store.EmDash))
	}
}

func TestStore_PathsComposeTheStoreLayout(t *testing.T) {
	root := filepath.Join("X:", "projects")
	if got, want := store.Dir(root, "ws"), filepath.Join(root, "ws", "memory"); got != want {
		t.Errorf("Dir = %q, want %q", got, want)
	}
	if got, want := store.IndexPath(root, "ws"), filepath.Join(root, "ws", "memory", "MEMORY.md"); got != want {
		t.Errorf("IndexPath = %q, want %q", got, want)
	}
	r := store.Roots{ProjectsRoot: root, StateRoot: filepath.Join("X:", "state")}
	if got, want := r.HistoryGitDir(), filepath.Join(r.StateRoot, "history.git"); got != want {
		t.Errorf("HistoryGitDir = %q, want %q", got, want)
	}
}
