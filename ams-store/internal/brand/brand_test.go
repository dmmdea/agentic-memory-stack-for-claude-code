package brand

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The shared corpus every resolver (Go, Python, PowerShell) runs.
func TestResolveBrand_SharedFixture(t *testing.T) {
	f, err := os.Open(filepath.Join("..", "..", "..", "tests", "fixtures", "brand-routing-cases.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
	n := 0
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var c struct {
			Map    json.RawMessage `json:"map"`
			Path   string          `json:"path"`
			Text   string          `json:"text"`
			Expect *string         `json:"expect"`
		}
		if err := json.Unmarshal(line, &c); err != nil {
			t.Fatalf("fixture line %d: %v", n+1, err)
		}
		n++
		data := c.Map
		if string(data) == "null" {
			data = nil
		}
		// A case may carry unusable entries on purpose (a broken regex, a rule with no brand): the
		// contract routes with the entries that are good, so the problems are logged, not fatal.
		m, err := Parse(data)
		if err != nil {
			t.Logf("case %d: the map loads with problems (%v)", n, err)
		}
		want := ""
		if c.Expect != nil {
			want = *c.Expect
		}
		if got := m.Resolve(c.Path, c.Text); got != want {
			t.Errorf("case %d (path %q, text %q): brand = %q, want %q", n, c.Path, c.Text, got, want)
		}
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	if n < 10 {
		t.Fatalf("the shared fixture has %d cases; it must not shrink below the agreed set", n)
	}
}

// A brand map that routes nothing leaves behaviour exactly as today.
func TestResolveBrand_MissingMap(t *testing.T) {
	m, err := Load(filepath.Join(t.TempDir(), "no-such-brands.json"))
	if err != nil {
		t.Fatalf("a missing map must not be an error: %v", err)
	}
	if got := m.Resolve("any-workspace", "any text"); got != "" {
		t.Errorf("brand = %q, want none", got)
	}
	if got, err := Load(""); err != nil || got.Resolve("x", "y") != "" {
		t.Errorf("no path configured must be brand-neutral: %v", err)
	}
	var nilMap *Map
	if nilMap.Resolve("x", "y") != "" || nilMap.IsShared("z") {
		t.Error("a nil map must route nothing")
	}
}

func TestResolveBrand_MalformedAndEmptyMapsAreNeutral(t *testing.T) {
	dir := t.TempDir()
	bad := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(bad, []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	m, err := Load(bad)
	if err == nil {
		t.Error("a malformed map should be reported so the caller can log it")
	}
	if m == nil || m.Resolve("x", "y") != "" {
		t.Error("a malformed map must still resolve to no brand, never crash")
	}
	emptyFile := filepath.Join(dir, "empty.json")
	if err := os.WriteFile(emptyFile, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if m, err := Load(emptyFile); err != nil || m.Resolve("x", "y") != "" {
		t.Errorf("an empty file must be brand-neutral: %v", err)
	}
}

func TestResolveBrand_OneBadPatternDoesNotTurnTheRestOff(t *testing.T) {
	m, err := Parse([]byte(`{"rules":[{"pattern":"(unclosed","brand":"x"},{"pattern":"good","brand":"y"}]}`))
	if err == nil {
		t.Error("the bad pattern should be reported")
	}
	if got := m.Resolve("a-good-path", ""); got != "y" {
		t.Errorf("brand = %q, want y from the rule that compiled", got)
	}
}

func TestResolveBrand_SharedBrandIsReturnedAsIs(t *testing.T) {
	m, err := Parse([]byte(`{"rules":[{"pattern":"lab","brand":"shared-lab"}],"shared_brands":["shared-lab"]}`))
	if err != nil {
		t.Fatal(err)
	}
	if got := m.Resolve("the-lab", ""); got != "shared-lab" {
		t.Errorf("brand = %q; sharing is the gate's job, the resolver returns the label", got)
	}
	if !m.IsShared("shared-lab") || m.IsShared("other") {
		t.Error("IsShared must answer from shared_brands only")
	}
}

// Path separators are ONE character to the matcher (C3). The operator writes a rule with "/"
// ("projects/client-a") and it must find the same place spelled as a Windows path, a Unix path or
// Claude Code's hyphenated workspace slug - the string the judge actually passes. A resolver that
// compared the raw strings would never match a slug, and every migrated fact would go untagged.
func TestResolveBrand_SeparatorsAreOneCharacter(t *testing.T) {
	m, err := Parse([]byte(`{"rules":[` +
		`{"pattern":"projects/client-a","brand":"brand-a"},` +
		`{"pattern":"beta[\\\\/ -]+labs","brand":"brand-b"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct{ path, want string }{
		{"g--My-Drive-Projects-Client-A", "brand-a"},       // the hyphenated slug
		{`G:\My Drive\Projects\Client-A\notes`, "brand-a"}, // a Windows path: backslashes and a space
		{"/home/u/projects/client-a", "brand-a"},           // a Unix path
		{"PROJECTS-CLIENT-A", "brand-a"},                   // case does not matter
		{"C--Work-beta-labs", "brand-b"},                   // a separator class inside the pattern
		{`C:\Work\beta labs\memory`, "brand-b"},
		{"g--My-Drive-Projects-Client-B", ""}, // another place: no brand
		{"", ""},                              // no path routes nothing
	}
	for _, c := range cases {
		if got := m.Resolve(c.path, ""); got != c.want {
			t.Errorf("Resolve(%q) = %q, want %q", c.path, got, c.want)
		}
	}
}

// An empty path routes nothing even when a pattern would match the empty string.
func TestResolveBrand_EmptyPathRoutesNothing(t *testing.T) {
	m, err := Parse([]byte(`{"rules":[{"pattern":".*","brand":"x"}],"content_rule_workspaces":[".*"],` +
		`"content_rules":[{"pattern":"word","brand":"y"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if got := m.Resolve("", "word"); got != "" {
		t.Errorf("Resolve(\"\") = %q, want no brand", got)
	}
	if got := m.Resolve("some-path", "word"); got != "x" {
		t.Errorf("Resolve(some-path) = %q, want the catch-all rule's brand x", got)
	}
}

// Only the PATH side is normalised. A content rule reads the fact's own words: a space in the
// pattern is a space in the text, not a separator.
func TestResolveBrand_ContentRulesAreMatchedAsWritten(t *testing.T) {
	m, err := Parse([]byte(`{"content_rule_workspaces":["mixed"],` +
		`"content_rules":[{"pattern":"beta shop","brand":"brand-b"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if got := m.Resolve("g--My-Drive-mixed", "the beta shop order"); got != "brand-b" {
		t.Errorf("brand = %q, want brand-b: the pattern's space matches the text's space", got)
	}
	if got := m.Resolve("g--My-Drive-mixed", "the beta-shop order"); got != "" {
		t.Errorf("brand = %q, want none: a content pattern is not rewritten to hyphens", got)
	}
}
