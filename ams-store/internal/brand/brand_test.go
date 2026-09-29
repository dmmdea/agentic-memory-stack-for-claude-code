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
		m, err := Parse(data)
		if err != nil {
			t.Fatalf("case %d: map does not load: %v", n, err)
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
