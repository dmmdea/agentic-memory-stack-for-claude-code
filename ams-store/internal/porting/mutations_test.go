package porting

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The mutation table's own anchor gate.
//
// Mutations is read by exactly one program - scripts/mutationgate - driven by a shell
// script the docs themselves say is never a CI job. So the table rots in silence: a
// refactor moves the line a hunk is anchored to, and nobody is told until someone
// happens to run the gate by hand. That is what happened to `index-untracked`. Commit
// 3cde555 moved the index-exclusion pathspec out of internal/merge/engine.go into
// internal/gitx.AddFactFiles; three commits later the branch was documented as "red 25,
// survived 0, broken 0" while the gate actually refused that mutation and exited 1.
//
// The counterpart table is anchored by a real Go test that re-reads the Pester files.
// This is the same idea for the mutation table: the contract is checked against the
// SOURCE on every `go test ./...`, so a moved anchor fails beside the refactor that
// moved it instead of waiting for a manual run.

// TestPorting_EveryMutationAnchorStillExists asserts each hunk's Old text occurs exactly
// once in the file it names.
//
// Exactly once is the gate's own rule, not a stricter one: mutationgate's apply refuses
// anything else, because a substitution that hit no occurrence reports a rule green that
// was never mutated, and one that hit the wrong occurrence reports a rule green that was
// mutated somewhere else. The comparison is over the file's RAW bytes with no newline
// normalization, exactly as apply does it - a table that would not apply must not pass
// here just because this test was more forgiving than the program it defends.
func TestPorting_EveryMutationAnchorStillExists(t *testing.T) {
	root := moduleRoot(t)
	cache := map[string]string{}
	readFile := func(rel string) (string, bool) {
		if text, ok := cache[rel]; ok {
			return text, true
		}
		raw, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
		if err != nil {
			t.Errorf("a mutation names %s, which cannot be read: %v.\n"+
				"The file moved or was deleted; re-anchor the entry or retire it.", rel, err)
			return "", false
		}
		cache[rel] = string(raw)
		return cache[rel], true
	}

	seenID := map[string]bool{}
	for _, m := range Mutations {
		if m.ID == "" {
			t.Errorf("a mutation has no ID; the gate addresses rows by id and cannot name this one")
			continue
		}
		if seenID[m.ID] {
			// find() returns the FIRST match, so a duplicate id is a row that can never
			// be applied and never be reported - it would simply not exist.
			t.Errorf("the mutation table lists %q twice", m.ID)
		}
		seenID[m.ID] = true
		if m.Pending() {
			continue
		}
		for i, h := range m.Hunks {
			rel := h.File
			if rel == "" {
				rel = m.File
			}
			if rel == "" {
				t.Errorf("hunk %d of %s names no file", i+1, m.ID)
				continue
			}
			text, ok := readFile(rel)
			if !ok {
				continue
			}
			if n := strings.Count(text, h.Old); n != 1 {
				t.Errorf("hunk %d of %s: its Old text occurs %d times in %s, want exactly 1.\n"+
					"The mutation gate refuses this row, so the rule %q is NOT defended -"+
					" re-anchor the hunk at the code that carries the rule now.",
					i+1, m.ID, n, rel, m.Rule)
			}
			if h.Old == h.New {
				t.Errorf("hunk %d of %s changes nothing; a mutation that is a no-op is a"+
					" rule reported red by accident or green by construction", i+1, m.ID)
			}
		}
	}
}

// TestPorting_EveryMutationNamesATestThatExists closes the rename hole the counterpart
// gate leaves open.
//
// The counterpart table protects the 95 Pester-mapped names. The tests the mutation
// table names are mostly NOT in it - they are spec fixtures from blueprint 10.1 - so
// renaming one slipped past every automated check and was caught only by the manual
// gate's NOTEST branch. Enumerating from `go test -list` is the same machinery the
// counterpart gate already uses, which is why this costs three lines rather than a
// second mechanism.
//
// A PENDING row is exempt by design: the table deliberately carries rules whose test the
// owning task has not written yet, and the gate reports those pending rather than
// failing. A row WITH hunks has no such excuse.
func TestPorting_EveryMutationNamesATestThatExists(t *testing.T) {
	have := goTestNames(t)
	for _, m := range Mutations {
		if m.Pending() {
			continue
		}
		if m.Test == "" {
			t.Errorf("mutation %s names no test; nothing can go red for rule %q", m.ID, m.Rule)
			continue
		}
		if !have[m.Test] {
			t.Errorf("mutation %s names Go test %s, which does not exist in this module.\n"+
				"Either the test was renamed - keep the named test, the table is the"+
				" contract - or the rule %q lost its only defence.", m.ID, m.Test, m.Rule)
		}
		if m.Package == "" {
			t.Errorf("mutation %s names no package; the gate has nothing to run %s in", m.ID, m.Test)
		}
	}
}
