// Package brand resolves which brand a migrated fact belongs to (shared contract C3).
//
// The map is operator configuration (brands.json), never part of this repository: it names
// businesses. A missing, empty or malformed map routes nothing, and "routes nothing" is the
// behaviour the judge had before the map existed - brand-neutral. It must never crash a
// nightly run, so every failure to read the map degrades to that.
//
// Resolution for one fact:
//
//  1. the first "rules" pattern matching the path (the workspace slug) decides the brand;
//  2. otherwise, when the path matches a "content_rule_workspaces" pattern, every
//     "content_rules" pattern is run over the fact text: exactly one distinct brand
//     matched decides it, zero or several decide nothing;
//  3. otherwise there is no brand.
//
// A brand listed in "shared_brands" is returned as-is; deciding that it is visible to every
// scope is the admission gate's job, not the resolver's. Patterns match case-insensitively.
//
// Path separators are ONE character to the matcher. The path is rewritten so each backslash,
// slash and space becomes "-", and the same literals inside a "rules" or
// "content_rule_workspaces" pattern are rewritten the same way, so "projects/client-a" finds
// a Windows path, a Unix path and Claude Code's hyphenated slug for the same directory alike.
// "content_rules" are the exception: they read the fact's own words and are matched as
// written. This is the rewrite the Python and PowerShell resolvers apply.
//
// The shared corpus tests/fixtures/brand-routing-cases.jsonl is run by every resolver (this
// one, the Python one and the PowerShell one), so the three cannot drift apart.
package brand

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"regexp"
)

type rule struct {
	re    *regexp.Regexp
	brand string
}

// Map is a loaded brand map. The zero value and a nil *Map both route nothing.
type Map struct {
	rules      []rule
	shared     map[string]bool
	contentWS  []*regexp.Regexp
	contentFor []rule
}

type patternBrand struct {
	Pattern string `json:"pattern"`
	Brand   string `json:"brand"`
}

type fileShape struct {
	Rules                 []patternBrand `json:"rules"`
	SharedBrands          []string       `json:"shared_brands"`
	ContentRuleWorkspaces []string       `json:"content_rule_workspaces"`
	ContentRules          []patternBrand `json:"content_rules"`
}

func empty() *Map { return &Map{shared: map[string]bool{}} }

// Parse builds a map from brands.json content. An empty document is an empty map; a
// document that does not parse returns an EMPTY map and the error, so a caller can log
// the problem and carry on brand-neutral. A pattern that does not compile is skipped and
// reported: one bad line must not turn every other rule off.
func Parse(data []byte) (*Map, error) {
	m := empty()
	if len(data) == 0 {
		return m, nil
	}
	var doc fileShape
	if err := json.Unmarshal(data, &doc); err != nil {
		return m, fmt.Errorf("brand map is not valid JSON: %w", err)
	}
	var bad []error
	for _, r := range doc.Rules {
		re, err := compilePath(r.Pattern)
		if err != nil || r.Brand == "" {
			bad = append(bad, fmt.Errorf("rule %q: unusable", r.Pattern))
			continue
		}
		m.rules = append(m.rules, rule{re: re, brand: r.Brand})
	}
	for _, s := range doc.SharedBrands {
		m.shared[s] = true
	}
	for _, p := range doc.ContentRuleWorkspaces {
		re, err := compilePath(p)
		if err != nil {
			bad = append(bad, fmt.Errorf("content_rule_workspaces %q: unusable", p))
			continue
		}
		m.contentWS = append(m.contentWS, re)
	}
	for _, r := range doc.ContentRules {
		re, err := compile(r.Pattern)
		if err != nil || r.Brand == "" {
			bad = append(bad, fmt.Errorf("content rule %q: unusable", r.Pattern))
			continue
		}
		m.contentFor = append(m.contentFor, rule{re: re, brand: r.Brand})
	}
	return m, errors.Join(bad...)
}

// Load reads a brand map from a file. An empty path or a file that does not exist is not an
// error: it is the brand-neutral map, which is what an install without one has always had.
func Load(path string) (*Map, error) {
	if path == "" {
		return empty(), nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return empty(), nil
		}
		return empty(), fmt.Errorf("read brand map %s: %w", path, err)
	}
	return Parse(data)
}

func compile(pattern string) (*regexp.Regexp, error) {
	if pattern == "" {
		return nil, errors.New("empty pattern")
	}
	return regexp.Compile("(?i)" + pattern)
}

// compilePath compiles a pattern that is matched against a PATH: its literal separators are
// rewritten to the same "-" the path's are (see normPattern).
func compilePath(pattern string) (*regexp.Regexp, error) {
	if pattern == "" {
		return nil, errors.New("empty pattern")
	}
	return regexp.Compile("(?i)" + normPattern(pattern))
}

var (
	// pathSep is a separator in a path or slug: backslash, slash or space.
	pathSep = regexp.MustCompile(`[\\/ ]`)
	// patternSep is a literal separator in a pattern: an escaped backslash pair, "\/", "\ ",
	// or a bare "/" or " ".
	patternSep = regexp.MustCompile(`\\\\|\\/|\\ |[/ ]`)
	// charClass and dashRun collapse a separator class such as [\\/ -], which the rewrite turns
	// into [----], back to [-]: a run of dashes is one dash inside a class.
	charClass = regexp.MustCompile(`\[[^\]]*\]`)
	dashRun   = regexp.MustCompile(`-{2,}`)
)

// normPath rewrites each separator in a path or slug to "-", so a Windows path, a Unix path
// and Claude Code's hyphenated slug for one directory are the same string.
func normPath(path string) string { return pathSep.ReplaceAllString(path, "-") }

// normPattern applies the same rewrite to the literal separators inside a path pattern.
func normPattern(pattern string) string {
	out := patternSep.ReplaceAllString(pattern, "-")
	return charClass.ReplaceAllStringFunc(out, func(class string) string {
		return dashRun.ReplaceAllString(class, "-")
	})
}

// Resolve returns the brand for one fact, or "" for none. path is the workspace slug (or
// any path-like string), text is the fact body. An empty path routes nothing.
func (m *Map) Resolve(path, text string) string {
	if m == nil {
		return ""
	}
	hay := normPath(path)
	if hay == "" {
		return ""
	}
	for _, r := range m.rules {
		if r.re.MatchString(hay) {
			return r.brand
		}
	}
	inContentWS := false
	for _, re := range m.contentWS {
		if re.MatchString(hay) {
			inContentWS = true
			break
		}
	}
	if !inContentWS {
		return ""
	}
	found := ""
	for _, r := range m.contentFor {
		if !r.re.MatchString(text) {
			continue
		}
		if found != "" && found != r.brand {
			return "" // two distinct brands named: the text does not decide
		}
		found = r.brand
	}
	return found
}

// IsShared reports whether a brand label is one every scope may see.
func (m *Map) IsShared(brand string) bool {
	return m != nil && m.shared[brand]
}
