package porting

import (
	"bufio"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// The parity gate (DESIGN:241, :369).
//
// The design's promise is that every behaviour the PowerShell suite pinned is still
// pinned after the rewrite. A promise like that decays silently: a scenario is dropped
// during a refactor, a Go test is renamed, a placeholder is left skipping, and nothing
// fails. So the gate READS the Pester files, extracts every `It`, maps it through the
// table in counterparts.go and asserts a Go test of that name exists in this module.
//
// It is deliberately built on the SOURCE files and on `go test -list`, not on a written
// count: a count agrees with itself forever.

// placeholderMark is the reason string the parallel builds used for a reserved
// counterpart name. It is assembled at run time so this file does not contain it.
var placeholderMark = "ported in " + "task"

// reIt matches the opening of a Pester scenario.
var reIt = regexp.MustCompile(`^\s*It\s+['"]`)

// moduleRoot is the ams-store module directory; repoRoot is its parent.
func moduleRoot(t *testing.T) string {
	t.Helper()
	// This test runs with the package directory as its working directory.
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err != nil {
		t.Fatalf("cannot locate the module root from %s: %v", root, err)
	}
	return root
}

func pesterDir(t *testing.T) string {
	t.Helper()
	dir := filepath.Join(filepath.Dir(moduleRoot(t)), "scripts", "windows", "tests")
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("cannot locate the Pester suite at %s: %v", dir, err)
	}
	return dir
}

// scenarios reads one Pester file and returns the line number of every `It`.
func scenarios(t *testing.T, path string) []int {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open %s: %v", path, err)
	}
	defer func() { _ = f.Close() }()
	var lines []int
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	n := 0
	for sc.Scan() {
		n++
		if reIt.MatchString(sc.Text()) {
			lines = append(lines, n)
		}
	}
	if err := sc.Err(); err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return lines
}

// TestPorting_EveryPesterScenarioHasANamedGoCounterpart is the gate itself.
func TestPorting_EveryPesterScenarioHasANamedGoCounterpart(t *testing.T) {
	dir := pesterDir(t)

	// The table, keyed the way the files are read.
	byKey := make(map[string]Counterpart, len(Counterparts))
	for _, c := range Counterparts {
		k := key(c.File, c.Line)
		if _, dup := byKey[k]; dup {
			t.Fatalf("the counterpart table lists %s twice", k)
		}
		byKey[k] = c
	}

	have := goTestNames(t)
	seen := make(map[string]bool, len(byKey))
	exempt := 0

	for _, file := range PesterFiles {
		path := filepath.Join(dir, file)
		lines := scenarios(t, path)
		if len(lines) == 0 {
			t.Fatalf("%s yielded no scenarios; the extractor, not the suite, is what broke", file)
		}
		for _, line := range lines {
			k := key(file, line)
			c, ok := byKey[k]
			if !ok {
				t.Errorf("%s has NO counterpart in the table.\n"+
					"A scenario that nobody mapped is a behaviour nobody ported. Add a row to"+
					" internal/porting/counterparts.go naming the Go test that carries it, or"+
					" an exemption saying which decision removed the behaviour.", k)
				continue
			}
			seen[k] = true
			if c.Test == "" {
				if strings.TrimSpace(c.Why) == "" {
					t.Errorf("%s is exempt with no reason. An exemption without a reason is a"+
						" scenario that was quietly dropped.", k)
				}
				exempt++
				continue
			}
			if !have[c.Test] {
				t.Errorf("%s maps to Go test %s, which does not exist in this module.\n"+
					"Either the test was renamed - keep the mapped name, the table is the"+
					" contract - or the scenario was never ported.", k, c.Test)
			}
		}
	}

	for k, c := range byKey {
		if !seen[k] {
			t.Errorf("the table maps %s -> %q, but no `It` starts on that line any more."+
				" The Pester file moved under the table; re-read it and fix the line.", k, c.Test)
		}
	}

	// Exactly one exemption, stated up front. A gate that lets exemptions accumulate is a
	// gate that ends up passing against an empty port.
	if exempt != 1 {
		t.Errorf("exempt scenarios = %d, want exactly 1 (MemoryCompactRobustness.Tests.ps1:420,"+
			" the -CatchUp fresh-then-stale run, whose spawn DESIGN:255-256 removes from the"+
			" PCs). Every other scenario must name a Go test.", exempt)
	}
}

// TestPorting_NoPlaceholderSkipsRemain.
//
// The port was built by five parallel tasks against reserved names that skipped with
// "ported in task N". A placeholder satisfies the counterpart gate above - `go test -list`
// reports it, because a skipped test is still a test - so the parity claim would pass over
// a suite that asserts nothing. This is the check that makes the other one mean something.
func TestPorting_NoPlaceholderSkipsRemain(t *testing.T) {
	root := moduleRoot(t)
	var offenders []string
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if d.Name() == "testdata" || d.Name() == ".git" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, "_test.go") {
			return nil
		}
		b, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		for i, line := range strings.Split(string(b), "\n") {
			// The match is the SKIP CALL, not the prose. The needle is assembled from
			// pieces because a checker that spells its own needle matches itself, and the
			// obvious fix - skipping this file by name - is a loophole the next
			// placeholder could be written through.
			if strings.Contains(line, "t.Skip") && strings.Contains(line, placeholderMark) {
				rel, _ := filepath.Rel(root, path)
				offenders = append(offenders, rel+":"+itoa(i+1))
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(offenders) > 0 {
		t.Fatalf("%d placeholder skip(s) remain: %v\n"+
			"Every one of them is a reserved counterpart name that reports as a test and"+
			" asserts nothing.", len(offenders), offenders)
	}
}

// goTestNames is every top-level test in the module, from `go test -list`.
//
// -list compiles each package's test binary and enumerates it WITHOUT running anything,
// so this asks the built binaries what they contain rather than grepping for a pattern
// that a rename or a build tag could make a lie.
func goTestNames(t *testing.T) map[string]bool {
	t.Helper()
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("go is not on PATH; the counterpart gate needs it to enumerate the suite")
	}
	cmd := exec.Command("go", "test", "-list", ".*", "./...")
	cmd.Dir = moduleRoot(t)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("go test -list: %v\n%s", err, out)
	}
	names := map[string]bool{}
	for _, line := range strings.Split(strings.ReplaceAll(string(out), "\r\n", "\n"), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "Test") && !strings.ContainsAny(line, " \t") {
			names[line] = true
		}
	}
	if len(names) == 0 {
		t.Fatalf("go test -list returned no test names:\n%s", out)
	}
	return names
}

func key(file string, line int) string { return file + ":" + itoa(line) }

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b [20]byte
	p := len(b)
	for i > 0 {
		p--
		b[p] = byte('0' + i%10)
		i /= 10
	}
	return string(b[p:])
}
