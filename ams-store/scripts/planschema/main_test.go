package main

import (
	"os"
	"path/filepath"
	"testing"
)

// The checked-in schema must be what this generator produces. An edit to plan.go that
// forgets the schema - a new verb, a renamed field, a widened enum - leaves the Python
// producer validating against yesterday's contract and the nightly silently stops
// deciding for the store whose plan no longer validates. CI catches it here instead.
func TestSchema_CheckedInFileIsCurrent(t *testing.T) {
	want, err := Render()
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	path := filepath.Join("..", "..", "..", "docs", "schemas", "judge-plan.schema.json")
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v\nRefresh it: cd ams-store && go run ./scripts/planschema", path, err)
	}
	if string(got) != string(want) {
		t.Fatalf("docs/schemas/judge-plan.schema.json is stale (%d bytes on disk, %d generated).\n"+
			"Refresh it: cd ams-store && go run ./scripts/planschema", len(got), len(want))
	}
}

// Render must be deterministic: the file is compared byte for byte, so a map iteration
// leaking into the output would make CI fail at random.
func TestSchema_RenderIsDeterministic(t *testing.T) {
	first, err := Render()
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	for i := 0; i < 8; i++ {
		again, err := Render()
		if err != nil {
			t.Fatalf("render %d: %v", i, err)
		}
		if string(again) != string(first) {
			t.Fatalf("render %d differs from the first render: the output is not deterministic", i)
		}
	}
}
