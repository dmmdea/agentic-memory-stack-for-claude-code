// Counterparts of MemoryCompact.Tests.ps1, one per SAFETY GUARD, ported 1:1 by name.
// The split from robustness_test.go is kept so the CI job names map to the Pester file
// names, even though Go's in-process fixtures no longer need it for time.
package judge

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// MemoryCompact.Tests.ps1:56 - GUARD 3: doctrine is untouchable.
//
// Both halves are asserted. Never OFFERED: doctrine is absent from the candidate set the
// judge's prompt is built from, so a night's attempt is never spent re-deciding a line
// that may not change. Never EDITED: a plan that names it anyway - a hand-written plan, a
// producer bug, a model that ignored its instructions - is refused at apply time.
func TestJudge_DoctrineNeverOfferedNeverEdited(t *testing.T) {
	lines, facts := bigStore(60)
	facts["fact7.md"] = factFile("fact7", "a standing rule", "feedback", "the rule")
	facts["fact9.md"] = factFile("fact9", "another standing rule", "feedback", "the rule")
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)

	before7, before9 := f.line("fact7.md"), f.line("fact9.md")
	// The third decision is the CONTROL: a non-doctrine line in the same plan, which must
	// apply. Without it this scenario passes against an apply that does nothing at all.
	res := f.apply([]Decision{
		shorten("fact7.md", "tiny"),
		migrate("fact9.md"),
		shorten("fact1.md", "detail number 1 kept short"),
	}, nil)

	if res.Status != StatusApplied || res.Shortened != 1 {
		t.Fatalf("the control decision did not apply: status %q shortened %d - this run proves nothing about doctrine", res.Status, res.Shortened)
	}
	if res.Migrated != 0 {
		t.Fatalf("doctrine was migrated: migrated=%d", res.Migrated)
	}
	if got := f.line("fact7.md"); got != before7 {
		t.Errorf("the doctrine line changed:\n before %q\n after  %q", before7, got)
	}
	if got := f.line("fact9.md"); got != before9 {
		t.Errorf("the doctrine line changed:\n before %q\n after  %q", before9, got)
	}
	if !f.exists("fact9.md") {
		t.Error("a doctrine fact was migrated away")
	}
	for _, slug := range []string{"fact7.md", "fact9.md"} {
		if hasCandidate(res.Candidates.Shorten, slug) || hasCandidate(res.Candidates.Migrate, slug) {
			t.Errorf("%s was offered to the judge; doctrine must never even be offered", slug)
		}
	}
	if len(f.mem.Posts()) != 0 {
		t.Errorf("a doctrine fact reached the corpus: %v", f.mem.Posts())
	}
}

// MemoryCompact.Tests.ps1:72 - GUARD 4: a genuine shortening applies and the index shrinks.
func TestJudge_AppliesGenuineShorten(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	before := f.indexBytes()

	res := f.apply([]Decision{
		shorten("fact1.md", "detail number 1 kept short"),
		shorten("fact2.md", "detail number 2 kept short"),
	}, nil)

	if res.Status != StatusApplied {
		t.Fatalf("status = %q, want %q (note: %s)", res.Status, StatusApplied, res.Note)
	}
	if res.Shortened != 2 {
		t.Fatalf("shortened = %d, want 2", res.Shortened)
	}
	if after := f.indexBytes(); after >= before {
		t.Errorf("index did not shrink: %d -> %d B", before, after)
	}
	if !strings.Contains(f.indexText(), "detail number 1 kept short") {
		t.Error("the rewritten hook is not in the index")
	}
	// v2: the hook is authoritative in the FILE and the index is derived from it, so a
	// SHORTEN that did not reach the fact file would be undone by the next derive.
	b, err := readFile(f.dir, "fact1.md")
	if err != nil {
		t.Fatalf("read fact1.md: %v", err)
	}
	if !strings.Contains(b, `hook: "detail number 1 kept short"`) {
		t.Errorf("the fact file did not receive the new hook:\n%s", b)
	}
}

// MemoryCompact.Tests.ps1:86 - GUARD 4: strict decrease and the anchor rule.
func TestJudge_RejectsNoShrinkAndAnchorLoss(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	before := f.indexText()

	// The not-shorter hook deliberately KEEPS its anchor ("detail number 1"), so only the
	// strict-decrease rule can reject it. Without that, the anchor guard would be what
	// catches it and this scenario would pass with strict decrease deleted.
	res := f.apply([]Decision{
		shorten("fact1.md", "detail number 1 "+strings.Repeat("x", 400)), // longer than the line it replaces
		shorten("fact2.md", "generic label with no anchors at all"),      // drops every anchor
	}, nil)

	if res.Shortened != 0 {
		t.Fatalf("shortened = %d, want 0", res.Shortened)
	}
	if res.Status != StatusNoOp {
		t.Errorf("status = %q, want %q", res.Status, StatusNoOp)
	}
	if f.indexText() != before {
		t.Error("the index changed although every rewrite was rejected")
	}
}

// MemoryCompact.Tests.ps1:97 - GUARD 4: the seal. One judge rewrite per line, ever.
//
// The rewritten hook deliberately stays OVER the 130 B cap (asserted below), so it is
// still a byte-cap candidate on run 2 and ONLY the seal can exclude it. Without that
// assertion the scenario passes with the seal deleted - the byte filter alone would keep
// it out of the second offer - which is the seam-test trap this test exists to avoid.
func TestJudge_SealPreventsReoffer(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	stillLong := "detail number 1 " + strings.Repeat("kept but still long ", 9)

	first := f.apply([]Decision{shorten("fact1.md", stillLong)}, nil)
	if first.Shortened != 1 {
		t.Fatalf("shortened = %d, want 1 - the rewrite must actually apply for the seal to mean anything", first.Shortened)
	}
	if got := index.ByteCount(f.line("fact1.md")); got <= store.LineByteCap {
		t.Fatalf("the sealed line is %d B, at or under the %d B cap: the byte filter, not the seal, would be what excludes it on run 2",
			got, store.LineByteCap)
	}

	// One judge attempt per store per 20 h: age run 1's receipt so run 2 is a fresh
	// attempt, or the window - not the seal - is what the second run proves.
	f.ageReceipts(30)

	second := f.apply(nil, nil)
	if hasCandidate(second.Candidates.Shorten, "fact1.md") {
		t.Error("the sealed line was offered again; one judge rewrite per line, ever")
	}
	if !hasCandidate(second.Candidates.Shorten, "fact2.md") {
		t.Error("an unsealed over-cap line must still be offered")
	}
}

// MemoryCompact.Tests.ps1:125 - GUARD 4: a hook carrying a markdown link is refused.
//
// The second link injects a phantom slug: a ghost hygiene cannot remove, so the
// post-write invariant fails, the index is restored, and the run is discarded - every
// night, forever.
func TestJudge_RejectsHookWithMarkdownLink(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)

	// fact2 is the CONTROL: a valid rewrite in the same plan, so a run that applied
	// nothing at all cannot pass this scenario.
	res := f.apply([]Decision{
		shorten("fact1.md", "detail number 1 see [other](ghost.md)"),
		shorten("fact2.md", "detail number 2 kept short"),
	}, nil)

	if res.Shortened != 1 {
		t.Fatalf("shortened = %d, want 1 (the control only)", res.Shortened)
	}
	if strings.Contains(f.indexText(), "ghost.md") {
		t.Error("a phantom slug reached the index")
	}
	if strings.Contains(f.line("fact1.md"), "see [other]") {
		t.Error("the rejected hook was written anyway")
	}
}

// MemoryCompact.Tests.ps1:154 - GUARD 5: write, then verify BY ID, then remove.
func TestMigrate_WriteThenVerifyByID(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)

	res := f.apply([]Decision{migrate("fact3.md")}, nil)

	if res.Migrated != 1 {
		t.Fatalf("migrated = %d, want 1 (status %s, note %s, orphans %v)", res.Migrated, res.Status, res.Note, res.Mem0Orphan)
	}
	if f.exists("fact3.md") {
		t.Error("the fact file survived a verified migration")
	}
	if strings.Contains(f.indexText(), "fact3.md") {
		t.Error("the index still points at the migrated fact")
	}
	row := strings.Join(res.Mem0, " ")
	if !strings.Contains(row, "stub-id-0001") {
		t.Errorf("the receipt must carry the corpus id: %q", row)
	}
	if !strings.Contains(f.mem.SourceTags(), "automemory:ws/fact3.md") {
		t.Errorf("the source tag is the A-to-B bridge and the dedup exemption key: %q", f.mem.SourceTags())
	}
	if key := f.mem.Posts()[0].APIKey; key != "test-key" {
		t.Errorf("X-API-Key = %q, want the configured key", key)
	}
}

// MemoryCompact.Tests.ps1:167 - a write that answers without an id keeps the line.
//
// Nothing is undone either: a record MAY have landed, and the retry is hash-idempotent,
// so deleting would risk removing something this run cannot prove it created.
func TestMigrate_NoIDKeepsLine(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0NoID)

	res := f.apply([]Decision{migrate("fact3.md")}, nil)

	if res.Migrated != 0 {
		t.Fatalf("migrated = %d, want 0", res.Migrated)
	}
	if !f.exists("fact3.md") {
		t.Error("the fact file was deleted against an unverifiable write")
	}
	if len(f.mem.Deleted()) != 0 {
		t.Errorf("nothing may be deleted when no id came back: %v", f.mem.Deleted())
	}
	if len(f.mem.Posts()) != 1 {
		t.Fatalf("the write must have been attempted: posts=%d", len(f.mem.Posts()))
	}
}

// MemoryCompact.Tests.ps1:176 - a read-back that is not byte-equal keeps the line.
func TestMigrate_ReadBackMismatchKeepsLine(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0Mismatch)

	res := f.apply([]Decision{migrate("fact3.md")}, nil)

	if res.Migrated != 0 {
		t.Fatalf("migrated = %d, want 0", res.Migrated)
	}
	if !f.exists("fact3.md") {
		t.Error("the fact file was deleted against a record that says something else")
	}
	if len(f.mem.Posts()) != 1 {
		t.Fatalf("the write must have been attempted and read back: posts=%d", len(f.mem.Posts()))
	}
}

// MemoryCompact.Tests.ps1:206 - feasibility: the protected set alone does not fit.
//
// The hard rule is never loosened autonomously. The run fails LOUD with nothing written
// rather than start shortening standing orders to make room.
func TestFeasibility_ProtectedSetOverflow(t *testing.T) {
	lines := []string{"# Memory Index", ""}
	facts := map[string]string{}
	for i := 1; i <= 150; i++ {
		slug := "rule" + itoa(i) + ".md"
		lines = append(lines, entryLine("Rule "+itoa(i), slug, strings.Repeat("standing rule text "+itoa(i)+" ", 8)))
		facts[slug] = factFile("rule"+itoa(i), "a rule", "feedback", "the rule")
	}
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	before := f.indexText()

	res := f.apply(nil, nil)

	if res.Status != StatusProtectedOverflow {
		t.Fatalf("status = %q, want %q", res.Status, StatusProtectedOverflow)
	}
	if f.indexText() != before {
		t.Error("it must fail loud, never loosen the hard rule")
	}
	if !res.Productive {
		t.Error("a reported overflow is a decision reached, so the run counts as productive")
	}
}

// MemoryCompact.Tests.ps1:248 - one judge attempt per store per 20 h: inside the window
// the plan is not applied.
func TestJudge_OncePerStorePerWindow_Skips(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	f.seedJudgeReceipt(2)
	before := f.indexText()

	res := f.apply([]Decision{shorten("fact1.md", "detail number 1 kept short")}, nil)

	if res.Status != StatusSkippedWindow {
		t.Fatalf("status = %q, want %q", res.Status, StatusSkippedWindow)
	}
	if res.Shortened != 0 || f.indexText() != before {
		t.Error("a plan from inside the window was applied")
	}
	if last := f.lastReceipt(); last.JudgeCalled {
		t.Error("judge_called must be false: the same attempt arriving twice is not a second attempt")
	}
	if res.Productive {
		t.Error("a skip is not a decision; the run must be retried, not counted as done")
	}
}

// MemoryCompact.Tests.ps1:261 - an attempt older than 20 h is applied.
func TestJudge_OncePerStorePerWindow_Calls(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	f.seedJudgeReceipt(30)

	res := f.apply([]Decision{shorten("fact1.md", "detail number 1 kept short")}, nil)

	if res.Status != StatusApplied {
		t.Fatalf("status = %q, want %q (note %s)", res.Status, StatusApplied, res.Note)
	}
	if res.Shortened != 1 {
		t.Fatalf("shortened = %d, want 1", res.Shortened)
	}
	if last := f.lastReceipt(); !last.JudgeCalled {
		t.Error("judge_called must record the attempt")
	}
}

// MemoryCompact.Tests.ps1:273 - the PC catch-up spawn is removed (DESIGN:255-256); the
// hub's once-nightly run replaces it. Two facts make that real: only the hub may apply a
// plan at all, and the hub applies one per store per window.
func TestNightly_HubOnceNightly(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)

	if err := RequireHub(f.roots.StateRoot, false); err == nil {
		t.Fatal("a machine with no role must not be able to apply a plan")
	}
	writeFileT(t, f.roots.StateRoot, RoleFileName, "pc\n")
	if err := RequireHub(f.roots.StateRoot, false); err == nil {
		t.Fatal("a PC must never apply a plan: a second judge on the same store is the concurrency bug in another form")
	}
	writeFileT(t, f.roots.StateRoot, RoleFileName, "  HUB \n")
	if err := RequireHub(f.roots.StateRoot, false); err != nil {
		t.Fatalf("the hub must be allowed to apply: %v", err)
	}

	first := f.apply([]Decision{shorten("fact1.md", "detail number 1 kept short")}, nil)
	if first.Status != StatusApplied {
		t.Fatalf("first run status = %q, want %q", first.Status, StatusApplied)
	}
	second := f.apply([]Decision{shorten("fact2.md", "detail number 2 kept short")}, nil)
	if second.Status != StatusSkippedWindow {
		t.Fatalf("second run status = %q, want %q - the hub judges once a night", second.Status, StatusSkippedWindow)
	}
	if second.Shortened != 0 {
		t.Error("the second run of the night applied edits")
	}
}

// MemoryCompact.Tests.ps1:287 - the operator's explicit bypass. -Force is Daniel saying
// "judge it now"; it lifts the window and nothing else.
func TestJudge_ForceBypassesWindow(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	f.seedJudgeReceipt(2)

	res := f.apply([]Decision{shorten("fact1.md", "detail number 1 kept short")},
		func(o *Options) { o.Force = true })

	if res.Status != StatusApplied || res.Shortened != 1 {
		t.Fatalf("force did not bypass the window: status %q shortened %d", res.Status, res.Shortened)
	}
	if last := f.lastReceipt(); !last.JudgeCalled {
		t.Error("a forced run is still an attempt")
	}
	// Force lifts the WINDOW. It must not lift a guard.
	f.ageReceipts(30)
	rejected := f.apply([]Decision{shorten("fact3.md", "generic label with no anchors at all")},
		func(o *Options) { o.Force = true })
	if rejected.Shortened != 0 {
		t.Error("force disarmed the anchor guard; no flag may ever do that")
	}
}

// --- small helpers -------------------------------------------------------------------

func itoa(n int) string { return strconv.Itoa(n) }

func readFile(dir, name string) (string, error) {
	b, err := os.ReadFile(filepath.Join(dir, name))
	return string(b), err
}

func writeFileT(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
}
