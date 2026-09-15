// Package derive is the single deterministic writer. It harvests index hook text into
// fact-file frontmatter, applies hygiene, renders the index doctrine-first and newest
// next, floors it to the byte budget, stops at the injection cap and writes MEMORY.md
// atomically as LF.
//
// It replaces the hygiene + floor half of the PowerShell compactor (COMPACT:443-529,
// :814-825) and all of the write gate's mutating block (GATE:57-73). Everything the judge
// owns - SHORTEN, MIGRATE, seals, mem0 - is deliberately absent: derive is a pure
// function of the fact set plus the shared history's commit times, which is what makes
// every PC render byte-identical output and MEMORY.md re-derivable rather than mergeable.
//
// ASCII-only source (see the store package doc).
package derive

import (
	"strings"
	"unicode"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// TruncateToBytes truncates text to a BYTE budget, on a word boundary where one sits late
// enough to be worth taking (LIB:618-632).
//
// Two properties are load-bearing:
//
//   - The budget is BYTES, never characters. An em-dash is one character and three bytes,
//     and an index measured in characters overshoots the harness's 25,000 B sync limit
//     silently.
//   - The cut never lands inside a character. The PowerShell original walks UTF-16 code
//     units and has to drop a trailing high surrogate by hand, because a lone high
//     surrogate encodes as EF BF BD and the index gains the replacement character. Go
//     walks runes, so the pair cannot be split - the test that asserts it stays anyway,
//     since it is a property of the output rather than of the loop.
//
// The word boundary is taken only past WordCutFraction of the truncated string: a hook
// that is one long path has no usable space, and cutting at an early one would leave two
// characters where a byte-exact cut leaves the whole prefix.
func TruncateToBytes(text string, maxBytes int) string {
	if len(text) <= maxBytes {
		return text
	}
	if maxBytes <= 0 {
		return ""
	}
	r := []rune(text)
	for len(r) > 0 && len(string(r)) > maxBytes {
		r = r[:len(r)-1]
	}
	s := string(r)
	if cut := strings.LastIndex(s, " "); cut > int(float64(len(s))*store.WordCutFraction) {
		s = s[:cut]
	}
	return strings.TrimRightFunc(s, unicode.IsSpace)
}
