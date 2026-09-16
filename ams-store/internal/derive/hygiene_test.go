package derive

import (
	"fmt"
	"regexp"
	"strings"
	"testing"
)

// MemoryCompactRobustness.Tests.ps1:9 - never duplicates a pointer whose line the entry
// regex cannot parse, and never grows the index.
//
// Reproduced in review: a bracketed title and an indented pointer both parsed as
// non-entries, their files were called orphans, and a SECOND pointer was appended to each,
// growing a store already at 96% of the sync limit.
func TestHygiene_UnparsablePointerNotDuplicatedNoGrowth(t *testing.T) {
	facts := bigIndexFacts(60)
	facts["bracket.md"] = factWithHook("bracket", "desc for bracket", "a hook")
	facts["nested.md"] = factWithHook("nested", "desc for nested", "a nested pointer")
	lines := bigIndexLines(60)
	lines = append(lines,
		"- [Title [with] brackets](bracket.md) "+emDash+" a hook",
		"  - [Nested](nested.md) "+emDash+" a nested pointer")

	e := newEnv(t, "ws", lines, facts)
	before := len(e.indexBytes())

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}

	text := e.indexText()
	for _, slug := range []string{"(bracket.md)", "(nested.md)"} {
		if n := strings.Count(text, slug); n != 1 {
			t.Errorf("%s appears %d times, want 1 - the file is already indexed, a second pointer is duplication", slug, n)
		}
	}
	if res.Reindexed != 0 {
		t.Errorf("reindexed = %d, want 0", res.Reindexed)
	}
	if after := len(e.indexBytes()); after > before {
		t.Errorf("index grew from %d to %d B - the maintainer must never grow a store", before, after)
	}
}

// MemoryCompactRobustness.Tests.ps1:31 - aborts instead of wiping the index when the store
// enumerates no fact files. If the directory cannot be read, every line looks dangling,
// and every downstream guard agrees because they all compare against the same empty set.
func TestDerive_AbortsWhenNoFactFiles(t *testing.T) {
	e := newEnv(t, "ws", bigIndexLines(60), map[string]string{})
	before := e.indexBytes()

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}

	if res.Status != StatusAbortedNoFactFiles {
		t.Errorf("status = %q, want %q", res.Status, StatusAbortedNoFactFiles)
	}
	if got := e.indexBytes(); string(got) != string(before) {
		t.Errorf("the index was rewritten during an abort (%d -> %d B)", len(before), len(got))
	}
	if r := e.receipts(); len(r) != 1 || r[0]["status"] != StatusAbortedNoFactFiles {
		t.Errorf("receipts = %v, want one aborted-no-fact-files row", r)
	}
}

// MemoryCompactRobustness.Tests.ps1:43 - a mass-dangling index is reported, not gutted.
func TestHygiene_BlastCapAborts(t *testing.T) {
	lines := bigIndexLines(60)
	for i := 1; i <= 30; i++ {
		lines = append(lines, fmt.Sprintf("- [Gone %d](missing%d.md) %s dangling", i, i, emDash))
	}
	e := newEnv(t, "ws", lines, bigIndexFacts(60))
	before := e.indexBytes()

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}

	if res.Status != StatusAbortedBlastCap {
		t.Errorf("status = %q, want %q", res.Status, StatusAbortedBlastCap)
	}
	if got := e.indexBytes(); string(got) != string(before) {
		t.Error("the index was rewritten although the blast cap aborted the run")
	}
}

// MemoryCompactRobustness.Tests.ps1:190 - the boundary: 23 dangling over 90 live aborts,
// 22 applies. cap = floor(0.2 * total entries), abort iff removals > cap. A regression
// that loosened the constant to 0.25 flips the first case.
func TestHygiene_BlastCapBoundary(t *testing.T) {
	for _, tc := range []struct {
		n    int
		want string
	}{{23, StatusAbortedBlastCap}, {22, StatusApplied}} {
		t.Run(fmt.Sprintf("%d_dangling", tc.n), func(t *testing.T) {
			facts := bigIndexFacts(60)
			lines := bigIndexLines(60)
			for i := 1; i <= 30; i++ {
				lines = append(lines, fmt.Sprintf("- [Live %d](live%d.md) %s ok", i, i, emDash))
				facts[fmt.Sprintf("live%d.md", i)] = factWithHook(fmt.Sprintf("live%d", i), "d", "ok")
			}
			for i := 1; i <= tc.n; i++ {
				lines = append(lines, fmt.Sprintf("- [Gone %d](missing%d.md) %s dangling", i, i, emDash))
			}
			e := newEnv(t, "ws", lines, facts)

			res, err := e.run()
			if err != nil {
				t.Fatalf("derive: %v", err)
			}
			if res.Status != tc.want {
				t.Errorf("%d dangling of %d entries: status = %q, want %q", tc.n, 90+tc.n, res.Status, tc.want)
			}
		})
	}
}

// BlastCap is max(1, floor(entries*0.2)): a store with four entries may still lose one, or
// a tiny store could never be repaired at all.
func TestHygiene_BlastCapNeverZero(t *testing.T) {
	for _, tc := range []struct{ entries, want int }{{0, 1}, {1, 1}, {4, 1}, {5, 1}, {10, 2}, {112, 22}, {113, 22}} {
		if got := BlastCap(tc.entries); got != tc.want {
			t.Errorf("BlastCap(%d) = %d, want %d", tc.entries, got, tc.want)
		}
	}
}

// MemoryCompactRobustness.Tests.ps1:123 - an unrepairable entry ghost aborts BEFORE any
// write, twice. A permanent ghost plus a mutating run deleted files every night behind an
// index that was restored every night, with a receipt reporting lost=0.
func TestHygiene_UnrepairableGhostAbortsBeforeWrite(t *testing.T) {
	facts := bigIndexFacts(60)
	facts["ninety.md"] = factWithHook("ninety", "d", "see [odd ] name](ghost-a.md) for detail")
	lines := append(bigIndexLines(60),
		"- [Ninety](ninety.md) "+emDash+" see [odd ] name](ghost-a.md) for detail")

	e := newEnv(t, "ws", lines, facts)
	before := e.indexBytes()

	for i := 1; i <= 2; i++ {
		res, err := e.run()
		if err != nil {
			t.Fatalf("run %d: %v", i, err)
		}
		if res.Status != StatusAbortedGhostLinks {
			t.Errorf("run %d: status = %q, want %q", i, res.Status, StatusAbortedGhostLinks)
		}
		if !strings.Contains(res.Note, "ghost-a.md") {
			t.Errorf("run %d: note %q does not name the ghost", i, res.Note)
		}
	}
	if got := e.indexBytes(); string(got) != string(before) {
		t.Error("the index changed although both runs aborted on a ghost link")
	}
	if !e.exists("fact3.md") || !e.exists("fact4.md") {
		t.Error("a fact file disappeared during a run that wrote nothing")
	}
}

// MemoryCompactRobustness.Tests.ps1:149 - a non-entry line mentioning a missing file is
// NOT a ghost. Hygiene never rewrites a non-entry line, so holding the run to one would
// fail the post-write invariant every night for a condition hygiene cannot touch.
func TestHygiene_NonEntryMentionIsNotAGhost(t *testing.T) {
	lines := bigIndexLines(60)
	// A prose note in the body of the index, not the leading heading: the derived render
	// replaces the heading, so the mention has to sit somewhere it survives for the
	// scenario to test anything.
	lines = append(lines, "", "Older notes were moved to (archive.md).")

	e := newEnv(t, "ws", lines, bigIndexFacts(60))

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Status == StatusAbortedGhostLinks {
		t.Fatalf("a prose mention of a missing file aborted the run: %s", res.Note)
	}
	if !strings.Contains(e.indexText(), "(archive.md)") {
		t.Error("the prose note was dropped; non-entry lines keep their text verbatim")
	}
}

// MemoryCompactRobustness.Tests.ps1:159 - repairs a dead extra link while KEEPING a live
// extra link on the same line. The round-trip check compares extras by SET EQUALITY, not
// subset, so a repair that also dropped the live link is rejected.
func TestHygiene_RepairsDeadExtraLinkKeepsLive(t *testing.T) {
	facts := bigIndexFacts(60)
	facts["merged.md"] = factWithHook("merged", "d", "see also [two](fact5.md) and [gone](ghost.md)")
	lines := append(bigIndexLines(60),
		"- [Merged](merged.md) "+emDash+" see also [two](fact5.md) and [gone](ghost.md)")

	e := newEnv(t, "ws", lines, facts)

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Status != StatusApplied {
		t.Fatalf("status = %q, want applied (note %q)", res.Status, res.Note)
	}
	text := e.indexText()
	if strings.Contains(text, "ghost.md") {
		t.Error("the dead extra link survived the repair")
	}
	if !regexp.MustCompile(`\[two\]\(fact5\.md\)`).MatchString(text) {
		t.Error("the LIVE extra link was dropped by the repair")
	}
	if regexp.MustCompile(`(?m)\[two\]\(fact5\.md\) and\s*$`).MatchString(text) {
		t.Error("the dangling conjunction was not tidied")
	}
	if res.Dedangled != 1 {
		t.Errorf("dedangled = %d, want 1 (the repair counts as one)", res.Dedangled)
	}
}

// MemoryCompactRobustness.Tests.ps1:174 - a checkbox line with a link is never removed as
// dangling, and a fenced list item is not an entry.
func TestHygiene_CheckboxAndFencedItemUntouched(t *testing.T) {
	lines := append(bigIndexLines(60),
		"- [x] task done, see [notes](missing-notes.md)",
		"```",
		"- [Example](example-only.md) - an illustration inside a code fence",
		"```")

	e := newEnv(t, "ws", lines, bigIndexFacts(60))

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	text := e.indexText()
	if !strings.Contains(text, "missing-notes.md") {
		t.Error("the ambiguous checkbox line was removed; it must be left alone, never deleted")
	}
	if !strings.Contains(text, "example-only.md") {
		t.Error("the fenced example was treated as a pointer and dropped")
	}
	if res.Dedangled != 0 {
		t.Errorf("dedangled = %d, want 0", res.Dedangled)
	}
}

// MemoryCompactRobustness.Tests.ps1:365 - re-indexes an orphan in a below-trigger store.
// Hygiene is correctness, and a small store is not exempt from it.
func TestHygiene_ReindexesOrphanBelowTrigger(t *testing.T) {
	e := newEnv(t, "small",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook"},
		map[string]string{
			"a.md":      factWithHook("a", "d", "hook"),
			"orphan.md": fact("orphan", "never indexed"),
		})

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Status != StatusApplied {
		t.Errorf("status = %q, want applied (note %q)", res.Status, res.Note)
	}
	if res.Reindexed != 1 {
		t.Errorf("reindexed = %d, want 1", res.Reindexed)
	}
	if !strings.Contains(e.indexText(), "(orphan.md)") {
		t.Errorf("the orphan is still unreachable from the index:\n%s", e.indexText())
	}
	if r := e.receipts(); len(r) != 1 {
		t.Errorf("receipts = %d, want 1 for the store that changed", len(r))
	}
}

// MemoryCompactRobustness.Tests.ps1:456 - a frontmatter-less orphan is re-indexed with a
// hook made from its first line of prose, with links stripped: a link inside the prose
// would inject a SECOND slug onto the line and become a ghost hygiene cannot repair.
func TestHygiene_SynthesizedHookNoInjectedSlug(t *testing.T) {
	addendum := "**2026-09-03 addendum " + emDash + " orphan engine cores.** vLLM engine cores are " +
		"setproctitle'd; see [old](x.md).\n\nmore text\n"

	e := newEnv(t, "fl",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook"},
		map[string]string{"a.md": factWithHook("a", "d", "hook"), "wsl2-traps.md": addendum})

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Reindexed != 1 {
		t.Fatalf("reindexed = %d, want 1", res.Reindexed)
	}
	text := e.indexText()
	if !regexp.MustCompile(`\(wsl2-traps\.md\).*2026-09-03 addendum`).MatchString(text) {
		t.Errorf("the synthesized hook is not the first line of prose:\n%s", text)
	}
	if strings.Contains(text, "no description") {
		t.Error("the placeholder hook was used although the file has prose")
	}
	if strings.Contains(text, "(x.md)") {
		t.Error("a link inside the prose became a second slug on the line")
	}
}

// COMPACT:517-525: re-indexing is the one hygiene action that ADDS bytes, so it must never
// be the reason a store crosses the hard sync limit.
func TestHygiene_OrphanLeftUnindexedWithoutHeadroom(t *testing.T) {
	facts := bigIndexFacts(62)
	lines := bigIndexLines(62)
	// Pad the store to within a line's width of the sync limit, so the ONLY thing standing
	// between it and an unloadable index is the orphan pointer hygiene wants to add.
	for i := 1; i <= 10; i++ {
		lines = append(lines, fmt.Sprintf("- [P%d](p%d.md) %s pad", i, i, emDash))
		facts[fmt.Sprintf("p%d.md", i)] = factWithHook(fmt.Sprintf("P%d", i), "d", "pad")
	}
	facts["late-orphan.md"] = fact("late orphan", "an orphan with nowhere to go")

	e := newEnv(t, "ws", lines, facts)
	if n := len(e.indexBytes()); n < 24800 || n >= 25000 {
		t.Fatalf("fixture is %d B; it must sit just under the 25,000 B sync limit so the FLOOR is not what makes room", n)
	}

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Reindexed != 0 {
		t.Errorf("reindexed = %d, want 0 - there is no byte headroom", res.Reindexed)
	}
	if !strings.Contains(res.Note, "no byte headroom") {
		t.Errorf("note = %q, want it to say the orphan was left unindexed for want of headroom", res.Note)
	}
	if strings.Contains(e.indexText(), "(late-orphan.md)") {
		t.Error("the orphan was re-indexed although doing so crosses the sync limit")
	}
}

// Hygiene pass A (COMPACT:447-450, :461; blueprint section 3.3). The mutation run found
// this one uncovered: removing the whole duplicate-slug pass turned NOTHING red, because
// every other scenario happened to carry a unique slug per line. A second pointer at one
// slug costs its bytes in every session's context forever and makes the store's own
// reachability accounting disagree with itself, so it is dropped and counted.
func TestHygiene_DuplicateSlugDropped(t *testing.T) {
	lines := append(bigIndexLines(60),
		"- [Fact 5 again](fact5.md) "+emDash+" a second pointer at a slug already indexed",
		"- [Fact 5 third time](fact5.md) "+emDash+" and a third")

	e := newEnv(t, "ws", lines, bigIndexFacts(60))

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.DedupSlug != 2 {
		t.Errorf("dedup_slug = %d, want 2 (the 2nd and 3rd pointer at fact5.md)", res.DedupSlug)
	}
	text := e.indexText()
	if n := strings.Count(text, "(fact5.md)"); n != 1 {
		t.Errorf("fact5.md is linked %d times, want exactly 1", n)
	}
	// The FIRST pointer is the one that survives: dropping the first and keeping a later
	// one would rewrite a line the session wrote for no reason a reader can see.
	if !strings.Contains(text, "- [Fact 5](fact5.md)") {
		t.Error("the surviving pointer is not the first one parsed")
	}
	if strings.Contains(text, "Fact 5 again") || strings.Contains(text, "Fact 5 third time") {
		t.Error("a duplicate pointer survived hygiene")
	}
	// Deduplication must not make the file look orphaned: the kept line still links it,
	// so the post-write invariants hold and no re-index pass adds a fourth pointer.
	if res.Reindexed != 0 {
		t.Errorf("reindexed = %d, want 0 - the deduplicated slug is still linked", res.Reindexed)
	}
}

// A store whose duplicate pointers outnumber the blast cap is REPORTED, never gutted: the
// dedup counter feeds the same cap as the dangling counter (COMPACT:545-556), so a
// pathological index cannot be emptied one fifth at a time.
func TestHygiene_DuplicateSlugCountsAgainstTheBlastCap(t *testing.T) {
	lines := bigIndexLines(10)
	for i := 0; i < 5; i++ {
		lines = append(lines, "- [Dup](fact1.md) "+emDash+" another pointer at fact1")
	}

	e := newEnv(t, "ws", lines, bigIndexFacts(10))

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Status != StatusAbortedBlastCap {
		t.Fatalf("status = %q, want %q (5 removals over a cap of %d)",
			res.Status, StatusAbortedBlastCap, 2)
	}
	if strings.Count(e.indexText(), "(fact1.md)") != 6 {
		t.Error("the index was modified although the run aborted on the blast cap")
	}
}
