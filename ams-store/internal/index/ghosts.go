package index

import (
	"sort"
	"strings"
)

// EntryGhosts lists the links ENTRY lines carry - primary slug or extra links - to files
// that do not exist, sorted (LIB:579-597).
//
// Only entries count. Hygiene never rewrites a non-entry line, so a "(see notes.md)" in
// a heading must not be a ghost it is expected to repair: that would fail the post-write
// invariant every night for a condition hygiene cannot touch. Non-entry links still
// count for REACHABILITY (LinkedSlugs), just never as ghosts.
func EntryGhosts(records []*Record, onDisk map[string]bool) []string {
	ghosts := make(map[string]bool)
	for _, r := range records {
		if r.Kind != KindEntry {
			continue
		}
		// An ambiguous shape - "- [x] task, see [notes](missing.md)", a checkbox rather
		// than a pointer - is never removed by hygiene, so it must not be a ghost
		// either, or a single checkbox line aborts the store's compaction forever.
		if strings.Contains(r.Title, "]") {
			continue
		}
		if !onDisk[r.Slug] {
			ghosts[r.Slug] = true
		}
		for _, e := range r.ExtraSlugs {
			if !onDisk[e] {
				ghosts[e] = true
			}
		}
	}
	out := make([]string, 0, len(ghosts))
	for k := range ghosts {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
