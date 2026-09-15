package index

// LinkedSlugs is every fact file the index references, from ANY line - parsed entry or
// not (LIB:300-320).
//
// Deliberately independent of entry parsing. A line whose title contains "]" or that is
// indented does not match the entry regex, so treating only entries as "linked" reported
// its file as an ORPHAN; the compactor then appended a SECOND pointer to a file that was
// already indexed, growing a store it exists to shrink (reproduced in review: 24,014 ->
// 24,101 B on a store already at 96% of the sync limit). Reachability must be decided by
// "is there a link to it anywhere", never by "did my regex like the line".
func LinkedSlugs(records []*Record) map[string]bool {
	set := make(map[string]bool)
	for _, r := range records {
		if r.Kind == KindEntry {
			set[r.Slug] = true
			for _, e := range r.ExtraSlugs {
				set[e] = true
			}
			continue
		}
		for _, m := range reLink.FindAllStringSubmatch(r.Raw, -1) {
			set[m[1]] = true
		}
	}
	return set
}
