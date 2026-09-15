package derive

import (
	"fmt"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// overLimitStore is New-OverLimitStore: 80 lines x ~370 B is over the 25,000 B sync limit
// with every line over the 130 B cap. HugeTitles makes every title alone eat the cap, so
// nothing is floorable and the run must report unconverged.
func overLimitStore(t *testing.T, ws string, count int, typ string, hugeTitles bool) *env {
	t.Helper()
	lines := []string{"# Memory Index", ""}
	facts := map[string]string{}
	for i := 1; i <= count; i++ {
		title := fmt.Sprintf("Fact %d", i)
		if hugeTitles {
			title = strings.Repeat("T", 140) + fmt.Sprint(i)
		}
		hook := strings.TrimRight(strings.Repeat(fmt.Sprintf("detail number %d ", i), 22), " ")
		lines = append(lines, fmt.Sprintf("- [%s](fact%d.md) %s %s", title, i, emDash, hook))
		facts[fmt.Sprintf("fact%d.md", i)] = withType(factWithHook(title, "d", hook), typ)
	}
	return newEnv(t, ws, lines, facts)
}

func withType(text, typ string) string {
	return strings.Replace(text, "  type: project", "  type: "+typ, 1)
}

// MemoryCompactRobustness.Tests.ps1:301 - floors an over-limit index deterministically and
// reports applied.
func TestFloor_OverLimitConvergesBelowTrigger(t *testing.T) {
	e := overLimitStore(t, "ws", 80, "project", false)
	if n := len(e.indexBytes()); n <= store.SyncLimitBytes {
		t.Fatalf("fixture is %d B; it must start over the %d B sync limit or the floor is never exercised", n, store.SyncLimitBytes)
	}

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}

	if res.Status != StatusApplied {
		t.Errorf("status = %q, want applied (note %q)", res.Status, res.Note)
	}
	if res.Floored <= 0 {
		t.Errorf("floored = %d, want > 0", res.Floored)
	}
	if res.Unconverged {
		t.Error("the run reported unconverged although every line was floorable")
	}
	after := len(e.indexBytes())
	if after >= store.TriggerBytes {
		t.Errorf("after = %d B, want below the TRIGGER %d (hysteresis, not merely below the sync limit)", after, store.TriggerBytes)
	}

	// The floor stops at the trigger and leaves the remainder to the judge: the property
	// is "fewer long lines than before", never "none".
	long := 0
	for _, ln := range strings.Split(e.indexText(), "\n") {
		if strings.HasPrefix(ln, "- [") && len(ln) > store.LineByteCap {
			long++
		}
	}
	if long >= 80 {
		t.Errorf("%d lines are still over the cap; all 80 started over it, so the floor truncated nothing", long)
	}
	if long == 0 {
		t.Error("no line is over the cap: the floor rewrote the whole index instead of stopping at the trigger")
	}
}

// MemoryCompactRobustness.Tests.ps1:321 - the floor runs with no judge at all. In v1 the
// scenario held the codex lock; in v2 derive has no judge to hold a lock for, so the
// property is simply that the deterministic floor is what makes the store loadable again.
func TestFloor_RunsWithoutJudge(t *testing.T) {
	e := overLimitStore(t, "ws", 80, "project", false)

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Status != StatusApplied {
		t.Errorf("status = %q, want applied", res.Status)
	}
	if res.Shortened != 0 || res.Migrated != 0 || res.JudgeCalled {
		t.Errorf("derive reported judge work (shortened=%d migrated=%d judge_called=%v); it has no judge",
			res.Shortened, res.Migrated, res.JudgeCalled)
	}
	if n := len(e.indexBytes()); n >= store.SyncLimitBytes {
		t.Errorf("after = %d B; a store must not stay unloadable because no judge was available", n)
	}
}

// MemoryCompactRobustness.Tests.ps1:332 - reports unconverged when nothing can be floored
// (the titles alone eat the cap). Only a doctrine-or-title overflow can produce it.
func TestFloor_UnconvergedExitsOne(t *testing.T) {
	e := overLimitStore(t, "ws", 80, "project", true)
	before := len(e.indexBytes())

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}

	if !res.Unconverged {
		t.Fatalf("unconverged = false; the store is %d B and nothing is floorable", before)
	}
	if res.Status != StatusUnconverged && res.Status != StatusAppliedUnconverged {
		t.Errorf("status = %q, want unconverged or applied-unconverged", res.Status)
	}
	if !strings.Contains(res.Note, "UNCONVERGED") {
		t.Errorf("note = %q, want it to say UNCONVERGED out loud", res.Note)
	}
	if res.Floored != 0 {
		t.Errorf("floored = %d, want 0: every budget is under the %d B minimum", res.Floored, store.MinHookBudget)
	}
}

// Mutation gate (blueprint 10.2, "doctrine never floored"): dropping the doctrine check
// must turn this red. A doctrine-only overflow is REPORTED, never "fixed".
func TestFloor_NeverTruncatesDoctrine(t *testing.T) {
	e := overLimitStore(t, "ws", 80, "feedback", false)

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}

	if res.Floored != 0 {
		t.Errorf("floored = %d lines, want 0: every entry is feedback-typed, i.e. doctrine", res.Floored)
	}
	if !res.Unconverged {
		t.Error("a doctrine-only overflow must be reported as unconverged, not silently accepted")
	}
	for _, ln := range strings.Split(e.indexText(), "\n") {
		if strings.HasPrefix(ln, "- [") && len(ln) <= store.LineByteCap {
			t.Fatalf("a doctrine line was truncated to the cap: %q", ln)
		}
	}
}

// Mutation gate: "floor descending length -> sort ascending" must turn this red. Biggest
// win first is what makes the floor converge in the fewest rewrites, so the LONGEST line
// is always among the ones truncated.
func TestDerive_FloorDescendingRenderedLength(t *testing.T) {
	lines := []string{"# Memory Index", ""}
	facts := map[string]string{}
	// One line far longer than the rest, plus enough bulk to put the store over the limit.
	longest := strings.TrimRight(strings.Repeat("the single longest hook in this store ", 40), " ")
	lines = append(lines, "- [Longest](longest.md) "+emDash+" "+longest)
	facts["longest.md"] = factWithHook("Longest", "d", longest)
	for i := 1; i <= 70; i++ {
		hook := strings.TrimRight(strings.Repeat(fmt.Sprintf("detail number %d ", i), 22), " ")
		lines = append(lines, fmt.Sprintf("- [Fact %d](fact%d.md) %s %s", i, i, emDash, hook))
		facts[fmt.Sprintf("fact%d.md", i)] = factWithHook(fmt.Sprintf("Fact %d", i), "d", hook)
	}
	e := newEnv(t, "ws", lines, facts)

	if _, err := e.run(); err != nil {
		t.Fatalf("derive: %v", err)
	}

	for _, ln := range strings.Split(e.indexText(), "\n") {
		if strings.Contains(ln, "(longest.md)") {
			if len(ln) > store.LineByteCap {
				t.Errorf("the longest line (%d B) was not truncated; the floor is not taking the biggest win first", len(ln))
			}
			return
		}
	}
	t.Fatal("the longest line vanished from the index")
}

// Mutation gate: "floor stop-below -> stop at SyncLimitBytes" must turn this red. The
// floor's stop is the TRIGGER, so the store leaves the nightly candidate set; stopping at
// the sync limit leaves it one session's growth away from being unloadable again.
func TestDerive_FloorStopsBelowTrigger(t *testing.T) {
	e := overLimitStore(t, "ws", 80, "project", false)

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if got := len(e.indexBytes()); got >= store.TriggerBytes {
		t.Errorf("after = %d B, want < the trigger %d", got, store.TriggerBytes)
	}
	if res.Floored == 0 {
		t.Fatal("nothing was floored")
	}

	// --stop-below overrides it, which is what re-creates the legacy hysteresis for the
	// parity fixtures.
	e2 := overLimitStore(t, "ws2", 80, "project", false)
	if _, err := e2.run(func(o *Options) { o.StopBelowBytes = store.TargetBytes }); err != nil {
		t.Fatalf("derive: %v", err)
	}
	if got := len(e2.indexBytes()); got >= store.TargetBytes {
		t.Errorf("--stop-below %d left the index at %d B", store.TargetBytes, got)
	}
}

// Decision Q2: the Phase 3 default is the LEGACY hysteresis - the floor engages only at or
// above the sync limit, so a store between the trigger and the limit is left exactly as it
// is. Flipping the engage threshold to the trigger is a Phase 4 change, not a silent one.
func TestFloor_LegacyEngageThresholdLeavesBetweenTriggerAndLimitAlone(t *testing.T) {
	e := overLimitStore(t, "ws", 58, "project", false)
	n := len(e.indexBytes())
	if n < store.TriggerBytes || n >= store.SyncLimitBytes {
		t.Fatalf("fixture is %d B; it must sit between the trigger %d and the sync limit %d",
			n, store.TriggerBytes, store.SyncLimitBytes)
	}

	res, err := e.run()
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if res.Floored != 0 {
		t.Errorf("floored = %d; the Phase 3 default floor engages only at or above the sync limit", res.Floored)
	}
	if got := len(e.indexBytes()); got != n {
		t.Errorf("index changed from %d to %d B", n, got)
	}
}

// LIB:675-686: four rejection gates. A truncation that does not shrink the line, or whose
// rebuilt line does not round-trip, is skipped rather than applied.
func TestFloor_RejectsNonShrinkingAndUnparseableCandidates(t *testing.T) {
	recs := index.Parse("- [A](a.md) " + emDash + " " + strings.Repeat("x", 300)).Records
	project := func(r []*index.Record) string { return index.RenderVerbatim(r, "\n") }

	// A budget the floor cannot beat: StopBelow above the projection means it never engages.
	res := Floor(recs, FloorOptions{
		Project:        project,
		EngageAtBytes:  10,
		StopBelowBytes: 5,
	})
	if res.Floored != 1 {
		t.Fatalf("floored = %d, want 1", res.Floored)
	}
	if got := len(index.EntryLine(recs[0].Title, recs[0].Slug, recs[0].Summary, recs[0].Indent)); got > store.LineByteCap {
		t.Errorf("the floored line is %d B, over the %d B cap", got, store.LineByteCap)
	}
	if !recs[0].Dirty {
		t.Error("the floored record was not marked Dirty, so the regenerator will emit its stale Raw")
	}
}

// LIB:674 - a title that alone eats the cap is SKIPPED, not floored to a stub.
//
// The mutation run reached this guard through an equivalent mutant: with a 140-character
// title the budget goes negative, TruncateToBytes returns "" and the empty-hook gate
// catches it anyway. The interesting band is the one in between - a title long enough to
// leave a budget of 1..23 bytes, where truncation would still "succeed" and would replace
// a readable hook with three words of nothing. That band is what MinHookBudget protects
// and what this fixture sits in.
func TestFloor_SkipsALineWhoseTitleEatsTheCap(t *testing.T) {
	title := strings.Repeat("T", 100) // + "a.md" + framing leaves a 15 B hook budget
	summary := strings.Repeat("real hook text ", 20)
	line := index.EntryLine(title, "a.md", summary, "")
	recs := index.Parse(line).Records

	overhead := index.ByteCount(index.EntryLine(title, "a.md", "x", "")) - 1
	if budget := store.LineByteCap - overhead; budget < 1 || budget >= store.MinHookBudget {
		t.Fatalf("fixture budget is %d B; it must sit in 1..%d for this guard to be the one under test",
			budget, store.MinHookBudget-1)
	}

	res := Floor(recs, FloorOptions{
		Project:        func(r []*index.Record) string { return index.RenderVerbatim(r, "\n") },
		EngageAtBytes:  10,
		StopBelowBytes: 5,
	})

	if res.Floored != 0 {
		t.Errorf("floored = %d, want 0 - a %d B hook budget buys nothing a reader can use", res.Floored, store.LineByteCap-overhead)
	}
	if recs[0].Summary != strings.TrimRight(summary, " ") {
		t.Errorf("the hook was truncated to %q; the whole line must be left for the judge or a human", recs[0].Summary)
	}
	if recs[0].Dirty {
		t.Error("the skipped record was marked Dirty, so the regenerator will rewrite a line nothing changed")
	}
}
