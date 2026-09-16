package gate

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// gateStore is the port of MemoryIndexWriteGate.Tests.ps1's New-GateStore (:8-21): count
// index lines whose hook is "detail number i " repeated, plus one fact file per line.
func gateStore(t *testing.T, count int, factType string, repeat int) (sb *testutil.Sandbox, indexPath, dir string) {
	t.Helper()
	sb = testutil.NewSandbox(t)
	lines := []string{"# Memory Index", ""}
	facts := map[string]string{}
	for i := 1; i <= count; i++ {
		detail := strings.Repeat(fmt.Sprintf("detail number %d ", i), repeat)
		lines = append(lines, fmt.Sprintf("- [Fact %d](fact%d.md) %s %s", i, i, store.EmDash, detail))
		facts[fmt.Sprintf("fact%d.md", i)] = "---\nname: fact" + itoa(i) +
			"\ndescription: \"d\"\nmetadata:\n  type: " + factType + "\n---\n\nbody\n"
	}
	dir = sb.AddStore("ws", lines, facts)
	return sb, filepath.Join(dir, store.IndexName), dir
}

func payload(path string) string {
	b, _ := json.Marshal(map[string]any{
		"hook_event_name": "PostToolUse",
		"tool_name":       "Edit",
		"tool_input":      map[string]string{"file_path": path},
	})
	return string(b)
}

type gateRun struct {
	Out   string
	Err   string
	Code  int
	Floor *refFloor
}

func runGate(t *testing.T, sb *testutil.Sandbox, path string) gateRun {
	t.Helper()
	var out, errb bytes.Buffer
	f := &refFloor{}
	code := Run(context.Background(), Options{
		Roots:   store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot},
		Floor:   f,
		Version: "test",
		Stdout:  &out,
		Stderr:  &errb,
	}, strings.NewReader(payload(path)))
	return gateRun{Out: out.String(), Err: errb.String(), Code: code, Floor: f}
}

func readBytes(t *testing.T, p string) []byte {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// TestGate_SilentUnderCaps is the counterpart of MemoryIndexWriteGate.Tests.ps1:37 - "is
// silent and writes nothing for an index under every cap".
//
// Byte identity, not "looks the same": the whole point of the fixture is BOM, CRLF and
// em-dash fidelity, and a string compare would pass while the file gained a BOM.
func TestGate_SilentUnderCaps(t *testing.T) {
	sb, idx, _ := gateStore(t, 5, "project", 2)
	before := readBytes(t, idx)

	r := runGate(t, sb, idx)
	if r.Code != 0 {
		t.Fatalf("the gate returned %d; it must always return 0", r.Code)
	}
	if strings.TrimSpace(r.Out) != "" {
		t.Fatalf("the gate spoke about a store under every cap:\n%s", r.Out)
	}
	if !bytes.Equal(before, readBytes(t, idx)) {
		t.Fatal("the gate rewrote an index that was under every cap")
	}
	if r.Floor.calls != 0 {
		t.Fatalf("the floor was invoked %d times under the caps", r.Floor.calls)
	}
}

// TestGate_IgnoresNonIndexPath is the counterpart of :45 - "ignores files that are not a
// workspace MEMORY.md".
func TestGate_IgnoresNonIndexPath(t *testing.T) {
	sb, idx, dir := gateStore(t, 80, "project", 22)
	other := filepath.Join(dir, "notes.md")
	if err := os.WriteFile(other, readBytes(t, idx), 0o644); err != nil {
		t.Fatal(err)
	}
	before := readBytes(t, other)

	r := runGate(t, sb, other)
	if strings.TrimSpace(r.Out) != "" {
		t.Fatalf("the gate spoke about a file that is not a store index:\n%s", r.Out)
	}
	if !bytes.Equal(before, readBytes(t, other)) {
		t.Fatal("the gate rewrote a file that is not a store index")
	}
	// And the real index, which IS over the limit, was not touched either: the gate acts
	// on the path the payload named, never on whatever store it can find.
	if r.Floor.calls != 0 {
		t.Fatalf("the floor ran for a non-index path (%d calls)", r.Floor.calls)
	}
}

// TestGate_AdvisesWithoutRewritingUnderSyncLimit is the counterpart of :53 - "advises but
// does NOT rewrite an index that is over the line cap yet under the sync limit".
func TestGate_AdvisesWithoutRewritingUnderSyncLimit(t *testing.T) {
	sb, idx, _ := gateStore(t, 20, "project", 22)
	before := readBytes(t, idx)
	if len(before) >= store.SyncLimitBytes {
		t.Fatalf("the fixture is %d B; this scenario needs it under the sync limit", len(before))
	}

	r := runGate(t, sb, idx)
	if !strings.Contains(r.Out, "index line(s) over 130 B") {
		t.Fatalf("the advisory does not name the over-cap lines:\n%s", r.Out)
	}
	if strings.Contains(r.Out, "NORMALIZED") {
		t.Fatalf("the gate normalized below the sync limit:\n%s", r.Out)
	}
	if !bytes.Equal(before, readBytes(t, idx)) {
		t.Fatal("the gate rewrote an index that was under the sync limit")
	}
	if r.Floor.calls != 0 {
		t.Fatalf("the floor ran below the sync limit (%d calls)", r.Floor.calls)
	}
}

// TestGate_NormalizesAtSyncLimitAndWarnsStale is the counterpart of :63 - "normalizes an
// index at/over the sync limit back under the trigger, receipted, and says the in-context
// copy is stale".
func TestGate_NormalizesAtSyncLimitAndWarnsStale(t *testing.T) {
	sb, idx, _ := gateStore(t, 80, "project", 22)
	before := readBytes(t, idx)
	if len(before) <= store.SyncLimitBytes {
		t.Fatalf("the fixture is %d B; this scenario needs it over the sync limit", len(before))
	}

	r := runGate(t, sb, idx)
	if !strings.Contains(r.Out, "NORMALIZED") {
		t.Fatalf("the gate did not normalize:\n%s", r.Out)
	}
	if !strings.Contains(r.Out, "STALE") {
		t.Fatalf("the advisory does not warn that the in-context copy is stale:\n%s", r.Out)
	}
	after := readBytes(t, idx)
	if len(after) >= store.TriggerBytes {
		t.Fatalf("the index is %d B after normalization, want under the %d B trigger", len(after), store.TriggerBytes)
	}

	// Normalization shortens hooks; it NEVER drops an entry. A gate that could delete a
	// pointer would silently lose a fact file from every future session.
	entries := 0
	for _, l := range strings.Split(string(after), "\n") {
		if strings.HasPrefix(l, "- [Fact ") && strings.Contains(l, ".md)") {
			entries++
		}
	}
	if entries != 80 {
		t.Fatalf("%d entries survived normalization, want 80", entries)
	}

	rec := readReceipt(t, sb.StateRoot)
	if !rec.Converged {
		t.Fatalf("the receipt does not report convergence: %+v", rec)
	}
	if rec.Floored == 0 || rec.BeforeBytes != len(before) || rec.AfterBytes != len(after) {
		t.Fatalf("the receipt does not describe what happened: %+v (before %d, after %d)", rec, len(before), len(after))
	}
	if rec.Index != idx {
		t.Fatalf("the receipt names %q, want %q", rec.Index, idx)
	}
}

// TestGate_DoctrineOnlyOverflowReported is the counterpart of :77 - "never truncates
// doctrine (feedback-typed) lines, and says so when nothing else is normalizable".
func TestGate_DoctrineOnlyOverflowReported(t *testing.T) {
	sb, idx, _ := gateStore(t, 80, "feedback", 22)
	before := readBytes(t, idx)

	r := runGate(t, sb, idx)
	if !bytes.Equal(before, readBytes(t, idx)) {
		t.Fatal("doctrine is the hard rule: the gate must never touch it")
	}
	if !strings.Contains(r.Out, "doctrine") {
		t.Fatalf("the advisory does not explain that the overflow is doctrine:\n%s", r.Out)
	}
	if _, err := os.Stat(filepath.Join(sb.StateRoot, ReceiptFile)); err == nil {
		t.Fatal("a receipt was written for a run that changed nothing")
	}
}

// TestGate_CompareAndSwapAbortsOnAConcurrentWrite: a live session writing the index back
// between the gate's read and its write must win. The gate re-takes the hash immediately
// before writing and abandons its edit on drift.
func TestGate_CompareAndSwapAbortsOnAConcurrentWrite(t *testing.T) {
	sb, idx, _ := gateStore(t, 80, "project", 22)

	var out bytes.Buffer
	racing := &racingFloor{path: idx, inner: &refFloor{}}
	code := Run(context.Background(), Options{
		Roots:  store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot},
		Floor:  racing,
		Stdout: &out,
	}, strings.NewReader(payload(idx)))

	if code != 0 {
		t.Fatalf("exit %d, want 0", code)
	}
	if !strings.Contains(out.String(), "index changed under the gate") {
		t.Fatalf("the gate did not report the abort:\n%s", out.String())
	}
	if got := string(readBytes(t, idx)); got != racingFloorContent {
		t.Fatal("the gate clobbered what the live session wrote")
	}
	if _, err := os.Stat(filepath.Join(sb.StateRoot, ReceiptFile)); err == nil {
		t.Fatal("an aborted run wrote a receipt")
	}
}

// TestGate_ContenderSkipsWithoutWaiting: the gate never queues behind another maintainer.
func TestGate_ContenderSkipsWithoutWaiting(t *testing.T) {
	sb, idx, _ := gateStore(t, 80, "project", 22)
	before := readBytes(t, idx)

	var out bytes.Buffer
	f := &refFloor{}
	code := Run(context.Background(), Options{
		Roots:   store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot},
		Floor:   f,
		TryLock: func() (func(), bool) { return nil, false },
		Stdout:  &out,
	}, strings.NewReader(payload(idx)))

	if code != 0 {
		t.Fatalf("exit %d, want 0", code)
	}
	if strings.TrimSpace(out.String()) != "" {
		t.Fatalf("a contender spoke:\n%s", out.String())
	}
	if f.calls != 0 {
		t.Fatal("a contender ran the floor")
	}
	if !bytes.Equal(before, readBytes(t, idx)) {
		t.Fatal("a contender rewrote the index")
	}
}

// TestGate_MarksDirtyOnlyAfterItWrote: the dirty marker and the local commit are what the
// watcher wakes on. A gate that marked dirty on every advisory would wake the watcher on
// every keystroke-sized edit.
func TestGate_MarksDirtyOnlyAfterItWrote(t *testing.T) {
	for _, tc := range []struct {
		name      string
		count     int
		wantWrite bool
	}{
		{"under the sync limit", 20, false},
		{"over the sync limit", 80, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sb, idx, dir := gateStore(t, tc.count, "project", 22)
			called := 0
			var gotDir string
			code := Run(context.Background(), Options{
				Roots: store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot},
				Floor: &refFloor{},
				OnWrite: func(_ context.Context, storeDir string) error {
					called++
					gotDir = storeDir
					return nil
				},
			}, strings.NewReader(payload(idx)))
			if code != 0 {
				t.Fatalf("exit %d", code)
			}
			if tc.wantWrite && called != 1 {
				t.Fatalf("OnWrite called %d times after a write, want 1", called)
			}
			if !tc.wantWrite && called != 0 {
				t.Fatalf("OnWrite called %d times without a write", called)
			}
			if tc.wantWrite && gotDir != dir {
				t.Fatalf("OnWrite got %q, want the store directory %q", gotDir, dir)
			}
		})
	}
}

// TestGate_NeverReturnsAnythingButZero covers the fail-open contract against the payloads
// that are not payloads at all.
func TestGate_NeverReturnsAnythingButZero(t *testing.T) {
	sb, _, _ := gateStore(t, 5, "project", 2)
	opt := Options{Roots: store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot}, Floor: &refFloor{}}
	for _, in := range []string{
		"",
		"   ",
		"{not json",
		`{"tool_input":{}}`,
		`{"tool_input":{"file_path":""}}`,
		`{"tool_input":{"file_path":"/nowhere/memory/MEMORY.md"}}`,
		`{"tool_input":{"file_path":"/tmp/other.md"}}`,
	} {
		if code := Run(context.Background(), opt, strings.NewReader(in)); code != 0 {
			t.Fatalf("payload %q gave exit %d, want 0", in, code)
		}
	}
	// A panicking floor is the worst case the recover exists for.
	panicking := Options{
		Roots: store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot},
		Floor: panicFloor{},
	}
	_, idx, _ := gateStore(t, 80, "project", 22)
	if code := Run(context.Background(), panicking, strings.NewReader(payload(idx))); code != 0 {
		t.Fatalf("a panicking floor gave exit %d, want 0", code)
	}
}

// TestGate_CountsNonBlankLinesNotEveryLine pins the scaffold correction "two line-count
// rules stay separate": the gate counts NON-BLANK lines (GATE:41) while index.LineCount
// counts every line less one trailing blank (LIB:411-412). Unifying them would make the
// gate's advisory and lint's over-inject-limit disagree about the same file.
func TestGate_CountsNonBlankLinesNotEveryLine(t *testing.T) {
	text := "# Memory Index\n\n- [A](a.md)\n\n\n- [B](b.md)\n"
	if got := CountNonBlank(text); got != 3 {
		t.Fatalf("CountNonBlank = %d, want 3 (heading plus two entries)", got)
	}
	if got := CountNonBlank("   \n\t\n"); got != 0 {
		t.Fatalf("whitespace-only lines counted as %d, want 0", got)
	}
	if got := CountNonBlank("a\r\nb\r\n"); got != 2 {
		t.Fatalf("CRLF text counted as %d, want 2", got)
	}
}

// TestGate_IndexPathMatching keeps the anchored match honest.
func TestGate_IndexPathMatching(t *testing.T) {
	for _, p := range []string{
		`C:\Users\x\.claude\projects\ws\memory\MEMORY.md`,
		"/home/x/.claude/projects/ws/memory/MEMORY.md",
	} {
		if !MatchesIndexPath(p) {
			t.Fatalf("MatchesIndexPath(%q) = false", p)
		}
	}
	for _, p := range []string{
		"/home/x/.claude/projects/ws/memory/MEMORY.md.bak",
		"/home/x/.claude/projects/ws/MEMORY.md",
		"/home/x/.claude/projects/ws/memory/notes.md",
		"/home/x/memoryMEMORY.md",
	} {
		if MatchesIndexPath(p) {
			t.Fatalf("MatchesIndexPath(%q) = true", p)
		}
	}
}

// racingFloor writes the index from "another session" while the floor is running, which
// is exactly the window the compare-and-swap covers.
type racingFloor struct {
	path  string
	inner *refFloor
}

const racingFloorContent = "# Memory Index\n\n- [Written by a live session](x.md)\n"

func (r *racingFloor) Floor(records []*index.Record, storeDir, newline string, engageAt, stopBelow int) (FloorResult, error) {
	res, err := r.inner.Floor(records, storeDir, newline, engageAt, stopBelow)
	_ = os.WriteFile(r.path, []byte(racingFloorContent), 0o644)
	return res, err
}

type panicFloor struct{}

func (panicFloor) Floor([]*index.Record, string, string, int, int) (FloorResult, error) {
	panic("the floor blew up")
}

func readReceipt(t *testing.T, stateRoot string) Receipt {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(stateRoot, ReceiptFile))
	if err != nil {
		t.Fatalf("no gate receipt was written: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	var r Receipt
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &r); err != nil {
		t.Fatalf("the receipt does not parse: %v\n%s", err, b)
	}
	return r
}
