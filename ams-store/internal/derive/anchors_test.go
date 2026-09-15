package derive

import (
	"strings"
	"testing"
)

// winPath is the Windows-path anchor the Pester scenario uses: C:\Users\x\.mem0.
const winPath = `C:\Users\x\.mem0`

// MemoryStoreLib.Tests.ps1:188 - anchor tokens: numbers, paths, backticked identifiers
// and ALL-CAPS words. They are what tells the reader WHEN to open the file, so a rewrite
// that keeps none of them has lost the detail even when it reads well.
func TestAnchors_NumbersPathsBackticksAllCaps(t *testing.T) {
	a := AnchorTokens("port 18791, " + winPath + ", `Test-Throttle`, DPAPI phase 3")

	for _, want := range []string{"18791", "Test-Throttle", "DPAPI"} {
		if !a[want] {
			t.Errorf("AnchorTokens did not extract %q; got %v", want, keys(a))
		}
	}
	found := false
	for k := range a {
		if strings.HasPrefix(k, `C:\Users`) {
			found = true
		}
	}
	if !found {
		t.Errorf("AnchorTokens did not extract the Windows path; got %v", keys(a))
	}
}

// MemoryCompact.Tests.ps1:137 - an anchor containing regex/wildcard characters must be
// compared literally. `cfg[0].name` is a character class to PowerShell's -like, which
// both rejected good hooks and ACCEPTED hooks that had dropped the anchor entirely.
func TestAnchors_NoWildcardFalseAccept(t *testing.T) {
	original := "the `cfg[0].name` knob matters a great deal here"

	a := AnchorTokens(original)
	if !a["cfg[0].name"] {
		t.Fatalf("the backticked anchor was not extracted verbatim; got %v", keys(a))
	}

	// The rewrite drops the anchor: cfg0.name is not cfg[0].name.
	if AnchorsRetained(original, "the cfg0.name knob matters") {
		t.Error("a rewrite that dropped cfg[0].name was accepted - the character class matched anything")
	}
	// The rewrite keeps it: accepted.
	if !AnchorsRetained(original, "the `cfg[0].name` knob") {
		t.Error("a rewrite that kept cfg[0].name verbatim was rejected")
	}
}

// A hook with no anchors at all cannot lose one, so the guard must not block a rewrite of
// it - otherwise every prose-only hook becomes unshortenable.
func TestAnchors_NoAnchorsMeansNothingToLose(t *testing.T) {
	if !AnchorsRetained("a plain prose hook", "a shorter hook") {
		t.Error("a hook with no anchors must be shortenable")
	}
}

func keys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
