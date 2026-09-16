package porting

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// TestPorting_EveryPackageThatResolvesTheHomeSandboxesIt.
//
// Two live incidents on 2026-09-15, hours apart, both from the same shape: a test that did
// not inject its roots ran against the operator's real profile. One harvested a hook: line
// into 248 live fact files across five workspaces; the other committed five live stores
// into the real history repo. Neither was caught by review, because in both cases the code
// was correct and the TEST was the thing reaching production.
//
// The rule that prevents it is "a package whose code can resolve the user's home runs its
// tests with the home moved". That rule lives in exactly one place
// (testutil.RunWithSandboxHome) and this is what keeps it applied: a package that starts
// calling store.DefaultRoots tomorrow and has no TestMain fails here, rather than the next
// time someone runs the suite on a machine with real stores on it.
func TestPorting_EveryPackageThatResolvesTheHomeSandboxesIt(t *testing.T) {
	root := moduleRoot(t)

	// Packages whose NON-test source can resolve the home.
	resolves := map[string]bool{}
	// Packages that have a TestMain, and whether it calls the shared sandbox.
	hasMain := map[string]bool{}
	sandboxed := map[string]bool{}
	// A package with no test files has no test that can reach production. It still
	// appears here the day it gains one, because that is when the risk appears.
	hasTests := map[string]bool{}

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
		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		b, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		text := string(b)
		dir, _ := filepath.Rel(root, filepath.Dir(path))
		dir = filepath.ToSlash(dir)

		if strings.HasSuffix(path, "_test.go") {
			hasTests[dir] = true
			if strings.Contains(text, "func TestMain(") {
				hasMain[dir] = true
				if strings.Contains(text, "testutil.RunWithSandboxHome") {
					sandboxed[dir] = true
				}
			}
			return nil
		}
		// The needle is a CALL, not the identifier: doc comments name these functions
		// while explaining the rule, and a comment cannot reach anyone's home directory.
		// internal/store DEFINES DefaultRoots, so it resolves the home by construction.
		if strings.Contains(text, "DefaultRoots(") || strings.Contains(text, "func HomeDir(") {
			resolves[dir] = true
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(resolves) == 0 {
		t.Fatal("no package appears to resolve the home at all; the scan, not the module, is what broke")
	}

	var offenders []string
	for dir := range resolves {
		if !hasTests[dir] {
			continue
		}
		switch {
		case !hasMain[dir]:
			offenders = append(offenders, dir+" (no TestMain)")
		case !sandboxed[dir]:
			offenders = append(offenders, dir+" (TestMain does not call testutil.RunWithSandboxHome)")
		}
	}
	sort.Strings(offenders)
	if len(offenders) > 0 {
		t.Fatalf("these packages can resolve the operator's home but do not move it for their tests: %v\n"+
			"Add `func TestMain(m *testing.M) { os.Exit(testutil.RunWithSandboxHome(m)) }`."+
			" A test that forgets to inject its roots must land in a temp directory, not in"+
			" the operator's live stores.", offenders)
	}
}
