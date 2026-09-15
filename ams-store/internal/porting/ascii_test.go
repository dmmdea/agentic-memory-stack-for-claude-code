package porting

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// TestPorting_SourceStaysASCII is blueprint section 1.3.
//
// The rule is inherited from the PowerShell library it replaces (LIB:7-9, :30): a
// BOM-less UTF-8 file read as ANSI by PowerShell 5.1 tokenises an em-dash as a smart
// quote, so the separator was always built from its code point and never typed. Go does
// not have that failure, but this module's source is edited by the same tools, on the
// same machines, beside those scripts - and the repo normalizes every text file, so a
// literal that survives one round trip is not evidence it survives the next.
//
// Keeping the source ASCII costs one escape and removes the whole class. This check is
// what keeps the rule applied: the merged scaffold had five literals in it, each written
// by someone who had read the rule.
func TestPorting_SourceStaysASCII(t *testing.T) {
	root := moduleRoot(t)
	var offenders []string
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			// testdata holds GOLDEN files, which are the product and are UTF-8 on
			// purpose: an ASCII golden could not prove the em-dash survives at all.
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
		for i, line := range strings.Split(string(b), "\n") {
			for _, c := range []byte(line) {
				if c > 0x7F {
					rel, _ := filepath.Rel(root, path)
					offenders = append(offenders, filepath.ToSlash(rel)+":"+itoa(i+1))
					break
				}
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(offenders)
	if len(offenders) > 0 {
		t.Fatalf("%d non-ASCII byte(s) in Go source: %v\n"+
			"Build the character from its code point instead: \"\u2014\", not the literal.",
			len(offenders), offenders)
	}
}
