package judge

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// A plan is written by a different program in a different language. Every rule here is a
// producer/consumer mismatch that would otherwise be applied as if it were a decision.
func TestPlan_ValidationRejectsMalformedPlans(t *testing.T) {
	cases := []struct {
		name string
		doc  string
		want string
	}{
		{"empty file", "  \n", "empty"},
		{"wrong version", `{"version":2,"stores":[{"workspace":"ws","decisions":[]}]}`, "version"},
		{"no stores", `{"version":1,"stores":[]}`, "no stores"},
		{"unknown field", `{"version":1,"stores":[{"workspace":"ws","decisions":[],"actions":[]}]}`, "unknown field"},
		{"unknown verb", `{"version":1,"stores":[{"workspace":"ws","decisions":[{"slug":"a.md","verb":"DELETE"}]}]}`, "unknown verb"},
		{"no verb", `{"version":1,"stores":[{"workspace":"ws","decisions":[{"slug":"a.md"}]}]}`, "no verb"},
		{"slug with a space", `{"version":1,"stores":[{"workspace":"ws","decisions":[{"slug":"a b.md","verb":"KEEP"}]}]}`, "is not a slug"},
		{"slug not .md", `{"version":1,"stores":[{"workspace":"ws","decisions":[{"slug":"a.txt","verb":"KEEP"}]}]}`, "is not a slug"},
		{"shorten with no hook", `{"version":1,"stores":[{"workspace":"ws","decisions":[{"slug":"a.md","verb":"SHORTEN"}]}]}`, "no new_hook"},
		{"keep with an edit", `{"version":1,"stores":[{"workspace":"ws","decisions":[{"slug":"a.md","verb":"KEEP","new_hook":"x"}]}]}`, "carries an edit"},
		{"duplicate slug", `{"version":1,"stores":[{"workspace":"ws","decisions":[{"slug":"a.md","verb":"KEEP"},{"slug":"a.md","verb":"KEEP"}]}]}`, "two decisions"},
		{"duplicate workspace", `{"version":1,"stores":[{"workspace":"ws","decisions":[]},{"workspace":"ws","decisions":[]}]}`, "twice"},
		{"decisions under a failed call", `{"version":1,"stores":[{"workspace":"ws","outcome":"unavailable","decisions":[{"slug":"a.md","verb":"KEEP"}]}]}`, "did not answer"},
		{"unknown outcome", `{"version":1,"stores":[{"workspace":"ws","outcome":"maybe","decisions":[]}]}`, "unknown outcome"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := ParsePlan([]byte(c.doc))
			if err == nil {
				t.Fatalf("a malformed plan was accepted")
			}
			if !strings.Contains(err.Error(), c.want) {
				t.Errorf("error %q does not name the violation (%q)", err, c.want)
			}
		})
	}
}

func TestPlan_AcceptsAValidPlanAndDefaultsTheOutcome(t *testing.T) {
	doc := `{"version":1,"generated_at":"2026-09-15T05:00:00Z","stores":[
      {"workspace":"ws","decisions":[
        {"slug":"a.md","verb":"SHORTEN","new_hook":"port 18791 is the authority"},
        {"slug":"b.md","verb":"MIGRATE","metadata":{"tier":"evidence"}},
        {"slug":"c.md","verb":"KEEP"}]}]}`
	p, err := ParsePlan([]byte(doc))
	if err != nil {
		t.Fatalf("a valid plan was rejected: %v", err)
	}
	sp, ok := p.Store("ws")
	if !ok {
		t.Fatal("the store is not addressable by workspace")
	}
	if sp.Outcome != OutcomeOK {
		t.Errorf("outcome = %q, want the ok default", sp.Outcome)
	}
	if len(sp.Decisions) != 3 {
		t.Fatalf("decisions = %d, want 3", len(sp.Decisions))
	}
	if _, ok := p.Store("other"); ok {
		t.Error("a workspace the plan does not name must not be addressable")
	}
}

func TestPlan_LoadReadsAFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "plan.json")
	if err := os.WriteFile(path, []byte(`{"version":1,"stores":[{"workspace":"ws","decisions":[]}]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadPlan(path); err != nil {
		t.Fatalf("LoadPlan: %v", err)
	}
	if _, err := LoadPlan(filepath.Join(dir, "absent.json")); err == nil {
		t.Error("a missing plan file must be an error, never an empty plan")
	}
}

// Absent and corrupt must not collapse. A truncated seal file read as "no seals" re-arms
// the judge on every already-shortened line, and the save that follows discards the
// history permanently - the same disarm as corruption, reached by a different door.
func TestSeal_AbsentIsEmptyButCorruptIsAnError(t *testing.T) {
	dir := t.TempDir()
	path := SealPath(dir)

	s, err := LoadSeal(path)
	if err != nil {
		t.Fatalf("a missing seal file must be an empty seal set: %v", err)
	}
	if len(s) != 0 {
		t.Fatalf("seal = %v, want empty", s)
	}

	if err := os.WriteFile(path, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadSeal(path); err == nil {
		t.Error("an EMPTY seal file is present-but-unparseable, not 'no seals'")
	}

	if err := os.WriteFile(path, []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadSeal(path); err == nil {
		t.Error("a corrupt seal file must stop the run")
	}
}

func TestSeal_StampAndSaveRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := SealPath(dir)
	s := Seal{}
	s.Stamp("a.md", time.Date(2026, 9, 15, 5, 0, 0, 0, time.UTC))
	if err := SaveSeal(path, s); err != nil {
		t.Fatalf("SaveSeal: %v", err)
	}
	back, err := LoadSeal(path)
	if err != nil {
		t.Fatalf("LoadSeal: %v", err)
	}
	if !back.Sealed("a.md") {
		t.Error("the seal did not survive a save/load")
	}
	if back.Sealed("b.md") {
		t.Error("an unsealed slug reads as sealed")
	}
}

func TestRole_HubOnlyGuard(t *testing.T) {
	dir := t.TempDir()

	if err := RequireHub(dir, false); err == nil {
		t.Error("no role file must mean 'not the hub'")
	}
	if err := RequireHub(dir, true); err != nil {
		t.Errorf("the explicit flag must let the hub run before its role file exists: %v", err)
	}
	for _, role := range []string{"pc", "replica", "", "hubs"} {
		if err := os.WriteFile(filepath.Join(dir, RoleFileName), []byte(role), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := RequireHub(dir, false); err == nil {
			t.Errorf("role %q was accepted as the hub", role)
		}
	}
	if err := os.WriteFile(filepath.Join(dir, RoleFileName), []byte("hub\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := RequireHub(dir, false); err != nil {
		t.Errorf("the hub was refused: %v", err)
	}
}

// The 20 h window is receipt state, never a timer. A dry run must not consume the
// night's attempt, and another workspace's attempt must not hold this one off.
func TestReceipts_JudgeWindowIsReceiptState(t *testing.T) {
	dir := t.TempDir()
	now := time.Date(2026, 9, 15, 5, 0, 0, 0, time.UTC)
	write := func(ws string, hoursAgo float64, dry, called bool) {
		r := Receipt{TS: now.Add(-time.Duration(hoursAgo * float64(time.Hour))).Format(time.RFC3339Nano),
			Workspace: ws, DryRun: dry, JudgeCalled: called, Status: StatusNoOp}
		if err := WriteReceipt(dir, r); err != nil {
			t.Fatal(err)
		}
	}

	if _, within, err := WithinJudgeWindow(dir, "ws", now); err != nil || within {
		t.Fatalf("no receipts must mean the window is open (within=%v err=%v)", within, err)
	}
	write("other", 1, false, true)
	if _, within, _ := WithinJudgeWindow(dir, "ws", now); within {
		t.Error("another workspace's attempt held this one off")
	}
	write("ws", 1, true, true)
	if _, within, _ := WithinJudgeWindow(dir, "ws", now); within {
		t.Error("a dry run consumed the night's attempt")
	}
	write("ws", 1, false, false)
	if _, within, _ := WithinJudgeWindow(dir, "ws", now); within {
		t.Error("a run that never called the judge consumed the attempt")
	}
	write("ws", JudgeWindowHours+1, false, true)
	if _, within, _ := WithinJudgeWindow(dir, "ws", now); within {
		t.Error("an attempt older than the window still held the store off")
	}
	write("ws", 2, false, true)
	last, within, _ := WithinJudgeWindow(dir, "ws", now)
	if !within {
		t.Fatal("an attempt two hours ago is inside the window")
	}
	if got := now.Sub(last); got != 2*time.Hour {
		t.Errorf("the newest attempt is %v old, want 2h", got)
	}
}
