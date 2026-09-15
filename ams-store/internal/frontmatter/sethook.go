package frontmatter

import (
	"fmt"
	"os"
	"regexp"
	"strings"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
)

// reHookLine matches a top-level hook: line inside a frontmatter block.
var reHookLine = regexp.MustCompile(`^\s*hook\s*:`)

// SetHook returns text with hook: set to the given value, replacing an existing hook:
// line in place and otherwise inserting one, and reports whether anything changed.
//
// This is the judge's SHORTEN write. It differs from InsertHook, which is harvest's and
// is deliberately idempotent: harvest copies an index hook into a file that has none and
// must never overwrite one, while the judge's whole decision IS the new hook text. Two
// functions rather than a flag, so neither call site can pass the wrong one by accident.
//
// The body is byte-preserved and the block's own line endings are kept: only the one
// line changes. A file with no frontmatter block gets none added - that would move it out
// of the no-frontmatter lint population, and its hook stays synthesized at render time.
func SetHook(text, hook string) (string, bool) {
	loc := reBlock.FindStringSubmatchIndex(text)
	if !strings.HasPrefix(text, "---") || loc == nil {
		return text, false
	}
	block := text[loc[2]:loc[3]]
	if cur, ok := Lookup(block, "hook"); ok {
		if cur == hook {
			return text, false
		}
		eol := "\n"
		if strings.Contains(block, "\r\n") {
			eol = "\r\n"
		}
		lines := reEOL.Split(block, -1)
		for i, l := range lines {
			if reHookLine.MatchString(l) {
				indent := l[:len(l)-len(strings.TrimLeft(l, " \t"))]
				lines[i] = indent + "hook: " + QuoteYAML(hook)
				break
			}
		}
		return text[:loc[2]] + strings.Join(lines, eol) + text[loc[3]:], true
	}
	return InsertHook(text, hook)
}

// WriteHook sets a fact file's hook: through the atomic writer and reports whether the
// file was rewritten.
func WriteHook(path, hook string) (bool, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return false, fmt.Errorf("set hook %s: %w", path, err)
	}
	out, changed := SetHook(string(b), hook)
	if !changed {
		return false, nil
	}
	if err := atomic.Write(path, out); err != nil {
		return false, fmt.Errorf("set hook %s: %w", path, err)
	}
	return true, nil
}
