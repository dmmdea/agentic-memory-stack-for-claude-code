package derive

import (
	"regexp"
	"strings"
)

// reAnchor is LIB:391-402. An anchor is a token that tells the reader WHEN to open the
// file: a number, a path/port/URL, a backticked identifier, or an ALL-CAPS word of at
// least two characters. A rewrite that keeps none of them has lost the trigger even when
// the prose still reads well.
var reAnchor = regexp.MustCompile("`[^`]+`" +
	`|\b\d[\d.,:/-]*\b` +
	`|[A-Za-z]:\\[^\s,;)]+` +
	`|/[A-Za-z0-9_./-]{3,}` +
	`|https?://\S+` +
	`|\b[A-Z][A-Z0-9_-]{1,}\b`)

// AnchorTokens extracts the anchor set of a hook. Backticks are stripped from a quoted
// identifier so the token compares against prose that does not re-quote it.
func AnchorTokens(text string) map[string]bool {
	set := make(map[string]bool)
	if text == "" {
		return set
	}
	for _, m := range reAnchor.FindAllString(text, -1) {
		if tok := strings.Trim(m, "`"); tok != "" {
			set[tok] = true
		}
	}
	return set
}

// AnchorsRetained reports whether a rewrite kept at least one of the original's anchors.
// A hook with no anchors has nothing to lose and is always retained - otherwise every
// prose-only hook would become unshortenable.
//
// The comparison is a literal substring test, never a wildcard/glob match. An anchor such
// as `cfg[0].name` is a character class to PowerShell's -like, which both REJECTED hooks
// that did keep the anchor and ACCEPTED hooks that had dropped it entirely; the false
// accept defeats the guard, which is the whole reason it exists.
func AnchorsRetained(original, rewritten string) bool {
	anchors := AnchorTokens(original)
	if len(anchors) == 0 {
		return true
	}
	for a := range anchors {
		if strings.Contains(rewritten, a) {
			return true
		}
	}
	return false
}
