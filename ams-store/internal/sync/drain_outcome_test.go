package sync

import (
	"context"
	"sort"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// fakeDrainer reports whatever the test scripts, and can rewrite the queue the way a real
// drain does, so the pass's before/after accounting is exercised end to end.
type fakeDrainer struct {
	report func() DrainResult
	after  []merge.DeferredEntry
	roots  func() (state, ws string)
}

func (f *fakeDrainer) ApplyDeferred(_ context.Context, opts DrainOptions) (DrainResult, error) {
	state, ws := f.roots()
	if err := merge.SaveDeferred(state, ws, merge.Deferred{Tree: "t", Entries: f.after}); err != nil {
		return DrainResult{}, err
	}
	return f.report(), nil
}

func outcomes(rc Receipt) map[string][]string {
	m := map[string][]string{}
	add := func(kind string, paths []string) {
		for _, p := range paths {
			m[p] = append(m[p], kind)
		}
	}
	add("applied", rc.DeferredApplied)
	add("still_queued", rc.DeferredStillQueued)
	add("resurrected", rc.Resurrected)
	add("gone", rc.DeferredGone)
	for p := range m {
		sort.Strings(m[p])
	}
	return m
}

// Every queued entry that was pending leaves exactly ONE outcome in the receipt of the
// pass that drained it - including an entry the engine's own report forgot.
func TestSync_DrainNamesOneOutcomePerPendingEntry(t *testing.T) {
	sb, _, opt := pcFixture(t, "ws", map[string]string{"a.md": testutil.FactFile("a", "d", "project", "body")})
	state := sb.StateRoot
	pend := []merge.DeferredEntry{
		{Path: "ws/memory/applied.md", Op: merge.OpDelete, QueuedAt: "2026-09-15T12:00:00Z"},
		{Path: "ws/memory/blocked.md", Op: merge.OpDelete, QueuedAt: "2026-09-15T12:00:00Z"},
		{Path: "ws/memory/edited.md", Op: merge.OpDelete, QueuedAt: "2026-09-15T12:00:00Z"},
		{Path: "ws/memory/absent.md", Op: merge.OpDelete, QueuedAt: "2026-09-15T12:00:00Z"},
		{Path: "ws/memory/forgotten-held.md", Op: merge.OpDelete, QueuedAt: "2026-09-15T12:00:00Z"},
		{Path: "ws/memory/forgotten-cleared.md", Op: merge.OpDelete, QueuedAt: "2026-09-15T12:00:00Z"},
	}
	if err := merge.SaveDeferred(state, "ws", merge.Deferred{Tree: "t", Entries: pend}); err != nil {
		t.Fatal(err)
	}
	opt.Drainer = &fakeDrainer{
		roots: func() (string, string) { return state, "ws" },
		// After the drain the queue still holds the blocked entry and the one the engine
		// forgot to report; everything else left the queue.
		after: []merge.DeferredEntry{pend[1], pend[4]},
		report: func() DrainResult {
			return DrainResult{
				Applied:     []string{"ws/memory/applied.md"},
				StillQueued: []string{"ws/memory/blocked.md"},
				Resurrected: []string{"ws/memory/edited.md"},
				Gone:        []string{"ws/memory/absent.md"},
			}
		},
	}

	res := Once(context.Background(), opt)
	if res.Err != nil {
		t.Fatalf("pass failed: %v", res.Err)
	}
	got := outcomes(res.Receipt)
	want := map[string]string{
		"ws/memory/applied.md":           "applied",
		"ws/memory/blocked.md":           "still_queued",
		"ws/memory/edited.md":            "resurrected",
		"ws/memory/absent.md":            "gone",
		"ws/memory/forgotten-held.md":    "still_queued",
		"ws/memory/forgotten-cleared.md": "gone",
	}
	for p, kind := range want {
		if len(got[p]) != 1 || got[p][0] != kind {
			t.Errorf("%s: outcomes = %v, want exactly [%s]", p, got[p], kind)
		}
	}
	if len(got) != len(want) {
		t.Errorf("receipt names %d paths, want %d: %v", len(got), len(want), got)
	}
}

func TestAccountFor_LeavesReportedPathsAlone(t *testing.T) {
	out := accountFor(
		[]string{"a", "b"}, []string{"b"},
		DrainResult{Applied: []string{"a"}, StillQueued: []string{"b"}},
	)
	if len(out.Applied) != 1 || len(out.StillQueued) != 1 || len(out.Gone) != 0 {
		t.Errorf("accountFor changed a fully reported result: %+v", out)
	}
}
