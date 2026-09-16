// Package live answers "is a Claude session writing right now".
//
// The signal is the transcript, never the OS process table. A harness session writes
// *.jsonl into its workspace directory as it runs; enumerating processes instead would
// be unportable, would need a name match that breaks on the next rename, and would call
// a session "live" while it sat idle for an hour. The transcript is already proven and
// already on disk.
//
// Everything here FAILS CLOSED. An enumeration that errors reports LIVE, and a probe
// with no directories to look at reports LIVE. "I could not tell" must mean "do not
// touch it", never "go ahead": the alternative once let a maintenance pass rewrite an
// index a session was holding in context, and the session wrote its stale copy back.
package live

import (
	"os"
	"path/filepath"
	"strings"
	"time"
)

// DefaultWithin is the quiet window that makes a workspace live. LIB:469.
const DefaultWithin = 30 * time.Minute

// EscalatedWithin is the shorter window used once a store is at or over the sync limit.
// A session between turns is not mid-write, and an index the harness refuses to load is
// not protected by waiting - it is broken for everyone. COMPACT:368.
const EscalatedWithin = 5 * time.Minute

// Workspace reports whether any *.jsonl directly inside one of dirs was written within
// `within` of now.
//
// Top-level files only, never a recursive walk: the transcript sits in the workspace
// directory itself, and recursing would pick up every nested project's own files.
//
// Pass a store's ProbeDirs, not just its canonical directory. A session running under an
// ALIAS path (a junction) writes its transcript into the alias directory, so probing the
// canonical name alone reports "not live" for a workspace that is very much live.
func Workspace(dirs []string, within time.Duration, now time.Time) bool {
	if len(dirs) == 0 {
		return true // nothing to probe is not "nothing is happening"
	}
	if within <= 0 {
		within = DefaultWithin
	}
	if now.IsZero() {
		now = time.Now()
	}
	cutoff := now.Add(-within)
	for _, d := range dirs {
		if d == "" {
			continue
		}
		// Stat before ReadDir so "absent" and "unreadable" stay distinguishable. They
		// do NOT distinguish themselves: on Windows a ReadDir of a regular file
		// reports ERROR_PATH_NOT_FOUND, which os.IsNotExist calls true, so a probe
		// directory that had been replaced by a file would have been silently skipped
		// on one OS and failed closed on the other.
		fi, err := os.Stat(d)
		if err != nil {
			if os.IsNotExist(err) {
				continue // a missing probe directory is skipped, not an error
			}
			return true // fail closed
		}
		if !fi.IsDir() {
			return true // present but not a directory: cannot tell, so do not touch it
		}
		entries, err := os.ReadDir(d)
		if err != nil {
			return true // fail closed
		}
		for _, e := range entries {
			if e.IsDir() || !strings.HasSuffix(e.Name(), ".jsonl") {
				continue
			}
			info, err := e.Info()
			if err != nil {
				return true // fail closed
			}
			if !info.ModTime().Before(cutoff) {
				return true
			}
		}
	}
	return false
}

// AnyClaudeSession reports whether ANY workspace under projectsRoot has a live session.
// This is the watcher's "is a Claude process live on this PC" signal (DESIGN:181-182) -
// the same transcript rule, widened from one workspace to the whole root.
func AnyClaudeSession(projectsRoot string, within time.Duration, now time.Time) bool {
	entries, err := os.ReadDir(projectsRoot)
	if err != nil {
		if os.IsNotExist(err) {
			// No projects root at all means no sessions. This is the one place the
			// fail-closed rule does not apply: the watcher's exit condition is "no
			// session is live", and failing closed there would keep a watcher resident
			// forever on a PC that has never run the harness.
			return false
		}
		return true
	}
	dirs := make([]string, 0, len(entries))
	for _, e := range entries {
		dirs = append(dirs, filepath.Join(projectsRoot, e.Name()))
	}
	if len(dirs) == 0 {
		return false
	}
	return Workspace(dirs, within, now)
}

// SessionStart is the oldest transcript write in a live workspace, clamped to now-24h.
// It is the mtime frontier the materialize guard compares a fact file against: a file a
// live session touched after its session began is never replaced under it.
func SessionStart(dirs []string, within time.Duration, now time.Time) time.Time {
	if now.IsZero() {
		now = time.Now()
	}
	if within <= 0 {
		within = DefaultWithin
	}
	floor := now.Add(-24 * time.Hour)
	oldest := time.Time{}
	cutoff := now.Add(-within)
	for _, d := range dirs {
		entries, err := os.ReadDir(d)
		if err != nil {
			continue
		}
		for _, e := range entries {
			if e.IsDir() || !strings.HasSuffix(e.Name(), ".jsonl") {
				continue
			}
			info, err := e.Info()
			if err != nil {
				continue
			}
			if info.ModTime().Before(cutoff) {
				continue
			}
			if oldest.IsZero() || info.ModTime().Before(oldest) {
				oldest = info.ModTime()
			}
		}
	}
	if oldest.IsZero() || oldest.Before(floor) {
		return floor
	}
	return oldest
}

// Decision is what the starvation guard concluded about one store.
type Decision struct {
	// Live is whether the 30-minute probe saw a session.
	Live bool
	// Override is true when the store is at or over the sync limit and the escalation
	// applies, so work proceeds despite a live session.
	Override bool
	// Why is the override's reason, empty when there is none.
	Why string
	// SkipStreak is what the streak becomes if this run skips.
	SkipStreak int
	// Note is the receipt's human note.
	Note string
}

// Escalate is the port of COMPACT:365-386, the guard that stopped a store starving.
//
// A skip protects against a LOST UPDATE: a live session writes the index back whole from
// its in-context copy, so a maintenance write under it is lost. That is recoverable in
// one night from the snapshot and the dedangle pass. An index the harness REFUSES TO
// LOAD is not recoverable by waiting - every new session loads a partial index until
// someone intervenes. So once the store is at or over the sync limit the guard
// escalates: the quiet window drops from 30 min to 5, and after two consecutive skips
// the run proceeds regardless and says so. Under the limit nothing changes.
//
// bytes is the index's size, skipStreak the streak the receipts report, and liveWithin /
// liveEscalated the two probes already taken (they are passed in rather than probed here
// so the decision stays a pure function and a test can drive the boundary exactly).
func Escalate(bytes int, syncLimit int, skipStreak int, liveWithin, liveEscalated bool) Decision {
	d := Decision{Live: liveWithin}
	if !liveWithin {
		return d
	}
	if bytes >= syncLimit {
		switch {
		case skipStreak >= 2:
			d.Override = true
			d.Why = "already skipped " + plural(skipStreak) + " consecutive run(s)"
		case !liveEscalated:
			d.Override = true
			d.Why = "no transcript write in the last 5 min"
		}
	}
	if d.Override {
		d.Note = "LIVENESS OVERRIDDEN: index " + itoa(bytes) + " B >= the " + itoa(syncLimit) +
			" B sync limit and " + d.Why +
			" - an index the harness refuses to load is worse than a recoverable lost update"
		return d
	}
	d.SkipStreak = skipStreak + 1
	d.Note = "a session in this workspace wrote within 30 min (or could not be probed); " +
		"the harness writes the index whole from an in-context copy"
	if d.SkipStreak >= 2 {
		d.Note += "; skip streak " + itoa(d.SkipStreak)
	}
	return d
}

func plural(n int) string { return itoa(n) }

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [24]byte
	pos := len(buf)
	for n > 0 {
		pos--
		buf[pos] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		pos--
		buf[pos] = '-'
	}
	return string(buf[pos:])
}
