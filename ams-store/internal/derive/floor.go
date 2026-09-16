package derive

import (
	"sort"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// FloorOptions drives the convergence floor.
type FloorOptions struct {
	// Doctrine reports whether a record is a standing order. Doctrine is NEVER truncated:
	// a doctrine-only overflow is reported, not "fixed".
	Doctrine func(*index.Record) bool
	// Project renders a candidate record set. The floor measures against the DERIVED
	// render, not the verbatim one, so its projection is the file that will be written.
	Project func([]*index.Record) string
	// EngageAtBytes is the size at or above which the floor engages at all. Zero means the
	// Phase 3 default, store.SyncLimitBytes - decision Q2's legacy hysteresis. Phase 4
	// lowers it to the trigger, after the zero-hooks-lost check, as a decided change
	// rather than a silent one.
	EngageAtBytes int
	// StopBelowBytes is the size the floor truncates down to. Zero means store.TriggerBytes,
	// so a floored store leaves the nightly candidate set entirely.
	StopBelowBytes int
}

// FloorResult is the count of lines truncated and the projected size afterwards.
type FloorResult struct {
	Floored int
	Bytes   int
}

// Floor is the deterministic BACKSTOP shared by derive and the write gate (LIB:634-688).
//
// Above the sync limit, convergence beats hook fidelity: every byte past the limit is a
// whole entry the next session never sees, and a judge that shortens ~13 lines a night
// while live sessions add more never catches up. So the longest non-doctrine lines are
// truncated on a word boundary to the line cap until the projected index is under
// StopBelowBytes. It mutates the records in place exactly like a judge SHORTEN and returns
// the count plus the projected size, so the CALLER decides converged vs unconverged.
func Floor(records []*index.Record, opt FloorOptions) FloorResult {
	stop := opt.StopBelowBytes
	if stop <= 0 {
		stop = store.TriggerBytes
	}
	engage := opt.EngageAtBytes
	if engage <= 0 {
		engage = store.SyncLimitBytes
	}
	// A stop threshold above the engage threshold would be unreachable; asking for a
	// smaller index than the floor is allowed to engage at is a request to engage there.
	if stop > engage {
		engage = stop
	}
	project := opt.Project
	if project == nil {
		project = func(r []*index.Record) string { return index.RenderVerbatim(r, "\n") }
	}

	projected := index.ByteCount(project(records))
	if projected < engage {
		return FloorResult{Floored: 0, Bytes: projected}
	}

	// Biggest win first. A line already at or under the cap is never touched.
	long := make([]*index.Record, 0, len(records))
	for _, r := range records {
		if r.Kind == index.KindEntry && r.Bytes > store.LineByteCap {
			long = append(long, r)
		}
	}
	sort.SliceStable(long, func(i, j int) bool { return long[i].Bytes > long[j].Bytes })

	floored := 0
	for _, rec := range long {
		if projected < stop {
			break
		}
		if opt.Doctrine != nil && opt.Doctrine(rec) {
			continue
		}
		// The overhead of "- [Title](slug) - " measured with a one-byte placeholder hook,
		// so a long title eats its own budget instead of the hook's.
		overhead := index.ByteCount(index.EntryLine(rec.Title, rec.Slug, "x", rec.Indent)) - 1
		budget := store.LineByteCap - overhead
		if budget < store.MinHookBudget {
			continue // a title that alone eats the cap cannot be floored
		}
		hook := TruncateToBytes(rec.Summary, budget)
		if hook == "" {
			continue
		}
		candidate := index.EntryLine(rec.Title, rec.Slug, hook, rec.Indent)
		newBytes := index.ByteCount(candidate)
		if newBytes >= rec.Bytes {
			continue // not a shortening
		}
		// No extras: a truncated hook that still carries a markdown link would inject a
		// phantom second slug, and the ghost check fails on it forever.
		if !index.LineRoundTrips(candidate, rec.Slug, nil) {
			continue
		}
		projected -= rec.Bytes - newBytes
		rec.Summary = hook
		rec.Bytes = newBytes
		rec.Dirty = true
		floored++
	}
	return FloorResult{Floored: floored, Bytes: projected}
}
