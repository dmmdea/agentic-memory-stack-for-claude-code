// Package frontmatter reads the leading --- block of a fact file and classifies the
// fact. It is deliberately lenient in exactly the ways the PowerShell original is
// (memory-store-lib.ps1:324-353): real files carry trailing spaces after keys, em-dashes
// and inner quotes in descriptions, and `type` NESTED under `metadata:`.
//
// ASCII-only source (see the store package doc).
package frontmatter

import (
	"os"
	"regexp"
	"strings"
)

// reBlock is LIB:331. Anchored at the start of the text, dot matches newline, lazy body,
// and the closing --- may end the file. No match, or no leading ---, means "no
// frontmatter" - and that answer is what makes lint emit `no-frontmatter`.
var reBlock = regexp.MustCompile(`(?s)^---\r?\n(.*?)\r?\n---(?:\r?\n|$)`)

// Keys is every frontmatter key this tool reads. `type` and `modified` live nested
// under metadata: in every real fact file; `hook` and `migrated` are net-new in v2.
var Keys = []string{"name", "description", "hook", "migrated", "type", "modified"}

// keyRe holds the per-key lookup regexes, built once at init so no lookup mutates shared
// state at run time (the watcher is concurrent and -race would find a lazily-filled
// cache). The lookup is LIB:335: first match at ANY indentation. That leniency is
// load-bearing, not sloppiness - `type` is nested under `metadata:` in every real fact
// file and a top-level ^type: matches zero of them.
var keyRe = func() map[string]*regexp.Regexp {
	m := make(map[string]*regexp.Regexp, len(Keys))
	for _, k := range Keys {
		m[k] = compileKey(k)
	}
	return m
}()

func compileKey(key string) *regexp.Regexp {
	return regexp.MustCompile(`(?m)^\s*` + regexp.QuoteMeta(key) + `:\s*(.*)$`)
}

// Frontmatter is a parsed leading --- block plus the body that follows it.
type Frontmatter struct {
	Name        string
	Description string
	// Hook is the index hook, authoritative over Description. Net-new in v2.
	Hook string
	// Migrated is the mem0 id a migrated fact was filed under. Net-new in v2.
	Migrated string
	// Type and Modified come from metadata: via the lenient any-indentation lookup.
	// Modified is read, never written and never acted on: the body-conflict winner is
	// decided by COMMIT time, never by a model-written stamp.
	Type     string
	Modified string
	Body     string

	BodyBytes int
	FileBytes int
	// Raw is the frontmatter block verbatim, for the field-aware merge.
	Raw string
	// Present is false only for a value built from a file with no --- block.
	Present bool
}

// Lookup performs the lenient first-match-at-any-indentation key read on a raw block,
// stripping exactly one outer quote pair from the value.
func Lookup(block, key string) (string, bool) {
	re, ok := keyRe[key]
	if !ok {
		re = compileKey(key)
	}
	m := re.FindStringSubmatch(block)
	if m == nil {
		return "", false
	}
	v := strings.TrimSpace(m[1])
	// Exactly one outer pair, and only when both ends agree.
	if len(v) >= 2 {
		f, l := v[0], v[len(v)-1]
		switch {
		case f == '"' && l == '"':
			// A double-quoted scalar is unescaped, so a value this package WROTE reads
			// back byte-identical. Only \" and \\ are recognised, which is exactly what
			// QuoteYAML produces: a real hand-written description carrying bare inner
			// quotes (the shape every shipped fact file uses) contains no backslash
			// escapes at all, so unescaping is a no-op on it.
			v = unescapeDoubleQuoted(v[1 : len(v)-1])
		case f == '\'' && l == '\'':
			v = v[1 : len(v)-1]
		}
	}
	return v, true
}

func unescapeDoubleQuoted(v string) string {
	if !strings.Contains(v, `\`) {
		return v
	}
	var b strings.Builder
	b.Grow(len(v))
	for i := 0; i < len(v); i++ {
		if v[i] == '\\' && i+1 < len(v) && (v[i+1] == '"' || v[i+1] == '\\') {
			b.WriteByte(v[i+1])
			i++
			continue
		}
		b.WriteByte(v[i])
	}
	return b.String()
}

// ParseText parses frontmatter out of a fact file's text. It returns nil when the text
// has no leading --- block at all.
func ParseText(text string) *Frontmatter {
	if !strings.HasPrefix(text, "---") {
		return nil
	}
	loc := reBlock.FindStringSubmatchIndex(text)
	if loc == nil {
		return nil
	}
	block := text[loc[2]:loc[3]]
	body := text[loc[1]:]
	get := func(key string) string { v, _ := Lookup(block, key); return v }
	return &Frontmatter{
		Name:        get("name"),
		Description: get("description"),
		Hook:        get("hook"),
		Migrated:    get("migrated"),
		Type:        get("type"),
		Modified:    get("modified"),
		Body:        body,
		BodyBytes:   len(body),
		FileBytes:   len(text),
		Raw:         block,
		Present:     true,
	}
}

// ParseFile is ParseText over a file. It returns nil when the file cannot be read, the
// same answer the PowerShell original gives (LIB:328) - "unreadable" and "no block"
// deliberately collapse here, because both mean "no frontmatter to act on" and the one
// consumer that must not collapse them, fact-file ENUMERATION, has its own fail-closed
// rule in the store package.
func ParseFile(path string) *Frontmatter {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	return ParseText(string(b))
}
