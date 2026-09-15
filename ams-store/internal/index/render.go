package index

import (
	"sort"
	"strings"
	"time"
)

// RenderVerbatim regenerates the index from records in PARSE order, rebuilding only the
// entries flagged Dirty and emitting everything else from Raw, joined with the given
// newline. This is the legacy regenerator (LIB:283-298): it is what the round-trip
// fixtures assert against and what the floor measures a projected size with.
//
// It is NOT what derive writes. derive uses RenderDerived.
func RenderVerbatim(records []*Record, newline string) string {
	out := make([]string, 0, len(records))
	for _, r := range records {
		if r.Kind == KindEntry && r.Dirty {
			out = append(out, EntryLine(r.Title, r.Slug, r.Summary, r.Indent))
			continue
		}
		out = append(out, r.Raw)
	}
	return strings.Join(out, newline)
}

// DefaultHeading is the fixed heading a derived render always emits.
const DefaultHeading = "# Memory Index"

// RenderOptions drives a derived render. The doctrine test and the commit-time lookup
// are injected so this package stays free of git and of frontmatter - one floor, one
// doctrine rule, shared by every caller rather than reimplemented per verb.
type RenderOptions struct {
	// Heading overrides DefaultHeading when non-empty.
	Heading string
	// Doctrine reports whether an entry is a standing order. Nil means "nothing is".
	Doctrine func(*Record) bool
	// CommitTime is the unix seconds of a slug's last change in the shared history.
	// ok=false means "no commit yet", which sorts as Now (newest). Callers must resolve
	// this from ONE `git log --format=%ct --name-only` pass, never one exec per file.
	CommitTime func(slug string) (unix int64, ok bool)
	// Now is the clock used for uncommitted files. Zero means time.Now().
	Now int64
	// InjectLimitLines stops the render at that many lines. Zero disables the stop.
	InjectLimitLines int
}

// RenderResult is a derived render plus what it had to leave out.
type RenderResult struct {
	Text string
	// Omitted holds the slugs of entries past the line cap, in render order. They stay
	// on disk and are the judge's first candidates.
	Omitted []string
	// ProtectedOverflow is set when doctrine alone pushed the render past the cap. The
	// render then goes past the cap rather than drop a standing order.
	ProtectedOverflow bool
}

// RenderDerived renders records in the derived order of blueprint section 3.4:
//
//  1. a fixed heading plus one blank line
//  2. doctrine entries first
//  3. then by the commit time of each file's last change, descending
//  4. slug as tiebreak, ascending lexical
//  5. always LF, no groups
//
// This is a behaviour CHANGE, not a port: the PowerShell regenerator re-emits records in
// parse order and joins with the prevailing newline. The point of the change is that
// every PC renders byte-identical output for the same fact set, which makes MEMORY.md
// re-derivable rather than mergeable - it is untracked everywhere and never conflicts.
//
// Non-entry lines (a sub-heading, a prose note) keep their relative order and are
// emitted between the fixed heading and the entries. The index's own leading heading and
// blank lines are dropped: the fixed heading replaces them, and a blank line inside a
// derived, group-less render has nothing to separate.
func RenderDerived(records []*Record, opt RenderOptions) RenderResult {
	heading := opt.Heading
	if heading == "" {
		heading = DefaultHeading
	}
	now := opt.Now
	if now == 0 {
		now = time.Now().Unix()
	}
	isDoctrine := func(r *Record) bool {
		return opt.Doctrine != nil && opt.Doctrine(r)
	}
	commitTime := func(r *Record) int64 {
		if opt.CommitTime == nil {
			return now
		}
		if ct, ok := opt.CommitTime(r.Slug); ok {
			return ct
		}
		return now
	}

	var others []string
	entries := make([]*Record, 0, len(records))
	for i, r := range records {
		if r.Kind == KindEntry {
			entries = append(entries, r)
			continue
		}
		if strings.TrimSpace(r.Raw) == "" {
			continue // the fixed heading owns the one blank line
		}
		if i == 0 && strings.HasPrefix(r.Raw, "#") {
			continue // the index's own heading is replaced, not duplicated
		}
		others = append(others, r.Raw)
	}

	sort.SliceStable(entries, func(a, b int) bool {
		x, y := entries[a], entries[b]
		if dx, dy := isDoctrine(x), isDoctrine(y); dx != dy {
			return dx
		}
		if cx, cy := commitTime(x), commitTime(y); cx != cy {
			return cx > cy
		}
		return x.Slug < y.Slug
	})

	lines := make([]string, 0, 2+len(others)+len(entries))
	lines = append(lines, heading, "")
	lines = append(lines, others...)
	used := len(lines)

	res := RenderResult{}
	limit := opt.InjectLimitLines
	for _, r := range entries {
		text := r.Raw
		if r.Dirty {
			text = EntryLine(r.Title, r.Slug, r.Summary, r.Indent)
		}
		if isDoctrine(r) {
			// Doctrine is never dropped. If doctrine alone exceeds the cap, say so and
			// render past it - a standing order that disappears from the index is a
			// standing order nobody obeys.
			lines = append(lines, text)
			used++
			if limit > 0 && used > limit {
				res.ProtectedOverflow = true
			}
			continue
		}
		if limit > 0 && used >= limit {
			res.Omitted = append(res.Omitted, r.Slug)
			continue
		}
		lines = append(lines, text)
		used++
	}

	res.Text = strings.Join(lines, "\n") + "\n"
	return res
}
