package gate

import (
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// refFloor is a TEST-ONLY implementation of the convergence floor (LIB:634-688).
//
// It exists because the five write-gate scenarios are end-to-end assertions - the index
// shrinks below the trigger, all 80 entries survive, a doctrine-only store is left
// byte-identical - and asserting those against a stub that returns a number would test
// nothing. The shipping floor is internal/derive's, wired through the Floorer interface;
// this one never leaves the test binary. If the two ever disagree, derive's own floor
// tests (TestFloor_*) are the authority, not this file.
type refFloor struct{ calls int }

func (f *refFloor) Floor(records []*index.Record, storeDir, newline string, engageAt, stopBelow int) (FloorResult, error) {
	f.calls++
	projected := len(index.RenderVerbatim(records, newline))

	candidates := make([]*index.Record, 0, len(records))
	for _, r := range records {
		if r.Kind == index.KindEntry && r.Bytes > store.LineByteCap {
			candidates = append(candidates, r)
		}
	}
	// Biggest win first: the longest rendered line buys the most bytes per truncation,
	// so the floor converges in the fewest edits.
	sort.SliceStable(candidates, func(i, j int) bool { return candidates[i].Bytes > candidates[j].Bytes })

	floored := 0
	for _, r := range candidates {
		if projected < stopBelow {
			break
		}
		if isDoctrine(storeDir, r) {
			continue // doctrine is untouchable on every path
		}
		overhead := len(index.EntryLine(r.Title, r.Slug, "x", r.Indent)) - 1
		budget := store.LineByteCap - overhead
		if budget < store.MinHookBudget {
			continue // a title that alone eats the cap cannot be floored
		}
		summary := truncateToBytes(r.Summary, budget)
		if summary == "" {
			continue
		}
		line := index.EntryLine(r.Title, r.Slug, summary, r.Indent)
		if len(line) >= r.Bytes {
			continue // a "truncation" that does not shrink is churn
		}
		if !index.LineRoundTrips(line, r.Slug, extraSlugsIn(summary)) {
			continue
		}
		projected -= r.Bytes - len(line)
		r.Summary = summary
		r.Bytes = len(line)
		r.Dirty = true
		floored++
	}
	return FloorResult{Floored: floored, Bytes: projected}, nil
}

func isDoctrine(storeDir string, r *index.Record) bool {
	var fm *frontmatter.Frontmatter
	if storeDir != "" && r.Slug != "" {
		fm = frontmatter.ParseFile(filepath.Join(storeDir, r.Slug))
	}
	return frontmatter.IsDoctrine(r.Summary, fm)
}

// truncateToBytes is LIB:618-632: drop runes until the UTF-8 byte count fits, then cut on
// a word boundary only if the last space sits past 60% of what is left.
func truncateToBytes(s string, max int) string {
	if len(s) <= max {
		return s
	}
	runes := []rune(s)
	for len(runes) > 0 && utf8.RuneCountInString(string(runes)) >= 0 && len(string(runes)) > max {
		runes = runes[:len(runes)-1]
	}
	out := string(runes)
	if i := strings.LastIndex(out, " "); i > int(float64(len(out))*store.WordCutFraction) {
		out = out[:i]
	}
	return strings.TrimRight(out, " ")
}

func extraSlugsIn(summary string) []string {
	return index.Parse("- [x](x.md) " + store.EmDash + " " + summary).Entries()[0].ExtraSlugs
}
