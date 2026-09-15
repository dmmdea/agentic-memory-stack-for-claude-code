package lint_test

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lint"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	amsync "github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/sync"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// fixedNow is the clock every scenario here runs against, so a summary's generated_at
// and every "hours ago" figure is a value the test states rather than one it reads back
// from the code under test.
var fixedNow = time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)

// hubHost is a PLACEHOLDER. The real MagicDNS name of the hub is configuration, never a
// literal in this repository: a hostname committed here is a hostname published with it.
const hubHost = "hub-host"

func roots(sb *testutil.Sandbox) store.Roots {
	return store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot}
}

// kinds maps finding kind -> the files it named, which is how every Pester assertion in
// these scenarios is phrased ("the orphan finding's file should contain orphan.md").
func kinds(fs []lint.Finding) map[string][]string {
	out := map[string][]string{}
	for _, f := range fs {
		out[f.Kind] = append(out[f.Kind], f.File)
	}
	return out
}

func has(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

// seedReceipts writes one maintenance receipt per status, oldest first, spaced an hour
// apart and ending hoursAgo before fixedNow. It is the port of the fixture's
// Seed-Receipts helper.
func seedReceipts(t *testing.T, path, workspace string, statuses []string, hoursAgo float64) {
	t.Helper()
	var b strings.Builder
	end := fixedNow.Add(-time.Duration(hoursAgo * float64(time.Hour)))
	for i, st := range statuses {
		ts := end.Add(-time.Duration(len(statuses)-1-i) * time.Hour)
		row := map[string]any{
			"ts":        ts.Format(time.RFC3339Nano),
			"workspace": workspace,
			"status":    st,
			"dry_run":   false,
		}
		enc, err := json.Marshal(row)
		if err != nil {
			t.Fatalf("marshal receipt: %v", err)
		}
		b.Write(enc)
		b.WriteString("\n")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, []byte(b.String()), 0o644); err != nil {
		t.Fatalf("write receipts: %v", err)
	}
}

func receiptPath(sb *testutil.Sandbox) string {
	return filepath.Join(sb.StateRoot, lint.ReceiptFile)
}

// firstStore returns the single canonical store of a sandbox.
func firstStore(t *testing.T, sb *testutil.Sandbox) store.Store {
	t.Helper()
	stores, _, err := store.Enumerate(sb.ProjectsRoot)
	if err != nil {
		t.Fatalf("enumerate: %v", err)
	}
	for _, s := range stores {
		if !s.IsAlias {
			return s
		}
	}
	t.Fatalf("no canonical store under %s", sb.ProjectsRoot)
	return store.Store{}
}

// --------------------------------------------------------------------------------
// MemoryStoreLib.Tests.ps1:261 - the stateless per-entry and per-file findings.
// --------------------------------------------------------------------------------

func TestLint_AllPerEntryAndPerFileFindings(t *testing.T) {
	sb := testutil.NewSandbox(t)
	long := "- [L](l.md) " + testutil.EmDash + " " + strings.Repeat("x", 140)
	sb.AddStore("ws", []string{
		"- [A](a.md)",
		"- [A again](a.md)",
		"- [Gone](gone.md)",
		long,
	}, map[string]string{
		"a.md":      testutil.FactFile("a", "d", "project", "body"),
		"l.md":      testutil.FactFile("l", "d", "project", "body"),
		"orphan.md": testutil.FactFile("o", "d", "project", "body"),
		"big.md":    testutil.FactFile("big", "d", "project", strings.Repeat("z", 12000)),
		"raw.md":    "no frontmatter here\n",
	})

	found, err := lint.StoreFindings(firstStore(t, sb))
	if err != nil {
		t.Fatalf("StoreFindings: %v", err)
	}
	k := kinds(found)

	// orphan.md and big.md are both on disk and linked by nothing.
	for _, want := range []string{"orphan.md", "big.md", "raw.md"} {
		if !has(k[lint.KindOrphan], want) {
			t.Errorf("orphan findings = %v, want it to contain %q", k[lint.KindOrphan], want)
		}
	}
	if !has(k[lint.KindDangling], "gone.md") {
		t.Errorf("dangling = %v, want gone.md - an index line pointing at a file that is not there", k[lint.KindDangling])
	}
	if !has(k[lint.KindDupSlug], "a.md") {
		t.Errorf("dup-slug = %v, want a.md linked twice", k[lint.KindDupSlug])
	}
	if !has(k[lint.KindLongLine], "l.md") {
		t.Errorf("long-line = %v, want l.md over the %d B cap", k[lint.KindLongLine], store.LineByteCap)
	}
	if !has(k[lint.KindOversizedFile], "big.md") {
		t.Errorf("oversized-file = %v, want big.md", k[lint.KindOversizedFile])
	}
	if !has(k[lint.KindNoFrontmatter], "raw.md") {
		t.Errorf("no-frontmatter = %v, want raw.md", k[lint.KindNoFrontmatter])
	}

	// no-frontmatter is REPORTED but not actionable: a fact file a human wrote by hand
	// is not broken, and putting it on the banner teaches the operator to ignore it.
	if lint.Actionable(lint.KindNoFrontmatter) || lint.Actionable(lint.KindLongLine) ||
		lint.Actionable(lint.KindOversizedFile) || lint.Actionable(lint.KindNearBudget) {
		t.Error("no-frontmatter, long-line, oversized-file and near-budget must stay reported-but-quiet")
	}
	for _, k := range []string{lint.KindOrphan, lint.KindDangling, lint.KindDupSlug, lint.KindScanError} {
		if !lint.Actionable(k) {
			t.Errorf("%s must be actionable - a finding off the list reaches no surface at all", k)
		}
	}
}

// --------------------------------------------------------------------------------
// MemoryStoreLib.Tests.ps1:279 - a clean store yields zero findings and correct stats.
// --------------------------------------------------------------------------------

func TestLint_CleanStoreZeroFindings(t *testing.T) {
	sb := testutil.NewSandbox(t)
	sb.AddStore("ws", []string{
		"# Index",
		"",
		"- [A](a.md) " + testutil.EmDash + " hook",
	}, map[string]string{"a.md": testutil.FactFile("a", "d", "project", "body")})

	s := firstStore(t, sb)
	found, err := lint.StoreFindings(s)
	if err != nil {
		t.Fatalf("StoreFindings: %v", err)
	}
	if len(found) != 0 {
		t.Fatalf("findings = %v, want none on a clean store", found)
	}

	st, err := lint.MeasureStore(s)
	if err != nil {
		t.Fatalf("MeasureStore: %v", err)
	}
	// 3 lines: the heading, the blank, the entry. LineCount discounts the ONE trailing
	// blank the file's final newline produces - and that discount is why the gate keeps
	// its own non-blank rule instead of reusing this number.
	if st.Lines != 3 {
		t.Errorf("Lines = %d, want 3", st.Lines)
	}
	if st.Entries != 1 {
		t.Errorf("Entries = %d, want 1", st.Entries)
	}
	if st.Files != 1 {
		t.Errorf("Files = %d, want 1", st.Files)
	}
	if st.OverTrigger {
		t.Error("OverTrigger = true on a three-line store")
	}
}

// --------------------------------------------------------------------------------
// MemoryStoreLib.Tests.ps1:345 / :359 - LastJudgeUtc.
// --------------------------------------------------------------------------------

func TestReceipts_LastJudgeUtcNewestCall(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "receipts.jsonl")
	body := `{"ts":"2026-09-09T03:00:00.0000000Z","workspace":"ws","status":"applied","judge_called":true}
{"ts":"2026-09-10T03:00:00.0000000Z","workspace":"ws","status":"rejected-no-shrink","judge_called":true}
{"ts":"2026-09-10T08:00:00.0000000Z","workspace":"ws","status":"skipped-live-session","judge_called":false}
`
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}

	h := lint.ReadRunHistory(p, "ws")
	if h.LastJudgeUTC == nil {
		t.Fatal("LastJudgeUTC = nil, want the newest judge_called receipt")
	}
	// A judge CALL, not a judge success: the newest row with judge_called is the
	// rejection, and a rejected result is a receipt, not a retry.
	wantJudge := time.Date(2026, 9, 10, 3, 0, 0, 0, time.UTC)
	if !h.LastJudgeUTC.Equal(wantJudge) {
		t.Errorf("LastJudgeUTC = %s, want %s", h.LastJudgeUTC.Format(time.RFC3339), wantJudge.Format(time.RFC3339))
	}
	if h.LastJudgeUTC.Location() != time.UTC {
		t.Errorf("LastJudgeUTC location = %v, want UTC", h.LastJudgeUTC.Location())
	}

	// The 5 h skew found 2026-09-10: pwsh 7's ConvertFrom-Json handed back a local-kind
	// DateTime that was then re-labelled UTC, shifting every receipt age by the offset.
	// Go's RFC3339 parse cannot do that - this pins it so nobody reintroduces it with a
	// hand-rolled parser.
	wantProductive := time.Date(2026, 9, 9, 3, 0, 0, 0, time.UTC)
	if h.LastProductiveUTC == nil || !h.LastProductiveUTC.Equal(wantProductive) {
		t.Errorf("LastProductiveUTC = %v, want %s", h.LastProductiveUTC, wantProductive.Format(time.RFC3339))
	}
	if h.LastStatus != lint.SkipLiveSession {
		t.Errorf("LastStatus = %q, want %q", h.LastStatus, lint.SkipLiveSession)
	}
	if h.SkipStreak != 1 {
		t.Errorf("SkipStreak = %d, want 1", h.SkipStreak)
	}
}

func TestReceipts_LastJudgeUtcNilWhenNoCall(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "r2.jsonl")
	body := `{"ts":"2026-09-10T03:00:00.0000000Z","workspace":"ws","status":"no-op"}` + "\n"
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	h := lint.ReadRunHistory(p, "ws")
	if h.LastJudgeUTC != nil {
		t.Errorf("LastJudgeUTC = %v, want nil when no receipt called the judge", h.LastJudgeUTC)
	}
	if h.LastProductiveUTC == nil {
		t.Error("LastProductiveUTC = nil, want the no-op row: reaching a decision is what ends starvation, and no-op IS a decision")
	}
}

func TestReceipts_MissingFileIsAZeroHistoryNotACrash(t *testing.T) {
	// "I cannot read the receipts" must not be reported as "this store is healthy" and
	// must not take down the banner either. A zero history says nothing, which is the
	// only honest answer, and the store's OTHER findings still render.
	h := lint.ReadRunHistory(filepath.Join(t.TempDir(), "absent.jsonl"), "ws")
	if h.SkipStreak != 0 || h.LastStatus != "" || h.LastProductiveUTC != nil || h.LastJudgeUTC != nil {
		t.Errorf("history from an absent file = %+v, want the zero value", h)
	}
}

func TestReceipts_OneTornLineDoesNotBlindTheReader(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "r.jsonl")
	body := `{"ts":"2026-09-09T03:00:00Z","workspace":"ws","status":"applied"}
{"ts":"2026-09-10T03:00:00Z","workspace":"ws","st
{"ts":"2026-09-11T03:00:00Z","workspace":"ws","status":"skipped-live-session"}
`
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	h := lint.ReadRunHistory(p, "ws")
	if h.LastStatus != lint.SkipLiveSession {
		t.Errorf("LastStatus = %q, want the row AFTER the torn append to still be read", h.LastStatus)
	}
	if h.LastProductiveUTC == nil {
		t.Error("the applied row before the torn append was lost")
	}
}

// --------------------------------------------------------------------------------
// MemoryCompactRobustness.Tests.ps1:568 / :583 - the G7 starvation metric.
// --------------------------------------------------------------------------------

func TestStarvation_G7MetricSurfacesStarvation(t *testing.T) {
	// A store over the trigger for 30 h whose only receipts are live-session skips. The
	// run stamp is fresh - another store's no-op marked it - which is exactly why the
	// metric cannot be receipt age: it has to be per-store and it has to be a DECISION.
	over := 30.0
	row := lint.StoreRow{
		Workspace:        "st",
		Bytes:            store.TriggerBytes + 500,
		OverTrigger:      true,
		SkipStreak:       2,
		LastStatus:       lint.SkipLiveSession,
		OverTriggerHours: &over,
	}
	h := lint.RunHistory{SkipStreak: 2, LastStatus: lint.SkipLiveSession}

	f := lint.StarvationFindings(row, h, fixedNow)
	if len(f) != 1 {
		t.Fatalf("findings = %v, want exactly one compactor-starved", f)
	}
	if f[0].Kind != lint.KindStarved {
		t.Errorf("kind = %q, want %q", f[0].Kind, lint.KindStarved)
	}
	if !lint.Actionable(f[0].Kind) {
		t.Error("compactor-starved must be actionable")
	}
	if !strings.Contains(f[0].Detail, "in a row") {
		t.Errorf("detail = %q, want it to name the skip streak", f[0].Detail)
	}
}

func TestStarvation_RecentDecisionNoAlarm(t *testing.T) {
	// Over the trigger for 30 h, but a decision landed an hour ago. A store that reached
	// a decision is not starved, however long it has been large: size is a budget, not a
	// fault.
	over := 30.0
	last := fixedNow.Add(-1 * time.Hour)
	row := lint.StoreRow{
		Workspace:        "st",
		Bytes:            store.TriggerBytes + 500,
		OverTrigger:      true,
		LastStatus:       "applied",
		OverTriggerHours: &over,
	}
	h := lint.RunHistory{LastStatus: "applied", LastProductiveUTC: &last}

	if f := lint.StarvationFindings(row, h, fixedNow); len(f) != 0 {
		t.Fatalf("findings = %v, want none: a decision an hour ago is not starvation", f)
	}
}

func TestStarvation_UnderTriggerIsNeverStarved(t *testing.T) {
	row := lint.StoreRow{Workspace: "st", Bytes: 100, OverTrigger: false, SkipStreak: 9}
	if f := lint.StarvationFindings(row, lint.RunHistory{SkipStreak: 9, LastStatus: lint.SkipLiveSession}, fixedNow); len(f) != 0 {
		t.Fatalf("findings = %v, want none - a small store that is skipped is being left alone correctly", f)
	}
}

func TestStarvation_OneSkipAtTheSyncLimitIsAlreadyStarvation(t *testing.T) {
	// Above the trigger one skip is a normal live night. AT the sync limit the harness
	// has stopped syncing the index at all, so one skip is already a store the nightly
	// is not getting.
	row := lint.StoreRow{
		Workspace: "st", Bytes: store.SyncLimitBytes + 1, OverTrigger: true,
		SkipStreak: 1, LastStatus: lint.SkipLiveSession,
	}
	f := lint.StarvationFindings(row, lint.RunHistory{SkipStreak: 1, LastStatus: lint.SkipLiveSession}, fixedNow)
	if len(f) != 1 {
		t.Fatalf("findings = %v, want one", f)
	}
	if !strings.Contains(f[0].Detail, "sync limit") {
		t.Errorf("detail = %q, want it to say the harness refuses to sync", f[0].Detail)
	}
}

// --------------------------------------------------------------------------------
// MemoryCompactRobustness.Tests.ps1:594 / :608 - compactor-starved through a full run.
// --------------------------------------------------------------------------------

// overTriggerStore builds a store big enough to sit above the byte trigger.
func overTriggerStore(t *testing.T, sb *testutil.Sandbox, ws string) {
	t.Helper()
	sb.AddStore(ws, testutil.BigIndex(60), testutil.BigIndexFacts(60))
	s := firstStoreNamed(t, sb, ws)
	st, err := lint.MeasureStore(s)
	if err != nil {
		t.Fatalf("MeasureStore: %v", err)
	}
	if !st.OverTrigger {
		t.Fatalf("fixture store %q is %d B / %d lines, not over the trigger", ws, st.Bytes, st.Lines)
	}
}

func firstStoreNamed(t *testing.T, sb *testutil.Sandbox, ws string) store.Store {
	t.Helper()
	stores, _, err := store.Enumerate(sb.ProjectsRoot)
	if err != nil {
		t.Fatalf("enumerate: %v", err)
	}
	for _, s := range stores {
		if s.Workspace == ws && !s.IsAlias {
			return s
		}
	}
	t.Fatalf("no store %q", ws)
	return store.Store{}
}

func runLint(t *testing.T, sb *testutil.Sandbox, nightlyUnit string) lint.Summary {
	t.Helper()
	sum, err := lint.Run(context.Background(), lint.Options{
		Roots:       roots(sb),
		NightlyUnit: nightlyUnit,
		Policy:      amsync.RemotePolicy{ExpectedHost: hubHost},
		Now:         fixedNow,
	})
	if err != nil {
		t.Fatalf("lint.Run: %v", err)
	}
	return sum
}

func countKind(sum lint.Summary, kind string) int {
	n := 0
	for _, f := range sum.Findings {
		if f.Kind == kind {
			n++
		}
	}
	return n
}

func TestLint_StarvedOnTwoSkips(t *testing.T) {
	sb := testutil.NewSandbox(t)
	overTriggerStore(t, sb, "lt")
	seedReceipts(t, receiptPath(sb), "lt", []string{lint.SkipLiveSession, lint.SkipLiveSession}, 1)

	sum := runLint(t, sb, "")

	var row *lint.StoreRow
	for i := range sum.Stores {
		if sum.Stores[i].Workspace == "lt" {
			row = &sum.Stores[i]
		}
	}
	if row == nil {
		t.Fatal("no store row for lt")
	}
	if row.SkipStreak != 2 {
		t.Errorf("skip_streak = %d, want 2 - the per-store signal both watchdogs lacked", row.SkipStreak)
	}
	if n := countKind(sum, lint.KindStarved); n != 1 {
		t.Errorf("compactor-starved findings = %d, want 1", n)
	}
	if sum.Counts.Actionable < 1 {
		t.Error("counts.actionable = 0: a finding missing from the actionable list reaches no surface at all")
	}
	if sum.Counts.Starved != 1 {
		t.Errorf("counts.starved = %d, want 1", sum.Counts.Starved)
	}
}

func TestLint_NoStarvedOnSingleSkip(t *testing.T) {
	sb := testutil.NewSandbox(t)
	overTriggerStore(t, sb, "lt")
	// applied, then one skip: one live night is normal, and the store reached a decision
	// an hour before that.
	seedReceipts(t, receiptPath(sb), "lt", []string{"applied", lint.SkipLiveSession}, 1)

	sum := runLint(t, sb, "")
	if n := countKind(sum, lint.KindStarved); n != 0 {
		t.Errorf("compactor-starved findings = %d, want 0 - one live night is not starvation", n)
	}
}

// --------------------------------------------------------------------------------
// MemoryCompactRobustness.Tests.ps1:620 / :635 - the neutral judge skip.
// --------------------------------------------------------------------------------

func TestLint_NeutralJudgeSkipNotUnproductive(t *testing.T) {
	sb := testutil.NewSandbox(t)
	overTriggerStore(t, sb, "lt")
	// A catch-up sweep re-visits every over-trigger store while any store is starved, so
	// a store whose judge already ran today collects one of these per session start.
	// They say "waiting", not "stuck".
	seedReceipts(t, receiptPath(sb), "lt", []string{
		"applied", lint.NeutralJudgeSkip, lint.NeutralJudgeSkip, lint.NeutralJudgeSkip,
	}, 1)

	sum := runLint(t, sb, "")
	if n := countKind(sum, lint.KindUnproductive); n != 0 {
		t.Errorf("compactor-unproductive findings = %d, want 0 - paging on a working store trains the operator to ignore the banner", n)
	}
}

func TestLint_UnproductiveAcrossNeutrals(t *testing.T) {
	sb := testutil.NewSandbox(t)
	overTriggerStore(t, sb, "lt")
	// Three rejections with neutrals between them. The neutrals are EXCLUDED from the
	// window rather than counted as good, so the three rejections still reach each other.
	seedReceipts(t, receiptPath(sb), "lt", []string{
		"rejected-no-shrink", lint.NeutralJudgeSkip,
		"rejected-no-shrink", lint.NeutralJudgeSkip,
		"rejected-no-shrink",
	}, 1)

	sum := runLint(t, sb, "")
	if n := countKind(sum, lint.KindUnproductive); n != 1 {
		t.Errorf("compactor-unproductive findings = %d, want 1", n)
	}
}

func TestLint_UnproductiveNeedsThreeNonNeutralRuns(t *testing.T) {
	sb := testutil.NewSandbox(t)
	overTriggerStore(t, sb, "lt")
	seedReceipts(t, receiptPath(sb), "lt", []string{"rejected-no-shrink", "rejected-no-shrink"}, 1)
	if n := countKind(runLint(t, sb, ""), lint.KindUnproductive); n != 0 {
		t.Errorf("compactor-unproductive = %d on two runs, want 0", n)
	}
}

func TestLint_UnproductiveIgnoresDryRuns(t *testing.T) {
	// A dry run decided nothing and changed nothing. Counting it either way is wrong;
	// LIB:522-528 drops it, and so does this.
	sb := testutil.NewSandbox(t)
	overTriggerStore(t, sb, "lt")
	p := receiptPath(sb)
	seedReceipts(t, p, "lt", []string{"rejected-no-shrink", "rejected-no-shrink", "rejected-no-shrink"}, 1)
	f, err := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	ts := fixedNow.Add(-30 * time.Minute).Format(time.RFC3339Nano)
	if _, err := f.WriteString(`{"ts":"` + ts + `","workspace":"lt","status":"applied","dry_run":true}` + "\n"); err != nil {
		t.Fatal(err)
	}
	f.Close()

	if n := countKind(runLint(t, sb, ""), lint.KindUnproductive); n != 1 {
		t.Errorf("compactor-unproductive = %d, want 1 - a dry-run 'applied' must not clear the finding", n)
	}
}

// --------------------------------------------------------------------------------
// Decision Q10 - compactor-silent is parameterised.
// --------------------------------------------------------------------------------

func TestLint_SilentDoesNotExistOnAPC(t *testing.T) {
	// The nightly was REMOVED from the PCs, so there is nothing local to be silent. A
	// finding naming a job that should not be running is noise, and noise on the banner
	// is how a real finding gets scrolled past.
	if f := lint.SilentFinding("", 3, nil); len(f) != 0 {
		t.Fatalf("SilentFinding on a PC = %v, want none", f)
	}

	sb := testutil.NewSandbox(t)
	overTriggerStore(t, sb, "lt")
	if n := countKind(runLint(t, sb, ""), lint.KindSilent); n != 0 {
		t.Errorf("compactor-silent findings = %d on a PC with no nightly unit, want 0", n)
	}
}

func TestLint_SilentNamesTheHubsTimer(t *testing.T) {
	sb := testutil.NewSandbox(t)
	overTriggerStore(t, sb, "lt")
	// No receipts file at all: the maintenance chain has never run here.
	sum := runLint(t, sb, "ams-nightly.timer")

	var silent *lint.Finding
	for i := range sum.Findings {
		if sum.Findings[i].Kind == lint.KindSilent {
			silent = &sum.Findings[i]
		}
	}
	if silent == nil {
		t.Fatal("no compactor-silent finding on the hub with stores above trigger and no receipts")
	}
	if silent.File != "ams-nightly.timer" {
		t.Errorf("file = %q, want the systemd unit - a hard-coded Windows task name is what the Linux port could not carry", silent.File)
	}
	if !lint.Actionable(lint.KindSilent) {
		t.Error("compactor-silent must be actionable")
	}
}

func TestLint_SilentIsQuietWhileTheReceiptsAreFresh(t *testing.T) {
	age := 3.0
	if f := lint.SilentFinding("ams-nightly.timer", 2, &age); len(f) != 0 {
		t.Fatalf("SilentFinding with a 3 h old receipts file = %v, want none", f)
	}
	stale := 60.0
	if f := lint.SilentFinding("ams-nightly.timer", 2, &stale); len(f) != 1 {
		t.Fatalf("SilentFinding with a 60 h old receipts file = %v, want one", f)
	}
}

func TestLint_SilentIsQuietWhenNoStoreIsOverTrigger(t *testing.T) {
	// A silent nightly over a fleet that needs nothing is a nightly with nothing to do.
	if f := lint.SilentFinding("ams-nightly.timer", 0, nil); len(f) != 0 {
		t.Fatalf("SilentFinding with no store above trigger = %v, want none", f)
	}
}

// --------------------------------------------------------------------------------
// Blueprint 7.2 - the Y-3 history-remote amendment.
// --------------------------------------------------------------------------------

func TestLint_HubOnAMagicDNSHostIsAllowed(t *testing.T) {
	sb := testutil.NewSandbox(t)
	sb.AddStore("ws", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})
	gitDir := sb.InitHistory()
	gitRun(t, gitDir, sb.ProjectsRoot, "remote", "add", "hub", "ams-hub@"+hubHost+":ams-store.git")

	sum := runLint(t, sb, "")
	if n := countKind(sum, lint.KindHistoryRemote); n != 0 {
		var got []string
		for _, f := range sum.Findings {
			if f.Kind == lint.KindHistoryRemote {
				got = append(got, f.Detail)
			}
		}
		t.Errorf("history-remote findings = %d (%v), want 0: Y-3 allows exactly one SSH remote named hub on the MagicDNS host", n, got)
	}
}

func TestLint_ARemoteThatIsNotTheHubIsAFinding(t *testing.T) {
	// Stores hold credentials and private brand facts. Before Y-3 ANY remote was a
	// finding because a push would publish them; Y-3 narrows that to one named, SSH,
	// MagicDNS remote and nothing else.
	cases := []struct {
		name, url, why string
	}{
		{"origin", "ams-hub@" + hubHost + ":ams-store.git", "the right host under the wrong remote name"},
		{"hub", "https://example.invalid/ams-store.git", "not SSH"},
		{"hub", "ams-hub@" + testutil.IPLiteral(100, 101, 102, 103) + ":ams-store.git", "a raw tailnet literal instead of the MagicDNS name"},
		// No dots, so the dotted-name rule cannot catch it. 2001:db8:: is RFC 3849's
		// documentation prefix: the rule is shape-based, so the prefix carries no
		// information and must not be a real tailnet ULA.
		{"hub", "ssh://ams-hub@[2001:db8::1]/ams-store.git", "a bracketed IPv6 literal"},
		{"hub", "ams-hub@some-other-box:ams-store.git", "a host that is not the hub"},
	}
	for _, c := range cases {
		t.Run(c.why, func(t *testing.T) {
			sb := testutil.NewSandbox(t)
			sb.AddStore("ws", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})
			gitDir := sb.InitHistory()
			gitRun(t, gitDir, sb.ProjectsRoot, "remote", "add", c.name, c.url)

			sum := runLint(t, sb, "")
			if n := countKind(sum, lint.KindHistoryRemote); n == 0 {
				t.Errorf("no history-remote finding for %s (%s)", c.url, c.why)
			}
		})
	}
}

func TestLint_ASecondRemoteIsAFindingEvenWhenTheHubIsRight(t *testing.T) {
	sb := testutil.NewSandbox(t)
	sb.AddStore("ws", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})
	gitDir := sb.InitHistory()
	gitRun(t, gitDir, sb.ProjectsRoot, "remote", "add", "hub", "ams-hub@"+hubHost+":ams-store.git")
	gitRun(t, gitDir, sb.ProjectsRoot, "remote", "add", "backup", "ams-hub@"+hubHost+":ams-store.git")

	if n := countKind(runLint(t, sb, ""), lint.KindHistoryRemote); n == 0 {
		t.Error("a second remote is still a finding: exactly one is the rule, and the second one is where the stores leave")
	}
}

// --------------------------------------------------------------------------------
// Blueprint 7.2 - resurrected and conflict-in-history come from the sync receipts.
// --------------------------------------------------------------------------------

func TestLint_ResurrectedAndConflictsAreReadFromTheSyncReceipts(t *testing.T) {
	sb := testutil.NewSandbox(t)
	sb.AddStore("ws", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})

	if err := amsync.AppendReceipt(sb.StateRoot, amsync.Receipt{
		TS:          fixedNow.Add(-time.Hour),
		Host:        "qube",
		Kind:        "once",
		Status:      amsync.StatusPushed,
		Resurrected: []string{"ws/memory/a.md"},
		ConflictsInHistory: []amsync.ConflictRef{
			{Path: "ws/memory/b.md", Commit: "0123456789abcdef0123456789abcdef01234567"},
		},
	}); err != nil {
		t.Fatalf("AppendReceipt: %v", err)
	}

	sum := runLint(t, sb, "")
	if countKind(sum, lint.KindResurrected) != 1 {
		t.Errorf("resurrected findings = %d, want 1 - a deliberate deletion that came back is a decision only a human makes", countKind(sum, lint.KindResurrected))
	}
	if countKind(sum, lint.KindConflictInHist) != 1 {
		t.Fatalf("conflict-in-history findings = %d, want 1", countKind(sum, lint.KindConflictInHist))
	}
	for _, f := range sum.Findings {
		if f.Kind == lint.KindConflictInHist && !strings.Contains(f.Detail, "0123456789abcdef") {
			t.Errorf("detail = %q, want the losing commit id - without it the loser is unrecoverable", f.Detail)
		}
		if f.Kind == lint.KindResurrected && f.Store != "ws" {
			t.Errorf("store = %q, want the workspace slug parsed out of the path", f.Store)
		}
	}
	if sum.Counts.Resurrected != 1 || sum.Counts.ConflictInHistory != 1 {
		t.Errorf("counts = %+v, want resurrected 1 and conflict_in_history 1", sum.Counts)
	}
}

// --------------------------------------------------------------------------------
// Blueprint 7.4 - the summary artifact.
// --------------------------------------------------------------------------------

func TestLint_SummaryGeneratedAtIsWholeSeconds(t *testing.T) {
	sb := testutil.NewSandbox(t)
	sb.AddStore("ws", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})

	// Deliberately a clock with sub-second precision: the 'o'/RFC3339Nano format would
	// emit seven fractional digits, which Python 3.10's fromisoformat rejects - and the
	// banner's staleness guard runs under exactly that runtime, so a nano stamp makes
	// the guard inert and presents a week-old summary as this morning's truth.
	sum, err := lint.Run(context.Background(), lint.Options{
		Roots: roots(sb),
		Now:   time.Date(2026, 9, 15, 12, 0, 0, 123456789, time.UTC),
	})
	if err != nil {
		t.Fatalf("lint.Run: %v", err)
	}
	if sum.GeneratedAt != "2026-09-15T12:00:00Z" {
		t.Fatalf("generated_at = %q, want whole-second Zulu", sum.GeneratedAt)
	}
	if strings.Contains(sum.GeneratedAt, ".") {
		t.Error("generated_at carries fractional seconds")
	}
	// The guard the stamp exists for: Python's fromisoformat equivalent here is that the
	// layout round-trips exactly.
	if _, err := time.Parse(lint.SummaryTimeFormat, sum.GeneratedAt); err != nil {
		t.Errorf("generated_at does not parse back: %v", err)
	}
}

func TestLint_SummaryCarriesTheKeysTheBannerReads(t *testing.T) {
	sb := testutil.NewSandbox(t)
	overTriggerStore(t, sb, "lt")
	sum := runLint(t, sb, "")
	if err := lint.Write(sb.StateRoot, sum); err != nil {
		t.Fatalf("Write: %v", err)
	}

	raw, err := os.ReadFile(lint.SummaryPath(sb.StateRoot))
	if err != nil {
		t.Fatalf("read summary: %v", err)
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("summary is not JSON: %v", err)
	}
	// storage-cap-check.sh reads these names. Renaming one silently EMPTIES the banner
	// rather than breaking it, which is the worst kind of change.
	for _, k := range []string{"generated_at", "stores", "findings", "counts", "last_receipt_age_hours"} {
		if _, ok := doc[k]; !ok {
			t.Errorf("summary has no %q", k)
		}
	}
	var counts map[string]json.RawMessage
	if err := json.Unmarshal(doc["counts"], &counts); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{
		"total", "orphan", "dangling", "dup_slug", "long_line", "oversized", "over_budget",
		"scan_error", "starved", "actionable",
		"resurrected", "conflict_in_history", "over_inject_limit",
	} {
		if _, ok := counts[k]; !ok {
			t.Errorf("counts has no %q", k)
		}
	}
	var rows []map[string]json.RawMessage
	if err := json.Unmarshal(doc["stores"], &rows); err != nil {
		t.Fatal(err)
	}
	if len(rows) == 0 {
		t.Fatal("stores[] is empty")
	}
	for _, k := range []string{"workspace", "bytes", "lines", "entries", "files", "over_trigger", "skip_streak", "over_trigger_hours"} {
		if _, ok := rows[0][k]; !ok {
			t.Errorf("stores[0] has no %q", k)
		}
	}
}

func TestLint_ScanErrorReachesTheBannerInsteadOfDisappearing(t *testing.T) {
	// An unreadable store used to vanish from the summary entirely: no row, no finding,
	// nothing to notice. scan-error is actionable precisely so that cannot happen again.
	sb := testutil.NewSandbox(t)
	dir := sb.AddStore("ws", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})
	// Replace MEMORY.md with a directory: readable as an entry, unreadable as a file, on
	// every OS.
	idx := filepath.Join(dir, store.IndexName)
	if err := os.Remove(idx); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(idx, 0o755); err != nil {
		t.Skipf("cannot stage an unreadable index here: %v", err)
	}

	sum := runLint(t, sb, "")
	if countKind(sum, lint.KindScanError) == 0 {
		t.Fatal("no scan-error finding for a store that cannot be read")
	}
	if sum.Counts.Actionable == 0 {
		t.Error("scan-error did not reach the actionable count")
	}
	found := false
	for _, r := range sum.Stores {
		if r.Workspace == "ws" {
			found = true
		}
	}
	if !found {
		t.Error("the unreadable store has no row at all - that is how it became invisible")
	}
}

func TestLint_NeverWritesInsideAStore(t *testing.T) {
	// Read-only by contract. Nothing but MEMORY.md and fact files may exist inside a
	// store: a maintenance file in there resurfaces in every agent's glob.
	sb := testutil.NewSandbox(t)
	dir := sb.AddStore("ws", []string{"- [A](a.md)", "- [Gone](gone.md)"}, map[string]string{
		"a.md":      testutil.FactFile("a", "d", "", ""),
		"orphan.md": testutil.FactFile("o", "d", "", ""),
	})
	before := snapshotDir(t, dir)

	sum := runLint(t, sb, "ams-nightly.timer")
	if len(sum.Findings) == 0 {
		t.Fatal("the fixture was built to produce findings; it produced none")
	}
	if err := lint.Write(sb.StateRoot, sum); err != nil {
		t.Fatalf("Write: %v", err)
	}

	after := snapshotDir(t, dir)
	if len(before) != len(after) {
		t.Fatalf("store contents changed: %v -> %v", before, after)
	}
	for k, v := range before {
		if after[k] != v {
			t.Errorf("%s changed under lint", k)
		}
	}
	// And the summary landed OUTSIDE the store, under the state root.
	if _, err := os.Stat(lint.SummaryPath(sb.StateRoot)); err != nil {
		t.Errorf("lint-summary.json is not under the state root: %v", err)
	}
}

func snapshotDir(t *testing.T, dir string) map[string]string {
	t.Helper()
	ents, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("readdir %s: %v", dir, err)
	}
	out := map[string]string{}
	for _, e := range ents {
		b, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			t.Fatalf("read %s: %v", e.Name(), err)
		}
		out[e.Name()] = string(b)
	}
	return out
}

func TestLint_AliasStoreIsNotReportedTwice(t *testing.T) {
	// An alias is the same physical store. Reporting it twice doubles every finding and
	// makes the counts a lie.
	sb := testutil.NewSandbox(t)
	sb.AddStore("ws", []string{"- [A](a.md)", "- [Gone](gone.md)"}, map[string]string{
		"a.md": testutil.FactFile("a", "d", "", ""),
	})
	sum := runLint(t, sb, "")
	if n := countKind(sum, lint.KindDangling); n != 1 {
		t.Errorf("dangling findings = %d, want exactly 1", n)
	}
	if len(sum.Stores) != 1 {
		t.Errorf("stores = %d, want 1", len(sum.Stores))
	}
}

// --------------------------------------------------------------------------------
// The over-trigger stamp (decision Q13) - min wins.
// --------------------------------------------------------------------------------

func TestLint_OverTriggerStampKeepsTheEarliestCrossing(t *testing.T) {
	sb := testutil.NewSandbox(t)
	later := fixedNow
	earlier := fixedNow.Add(-30 * time.Hour)

	if _, err := lint.RecordOverTrigger(sb.ProjectsRoot, "ws", true, later); err != nil {
		t.Fatalf("RecordOverTrigger: %v", err)
	}
	// A PC that only just noticed must not reset the clock: the clock IS the metric.
	s, err := lint.RecordOverTrigger(sb.ProjectsRoot, "ws", true, earlier)
	if err != nil {
		t.Fatalf("RecordOverTrigger: %v", err)
	}
	if got := s.OverTriggerSince["ws"]; !got.Equal(earlier) {
		t.Errorf("over_trigger_since = %s, want the earlier crossing %s", got, earlier)
	}

	h := s.HoursOverTrigger("ws", fixedNow)
	if h == nil || *h != 30 {
		t.Errorf("HoursOverTrigger = %v, want 30", h)
	}
	if lint.AlarmHours != 24 {
		t.Errorf("AlarmHours = %v, want 24", lint.AlarmHours)
	}
}

func TestLint_OverTriggerStampClearsWhenTheStoreFallsBack(t *testing.T) {
	sb := testutil.NewSandbox(t)
	if _, err := lint.RecordOverTrigger(sb.ProjectsRoot, "ws", true, fixedNow.Add(-30*time.Hour)); err != nil {
		t.Fatal(err)
	}
	s, err := lint.RecordOverTrigger(sb.ProjectsRoot, "ws", false, fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := s.OverTriggerSince["ws"]; ok {
		t.Error("the stamp survived the store falling back under the trigger; the next crossing must start a fresh clock")
	}
}

func TestLint_MergeStampsTakesTheMin(t *testing.T) {
	a := lint.Stamps{OverTriggerSince: map[string]time.Time{
		"one": fixedNow, "only-a": fixedNow,
	}}
	b := lint.Stamps{OverTriggerSince: map[string]time.Time{
		"one": fixedNow.Add(-5 * time.Hour), "only-b": fixedNow,
	}}
	got := lint.MergeStamps(a, b)
	if !got.OverTriggerSince["one"].Equal(fixedNow.Add(-5 * time.Hour)) {
		t.Errorf("merged 'one' = %s, want the earlier side", got.OverTriggerSince["one"])
	}
	if _, ok := got.OverTriggerSince["only-a"]; !ok {
		t.Error("only-a was dropped")
	}
	if _, ok := got.OverTriggerSince["only-b"]; !ok {
		t.Error("only-b was dropped")
	}
}

func TestLint_StampIsOutsideEveryStore(t *testing.T) {
	// It lives under <PROJECTS_ROOT>/.ams/ - inside the SYNCED tree so every PC agrees,
	// OUTSIDE every store because a maintenance file in a store resurfaces in every
	// agent's glob.
	sb := testutil.NewSandbox(t)
	dir := sb.AddStore("ws", []string{"- [A](a.md)"}, map[string]string{"a.md": testutil.FactFile("a", "d", "", "")})
	if _, err := lint.RecordOverTrigger(sb.ProjectsRoot, "ws", true, fixedNow); err != nil {
		t.Fatal(err)
	}
	p := lint.StampPath(sb.ProjectsRoot)
	if !strings.HasPrefix(p, filepath.Join(sb.ProjectsRoot, lint.StampDir)) {
		t.Errorf("stamp path = %q, want it under %s", p, lint.StampDir)
	}
	ents, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range ents {
		if e.Name() != store.IndexName && !strings.HasSuffix(e.Name(), ".md") {
			t.Errorf("%s appeared inside the store", e.Name())
		}
	}
}

func TestLint_CorruptStampIsAnErrorNotAnEmptySet(t *testing.T) {
	// "Absent" and "corrupt" must not collapse. A missing file is an empty set; a file
	// that exists but does not parse is an ERROR, because reading a truncated state file
	// as "nothing here" is the same disarm as corruption.
	sb := testutil.NewSandbox(t)
	p := lint.StampPath(sb.ProjectsRoot)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := lint.ReadStamps(sb.ProjectsRoot); err == nil {
		t.Error("a corrupt stamp file read as an empty set")
	}

	// And an ABSENT one is not an error.
	sb2 := testutil.NewSandbox(t)
	s, err := lint.ReadStamps(sb2.ProjectsRoot)
	if err != nil {
		t.Errorf("an absent stamp file errored: %v", err)
	}
	if len(s.OverTriggerSince) != 0 {
		t.Errorf("absent stamps = %v, want empty", s.OverTriggerSince)
	}
}

// gitRun runs a git command against the out-of-tree history repo.
func gitRun(t *testing.T, gitDir, workTree string, args ...string) {
	t.Helper()
	full := append([]string{"--git-dir=" + gitDir, "--work-tree=" + workTree}, args...)
	out, err := exec.Command("git", full...).CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}
