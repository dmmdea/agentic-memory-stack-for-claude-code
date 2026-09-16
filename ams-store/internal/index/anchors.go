package index

import (
	"regexp"
	"strings"
)

// reAnchor is the port of Get-AmAnchorTokens (LIB:391-402): backticked identifiers,
// numbers, Windows paths, /unix/paths of three characters or more, http(s) URLs, and
// ALL-CAPS words of at least two characters.
//
// The alternation order matters: a backticked identifier is matched whole before its
// insides can be picked apart by the number or path branches.
var reAnchor = regexp.MustCompile("`[^`]+`" + `|\b\d[\d.,:/-]*\b|[A-Za-z]:\\[^\s,;)]+|/[A-Za-z0-9_./-]{3,}|https?://\S+|\b[A-Z][A-Z0-9_-]{1,}\b`)

// AnchorTokens is the set of tokens a rewritten hook must keep at least one of.
//
// The tokens are what told the reader WHEN to open the file: a port number, a path, an
// identifier, a version, an ALL-CAPS term. A rewrite that keeps none of them has kept
// the topic and lost the trigger, which is the one thing an index line is for. The
// backticks are stripped from a backticked token so the comparison is against the
// identifier itself.
//
// This lives in index rather than in the judge because it is a property of an index
// line's hook text, and both the judge's apply guard and the lib-level parity fixture
// read it. One implementation, never two: a second copy would drift from this one and
// the guard would accept what the other rejects.
func AnchorTokens(text string) map[string]bool {
	set := make(map[string]bool)
	if text == "" {
		return set
	}
	for _, m := range reAnchor.FindAllString(text, -1) {
		t := trimBackticks(m)
		if t != "" {
			set[t] = true
		}
	}
	return set
}

func trimBackticks(s string) string { return strings.Trim(s, "`") }

// KeepsAnAnchor reports whether hook retains at least one of the anchors of old.
//
// The comparison is plain substring containment, never a wildcard match: an anchor such
// as `cfg[0].name` is a character CLASS to a wildcard matcher, which both rejects hooks
// that do keep the anchor and accepts hooks that dropped it. The false accept is what
// defeats the guard entirely, so it is pinned by its own test.
//
// A hook with no anchors in the old text is unconstrained: there was no trigger detail
// to lose.
func KeepsAnAnchor(oldText, hook string) bool {
	anchors := AnchorTokens(oldText)
	if len(anchors) == 0 {
		return true
	}
	for a := range anchors {
		if strings.Contains(hook, a) {
			return true
		}
	}
	return false
}
