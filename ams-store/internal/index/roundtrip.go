package index

import "sort"

// LineRoundTrips reports whether a constructed line parses back to exactly the entry it
// intended: one entry record, the expected slug, and exactly the expected extra links
// (LIB:551-577).
//
// Without this check, two constructed lines can poison a store permanently. A hook
// containing "(see other.md)" injects a second link to a file that may not exist - a
// ghost hygiene never removes, so the post-write invariant fails every night, the index
// is restored every night, and the run is discarded forever. A title containing "]"
// makes the regenerated line unparseable, so its file reads back as an orphan on the
// next run: the same endless loop.
//
// The extras comparison is set EQUALITY, not subset: a repair that drops a DEAD extra
// link must keep a LIVE one, or the repair is rejected and the dead link stays forever.
func LineRoundTrips(line, expectedSlug string, expectedExtras []string) bool {
	recs := Parse(line).Entries()
	if len(recs) != 1 {
		return false
	}
	if recs[0].Slug != expectedSlug {
		return false
	}
	got := append([]string(nil), recs[0].ExtraSlugs...)
	want := append([]string(nil), expectedExtras...)
	if len(got) != len(want) {
		return false
	}
	sort.Strings(got)
	sort.Strings(want)
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}
