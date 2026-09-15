package store_test

import (
	"runtime"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// Decision Q5: the canonical store key is case-folded on Windows (where the filesystem
// folds too) and case-PRESERVING elsewhere, because on a case-sensitive filesystem two
// genuinely distinct workspaces differing only in case would collapse into one and one
// would be silently marked IsAlias. Two such stores get a startup warning instead.
func TestStores_CaseOnlyDifferenceIsTwoStoresAndWarnsOffWindows(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("a case-insensitive filesystem cannot hold two workspaces differing only in case")
	}
	s := testutil.NewSandbox(t)
	s.AddStore("ws-a", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})
	s.AddStore("WS-A", []string{"- [B](b.md)"}, map[string]string{"b.md": testutil.FactFile("b", "d", "", "")})

	stores, warnings, err := store.Enumerate(s.ProjectsRoot)
	if err != nil {
		t.Fatalf("Enumerate: %v", err)
	}
	canonical := 0
	for _, st := range stores {
		if !st.IsAlias {
			canonical++
		}
	}
	if canonical != 2 {
		t.Errorf("canonical stores = %d, want 2: case folding is a Windows assumption", canonical)
	}
	joined := strings.Join(warnings, " | ")
	if !strings.Contains(strings.ToLower(joined), "case") {
		t.Errorf("warnings = %v, want one naming the case-only collision", warnings)
	}
}

func TestStores_CanonicalKeyFoldsOnlyOnWindows(t *testing.T) {
	a := store.CanonicalKey("/tmp/Projects/WS")
	b := store.CanonicalKey("/tmp/projects/ws")
	if runtime.GOOS == "windows" {
		if a != b {
			t.Errorf("CanonicalKey must fold case on Windows: %q vs %q", a, b)
		}
		return
	}
	if a == b {
		t.Errorf("CanonicalKey must preserve case off Windows: both folded to %q", a)
	}
}
