package frontmatter

import (
	"regexp"
	"strings"
)

// reImperative is the port of mem0-server/imperative_canary.py::is_imperative_canonical
// (LIB:358), kept in lock-step with it by a fixture that mirrors its documented
// examples. A standing order opens with MUST/NEVER/ALWAYS/SHALL/DO NOT/DON'T/RULE:, or
// says "you must" anywhere.
//
// The PowerShell form is (?ix) - free-spacing plus ignore-case. Go's RE2 has no inline
// (?x), so the whitespace is expanded by hand here; the (?i) is all that is left.
var reImperative = regexp.MustCompile(`(?i)(?:^\s*(?:MUST|NEVER|ALWAYS|SHALL)\b|^\s*(?:DO\s+NOT|DON'T)\b|^\s*RULE\s*:|\byou\s+must\b)`)

// reAttributed (LIB:364): an ATTRIBUTED statement - "Owner: ..." or "Owner (CANONICAL):
// ..." - is a standing order from a person, whatever verb follows. One capitalised word
// (a name), an optional parenthetical, a colon, then text.
//
// "Open for X:" and "RECURRING:" deliberately do NOT match: a space before the colon, or
// an all-caps word with no lowercase tail, is not a name. The rule exists because the
// line floor migrated an attributed standing order that was typed `project` and did not
// open with an imperative.
var reAttributed = regexp.MustCompile(`^\s*[A-Z][a-z]+(?:\s*\([^)]*\))?\s*:\s+\S`)

// reSentence splits on sentence terminators and newlines (LIB:374).
var reSentence = regexp.MustCompile(`[.!?\n]+`)

// IsImperative reports whether ANY sentence in text is a standing order. The test is
// SENTENCE-level, so "Fact one.\nNEVER do X." is doctrine even though the text does not
// open with the imperative.
func IsImperative(text string) bool {
	if strings.TrimSpace(text) == "" {
		return false
	}
	for _, s := range reSentence.Split(text, -1) {
		if strings.TrimSpace(s) == "" {
			continue
		}
		if reImperative.MatchString(s) {
			return true
		}
	}
	return false
}

// IsAttributed reports whether text is an attributed statement. Applied WHOLE-text, not
// per sentence, and guarding empty input first.
func IsAttributed(text string) bool {
	if strings.TrimSpace(text) == "" {
		return false
	}
	return reAttributed.MatchString(text)
}

// IsDoctrine is the five-way hard rule every job honours before any LLM sees a line
// (LIB:380-389). summary is the index line's hook text; fm may be nil, which is what a
// missing file yields - and the summary-based tests still apply in that case.
//
// Doctrine is untouchable on every path: never dropped, never merged, never migrated,
// never truncated by the floor, never offered to the judge.
func IsDoctrine(summary string, fm *Frontmatter) bool {
	if fm != nil && strings.EqualFold(fm.Type, "feedback") {
		return true
	}
	if IsImperative(summary) {
		return true
	}
	if fm != nil && IsImperative(fm.Description) {
		return true
	}
	if IsAttributed(summary) {
		return true
	}
	if fm != nil && IsAttributed(fm.Description) {
		return true
	}
	return false
}
