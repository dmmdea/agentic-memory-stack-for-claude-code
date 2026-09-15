package cli_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/cli"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// TestCLI_CrossPlatformIdenticalOutput is the counterpart of MemoryStoreLib.Tests.ps1:235.
//
// The Pester scenario asserted that the store library LOADS under PowerShell 5.1 and
// round-trips a multi-byte index byte for byte. That mechanic is PowerShell's: 5.1 reads a
// BOM-less UTF-8 file as ANSI and mojibakes every em-dash, so the parity had to be proved
// in a child 5.1 process. Go has no such split.
//
// What survives the port is the OBLIGATION the scenario existed for: the fleet runs this
// binary on Windows and on the Linux authority against the SAME shared history, so any
// byte of its product that depends on the host is a byte two PCs will disagree about and
// re-commit at each other forever. The golden files here are committed, and CI runs this
// test on ubuntu-latest and on windows-latest: a difference between the two shows up as
// one of them failing against the shared golden, which is the cross-platform compare the
// blueprint asks for.
//
// The fixture is deliberately hostile to the things that do differ across platforms: a
// CRLF index, an em-dash in every line, a Windows-style path inside a hook, mixed-case
// slugs, and a doctrine entry sorted last in the file.
func TestCLI_CrossPlatformIdenticalOutput(t *testing.T) {
	for _, newline := range []string{"\n", "\r\n"} {
		name := "LF"
		if newline == "\r\n" {
			name = "CRLF"
		}
		t.Run(name, func(t *testing.T) {
			sb := testutil.NewSandbox(t)
			dir := sb.AddStoreNL("ws", goldenIndexLines(), goldenFacts(), newline)

			code, _, stderr := run(t, "derive", "--store", dir,
				"--projects-root", sb.ProjectsRoot, "--state-root", sb.StateRoot,
				"--now", "2026-09-15T12:00:00Z")
			if code != cli.ExitOK {
				t.Fatalf("derive exit = %d: %s", code, stderr)
			}

			got, err := os.ReadFile(filepath.Join(dir, store.IndexName))
			if err != nil {
				t.Fatal(err)
			}
			assertGolden(t, "derived-index.golden", string(got))

			// The gate's advisory block is the other product: it is what the harness shows
			// the model, so a platform-dependent byte there is a platform-dependent prompt.
			// It needs a store over the sync limit, because a silent gate is a golden that
			// asserts nothing.
			big := testutil.NewSandbox(t)
			bigDir := big.AddStoreNL("ws", testutil.BigIndex(70), testutil.BigIndexFacts(70), newline)
			payload := `{"tool_input":{"file_path":` + mustJSON(t, filepath.Join(bigDir, store.IndexName)) + `}}`
			code, stdout, _ := runStdin(t, payload, "gate",
				"--state-root", big.StateRoot, "--projects-root", big.ProjectsRoot,
				"--now", "2026-09-15T12:00:00Z")
			if code != cli.ExitOK {
				t.Fatalf("gate exit = %d, want 0", code)
			}
			if stdout == "" {
				t.Fatal("the gate said nothing about an over-limit index; the golden would assert nothing")
			}
			// One golden per newline here, and ONE shared golden for the derived index
			// above. That asymmetry is the invariant itself: derive always writes LF, so
			// its product must not depend on what the file used to be; the gate reports
			// the size of the file the harness just wrote, and a CRLF file genuinely is
			// one byte per line larger. Sharing this golden would have asserted something
			// false and been "fixed" by loosening the comparison.
			assertGolden(t, "gate-advisory-"+strings.ToLower(name)+".golden", stdout)
		})
	}
}

func goldenIndexLines() []string {
	return []string{
		"# Memory Index",
		"",
		"Some preamble line the parser keeps verbatim.",
		"- [Zulu path](zulu.md) " + emDash + " config at `C:\\Users\\x\\.claude` and port 18791",
		"- [Alpha](Alpha.md) " + emDash + " an ordinary fact with an " + emDash + " inside it",
		"- [Rule](rule.md) " + emDash + " NEVER ship without the gate",
		"- [Mid](mid.md) " + emDash + " a middling fact",
	}
}

func goldenFacts() map[string]string {
	return map[string]string{
		"zulu.md":  testutil.FactFile("zulu", "the zulu fact", "project", "zulu body"),
		"Alpha.md": testutil.FactFile("Alpha", "the alpha fact", "project", "alpha body"),
		"rule.md":  testutil.FactFile("rule", "a standing order", "feedback", "rule body"),
		"mid.md":   testutil.FactFile("mid", "the mid fact", "reference", "mid body"),
	}
}

// assertGolden compares against the committed file, and regenerates it under -update.
//
// The comparison is on raw BYTES, never on lines: a golden compared line by line would
// accept a CRLF file on Windows and an LF file on Linux as "the same output", which is
// precisely the difference this test exists to catch.
func assertGolden(t *testing.T, name, got string) {
	t.Helper()
	p := filepath.Join("testdata", name)
	if os.Getenv("AMS_UPDATE_GOLDEN") == "1" {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(got), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("read golden %s: %v (regenerate with AMS_UPDATE_GOLDEN=1)", p, err)
	}
	if string(want) != got {
		t.Fatalf("output differs from the committed golden %s.\n--- want (%d B) ---\n%s\n--- got (%d B) ---\n%s",
			p, len(want), visible(string(want)), len(got), visible(got))
	}
}

// visible makes a CR visible in a failure message; an invisible byte difference is the
// one this test is most likely to be reporting.
func visible(s string) string { return strings.ReplaceAll(s, "\r", "<CR>") }
