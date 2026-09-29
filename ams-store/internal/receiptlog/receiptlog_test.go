package receiptlog

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type row struct {
	TS     string `json:"ts"`
	Store  string `json:"workspace"`
	Status string `json:"status"`
	N      int    `json:"n"`
}

func lines(t *testing.T, path string) []string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		t.Fatal(err)
	}
	var out []string
	for _, l := range strings.Split(string(b), "\n") {
		if strings.TrimSpace(l) != "" {
			out = append(out, l)
		}
	}
	return out
}

// The live file is bounded: at the limit it rotates, generations shift up, and only Keep
// rotated generations survive.
func TestAppend_RotatesAtTheLimitAndKeepsThreeGenerations(t *testing.T) {
	path := filepath.Join(t.TempDir(), "r.jsonl")
	const limit = 300 // a handful of rows
	for i := 0; i < 60; i++ {
		if err := AppendLimit(path, row{TS: "t", Store: "ws", Status: "ok", N: i}, limit); err != nil {
			t.Fatalf("append %d: %v", i, err)
		}
	}
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Size() >= limit+200 {
		t.Errorf("live file is %d B; it must rotate at %d B", fi.Size(), limit)
	}
	for n := 1; n <= Keep; n++ {
		if _, err := os.Stat(Generation(path, n)); err != nil {
			t.Errorf("generation %d is missing: %v", n, err)
		}
	}
	if _, err := os.Stat(Generation(path, Keep+1)); err == nil {
		t.Errorf("generation %d exists; only %d rotated generations are kept", Keep+1, Keep)
	}
	// The newest generation continues exactly where it left off: the last row of .1 is the
	// one before the first row of the live file.
	live, gen1 := lines(t, path), lines(t, Generation(path, 1))
	var first, last row
	if err := json.Unmarshal([]byte(live[0]), &first); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(gen1[len(gen1)-1]), &last); err != nil {
		t.Fatal(err)
	}
	if last.N+1 != first.N {
		t.Errorf("generation 1 ends at row %d and the live file starts at %d; rotation lost or reordered rows", last.N, first.N)
	}
}

// A tail by row count reads across the newest rotated generation, so a rotation that just
// happened does not blind the reader.
func TestReadTailLines_ReadsAcrossTheNewestGeneration(t *testing.T) {
	path := filepath.Join(t.TempDir(), "r.jsonl")
	for i := 0; i < 40; i++ {
		if err := AppendLimit(path, row{TS: "t", Store: "ws", Status: "ok", N: i}, 250); err != nil {
			t.Fatal(err)
		}
	}
	live := lines(t, path)
	if len(live) >= 10 {
		t.Fatalf("the live file holds %d rows; the fixture must leave it short", len(live))
	}
	got, err := ReadTailLines(path, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 10 {
		t.Fatalf("tail = %d rows, want 10 (live %d + the newest generation)", len(got), len(live))
	}
	var last row
	if err := json.Unmarshal([]byte(got[len(got)-1]), &last); err != nil || last.N != 39 {
		t.Errorf("newest row = %+v (%v), want n=39", last, err)
	}
	if none, err := ReadTailLines(filepath.Join(t.TempDir(), "absent"), 5); err != nil || len(none) != 0 {
		t.Errorf("a missing file is no rows and no error: %v %v", none, err)
	}
}

func sameAbort(prev, cur map[string]any) bool {
	st, _ := cur["status"].(string)
	return strings.HasPrefix(st, "aborted-") && prev["status"] == cur["status"] && prev["workspace"] == cur["workspace"]
}

// Consecutive identical rows collapse into one with a repeat count; a different row in
// between ends the run.
func TestAppendCollapsing_CollapsesConsecutiveIdenticalRows(t *testing.T) {
	path := filepath.Join(t.TempDir(), "r.jsonl")
	appendOne := func(ts, ws, status string) {
		t.Helper()
		if err := AppendCollapsingLimit(path, row{TS: ts, Store: ws, Status: status}, sameAbort, MaxBytes); err != nil {
			t.Fatal(err)
		}
	}
	appendOne("t1", "wedged", "aborted-no-fact-files")
	appendOne("t2", "wedged", "aborted-no-fact-files")
	appendOne("t3", "wedged", "aborted-no-fact-files")
	got := lines(t, path)
	if len(got) != 1 {
		t.Fatalf("rows = %d, want 1 collapsed row: %v", len(got), got)
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(got[0]), &m); err != nil {
		t.Fatal(err)
	}
	if m["repeat"] != float64(3) || m["ts"] != "t3" || m["first_ts"] != "t1" {
		t.Errorf("collapsed row = %v, want repeat 3, ts t3, first_ts t1", m)
	}

	// Another store, or a different status, is a different event.
	appendOne("t4", "other", "aborted-no-fact-files")
	appendOne("t5", "wedged", "aborted-no-fact-files")
	appendOne("t6", "wedged", "applied")
	appendOne("t7", "wedged", "applied") // not an aborted-* row: never collapsed
	if got := lines(t, path); len(got) != 5 {
		t.Errorf("rows = %d, want 5 (the run for wedged was interrupted by other, and applied rows are never collapsed): %v", len(got), got)
	}
}

// One row that does not decode (a torn append) never blocks collapsing or appending.
func TestAppendCollapsing_ATornLastRowIsNotCollapsedInto(t *testing.T) {
	path := filepath.Join(t.TempDir(), "r.jsonl")
	if err := os.WriteFile(path, []byte(`{"ts":"t0","workspace":"w","status":"aborted-x"`+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := AppendCollapsingLimit(path, row{TS: "t1", Store: "w", Status: "aborted-x"}, sameAbort, MaxBytes); err != nil {
		t.Fatal(err)
	}
	if got := lines(t, path); len(got) != 2 {
		t.Errorf("rows = %d, want the torn row kept and the new row appended: %v", len(got), got)
	}
}

func TestAppendCollapsing_NilSameAlwaysAppends(t *testing.T) {
	path := filepath.Join(t.TempDir(), "r.jsonl")
	for i := 0; i < 3; i++ {
		if err := AppendCollapsingLimit(path, row{TS: fmt.Sprint(i), Store: "w", Status: "aborted-x"}, nil, MaxBytes); err != nil {
			t.Fatal(err)
		}
	}
	if got := lines(t, path); len(got) != 3 {
		t.Errorf("rows = %d, want 3", len(got))
	}
}
