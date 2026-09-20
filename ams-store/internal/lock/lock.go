// Package lock is the per-PC cross-process lock that covers derive and sync, plus the
// singleton primitive the watcher holds.
//
// Two facts shape it.
//
// A PID alone is not identity. The PowerShell compactor's guard was a session-local
// named mutex with no staleness window, so a process that died holding it left nothing
// behind, and a recycled PID in a stale file looks exactly like a live holder. The lock
// file therefore records the holder's PID *and* that process's start time, and a holder
// whose start time does not match the PID's real start time is dead, whatever the file
// says.
//
// A contender skips, it never waits. Maintenance that queues behind maintenance is
// maintenance that runs under a live session: by the time the lock frees, the state the
// contender read is stale. There is no retry, no backoff and no timeout parameter -
// Acquire either takes the lock now or reports ErrHeld, and the caller exits 4.
package lock

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// StaleAfter is how long a lock may be held before a contender may break it. A holder
// that has been in the lock for longer than this either crashed without releasing or is
// wedged; either way the fleet cannot stop maintaining a store because one process
// stopped answering. DESIGN:189.
const StaleAfter = 10 * time.Minute

// FileName is the lock file's name inside STATE_ROOT.
const FileName = "ams-store.lock"

// MutexName is the Windows named mutex ams-store itself holds alongside the file lock.
// COMPACT:60-77 is the precedent: four concurrent instances of the PowerShell compactor
// produced 243 receipts in nine hours before the mutex existed.
const MutexName = `Local\ams-store`

// LegacyMutexName is the PowerShell compactor's own mutex. Decision Q9: while both
// implementations exist (the PS originals are not deleted until Phase 5), ams-store
// takes this one too, so a Go derive and a PowerShell compaction cannot run at once.
// It is taken AFTER MutexName and released before it, so two processes taking both can
// never deadlock on each other.
const LegacyMutexName = `Local\ams-memory-compact`

// WatchMutexName is the watcher's singleton mutex. DESIGN:178.
const WatchMutexName = `Local\ams-store-watch`

// WatchFileName is the watcher's singleton lock file, flocked on Linux.
const WatchFileName = "watch.lock"

// BreakGuardStale bounds how long a break guard (see breakDead) may sit on disk before a
// contender treats it as abandoned by a breaker that died mid-break.
const BreakGuardStale = time.Minute

// testBeforeBreak, when a test sets it, runs after a dead holder has been read and before
// its file is broken - the window two contenders race in. Nil in production.
var testBeforeBreak func()

// ErrHeld is returned by Acquire when another live process holds the lock. It is not a
// failure: the caller reports exit 4 and does nothing else.
var ErrHeld = errors.New("the per-PC lock is held by another process")

// Holder is the lock file's content: who holds it, since when, and why.
type Holder struct {
	PID int `json:"pid"`
	// StartTimeUnix is the holder PROCESS's start time, not the lock's. A recycled PID
	// carries a different start time, so a dead holder can never masquerade as a live
	// one.
	StartTimeUnix int64     `json:"start_time_unix"`
	Host          string    `json:"host"`
	AcquiredAt    time.Time `json:"acquired_at"`
	Reason        string    `json:"reason"`
}

// Options configures one lock. The names are fields rather than constants at the call
// site so a test can isolate itself from the production mutex - two test binaries
// running at once must not contend over the operator's real lock name.
type Options struct {
	// Path is the lock file. Required.
	Path string
	// Reason is recorded in the file: "gate", "derive", "sync".
	Reason string
	// Now is the clock. Zero means time.Now().
	Now time.Time
	// MutexName overrides MutexName. Empty means the production name.
	MutexName string
	// LegacyMutexName overrides LegacyMutexName. Empty means the production name; "-"
	// disables the legacy mutex entirely (used after the Phase 5 deletion).
	LegacyMutexName string
	// StaleAfter overrides StaleAfter. Zero means the production window.
	StaleAfter time.Duration
	// Host overrides the recorded hostname.
	Host string
}

// Lock is a held lock. Release is idempotent.
type Lock struct {
	path     string
	holder   Holder
	mutexes  []mutexHandle
	released bool
}

// Holder reports who this lock records as its holder.
func (l *Lock) Holder() Holder { return l.holder }

// Path is the lock file's path.
func (l *Lock) Path() string { return l.path }

// Acquire takes the lock, or returns ErrHeld immediately.
//
// Order is load-bearing: the named mutexes first (a same-desktop second process is
// refused before any file is touched), then the O_EXCL create. A stale or dead holder's
// file is removed and the create retried exactly once - "retry once after breaking a
// dead lock" is not waiting, it is the same attempt against a lock that turned out not
// to exist.
func Acquire(opt Options) (*Lock, error) {
	if opt.Path == "" {
		return nil, errors.New("lock: Path is required")
	}
	now := opt.Now
	if now.IsZero() {
		now = time.Now()
	}
	stale := opt.StaleAfter
	if stale <= 0 {
		stale = StaleAfter
	}
	host := opt.Host
	if host == "" {
		host, _ = os.Hostname()
	}

	if err := os.MkdirAll(filepath.Dir(opt.Path), 0o755); err != nil {
		return nil, fmt.Errorf("lock: create state dir: %w", err)
	}

	names := mutexNames(opt)
	var taken []mutexHandle
	for _, n := range names {
		h, ok, err := acquireMutex(n)
		if err != nil {
			releaseMutexes(taken)
			return nil, fmt.Errorf("lock: named mutex %s: %w", n, err)
		}
		if !ok {
			releaseMutexes(taken)
			return nil, ErrHeld
		}
		taken = append(taken, h)
	}

	self := Holder{
		PID:           os.Getpid(),
		StartTimeUnix: SelfStartTimeUnix(),
		Host:          host,
		AcquiredAt:    now.UTC(),
		Reason:        opt.Reason,
	}

	for attempt := 0; attempt < 2; attempt++ {
		err := writeExclusive(opt.Path, self)
		if err == nil {
			return &Lock{path: opt.Path, holder: self, mutexes: taken}, nil
		}
		if !errors.Is(err, os.ErrExist) {
			releaseMutexes(taken)
			return nil, fmt.Errorf("lock: create %s: %w", opt.Path, err)
		}
		if attempt == 1 {
			break
		}
		existing, readErr := ReadHolder(opt.Path)
		if readErr != nil {
			// A lock file that exists but cannot be read is not a licence to proceed.
			// "I could not tell" means "do not touch it".
			releaseMutexes(taken)
			return nil, ErrHeld
		}
		if existing == nil {
			continue // it vanished between create and read; try once more
		}
		if IsLive(*existing, now, stale) {
			releaseMutexes(taken)
			return nil, ErrHeld
		}
		if testBeforeBreak != nil {
			testBeforeBreak()
		}
		if err := breakDead(opt.Path, *existing, now); err != nil {
			releaseMutexes(taken)
			return nil, ErrHeld
		}
	}
	releaseMutexes(taken)
	return nil, ErrHeld
}

// breakDead removes a dead or stale holder's file so the caller's next O_EXCL create can
// win it - under a guard, because the bare remove it replaced was a race two contenders
// could both win: each read the same dead holder, the first removed it and created its
// own lock, and the second's remove then deleted THAT fresh lock and created another. Two
// processes then held "the" per-PC lock (P5-10, 2026-09-19).
//
// The guard is a sibling file taken with O_EXCL: only its creator may break, everyone
// else yields with ErrHeld and comes back on its next wake. Under the guard the holder is
// read AGAIN and must still be the dead one that was read before - a fresh live holder
// that appeared in between is left alone. A guard older than BreakGuardStale belongs to
// a breaker that died mid-break; it is cleared and this contender still yields, so the
// next one gets a clean attempt. Whoever breaks does not automatically win: the create
// after this is O_EXCL, and a loser there yields too.
func breakDead(path string, dead Holder, now time.Time) error {
	guard := path + ".breaking"
	f, err := os.OpenFile(guard, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			if st, sErr := os.Stat(guard); sErr == nil && now.Sub(st.ModTime()) > BreakGuardStale {
				_ = os.Remove(guard)
			}
		}
		return ErrHeld
	}
	_ = f.Close()
	defer os.Remove(guard)
	cur, rErr := ReadHolder(path)
	if rErr != nil {
		return ErrHeld
	}
	if cur != nil && (cur.PID != dead.PID || !cur.AcquiredAt.Equal(dead.AcquiredAt)) {
		return ErrHeld // someone re-took it while this contender was deciding
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return ErrHeld
	}
	return nil
}

// Release drops the lock. It removes the file only when the file still records THIS
// process as the holder: a lock that was broken as stale and re-taken by someone else
// belongs to them now, and deleting it would hand a third process the lock while the
// second is still working.
func (l *Lock) Release() error {
	if l == nil || l.released {
		return nil
	}
	l.released = true
	defer releaseMutexes(l.mutexes)
	cur, err := ReadHolder(l.path)
	if err == nil && cur != nil && (cur.PID != l.holder.PID || !cur.AcquiredAt.Equal(l.holder.AcquiredAt)) {
		return nil
	}
	if err := os.Remove(l.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("lock: release %s: %w", l.path, err)
	}
	return nil
}

// ReadHolder reads the lock file. It returns (nil, nil) when the file is absent, and an
// error when the file exists but does not parse - absent and corrupt must not collapse.
func ReadHolder(path string) (*Holder, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("lock: read %s: %w", path, err)
	}
	var h Holder
	if err := json.Unmarshal(b, &h); err != nil {
		return nil, fmt.Errorf("lock: parse %s: %w", path, err)
	}
	return &h, nil
}

// IsLive reports whether a holder still owns the lock: the window has not expired AND
// the recorded process is genuinely running with the recorded start time.
func IsLive(h Holder, now time.Time, stale time.Duration) bool {
	if stale <= 0 {
		stale = StaleAfter
	}
	if now.Sub(h.AcquiredAt.UTC()) >= stale {
		return false
	}
	return ProcessAlive(h.PID, h.StartTimeUnix)
}

// Status describes the lock file for `ams-store lock status`.
type Status struct {
	Present bool      `json:"present"`
	Stale   bool      `json:"stale"`
	Live    bool      `json:"live"`
	AgeSecs int64     `json:"age_seconds"`
	Holder  *Holder   `json:"holder,omitempty"`
	Now     time.Time `json:"-"`
}

// Inspect reads the lock file and classifies it without taking it.
func Inspect(path string, now time.Time, stale time.Duration) (Status, error) {
	if now.IsZero() {
		now = time.Now()
	}
	if stale <= 0 {
		stale = StaleAfter
	}
	h, err := ReadHolder(path)
	if err != nil {
		return Status{Now: now}, err
	}
	if h == nil {
		return Status{Present: false, Now: now}, nil
	}
	age := now.Sub(h.AcquiredAt.UTC())
	return Status{
		Present: true,
		Stale:   age >= stale,
		Live:    IsLive(*h, now, stale),
		AgeSecs: int64(age / time.Second),
		Holder:  h,
		Now:     now,
	}, nil
}

// Break removes the lock file whatever it says, and reports who it took it from. It is
// operator-only: the CLI prints the holder before breaking so a break is never silent.
func Break(path string) (*Holder, error) {
	h, err := ReadHolder(path)
	if err != nil {
		// A corrupt lock file is exactly what break exists for, so remove it anyway.
		h = nil
	}
	if rmErr := os.Remove(path); rmErr != nil && !errors.Is(rmErr, os.ErrNotExist) {
		return h, fmt.Errorf("lock: break %s: %w", path, rmErr)
	}
	return h, nil
}

func writeExclusive(path string, h Holder) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	b, mErr := json.Marshal(h)
	if mErr != nil {
		f.Close()
		os.Remove(path)
		return mErr
	}
	if _, err := f.Write(append(b, '\n')); err != nil {
		f.Close()
		os.Remove(path)
		return err
	}
	// fsync: a lock whose bytes are still in the page cache when the box loses power is a
	// lock nobody can attribute after the reboot.
	if err := f.Sync(); err != nil {
		f.Close()
		os.Remove(path)
		return err
	}
	return f.Close()
}

func mutexNames(opt Options) []string {
	primary := opt.MutexName
	if primary == "" {
		primary = MutexName
	}
	legacy := opt.LegacyMutexName
	if legacy == "" {
		legacy = LegacyMutexName
	}
	if legacy == "-" {
		return []string{primary}
	}
	return []string{primary, legacy}
}

func releaseMutexes(hs []mutexHandle) {
	// Reverse order: taken primary-then-legacy, released legacy-then-primary.
	for i := len(hs) - 1; i >= 0; i-- {
		releaseMutex(hs[i])
	}
}
