package frontmatter

import "testing"

func block(lines ...string) string {
	out := ""
	for i, l := range lines {
		if i > 0 {
			out += "\n"
		}
		out += l
	}
	return out
}

// TestMergeBlocks_HookTakesTheJudgeUnlessLocalChangedIt is the `hook:` rule in all four
// of its shapes. Getting it wrong in either direction is a real failure: always taking
// the judge overwrites a PC's own harvest, and never taking it makes the judge's SHORTEN
// a no-op fleet-wide.
func TestMergeBlocks_HookTakesTheJudgeUnlessLocalChangedIt(t *testing.T) {
	base := block("name: N", `hook: "base"`)

	cases := []struct {
		name         string
		ours, theirs string
		want         string
	}{
		{"neither changed it", block("name: N", `hook: "base"`), block("name: N", `hook: "base"`), `hook: "base"`},
		{"only the judge changed it", block("name: N", `hook: "base"`), block("name: N", `hook: "judge"`), `hook: "judge"`},
		{"only the local side changed it", block("name: N", `hook: "local"`), block("name: N", `hook: "base"`), `hook: "local"`},
		{"both changed it: local wins", block("name: N", `hook: "local"`), block("name: N", `hook: "judge"`), `hook: "local"`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			merged, conflicts := MergeBlocks(MergeInput{Base: base, Ours: c.ours, Theirs: c.theirs, BasePresent: true, OursNewer: true})
			if !containsLine(merged, c.want) {
				t.Fatalf("expected %q in\n%s", c.want, merged)
			}
			if len(conflicts) != 0 {
				t.Fatalf("hook: never conflicts, got %v", conflicts)
			}
		})
	}
}

// TestMergeBlocks_ModifiedIsAdvisoryOnly: the stamp is model-written. It is never a
// tiebreak and never a conflict; it simply follows the side that committed later.
func TestMergeBlocks_ModifiedIsAdvisoryOnly(t *testing.T) {
	base := block("name: N", "metadata:", "  modified: 2026-01-01")
	ours := block("name: N", "metadata:", "  modified: 2099-12-31")
	theirs := block("name: N", "metadata:", "  modified: 1999-01-01")

	merged, conflicts := MergeBlocks(MergeInput{Base: base, Ours: ours, Theirs: theirs, BasePresent: true, OursNewer: false})
	if len(conflicts) != 0 {
		t.Fatalf("modified: must never be a conflict, got %v", conflicts)
	}
	if !containsLine(merged, "  modified: 1999-01-01") {
		t.Fatalf("the newer-COMMITTING side's stamp wins, whatever the dates say:\n%s", merged)
	}

	// The side that wins having no stamp at all drops the key rather than inventing one.
	merged, _ = MergeBlocks(MergeInput{
		Base: base, Ours: ours, Theirs: block("name: N", "metadata:"),
		BasePresent: true, OursNewer: false,
	})
	if containsPrefix(merged, "  modified:") {
		t.Fatalf("the key should have been dropped:\n%s", merged)
	}
}

// TestMergeBlocks_MigratedIsCarried: whichever side knows the mem0 id, the merged file
// keeps it - that id is what lets the judge UPDATE a re-created slug instead of filing a
// fresh variant every night.
func TestMergeBlocks_MigratedIsCarried(t *testing.T) {
	ours := block("name: N", `description: "local"`)
	theirs := block("name: N", `description: "judge"`, "migrated: 8f3c9a21")

	merged, _ := MergeBlocks(MergeInput{Ours: ours, Theirs: theirs, OursNewer: true})
	if !containsLine(merged, "migrated: 8f3c9a21") {
		t.Fatalf("the id must be carried when only one side has it:\n%s", merged)
	}

	// Both sides have one and they differ: the newer-committing side wins and it is
	// reportable.
	merged, conflicts := MergeBlocks(MergeInput{
		Ours:   block("name: N", "migrated: aaa"),
		Theirs: block("name: N", "migrated: bbb"),
	})
	if !containsLine(merged, "migrated: bbb") {
		t.Fatalf("theirs should have won:\n%s", merged)
	}
	if len(conflicts) != 1 || conflicts[0] != FieldMigrated {
		t.Fatalf("two different ids must be reported, got %v", conflicts)
	}
}

// TestMergeBlocks_OrdinaryFieldsThreeWay covers name/description/type and any key this
// tool does not know about.
func TestMergeBlocks_OrdinaryFieldsThreeWay(t *testing.T) {
	base := block("name: N", `description: "base"`, "metadata:", "  type: project", "custom: keep-me")
	ours := block("name: N", `description: "ours"`, "metadata:", "  type: project", "custom: keep-me")
	theirs := block("name: N", `description: "base"`, "metadata:", "  type: feedback", "custom: keep-me")

	merged, conflicts := MergeBlocks(MergeInput{Base: base, Ours: ours, Theirs: theirs, BasePresent: true, OursNewer: true})
	if len(conflicts) != 0 {
		t.Fatalf("each side changed a DIFFERENT field, which is not a conflict: %v", conflicts)
	}
	if !containsLine(merged, `description: "ours"`) {
		t.Fatalf("our description change was lost:\n%s", merged)
	}
	if !containsLine(merged, "  type: feedback") {
		t.Fatalf("their type change was lost - and this one flips doctrine:\n%s", merged)
	}
	if !containsLine(merged, "custom: keep-me") {
		t.Fatalf("an unknown key must be preserved:\n%s", merged)
	}

	// Both changed the same field differently: the newer-committing side wins and the
	// loss is reported.
	theirs2 := block("name: N", `description: "theirs"`, "metadata:", "  type: project", "custom: keep-me")
	merged, conflicts = MergeBlocks(MergeInput{Base: base, Ours: ours, Theirs: theirs2, BasePresent: true, OursNewer: false})
	if !containsLine(merged, `description: "theirs"`) {
		t.Fatalf("the newer-committing side must win:\n%s", merged)
	}
	if len(conflicts) != 1 || conflicts[0] != "description" {
		t.Fatalf("the losing field must be reported, got %v", conflicts)
	}
}

// TestMergeBlocks_KeyOrderIsDeterministic: two PCs merging the same pair must produce the
// same bytes, or the merged file differs on each side and conflicts with itself forever.
func TestMergeBlocks_KeyOrderIsDeterministic(t *testing.T) {
	ours := block("name: N", `description: "d"`, `hook: "h"`, "metadata:", "  type: project")
	theirs := block(`description: "d"`, "name: N", "migrated: id1", "metadata:", "  type: project")

	first, _ := MergeBlocks(MergeInput{Ours: ours, Theirs: theirs, OursNewer: true})
	for i := 0; i < 5; i++ {
		again, _ := MergeBlocks(MergeInput{Ours: ours, Theirs: theirs, OursNewer: true})
		if again != first {
			t.Fatalf("merge is not deterministic:\n%s\nvs\n%s", first, again)
		}
	}
	// Ours' order leads; a key only theirs has is appended.
	lines := splitBlock(first)
	if len(lines) == 0 || lines[0].key != "name" {
		t.Fatalf("ours' key order must lead:\n%s", first)
	}
	if lines[len(lines)-1].key != "migrated" {
		t.Fatalf("a theirs-only key is appended last:\n%s", first)
	}
}

func containsLine(block, want string) bool {
	for _, l := range splitBlock(block) {
		if l.raw == want {
			return true
		}
	}
	return false
}

func containsPrefix(block, prefix string) bool {
	for _, l := range splitBlock(block) {
		if len(l.raw) >= len(prefix) && l.raw[:len(prefix)] == prefix {
			return true
		}
	}
	return false
}
