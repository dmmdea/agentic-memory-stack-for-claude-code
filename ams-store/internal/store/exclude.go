package store

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

// Scratch and temp workspaces are never stores.
//
// A throwaway session whose working directory sits under the OS temp dir (or a session
// scratchpad) leaves a projects/<slug>/memory directory behind. Enrolled as a store it is
// synced fleet-wide, judged, and its test facts are migrated into the production corpus;
// once the judge has migrated its only fact the store can never converge. Excluding the
// workspace at enumeration keeps it out of every verb at once (sync, derive, lint, judge).
//
// The exclusion is by SLUG, because a projects-root directory name is the working
// directory path with every non-alphanumeric character replaced by "-": the path itself
// cannot be recovered, but the OS temp dir can be encoded the same way and compared.

// PolicyDir and PolicyFile locate the per-root policy under the projects root.
const (
	PolicyDir  = ".ams"
	PolicyFile = "policy.json"
)

// DefaultExcludeSlugs are the slug fragments excluded when the policy names none. They are
// the shape of a Windows temp path and of a session scratchpad.
var DefaultExcludeSlugs = []string{"-AppData-Local-Temp-", "-scratchpad-"}

// policy is <projects root>/.ams/policy.json. Only store_exclude is read here.
type policy struct {
	// StoreExclude replaces DefaultExcludeSlugs when present: each entry is a
	// case-insensitive fragment of a workspace slug that keeps it from being a store.
	StoreExclude *[]string `json:"store_exclude"`
}

// EncodeWorkspacePath spells a filesystem path the way the harness names a workspace
// directory: every character that is not an ASCII letter or digit becomes "-".
func EncodeWorkspacePath(p string) string {
	var b strings.Builder
	b.Grow(len(p))
	for _, r := range p {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
			b.WriteRune(r)
		default:
			b.WriteByte('-')
		}
	}
	return b.String()
}

// ExcludeRules is what one enumeration excludes.
type ExcludeRules struct {
	// tempPrefix is the encoded OS temp dir, lower-cased; a slug under it is temp.
	tempPrefix string
	// fragments are lower-cased slug fragments.
	fragments []string
}

// LoadExcludeRules reads the exclusion policy for a projects root. It fails OPEN to the
// defaults: a missing or unreadable policy must never make a real store disappear, and the
// defaults exclude nothing a person works in.
func LoadExcludeRules(projectsRoot string) ExcludeRules {
	rules := ExcludeRules{}
	if tmp := os.TempDir(); tmp != "" {
		rules.tempPrefix = strings.ToLower(strings.TrimRight(EncodeWorkspacePath(filepath.Clean(tmp)), "-"))
	}
	frags := DefaultExcludeSlugs
	if data, err := os.ReadFile(filepath.Join(projectsRoot, PolicyDir, PolicyFile)); err == nil {
		var p policy
		if json.Unmarshal(data, &p) == nil && p.StoreExclude != nil {
			frags = *p.StoreExclude
		}
	}
	for _, f := range frags {
		if f = strings.TrimSpace(f); f != "" {
			rules.fragments = append(rules.fragments, strings.ToLower(f))
		}
	}
	return rules
}

// Excludes reports whether a workspace slug is a scratch or temp workspace.
func (r ExcludeRules) Excludes(slug string) bool {
	l := strings.ToLower(slug)
	// A slug UNDER the temp dir: the encoded temp dir followed by a separator. The bare
	// encoded dir alone is a workspace rooted AT the temp dir, which is not a scratch
	// child, but is still a temp workspace, so both are excluded.
	if r.tempPrefix != "" && (l == r.tempPrefix || strings.HasPrefix(l, r.tempPrefix+"-")) {
		return true
	}
	for _, f := range r.fragments {
		if strings.Contains(l, f) {
			return true
		}
	}
	return false
}
