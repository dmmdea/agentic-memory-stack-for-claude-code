package frontmatter

import (
	"fmt"
	"os"
	"regexp"
	"strings"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
)

var (
	reEOL         = regexp.MustCompile(`\r?\n`)
	reTopDesc     = regexp.MustCompile(`^description\s*:`)
	reTopHook     = regexp.MustCompile(`^hook\s*:`)
	reTopMetadata = regexp.MustCompile(`^metadata\s*:`)
)

// QuoteYAML renders a value as a YAML double-quoted scalar. Backslash first, then the
// quote, or the escape of one would escape the other. A real hook legitimately carries
// em-dashes and inner quotes, so quoting is not optional.
func QuoteYAML(v string) string {
	v = strings.ReplaceAll(v, `\`, `\\`)
	v = strings.ReplaceAll(v, `"`, `\"`)
	return `"` + v + `"`
}

// HasHook reports whether a fact file's text already carries a hook: key.
func HasHook(text string) bool {
	fm := ParseText(text)
	if fm == nil {
		return false
	}
	_, ok := Lookup(fm.Raw, "hook")
	return ok
}

// InsertHook returns text with hook: written into its frontmatter block, and reports
// whether anything changed.
//
// Blueprint section 3.2: the hook goes immediately after description: when there is one,
// otherwise as the last top-level key before metadata:. A file with no frontmatter block
// gets NO block added - adding one would change the no-frontmatter lint population, and
// its hook stays synthesized at render time. A file that already carries hook: is never
// rewritten, which is what makes the step idempotent and what keeps `hook:`-only
// differences from reaching the merge on a second run.
//
// The body is byte-preserved: only the frontmatter block is rebuilt.
func InsertHook(text, hook string) (string, bool) {
	if hook == "" {
		return text, false
	}
	return InsertKey(text, "hook", QuoteYAML(hook))
}

// insertAfter is the placement preference per key: the first pattern that matches a
// top-level line puts the new key on the line AFTER it. Deterministic placement matters
// because two PCs harvesting the same file must produce the same bytes.
var insertAfter = map[string][]*regexp.Regexp{
	"hook":     {reTopDesc},
	"migrated": {reTopHook, reTopDesc},
}

// InsertKey writes `key: value` into a file's existing frontmatter block, verbatim - value
// is already rendered, so a caller decides whether it needs quoting. It reports whether
// anything changed.
//
// A file with no frontmatter block is left ALONE rather than given one: adding a block
// would change the no-frontmatter lint population, and it is not derive's business to
// invent metadata for a file a session wrote as plain prose. A key that is already present
// is never rewritten, which is what makes both harvest steps idempotent.
func InsertKey(text, key, value string) (string, bool) {
	loc := reBlock.FindStringSubmatchIndex(text)
	if !strings.HasPrefix(text, "---") || loc == nil {
		return text, false
	}
	block := text[loc[2]:loc[3]]
	if _, ok := Lookup(block, key); ok {
		return text, false
	}

	eol := "\n"
	if strings.Contains(block, "\r\n") {
		eol = "\r\n"
	}
	lines := reEOL.Split(block, -1)

	at := -1
	for _, pattern := range insertAfter[key] {
		for i, l := range lines {
			if pattern.MatchString(l) {
				at = i + 1
				break
			}
		}
		if at >= 0 {
			break
		}
	}
	if at < 0 {
		// Last top-level key before metadata:, so the nested block stays at the bottom
		// where every real fact file keeps it.
		for i, l := range lines {
			if reTopMetadata.MatchString(l) {
				at = i
				break
			}
		}
	}
	if at < 0 {
		at = len(lines)
	}

	out := make([]string, 0, len(lines)+1)
	out = append(out, lines[:at]...)
	out = append(out, key+": "+value)
	out = append(out, lines[at:]...)

	return text[:loc[2]] + strings.Join(out, eol) + text[loc[3]:], true
}

// Harvest copies an index entry's hook text into the fact file's frontmatter as hook:,
// and reports whether the file was rewritten. It is the ONLY fact-file write derive
// makes, and it must run on every PC before that PC's first push so that `hook:`-only
// differences never become a merge conflict.
func Harvest(path, hook string) (bool, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return false, fmt.Errorf("harvest %s: %w", path, err)
	}
	out, changed := InsertHook(string(b), hook)
	if !changed {
		return false, nil
	}
	if err := atomic.Write(path, out); err != nil {
		return false, fmt.Errorf("harvest %s: %w", path, err)
	}
	return true, nil
}
