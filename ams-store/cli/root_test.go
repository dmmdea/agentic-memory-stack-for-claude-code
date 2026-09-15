package cli_test

import (
	"bytes"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/cli"
)

func run(t *testing.T, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	var out, errb bytes.Buffer
	code = cli.RunWith(cli.Env{Stdout: &out, Stderr: &errb, Stdin: strings.NewReader("")}, args)
	return code, out.String(), errb.String()
}

// The mandated command surface. Every verb exists from day one so a caller can discover
// the surface; the engines land in the tasks that follow.
func TestCLI_MandatedVerbsArePresent(t *testing.T) {
	want := []string{"derive", "lint", "gate", "sync", "lock", "judge-apply", "harvest"}
	got := cli.Verbs()
	if len(got) != len(want) {
		t.Fatalf("Verbs() = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("Verbs()[%d] = %q, want %q", i, got[i], want[i])
		}
	}
}

// TestCLI_StubVerbsExitNotImplementedWithSilentStdout covers the verbs whose engines have
// not landed yet.
//
// It iterates cli.StubVerbs, NOT cli.Verbs. Iterating every verb meant that the moment a
// verb was wired this test invoked it for real with no roots - which on 2026-09-15 ran
// sync against the operator's live history repo and committed 5 live stores. A test that
// asserts "not implemented" must be told which verbs those are, not guess.
func TestCLI_StubVerbsExitNotImplementedWithSilentStdout(t *testing.T) {
	stubs := cli.StubVerbs()
	if len(stubs) == 0 {
		t.Skip("every verb is wired")
	}
	for _, v := range stubs {
		code, stdout, stderr := run(t, v)
		if code != cli.ExitNotImplemented {
			t.Errorf("%s: exit = %d, want %d", v, code, cli.ExitNotImplemented)
		}
		if stdout != "" {
			t.Errorf("%s: stdout = %q, want empty - stdout is the product and a stub has no product", v, stdout)
		}
		if !strings.Contains(stderr, "not implemented") {
			t.Errorf("%s: stderr = %q, want it to say the verb is not implemented", v, stderr)
		}
	}
}

// TestCLI_WritingVerbsRefuseAnImplicitScope is the regression guard for the incident that
// produced it: `ams-store derive` with no scope meant "every populated store under the
// real projects root", so a test that walked the verb table with no arguments harvested
// hook: into every live fact file on the PC and re-rendered every live MEMORY.md. A verb
// that writes must be told what to write to.
func TestCLI_WritingVerbsRefuseAnImplicitScope(t *testing.T) {
	for _, v := range []string{"derive", "harvest"} {
		code, stdout, stderr := run(t, v)
		if code != cli.ExitUsage {
			t.Errorf("%s: exit = %d, want %d (bad invocation)", v, code, cli.ExitUsage)
		}
		if stdout != "" {
			t.Errorf("%s: stdout = %q, want empty", v, stdout)
		}
		for _, want := range []string{"--store", "--all", "--workspace"} {
			if !strings.Contains(stderr, want) {
				t.Errorf("%s: stderr = %q, want it to name %s", v, stderr, want)
			}
		}
	}
}

func TestCLI_UnknownVerbIsAUsageError(t *testing.T) {
	code, stdout, stderr := run(t, "frobnicate")
	if code != cli.ExitUsage {
		t.Errorf("exit = %d, want %d", code, cli.ExitUsage)
	}
	if stdout != "" {
		t.Errorf("stdout = %q, want empty", stdout)
	}
	if !strings.Contains(stderr, "frobnicate") {
		t.Errorf("stderr = %q, want it to name the unknown verb", stderr)
	}
}

func TestCLI_NoArgumentsIsAUsageError(t *testing.T) {
	code, stdout, stderr := run(t)
	if code != cli.ExitUsage {
		t.Errorf("exit = %d, want %d", code, cli.ExitUsage)
	}
	if stdout != "" {
		t.Errorf("stdout = %q, want empty", stdout)
	}
	if !strings.Contains(stderr, "usage:") {
		t.Errorf("stderr = %q, want the usage block", stderr)
	}
}

func TestCLI_HelpGoesToStdoutAndListsEveryVerb(t *testing.T) {
	for _, flag := range []string{"--help", "-h", "help"} {
		code, stdout, _ := run(t, flag)
		if code != cli.ExitOK {
			t.Errorf("%s: exit = %d, want 0", flag, code)
		}
		for _, v := range cli.Verbs() {
			if !strings.Contains(stdout, v) {
				t.Errorf("%s: help does not mention %q", flag, v)
			}
		}
	}
}

func TestCLI_VerbHelpCarriesTheBlueprintFlags(t *testing.T) {
	for verb, flag := range map[string]string{
		"derive":      "--stop-below",
		"lint":        "--summary-out",
		"gate":        "--stdin-payload",
		"sync":        "--watch",
		"lock":        "acquire",
		"judge-apply": "--max-migrations",
		"harvest":     "--store",
	} {
		code, stdout, _ := run(t, verb, "--help")
		if code != cli.ExitOK {
			t.Errorf("%s --help: exit = %d, want 0", verb, code)
		}
		if !strings.Contains(stdout, flag) {
			t.Errorf("%s --help does not document %q:\n%s", verb, flag, stdout)
		}
		if !strings.Contains(stdout, "usage: ams-store "+verb) {
			t.Errorf("%s --help does not open with its usage line:\n%s", verb, stdout)
		}
	}
}

func TestCLI_VersionLineNamesTheBuild(t *testing.T) {
	cli.BuildInfo = cli.Build{Version: "1.24.0", Commit: "abc1234", Built: "2026-09-15T00:00:00Z"}
	t.Cleanup(func() { cli.BuildInfo = cli.Build{Version: "dev", Commit: "none", Built: "unknown"} })

	code, stdout, _ := run(t, "--version")
	if code != cli.ExitOK {
		t.Errorf("exit = %d, want 0", code)
	}
	for _, want := range []string{"ams-store", "1.24.0", "abc1234", "2026-09-15T00:00:00Z", "go1."} {
		if !strings.Contains(stdout, want) {
			t.Errorf("--version output %q does not contain %q", stdout, want)
		}
	}
}

// Exit codes are a contract (blueprint section 1.2): a caller must be able to tell
// "skipped" from "done" from "refused" without parsing text.
func TestCLI_ExitCodeContract(t *testing.T) {
	for _, tc := range []struct {
		name string
		got  int
		want int
	}{
		{"ExitOK", cli.ExitOK, 0},
		{"ExitUnconverged", cli.ExitUnconverged, 1},
		{"ExitUsage", cli.ExitUsage, 2},
		{"ExitRefused", cli.ExitRefused, 3},
		{"ExitLocked", cli.ExitLocked, 4},
		{"ExitNetwork", cli.ExitNetwork, 5},
		{"ExitConflict", cli.ExitConflict, 6},
		{"ExitNotImplemented", cli.ExitNotImplemented, 64},
	} {
		if tc.got != tc.want {
			t.Errorf("%s = %d, want %d", tc.name, tc.got, tc.want)
		}
	}
}

// runStdin is run with a hook payload on stdin, for the gate.
func runStdin(t *testing.T, stdin string, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	var out, errb bytes.Buffer
	code = cli.RunWith(cli.Env{Stdout: &out, Stderr: &errb, Stdin: strings.NewReader(stdin)}, args)
	return code, out.String(), errb.String()
}
