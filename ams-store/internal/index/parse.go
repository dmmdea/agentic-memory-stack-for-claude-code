// Package index parses MEMORY.md into records and renders records back into text.
//
// ASCII-only source (see the store package doc): the em-dash separator comes from
// store.EmDash, never from a literal in this file.
package index

import (
	"regexp"
	"strings"
	"unicode"
)

// Kind values a record can carry.
const (
	KindEntry = "entry"
	KindOther = "other"
)

var (
	// reEntry is LIB:223. The title is LAZY so a title containing "]" still parses, and
	// the leading indent group makes an indented pointer an entry rather than opaque
	// text - both shapes previously fell through to Kind='other', which is how an
	// indexed file came to read as an ORPHAN and gain a second pointer.
	reEntry = regexp.MustCompile(`^(?P<indent>\s*)- \[(?P<title>.*?)\]\((?P<slug>[^)\s]+\.md)\)(?P<rest>.*)$`)
	// reLink is LIB:224: any markdown link to a .md file, wherever it sits.
	reLink = regexp.MustCompile(`\(([^)\s]+\.md)\)`)
	// reSep is LIB:246: the separator between a pointer and its hook is an em-dash, one
	// or two hyphens, or a colon.
	reSep = regexp.MustCompile(`^\s*(?:\x{2014}|--?|:)\s*`)
	// reFence is LIB:240. A list item inside a code fence is text, not a pointer.
	reFence = regexp.MustCompile("^\\s*(?:```|~~~)")
	// reSplit keeps the trailing empty element for a newline-terminated file, so the
	// regenerator reproduces the final newline (LIB:231-233).
	reSplit = regexp.MustCompile(`\r?\n`)
)

// Record is one line of the index. Every line - blank lines and headings included - is
// preserved verbatim in Raw so an untouched line reproduces byte-for-byte.
type Record struct {
	Index      int
	Kind       string
	Raw        string
	Indent     string
	Title      string
	Slug       string
	Summary    string
	ExtraSlugs []string
	Bytes      int
	Dirty      bool
}

// Index is a parsed MEMORY.md.
type Index struct {
	Newline string
	Records []*Record
	Lines   int
}

// ByteCount is the UTF-8 byte length of s. Every cap in this tool is a BYTE figure, not
// a character count: an em-dash is one character and three bytes, and an index measured
// in characters silently overshoots the harness's 25,000 B sync limit.
func ByteCount(s string) int { return len(s) }

// Newline returns the store's prevailing line ending. Majority, not "contains": a
// mostly-LF index with one stray CRLF would otherwise be rewritten wholly to CRLF,
// changing every line the job never touched.
//
// derive writes LF unconditionally (blueprint section 3.4). This rule still governs
// every read path and the legacy parity fixtures, so it is ported rather than dropped.
func Newline(text string) string {
	if text == "" {
		return "\n"
	}
	crlf := strings.Count(text, "\r\n")
	if crlf == 0 {
		return "\n"
	}
	lf := strings.Count(text, "\n")
	if crlf*2 >= lf {
		return "\r\n"
	}
	return "\n"
}

// Parse splits text into records, preserving every line verbatim in Raw.
func Parse(text string) *Index {
	lines := reSplit.Split(text, -1)
	records := make([]*Record, 0, len(lines))
	inFence := false
	for i, ln := range lines {
		// The fence state is toggled BEFORE the entry match, exactly as the original
		// does: the opening fence line is already "inside" when it is tested.
		if reFence.MatchString(ln) {
			inFence = !inFence
		}
		var m []string
		if !inFence {
			m = reEntry.FindStringSubmatch(ln)
		}
		if m != nil {
			rest := m[4]
			summary := strings.TrimRightFunc(reSep.ReplaceAllString(rest, ""), unicode.IsSpace)
			extra := []string{}
			for _, lm := range reLink.FindAllStringSubmatch(rest, -1) {
				extra = append(extra, lm[1])
			}
			records = append(records, &Record{
				Index:      i,
				Kind:       KindEntry,
				Raw:        ln,
				Indent:     m[1],
				Title:      m[2],
				Slug:       m[3],
				Summary:    summary,
				ExtraSlugs: extra,
				Bytes:      ByteCount(ln),
			})
			continue
		}
		records = append(records, &Record{
			Index:      i,
			Kind:       KindOther,
			Raw:        ln,
			ExtraSlugs: []string{},
			Bytes:      ByteCount(ln),
		})
	}
	return &Index{Newline: Newline(text), Records: records, Lines: len(lines)}
}

// Entries returns only the entry records, in parse order.
func (ix *Index) Entries() []*Record {
	out := make([]*Record, 0, len(ix.Records))
	for _, r := range ix.Records {
		if r.Kind == KindEntry {
			out = append(out, r)
		}
	}
	return out
}

// LineCount is the index's line count with a single trailing blank discounted, the way
// the lib counts it (LIB:411-412, :448-449). The write gate counts non-blank lines
// instead (GATE:41); the two rules are deliberately NOT unified, because unifying them
// would move one of the two surfaces' thresholds without anyone deciding to.
func (ix *Index) LineCount() int {
	n := ix.Lines
	if n > 0 && ix.Records[n-1].Raw == "" {
		n--
	}
	return n
}
