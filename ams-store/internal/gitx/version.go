package gitx

import (
	"context"
	"fmt"
	"regexp"
	"strconv"
	"time"
)

// MinMajor and MinMinor are the git floor ams-store requires: `git merge-tree
// --write-tree` arrived in 2.38 and the merge engine has no fallback. The output shape
// is also not stable across 2.38 -> 2.45, so the merge parser reads defensively and the
// floor is checked once at startup rather than discovered mid-merge.
const (
	MinMajor = 2
	MinMinor = 38
)

// reVersion tolerates every shipped spelling: "2.38.0", "2.45.2", "2.9" and the Windows
// build's "2.55.0.windows.2".
var reVersion = regexp.MustCompile(`(\d+)\.(\d+)(?:\.(\d+))?`)

// Version is a parsed `git --version`.
type Version struct {
	Major int
	Minor int
	Patch int
	Raw   string
}

// ParseVersion reads a `git --version` line.
func ParseVersion(s string) (Version, error) {
	m := reVersion.FindStringSubmatch(s)
	if m == nil {
		return Version{}, fmt.Errorf("cannot read a git version out of %q", s)
	}
	major, _ := strconv.Atoi(m[1])
	minor, _ := strconv.Atoi(m[2])
	patch := 0
	if m[3] != "" {
		patch, _ = strconv.Atoi(m[3])
	}
	return Version{Major: major, Minor: minor, Patch: patch, Raw: s}, nil
}

// AtLeast reports whether v is at or above major.minor.
func (v Version) AtLeast(major, minor int) bool {
	if v.Major != major {
		return v.Major > major
	}
	return v.Minor >= minor
}

// String renders the version as major.minor.patch.
func (v Version) String() string { return fmt.Sprintf("%d.%d.%d", v.Major, v.Minor, v.Patch) }

// Detect runs `git --version`.
func Detect(ctx context.Context) (Version, error) {
	res, err := Run(ctx, Options{Timeout: 10 * time.Second}, "--version")
	if err != nil {
		return Version{}, fmt.Errorf("detect git version: %w", err)
	}
	return ParseVersion(res.Stdout)
}

// Require is Detect plus the >= 2.38 floor.
func Require(ctx context.Context) (Version, error) {
	v, err := Detect(ctx)
	if err != nil {
		return v, err
	}
	if !v.AtLeast(MinMajor, MinMinor) {
		return v, fmt.Errorf("git %s is too old: ams-store needs at least %d.%d for `merge-tree --write-tree`", v.String(), MinMajor, MinMinor)
	}
	return v, nil
}
