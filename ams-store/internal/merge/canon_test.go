package merge

import "testing"

// TestCanon_IgnoresOnlyLineEndingsAndTheTwoAdvisoryKeYS pins the exact width of the
// normalized comparison. It ignores CRLF and the `hook:` / `modified:` lines and NOTHING
// else: widen it and a real edit stops counting as a modification, so the deletion table
// silently drops somebody's work; narrow it and a routine harvest resurrects every
// deletion the judge made.
func TestCanon_IgnoresOnlyLineEndingsAndTheTwoAdvisoryKeys(t *testing.T) {
	base := "---\nname: N\ndescription: \"d\"\nhook: \"one\"\nmetadata:\n  type: project\n  modified: 2026-01-01\n---\n\nbody\n"

	crlf := ""
	for _, ch := range base {
		if ch == '\n' {
			crlf += "\r\n"
			continue
		}
		crlf += string(ch)
	}
	if !NormalizedEqual([]byte(base), []byte(crlf)) {
		t.Fatal("a CRLF-only difference must compare equal")
	}

	hookChanged := "---\nname: N\ndescription: \"d\"\nhook: \"two\"\nmetadata:\n  type: project\n  modified: 2026-01-01\n---\n\nbody\n"
	if !NormalizedEqual([]byte(base), []byte(hookChanged)) {
		t.Fatal("a hook-only difference must compare equal")
	}

	modChanged := "---\nname: N\ndescription: \"d\"\nhook: \"one\"\nmetadata:\n  type: project\n  modified: 2099-12-31\n---\n\nbody\n"
	if !NormalizedEqual([]byte(base), []byte(modChanged)) {
		t.Fatal("a modified-only difference must compare equal")
	}

	for _, real := range []string{
		"---\nname: OTHER\ndescription: \"d\"\nhook: \"one\"\nmetadata:\n  type: project\n  modified: 2026-01-01\n---\n\nbody\n",
		"---\nname: N\ndescription: \"other\"\nhook: \"one\"\nmetadata:\n  type: project\n  modified: 2026-01-01\n---\n\nbody\n",
		"---\nname: N\ndescription: \"d\"\nhook: \"one\"\nmetadata:\n  type: feedback\n  modified: 2026-01-01\n---\n\nbody\n",
		"---\nname: N\ndescription: \"d\"\nhook: \"one\"\nmetadata:\n  type: project\n  modified: 2026-01-01\n---\n\nother body\n",
	} {
		if NormalizedEqual([]byte(base), []byte(real)) {
			t.Fatalf("a real difference must NOT compare equal: %q", real)
		}
	}

	// Canon is a comparison form only: it must never be what gets written.
	if string(Canon([]byte(base))) == base {
		t.Fatal("the fixture is wrong: canon should differ from the original here")
	}
}

func TestOverTrigger_MinReducerAndDeterministicRender(t *testing.T) {
	ours := []byte(`{"over_trigger_since":{"w1":"2026-09-05T00:00:00Z","w2":"2026-09-20T00:00:00Z"}}`)
	theirs := []byte(`{"over_trigger_since":{"w1":"2026-09-12T00:00:00Z","w3":"2026-09-01T00:00:00Z"}}`)

	merged, err := MergeOverTrigger(ours, theirs)
	if err != nil {
		t.Fatal(err)
	}
	got, err := ParseOverTrigger(merged)
	if err != nil {
		t.Fatal(err)
	}
	if got.OverTriggerSince["w1"] != "2026-09-05T00:00:00Z" {
		t.Fatalf("min wins for a key both sides know: %s", merged)
	}
	if got.OverTriggerSince["w2"] != "2026-09-20T00:00:00Z" || got.OverTriggerSince["w3"] != "2026-09-01T00:00:00Z" {
		t.Fatalf("a key only one side knows must be kept: %s", merged)
	}

	// The reduction is commutative and the render deterministic, or two PCs converge on
	// different bytes and the file conflicts with itself forever.
	other, err := MergeOverTrigger(theirs, ours)
	if err != nil {
		t.Fatal(err)
	}
	if string(other) != string(merged) {
		t.Fatalf("min must be commutative: %q vs %q", merged, other)
	}

	// An unparseable stamp loses to a parseable one rather than poisoning the result.
	junk := []byte(`{"over_trigger_since":{"w1":"not a timestamp"}}`)
	mixed, err := MergeOverTrigger(junk, ours)
	if err != nil {
		t.Fatal(err)
	}
	gotMixed, _ := ParseOverTrigger(mixed)
	if gotMixed.OverTriggerSince["w1"] != "2026-09-05T00:00:00Z" {
		t.Fatalf("an unparseable stamp must lose: %s", mixed)
	}
}
