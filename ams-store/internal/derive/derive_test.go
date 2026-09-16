package derive

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// MemoryCompact.Tests.ps1:15 - a store below the trigger is left alone: no floor, no
// truncation, no receipt.
//
// The v2 form has one difference worth stating: derive always renders, so "does nothing"
// is only observable on a store that is ALREADY in derived shape and already harvested.
// The second half of the test covers the other case - a store that is not in derived shape
// gains the fixed heading and nothing else, hook text byte-exact.
func TestDerive_BelowTriggerNoChange(t *testing.T) {
	e := newEnv(t, "small",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook"},
		map[string]string{"a.md": factWithHook("a", "d", "hook")})
	before := e.indexBytes()

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Status != StatusNoOp {
		t.Errorf("status = %q, want no-op (note %q)", res.Status, res.Note)
	}
	if res.Floored != 0 || res.Dedangled != 0 || res.DedupSlug != 0 || res.Reindexed != 0 {
		t.Errorf("a below-trigger clean store was mutated: %+v", res)
	}
	if got := e.indexBytes(); string(got) != string(before) {
		t.Errorf("index changed:\nbefore %q\nafter  %q", before, got)
	}
	if r := e.receipts(); len(r) != 0 {
		t.Errorf("receipts = %d, want 0 for a store nothing happened to", len(r))
	}

	// Not in derived shape: the heading is added, the hook text survives byte-exact.
	e2 := newEnv(t, "small2",
		[]string{"- [A](a.md) " + emDash + " the original hook text"},
		map[string]string{"a.md": factWithHook("a", "some other description", "the original hook text")})
	if _, err := e2.run(); err != nil {
		t.Fatalf("derive: %v", err)
	}
	want := index.DefaultHeading + "\n\n- [A](a.md) " + emDash + " the original hook text\n"
	if got := e2.indexText(); got != want {
		t.Errorf("derived index = %q, want %q", got, want)
	}
}

// MemoryCompact.Tests.ps1:41 - compare-and-swap: the index changing mid-run aborts the
// write, and the concurrent write survives untouched.
//
// The mid-run seam is the injected commit-time lookup, which derive calls while rendering:
// after it takes the pre-run hash, before it writes. That is the same place the Pester
// fixture mutates the index "while Codex thinks" - a real call site, not a test-only hook.
func TestDerive_AbortsOnConcurrentIndexWrite(t *testing.T) {
	e := newEnv(t, "ws", bigIndexLines(60), bigIndexFacts(60))

	appended := "- [Appended by a live session](fact1.md) " + emDash + " written mid-run\n"
	mutate := &fakeCommits{onCall: func() {
		f, err := os.OpenFile(e.st.IndexPath, os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			t.Fatalf("open index: %v", err)
		}
		defer f.Close()
		if _, err := f.WriteString(appended); err != nil {
			t.Fatalf("append: %v", err)
		}
	}}

	res, err := e.run(func(o *Options) { o.Commits = mutate })
	if err != nil {
		t.Fatalf("derive: %v", err)
	}

	if res.Status != StatusAbortedConcurrent {
		t.Errorf("status = %q, want %q", res.Status, StatusAbortedConcurrent)
	}
	if !strings.Contains(e.indexText(), "Appended by a live session") {
		t.Error("the concurrent write was clobbered; an abort must never roll back over a live session")
	}
}

// MemoryCompactRobustness.Tests.ps1:57 - a concurrent write aborts with every fact file
// still on disk: nothing deleted, not just nothing written.
func TestDerive_ConcurrentAbortDeletesNothing(t *testing.T) {
	facts := bigIndexFacts(60)
	facts["orphan.md"] = fact("orphan", "an unindexed fact")
	e := newEnv(t, "ws", bigIndexLines(60), facts)

	mutate := &fakeCommits{onCall: func() {
		f, err := os.OpenFile(e.st.IndexPath, os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			t.Fatalf("open index: %v", err)
		}
		defer f.Close()
		fmt.Fprintf(f, "- [Appended by a live session](fact1.md) %s written mid-run\n", emDash)
	}}

	res, err := e.run(func(o *Options) { o.Commits = mutate })
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Status != StatusAbortedConcurrent {
		t.Fatalf("status = %q, want %q", res.Status, StatusAbortedConcurrent)
	}
	for i := 1; i <= 60; i++ {
		if !e.exists(fmt.Sprintf("fact%d.md", i)) {
			t.Fatalf("fact%d.md was removed by a run that wrote nothing", i)
		}
	}
	if !e.exists("orphan.md") {
		t.Error("orphan.md was removed by a run that wrote nothing")
	}
	if !strings.Contains(e.indexText(), "fact3.md") {
		t.Error("the index lost an entry although the run aborted")
	}
}

// MemoryCompact.Tests.ps1:187 - deterministic hygiene still applies with no judge. derive
// HAS no judge, so this is its whole contract: dangling out, orphan in, nothing shortened.
func TestDerive_HygieneWithoutJudge(t *testing.T) {
	facts := bigIndexFacts(60)
	facts["orphan.md"] = fact("orphan", "an unindexed fact")
	lines := append(bigIndexLines(60), "- [Gone](missing-file.md) "+emDash+" dangling")

	e := newEnv(t, "ws", lines, facts)

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Dedangled != 1 {
		t.Errorf("dedangled = %d, want 1", res.Dedangled)
	}
	if res.Reindexed != 1 {
		t.Errorf("reindexed = %d, want 1", res.Reindexed)
	}
	if res.Shortened != 0 {
		t.Errorf("shortened = %d, want 0 - derive never rewrites a hook for meaning", res.Shortened)
	}
	text := e.indexText()
	if strings.Contains(text, "missing-file.md") {
		t.Error("the dangling line survived")
	}
	if !strings.Contains(text, "orphan.md") {
		t.Error("the orphan was not re-indexed")
	}
}

// MemoryCompactRobustness.Tests.ps1:243 - a second run over a derived store changes
// nothing, byte for byte.
func TestDerive_Idempotent(t *testing.T) {
	facts := bigIndexFacts(60)
	facts["orphan.md"] = fact("orphan", "an unindexed fact")
	lines := append(bigIndexLines(60), "- [Gone](missing-file.md) "+emDash+" dangling")
	e := newEnv(t, "ws", lines, facts)

	if _, err := e.run(); err != nil {
		t.Fatalf("run 1: %v", err)
	}
	mid := e.indexBytes()

	res, err := e.run()
	if err != nil {
		t.Fatalf("run 2: %v", err)
	}
	if got := e.indexBytes(); string(got) != string(mid) {
		t.Errorf("run 2 changed the index:\nrun1 %q\nrun2 %q", mid, got)
	}
	if res.Status != StatusNoOp {
		t.Errorf("run 2 status = %q, want no-op", res.Status)
	}
	if res.Harvested != 0 {
		t.Errorf("run 2 harvested %d file(s); harvest is idempotent", res.Harvested)
	}
}

// MemoryCompactRobustness.Tests.ps1:265 - a dry run reports and writes nothing: not the
// index, and not the frontmatter harvest either.
func TestDerive_DryRunWritesNothing(t *testing.T) {
	facts := bigIndexFacts(60)
	facts["orphan.md"] = fact("orphan", "an unindexed fact")
	// A LONG dangling line, so removing it outweighs the bytes the orphan re-index adds
	// and the projected size really is smaller - the receipt has to be able to say so.
	lines := append(bigIndexLines(60),
		"- [Gone](missing-file.md) "+emDash+" "+strings.Repeat("dangling detail ", 30))
	e := newEnv(t, "ws", lines, facts)
	before := e.indexBytes()
	beforeOrphan := e.fact("orphan.md")

	res, err := e.run(func(o *Options) { o.DryRun = true })
	if err != nil {
		t.Fatalf("derive: %v", err)
	}

	if res.Status != StatusDryRun {
		t.Errorf("status = %q, want dry-run", res.Status)
	}
	if res.AfterBytes == nil || res.BeforeBytes == 0 {
		t.Fatalf("a dry run must still report before/after: %+v", res)
	}
	if *res.AfterBytes >= res.BeforeBytes {
		t.Errorf("after_bytes = %d, want less than before_bytes %d", *res.AfterBytes, res.BeforeBytes)
	}
	if res.Dedangled != 1 || res.Reindexed != 1 {
		t.Errorf("a dry run must PROJECT its hygiene: dedangled=%d reindexed=%d", res.Dedangled, res.Reindexed)
	}
	if got := e.indexBytes(); string(got) != string(before) {
		t.Error("a dry run wrote the index")
	}
	if got := e.fact("orphan.md"); got != beforeOrphan {
		t.Error("a dry run harvested a hook into a fact file")
	}
	if r := e.receipts(); len(r) != 1 || r[0]["dry_run"] != true {
		t.Errorf("receipts = %v, want one dry_run row", r)
	}
}

// MemoryCompactRobustness.Tests.ps1:378 - a clean, already-derived, already-harvested store
// writes no receipt and no log line. A receipt per store per run would bury the ones that
// matter, which is the whole reason the compactor stays quiet on its common case.
func TestDerive_CleanStoreSilent(t *testing.T) {
	e := newEnv(t, "clean",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook"},
		map[string]string{"a.md": factWithHook("a", "d", "hook")})

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Status != StatusNoOp {
		t.Errorf("status = %q, want no-op", res.Status)
	}
	if r := e.receipts(); len(r) != 0 {
		t.Errorf("receipts = %v, want none", r)
	}
	if s := e.log.String(); strings.Contains(s, "clean: no-op") {
		t.Errorf("the run logged a line for a store nothing happened to: %q", s)
	}
}

// ---------------------------------------------------------------- derived render rules

// Mutation gate (blueprint 10.2, "derive doctrine-first"): sorting by slug alone must turn
// this red. Doctrine leads the index because the injection cap truncates from the bottom.
func TestDerive_DoctrineFirst(t *testing.T) {
	e := newEnv(t, "ws",
		[]string{
			"# Memory Index", "",
			"- [Zeta plain](zeta.md) " + emDash + " a plain location fact",
			"- [Alpha rule](alpha.md) " + emDash + " NEVER bind port 80",
		},
		map[string]string{
			"zeta.md":  factWithHook("Zeta plain", "where the PDFs are", "a plain location fact"),
			"alpha.md": factWithHook("Alpha rule", "a standing order", "NEVER bind port 80"),
		})

	if _, err := e.run(func(o *Options) {
		// Make the PLAIN fact the newest, so only the doctrine rule can put alpha first.
		o.Commits = &fakeCommits{times: map[string]int64{"zeta.md": 2000, "alpha.md": 1000}}
	}); err != nil {
		t.Fatalf("derive: %v", err)
	}

	lines := entryLines(e.indexText())
	if len(lines) != 2 {
		t.Fatalf("entry lines = %v", lines)
	}
	if !strings.Contains(lines[0], "(alpha.md)") {
		t.Errorf("first entry is %q, want the doctrine line (alpha.md) - doctrine leads the index", lines[0])
	}
}

// Newest next, slug as the tiebreak: two files committed in the same second order
// lexically, so two PCs deriving the same fact set produce the same bytes.
func TestDerive_NewestNextSlugTiebreak(t *testing.T) {
	e := newEnv(t, "ws",
		[]string{
			"# Memory Index", "",
			"- [One](b-one.md) " + emDash + " one",
			"- [Two](a-two.md) " + emDash + " two",
			"- [Old](c-old.md) " + emDash + " old",
		},
		map[string]string{
			"b-one.md": factWithHook("One", "d", "one"),
			"a-two.md": factWithHook("Two", "d", "two"),
			"c-old.md": factWithHook("Old", "d", "old"),
		})

	if _, err := e.run(func(o *Options) {
		o.Commits = &fakeCommits{times: map[string]int64{"b-one.md": 500, "a-two.md": 500, "c-old.md": 100}}
	}); err != nil {
		t.Fatalf("derive: %v", err)
	}

	want := []string{"(a-two.md)", "(b-one.md)", "(c-old.md)"}
	lines := entryLines(e.indexText())
	for i, w := range want {
		if i >= len(lines) || !strings.Contains(lines[i], w) {
			t.Fatalf("entry %d = %q, want it to carry %s\nfull:\n%s", i, lines, w, e.indexText())
		}
	}
}

// Mutation gate: "always LF -> use the prevailing newline" must turn this red. A derived
// index is LF on every PC or the fact set does not produce byte-identical output.
func TestDerive_AlwaysLF(t *testing.T) {
	e := newEnvNL(t, "ws",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook"},
		map[string]string{"a.md": factWithHook("a", "d", "hook")}, "\r\n")

	if _, err := e.run(); err != nil {
		t.Fatalf("derive: %v", err)
	}

	b := e.indexBytes()
	if strings.Contains(string(b), "\r\n") {
		t.Errorf("the derived index carries CRLF: %q", b)
	}
	if b[0] == 0xEF {
		t.Error("the derived index starts with a BOM")
	}
	if b[0] != '#' {
		t.Errorf("the derived index starts with %q, want the fixed heading", b[0])
	}
}

// Spec fixture (DESIGN:362): a CRLF-only difference derives to an IDENTICAL index. The
// same fact set on a CRLF PC and an LF PC must produce the same bytes, or every first sync
// is a whole-file diff on both sides.
func TestDerive_CRLFOnlyDifference_Identical(t *testing.T) {
	lines := []string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook", "- [B](b.md) " + emDash + " other"}
	facts := map[string]string{
		"a.md": factWithHook("a", "d", "hook"),
		"b.md": factWithHook("b", "d", "other"),
	}
	lf := newEnvNL(t, "ws", lines, facts, "\n")
	crlf := newEnvNL(t, "ws", lines, facts, "\r\n")

	for _, e := range []*env{lf, crlf} {
		if _, err := e.run(); err != nil {
			t.Fatalf("derive: %v", err)
		}
	}

	if a, b := lf.indexBytes(), crlf.indexBytes(); string(a) != string(b) {
		t.Errorf("CRLF and LF stores derived differently:\nLF   %q\nCRLF %q", a, b)
	}
}

// Mutation gate: "200-line stop -> remove the cap" must turn this red. Decision Q3: the
// omitted entries stay ON DISK and are reported as over-inject-limit N.
func TestDerive_TwoHundredLineStop_ReportsOverInjectLimit(t *testing.T) {
	const n = 220
	lines := []string{"# Memory Index", ""}
	facts := map[string]string{}
	for i := 1; i <= n; i++ {
		lines = append(lines, fmt.Sprintf("- [Fact %03d](fact%03d.md) %s hook %d", i, i, emDash, i))
		facts[fmt.Sprintf("fact%03d.md", i)] = factWithHook(fmt.Sprintf("Fact %03d", i), "d", fmt.Sprintf("hook %d", i))
	}
	e := newEnv(t, "ws", lines, facts)

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}

	rendered := index.Parse(e.indexText())
	if got := rendered.LineCount(); got > store.InjectLimitLines {
		t.Errorf("the derived index is %d lines, over the %d line injection cap", got, store.InjectLimitLines)
	}
	if res.OverInjectLimit == 0 {
		t.Fatal("over_inject_limit = 0; the omitted entries were not reported")
	}
	if want := n - (store.InjectLimitLines - 2); res.OverInjectLimit != want {
		t.Errorf("over_inject_limit = %d, want %d (the heading and its blank line count toward the cap)", res.OverInjectLimit, want)
	}
	// The omitted files stay on disk: they are the judge's first candidates, not deletions.
	for i := 1; i <= n; i++ {
		if !e.exists(fmt.Sprintf("fact%03d.md", i)) {
			t.Fatalf("fact%03d.md was deleted by the render stop", i)
		}
	}
	if res.Status != StatusApplied {
		t.Errorf("status = %q, want applied", res.Status)
	}
}

// Decision Q3's guard: doctrine is NEVER dropped. If doctrine alone exceeds the cap the
// render goes past it and reports protected-set-overflow, rather than dropping a standing
// order that then disappears from every session.
func TestDerive_ProtectedSetOverflowRendersPastTheCap(t *testing.T) {
	const n = 210
	lines := []string{"# Memory Index", ""}
	facts := map[string]string{}
	for i := 1; i <= n; i++ {
		lines = append(lines, fmt.Sprintf("- [Rule %03d](rule%03d.md) %s NEVER do thing %d", i, i, emDash, i))
		facts[fmt.Sprintf("rule%03d.md", i)] = withType(
			factWithHook(fmt.Sprintf("Rule %03d", i), "a rule", fmt.Sprintf("NEVER do thing %d", i)), "feedback")
	}
	e := newEnv(t, "ws", lines, facts)

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}

	if !res.ProtectedSetOverflow {
		t.Error("protected_set_overflow = false although doctrine alone exceeds the injection cap")
	}
	if !strings.Contains(res.Note, "protected-set-overflow") {
		t.Errorf("note = %q, want it to name protected-set-overflow", res.Note)
	}
	if res.OverInjectLimit != 0 {
		t.Errorf("over_inject_limit = %d; no doctrine line may be dropped", res.OverInjectLimit)
	}
	for i := 1; i <= n; i++ {
		if !strings.Contains(e.indexText(), fmt.Sprintf("(rule%03d.md)", i)) {
			t.Fatalf("doctrine rule%03d.md was dropped from the index", i)
		}
	}
}

// ---------------------------------------------------------------- harvest

// Spec fixture (DESIGN:360-364): an index line carrying a hook that is not in its fact
// file. Harvest copies it into the file's frontmatter, and the derived line still carries
// it byte for byte - the hook text is the only copy until harvest has run, which is why
// harvest is a precondition of the first push and not an optimisation.
func TestDerive_IndexHookNotInFile_SurvivesHarvest(t *testing.T) {
	const hook = "the index hook, with an " + emDash + " and \"inner quotes\""
	e := newEnv(t, "ws",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " " + hook},
		map[string]string{"a.md": fact("a", "a completely different description")})

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Harvested != 1 {
		t.Errorf("harvested = %d, want 1", res.Harvested)
	}

	fm := frontmatter.ParseText(e.fact("a.md"))
	if fm == nil {
		t.Fatal("a.md lost its frontmatter block")
	}
	if fm.Hook != hook {
		t.Errorf("hook: = %q, want %q", fm.Hook, hook)
	}
	if !strings.Contains(e.indexText(), hook) {
		t.Errorf("the hook did not survive the derive:\n%s", e.indexText())
	}

	// And with --no-harvest the hook still survives: derive must reproduce the current
	// index from the current index, or the zero-hooks-lost check can never pass.
	e2 := newEnv(t, "ws2",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " " + hook},
		map[string]string{"a.md": fact("a", "a completely different description")})
	res2, err := e2.run(func(o *Options) { o.NoHarvest = true })
	if err != nil {
		t.Fatalf("derive --no-harvest: %v", err)
	}
	if res2.Harvested != 0 {
		t.Errorf("--no-harvest harvested %d file(s)", res2.Harvested)
	}
	if !strings.Contains(e2.indexText(), hook) {
		t.Errorf("--no-harvest lost the index hook:\n%s", e2.indexText())
	}
}

// Harvest is idempotent and never adds a frontmatter block to a file that has none: doing
// so would change the no-frontmatter lint population, and such a file's hook stays
// synthesized at render time.
func TestHarvest_IdempotentAndNeverAddsABlock(t *testing.T) {
	e := newEnv(t, "ws",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook one", "- [B](b.md) " + emDash + " hook two"},
		map[string]string{
			"a.md": fact("a", "desc"),
			"b.md": "no frontmatter here, just prose\n",
		})

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Harvested != 1 {
		t.Errorf("harvested = %d, want 1 (only the file with a block)", res.Harvested)
	}
	if got := e.fact("b.md"); got != "no frontmatter here, just prose\n" {
		t.Errorf("b.md was rewritten: %q", got)
	}

	res2, err := e.run()
	if err != nil {
		t.Fatalf("second derive: %v", err)
	}
	if res2.Harvested != 0 {
		t.Errorf("second run harvested %d file(s); harvest is idempotent", res2.Harvested)
	}
}

// Decision Q8: a slug the judge migrated and a session re-created carries `migrated: <id>`
// again, sourced from the `Migrated: <slug> <id>` trailer on the judge's deletion commit,
// so the judge updates the corpus record by id instead of adding a variant every night.
func TestHarvest_MigratedTrailerStampsRecreatedSlug(t *testing.T) {
	e := newEnv(t, "ws",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook", "- [B](b.md) " + emDash + " other"},
		map[string]string{
			"a.md": fact("a", "a fact a session re-created"),
			"b.md": fact("b", "never migrated"),
		})

	res, err := e.run(func(o *Options) {
		o.Migrated = &fakeMigrated{ids: map[string]string{"a.md": "8f3c0011"}}
	})
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.MigratedStamped != 1 {
		t.Errorf("migrated_stamped = %d, want 1", res.MigratedStamped)
	}

	fm := frontmatter.ParseText(e.fact("a.md"))
	if fm == nil || fm.Migrated != "8f3c0011" {
		t.Errorf("a.md migrated: = %q, want 8f3c0011", migratedOf(fm))
	}
	if fm2 := frontmatter.ParseText(e.fact("b.md")); fm2 == nil || fm2.Migrated != "" {
		t.Errorf("b.md gained migrated: = %q; nothing in history says it was migrated", migratedOf(fm2))
	}

	// Idempotent: a file that already carries the id is never rewritten.
	res2, err := e.run(func(o *Options) {
		o.Migrated = &fakeMigrated{ids: map[string]string{"a.md": "8f3c0011"}}
	})
	if err != nil {
		t.Fatalf("second derive: %v", err)
	}
	if res2.MigratedStamped != 0 {
		t.Errorf("second run stamped %d file(s); the step is idempotent", res2.MigratedStamped)
	}
}

// ---------------------------------------------------------------- the lock

// DESIGN:189-190: a contender skips IMMEDIATELY - it never waits - and writes nothing.
func TestDerive_ContenderSkipsWithoutWriting(t *testing.T) {
	facts := bigIndexFacts(60)
	facts["orphan.md"] = fact("orphan", "an unindexed fact")
	e := newEnv(t, "ws", bigIndexLines(60), facts)
	before := e.indexBytes()

	lock := &heldLock{}
	res, err := e.run(func(o *Options) { o.Lock = lock })
	if err == nil || !isLocked(err) {
		t.Fatalf("err = %v, want ErrLocked so a caller can tell skipped from done", err)
	}
	if res == nil || res.Status != StatusSkippedLockHeld {
		t.Errorf("status = %v, want %q", res, StatusSkippedLockHeld)
	}
	if lock.asked != 1 {
		t.Errorf("TryAcquire called %d times, want exactly 1 - a contender never retries", lock.asked)
	}
	if got := e.indexBytes(); string(got) != string(before) {
		t.Error("a contender wrote the index")
	}
	if r := e.receipts(); len(r) != 0 {
		t.Errorf("a contender wrote %d receipt(s); it did nothing to receipt", len(r))
	}
}

func TestDerive_ReleasesTheLockItTook(t *testing.T) {
	e := newEnv(t, "ws",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook"},
		map[string]string{"a.md": factWithHook("a", "d", "hook")})

	lock := &freeLock{}
	if _, err := e.run(func(o *Options) { o.Lock = lock }); err != nil {
		t.Fatalf("derive: %v", err)
	}
	if lock.acquired != 1 || lock.released != 1 {
		t.Errorf("acquired=%d released=%d, want 1 and 1", lock.acquired, lock.released)
	}
}

// ---------------------------------------------------------------- write mechanics

// The dirty marker is what wakes the singleton watcher; derive touches it only when it
// actually wrote, and never on a dry run.
func TestDerive_TouchesTheDirtyMarkerOnlyWhenItWrote(t *testing.T) {
	facts := bigIndexFacts(60)
	facts["orphan.md"] = fact("orphan", "an unindexed fact")
	e := newEnv(t, "ws", bigIndexLines(60), facts)
	marker := filepath.Join(e.sb.StateRoot, "dirty")

	if _, err := e.run(func(o *Options) { o.DryRun = true }); err != nil {
		t.Fatalf("dry run: %v", err)
	}
	if _, err := os.Stat(marker); err == nil {
		t.Error("a dry run touched the dirty marker")
	}

	if _, err := e.run(); err != nil {
		t.Fatalf("derive: %v", err)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Errorf("the dirty marker was not touched after a write: %v", err)
	}
}

// LIB:203-217: a leftover .am-tmp is a full copy of the index inside a directory the
// harness syncs and agents glob. derive sweeps it before it does anything else.
func TestDerive_SweepsLeftoverTempFiles(t *testing.T) {
	e := newEnv(t, "ws",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook"},
		map[string]string{"a.md": factWithHook("a", "d", "hook")})
	leftover := filepath.Join(e.dir, store.IndexName+store.TempSuffix)
	if err := os.WriteFile(leftover, []byte("a full copy of the index"), 0o644); err != nil {
		t.Fatal(err)
	}

	if _, err := e.run(); err != nil {
		t.Fatalf("derive: %v", err)
	}
	if _, err := os.Stat(leftover); err == nil {
		t.Error("the leftover .am-tmp survived the run")
	}
}

// The store enumeration is fail-CLOSED: "could not read" must never be spellable as
// "nothing there", because a caller comparing the index against an empty set concludes
// every line is dangling.
func TestDerive_UnreadableStoreIsAnErrorNotAnEmptyStore(t *testing.T) {
	e := newEnv(t, "ws",
		[]string{"# Memory Index", "", "- [A](a.md) " + emDash + " hook"},
		map[string]string{"a.md": factWithHook("a", "d", "hook")})
	e.st.Dir = filepath.Join(e.dir, "does-not-exist")
	e.st.IndexPath = filepath.Join(e.st.Dir, store.IndexName)

	res, err := e.run()
	if err == nil {
		t.Fatalf("derive returned no error for an unreadable store: %+v", res)
	}
	if res != nil && res.Status == StatusApplied {
		t.Error("an unreadable store reported applied")
	}
}

// ---------------------------------------------------------------- helpers

func entryLines(text string) []string {
	var out []string
	for _, r := range index.Parse(text).Entries() {
		out = append(out, r.Raw)
	}
	return out
}

func migratedOf(fm *frontmatter.Frontmatter) string {
	if fm == nil {
		return "<no frontmatter>"
	}
	return fm.Migrated
}

func isLocked(err error) bool {
	return err == ErrLocked || strings.Contains(err.Error(), ErrLocked.Error())
}

// A store with no index is the fresh-checkout shape: the index is derived and never
// tracked, so a PC (or the hub's own checkout) that has just materialized a store from the
// hub holds fact files and nothing else. derive renders the index from them - harvest finds
// nothing, every file is re-indexed with its frontmatter hook - and the second derive is
// the fixed point. Seen RED on 2026-09-16: the first sync of a fresh checkout materialized
// five stores and then failed on "read index ...: The system cannot find the file
// specified", leaving no index anywhere.
func TestDerive_MissingIndexIsRenderedFromTheFactFiles(t *testing.T) {
	e := newEnv(t, "fresh",
		[]string{"# Memory Index", ""},
		map[string]string{
			"a.md": factWithHook("a", "desc a", "hook a"),
			"b.md": factWithHook("b", "desc b", "hook b"),
		})
	if err := os.Remove(e.st.IndexPath); err != nil {
		t.Fatal(err)
	}
	res, err := e.run()
	if err != nil {
		t.Fatalf("derive on a store with no index: %v", err)
	}
	if res.Status != StatusApplied {
		t.Errorf("status = %q, want applied (note %q)", res.Status, res.Note)
	}
	if res.BeforeBytes != 0 || res.Reindexed != 2 {
		t.Errorf("before_bytes = %d reindexed = %d, want 0 and 2 (note %q)", res.BeforeBytes, res.Reindexed, res.Note)
	}
	got := e.indexText()
	for _, want := range []string{index.DefaultHeading, "(a.md)", "(b.md)", "hook a", "hook b"} {
		if !strings.Contains(got, want) {
			t.Errorf("derived index lacks %q:\n%s", want, got)
		}
	}
	res2, err := e.run()
	if err != nil {
		t.Fatalf("second derive: %v", err)
	}
	if res2.Status != StatusNoOp {
		t.Errorf("second derive status = %q, want no-op: the render must be a fixed point (note %q)", res2.Status, res2.Note)
	}
}
