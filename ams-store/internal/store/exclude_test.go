package store_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

func enumerated(t *testing.T, root string) map[string]bool {
	t.Helper()
	stores, _, err := store.Enumerate(root)
	if err != nil {
		t.Fatalf("Enumerate: %v", err)
	}
	got := map[string]bool{}
	for _, s := range stores {
		got[s.Workspace] = true
	}
	return got
}

// A scratch or temp workspace is never a store: it would be synced fleet-wide, judged, and
// its test facts migrated into the production corpus.
func TestEnumerate_SkipsScratchAndTempWorkspaces(t *testing.T) {
	sb := testutil.NewSandbox(t)
	fact := map[string]string{"a.md": testutil.FactFile("a", "d", "", "")}
	idx := []string{"- [A](a.md)"}
	underOSTemp := store.EncodeWorkspacePath(os.TempDir()) + "-someproject"
	for _, ws := range []string{
		"real-project",
		"C--Users-someone-AppData-Local-Temp-claude-sess-scratchpad-probe",
		"C--Work-notes-scratchpad-x",
		underOSTemp,
	} {
		sb.AddStore(ws, idx, fact)
	}

	got := enumerated(t, sb.ProjectsRoot)
	if !got["real-project"] {
		t.Fatalf("a real project was excluded: %v", got)
	}
	for ws := range got {
		if ws != "real-project" {
			t.Errorf("workspace %q is scratch/temp and must not be enumerated", ws)
		}
	}
}

// The exclude list is configurable in the projects root's .ams/ policy and replaces the
// defaults; a missing or unreadable policy fails open to the defaults.
func TestEnumerate_ExcludeListComesFromThePolicy(t *testing.T) {
	sb := testutil.NewSandbox(t)
	fact := map[string]string{"a.md": testutil.FactFile("a", "d", "", "")}
	idx := []string{"- [A](a.md)"}
	sb.AddStore("keep-scratchpad-me", idx, fact)
	sb.AddStore("drop-quarantine-me", idx, fact)
	sb.AddStore("plain", idx, fact)

	// No policy: the defaults exclude the scratchpad name.
	got := enumerated(t, sb.ProjectsRoot)
	if got["keep-scratchpad-me"] || !got["drop-quarantine-me"] || !got["plain"] {
		t.Fatalf("default rules: got %v", got)
	}

	policyDir := filepath.Join(sb.ProjectsRoot, store.PolicyDir)
	if err := os.MkdirAll(policyDir, 0o755); err != nil {
		t.Fatal(err)
	}
	policy := filepath.Join(policyDir, store.PolicyFile)
	if err := os.WriteFile(policy, []byte(`{"store_exclude":["-quarantine-"]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	got = enumerated(t, sb.ProjectsRoot)
	if !got["keep-scratchpad-me"] || got["drop-quarantine-me"] || !got["plain"] {
		t.Fatalf("policy rules: got %v (the list replaces the defaults)", got)
	}

	if err := os.WriteFile(policy, []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	got = enumerated(t, sb.ProjectsRoot)
	if got["keep-scratchpad-me"] || !got["drop-quarantine-me"] {
		t.Fatalf("a malformed policy must fall back to the defaults: got %v", got)
	}
}
