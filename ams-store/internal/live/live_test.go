package live

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

func transcript(t *testing.T, dir, name string, age time.Duration) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(`{"type":"user"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	when := time.Now().Add(-age)
	if err := os.Chtimes(p, when, when); err != nil {
		t.Fatal(err)
	}
	return p
}

// TestLiveness_RecentStaleAbsent is the counterpart of MemoryStoreLib.Tests.ps1:293 -
// "is live when a transcript in that workspace was written recently, not otherwise".
// The Pester original walks the same three states: fresh, 3 h old, and no directory.
func TestLiveness_RecentStaleAbsent(t *testing.T) {
	root := t.TempDir()
	ws := filepath.Join(root, "ws")
	now := time.Now()

	transcript(t, ws, "session.jsonl", 0)
	if !Workspace([]string{ws}, DefaultWithin, now) {
		t.Fatal("a transcript written just now must read as live")
	}

	transcript(t, ws, "session.jsonl", 3*time.Hour)
	if Workspace([]string{ws}, DefaultWithin, now) {
		t.Fatal("a transcript 3 h old must not read as live")
	}

	if Workspace([]string{filepath.Join(root, "absent")}, DefaultWithin, now) {
		t.Fatal("a workspace with no directory at all must not read as live")
	}
}

// TestLiveness_FailsClosed: an unreadable probe directory reports LIVE, and a probe with
// no directories at all reports LIVE. "I could not tell" is never "go ahead".
func TestLiveness_FailsClosed(t *testing.T) {
	if !Workspace(nil, DefaultWithin, time.Now()) {
		t.Fatal("a probe with no directories must fail closed to LIVE")
	}
	root := t.TempDir()
	// A plain FILE where a probe directory is expected: ReadDir fails with something
	// other than NotExist, which is the "cannot tell" case.
	notADir := filepath.Join(root, "ws")
	if err := os.WriteFile(notADir, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !Workspace([]string{notADir}, DefaultWithin, time.Now()) {
		t.Fatal("an unreadable probe directory must fail closed to LIVE")
	}
}

// TestLiveness_ProbesAliasDirectories: a session running under a junction writes its
// transcript into the ALIAS directory, so the canonical name alone would miss it.
func TestLiveness_ProbesAliasDirectories(t *testing.T) {
	root := t.TempDir()
	canonical := filepath.Join(root, "real")
	alias := filepath.Join(root, "alias")
	if err := os.MkdirAll(canonical, 0o755); err != nil {
		t.Fatal(err)
	}
	transcript(t, alias, "session.jsonl", time.Minute)

	if Workspace([]string{canonical}, DefaultWithin, time.Now()) {
		t.Fatal("the canonical directory alone has no transcript and must not read as live")
	}
	if !Workspace([]string{canonical, alias}, DefaultWithin, time.Now()) {
		t.Fatal("probing the alias directory too must find the live session")
	}
}

// TestLiveness_AnyClaudeSessionIsTheWatchersSignal: the watcher exits when no session is
// live anywhere on the PC, and that is the same transcript rule widened to the root.
func TestLiveness_AnyClaudeSessionIsTheWatchersSignal(t *testing.T) {
	root := t.TempDir()
	projects := filepath.Join(root, "projects")
	if err := os.MkdirAll(projects, 0o755); err != nil {
		t.Fatal(err)
	}
	if AnyClaudeSession(projects, DefaultWithin, time.Now()) {
		t.Fatal("an empty projects root has no live session")
	}
	if AnyClaudeSession(filepath.Join(root, "never-existed"), DefaultWithin, time.Now()) {
		t.Fatal("a PC with no projects root at all has no live session")
	}
	transcript(t, filepath.Join(projects, "ws"), "session.jsonl", time.Minute)
	if !AnyClaudeSession(projects, DefaultWithin, time.Now()) {
		t.Fatal("a fresh transcript under any workspace is a live session")
	}
}

// TestLiveness_SessionStartIsTheOldestLiveTranscriptClampedTo24h backs the materialize
// guard's mtime frontier.
func TestLiveness_SessionStartIsTheOldestLiveTranscriptClampedTo24h(t *testing.T) {
	root := t.TempDir()
	ws := filepath.Join(root, "ws")
	now := time.Now()
	transcript(t, ws, "a.jsonl", 20*time.Minute)
	transcript(t, ws, "b.jsonl", 2*time.Minute)

	got := SessionStart([]string{ws}, DefaultWithin, now)
	want := now.Add(-20 * time.Minute)
	if got.Sub(want) > 2*time.Second || want.Sub(got) > 2*time.Second {
		t.Fatalf("session start %v, want about %v", got, want)
	}

	// No live transcript at all clamps to now-24h rather than the zero time, so a
	// comparison against it can never accidentally protect every file on disk.
	empty := SessionStart([]string{filepath.Join(root, "none")}, DefaultWithin, now)
	if empty.Before(now.Add(-25*time.Hour)) || empty.After(now.Add(-23*time.Hour)) {
		t.Fatalf("session start with no transcripts is %v, want about now-24h", empty)
	}
}

// TestStarvation_SkipStreakCounted is the counterpart of
// MemoryCompactRobustness.Tests.ps1:523 - "UNDER the sync limit a live session still
// skips, and the receipt now counts the streak". One prior skip plus this one is 2.
func TestStarvation_SkipStreakCounted(t *testing.T) {
	d := Escalate(store.TriggerBytes+500, store.SyncLimitBytes, 1, true, true)
	if d.Override {
		t.Fatal("under the sync limit a live session is never overridden")
	}
	if d.SkipStreak != 2 {
		t.Fatalf("skip streak %d, want 2", d.SkipStreak)
	}
	if !contains(d.Note, "skip streak 2") {
		t.Fatalf("the receipt note must carry the streak: %q", d.Note)
	}
}

// TestStarvation_OverrideAfterTwoSkips is the counterpart of
// MemoryCompactRobustness.Tests.ps1:535 - at the sync limit after two skips a live
// session no longer blocks, and the receipt says why.
func TestStarvation_OverrideAfterTwoSkips(t *testing.T) {
	d := Escalate(30000, store.SyncLimitBytes, 2, true, true)
	if !d.Override {
		t.Fatal("at the sync limit after two skips the run must proceed")
	}
	if !contains(d.Note, "LIVENESS OVERRIDDEN") {
		t.Fatalf("note %q must say LIVENESS OVERRIDDEN", d.Note)
	}
	if !contains(d.Why, "already skipped 2") {
		t.Fatalf("why %q must name the streak", d.Why)
	}
	if d.SkipStreak != 0 {
		t.Fatalf("an overriding run does not extend the streak, got %d", d.SkipStreak)
	}
}

// TestStarvation_OverrideWhenQuietFiveMinutes is the counterpart of
// MemoryCompactRobustness.Tests.ps1:548 - at the sync limit with no prior skip, a session
// quiet for 10 min is not mid-write.
func TestStarvation_OverrideWhenQuietFiveMinutes(t *testing.T) {
	// live within 30 min, NOT within 5: the escalated probe is what opens the door.
	d := Escalate(30000, store.SyncLimitBytes, 0, true, false)
	if !d.Override {
		t.Fatal("a session quiet for more than 5 min at the sync limit must not block")
	}
	if !contains(d.Why, "no transcript write in the last 5 min") {
		t.Fatalf("why %q must name the 5-minute window", d.Why)
	}
}

// TestStarvation_NoOverrideWhenJustWritten is the counterpart of
// MemoryCompactRobustness.Tests.ps1:558 - at the sync limit, a session that wrote a
// minute ago STILL skips. The override is a boundary, not a blanket.
func TestStarvation_NoOverrideWhenJustWritten(t *testing.T) {
	d := Escalate(30000, store.SyncLimitBytes, 0, true, true)
	if d.Override {
		t.Fatal("a session that wrote within 5 min is mid-write; the run must still skip")
	}
	if d.SkipStreak != 1 {
		t.Fatalf("skip streak %d, want 1", d.SkipStreak)
	}
}

// TestStarvation_EscalationIsOnlyAtTheSyncLimit: the whole escalation is gated on the
// store being at or over the limit. One byte under, nothing changes.
func TestStarvation_EscalationIsOnlyAtTheSyncLimit(t *testing.T) {
	if d := Escalate(store.SyncLimitBytes-1, store.SyncLimitBytes, 5, true, false); d.Override {
		t.Fatal("under the limit, neither a streak nor a quiet window overrides liveness")
	}
	if d := Escalate(store.SyncLimitBytes, store.SyncLimitBytes, 5, true, false); !d.Override {
		t.Fatal("AT the limit (not just over) the escalation applies")
	}
}

// TestStarvation_NotLiveNeedsNoDecision: with no live session there is nothing to
// override and nothing to skip.
func TestStarvation_NotLiveNeedsNoDecision(t *testing.T) {
	d := Escalate(30000, store.SyncLimitBytes, 3, false, false)
	if d.Live || d.Override || d.SkipStreak != 0 || d.Note != "" {
		t.Fatalf("a store with no live session yielded %+v", d)
	}
}

func contains(s, sub string) bool {
	return len(sub) == 0 || (len(s) >= len(sub) && indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
