package lock

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// isolate gives each test its own mutex names so two tests - or two test binaries - can
// never contend over the operator's real Local\ams-store. Without this the suite would
// be testing whether the developer's own ams-store happened to be running.
func isolate(t *testing.T, o Options) Options {
	t.Helper()
	o.MutexName = `Local\ams-store-test-` + t.Name()
	o.LegacyMutexName = `Local\ams-store-test-legacy-` + t.Name()
	if o.Path == "" {
		o.Path = filepath.Join(t.TempDir(), FileName)
	}
	return o
}

// TestLock_ContenderSkipsImmediately is the counterpart of MemoryCompact.Tests.ps1:225 -
// "the lock is held, so the contender exits 0 with no receipt".
//
// The Pester original holds the mutex from the test process and asserts the compactor
// wrote no receipt. The Go form asserts the primitive directly AND that it does not
// wait: a lock that queues is a lock that hands the work to a live session.
//
// The contender is driven through the LOCK FILE, not through a second Acquire in this
// process. On Windows a same-process second Acquire is refused by the named mutex at the
// top of Acquire and never reaches the holder-liveness branch at all, so the whole
// file-lock half of the rule had no coverage on the one OS the fleet's PCs run - the
// mutation "never see a holder as live" survived there while it went red off Windows.
// A seeded file naming a genuinely live holder (this process: a real pid with its real
// start time, which is the only holder a test can prove is alive) reaches that branch on
// every OS.
func TestLock_ContenderSkipsImmediately(t *testing.T) {
	opt := isolate(t, Options{Reason: "derive"})

	// The holder is live on two counts, and both matter: the pid is running with the
	// recorded start time, and the lock is far younger than StaleAfter. A dead-pid or an
	// aged fixture would be broken and taken by design, and the test would pass with the
	// liveness check deleted.
	held := Holder{
		PID:           os.Getpid(),
		StartTimeUnix: SelfStartTimeUnix(),
		Host:          "test",
		AcquiredAt:    time.Now().UTC(),
		Reason:        "sync",
	}
	writeHolder(t, opt.Path, held)
	before, err := os.ReadFile(opt.Path)
	if err != nil {
		t.Fatalf("read the seeded lock: %v", err)
	}

	start := time.Now()
	second, err := Acquire(opt)
	elapsed := time.Since(start)
	if err != ErrHeld {
		if second != nil {
			_ = second.Release()
		}
		t.Fatalf("contender got err=%v, want ErrHeld", err)
	}
	if second != nil {
		t.Fatal("contender got a lock while a live holder held it")
	}
	// Refused is not enough: a contender that removed the file and then failed to take
	// it would have destroyed the holder's lock on its way past.
	after, err := os.ReadFile(opt.Path)
	if err != nil {
		t.Fatalf("the live holder's lock file is gone after a refused Acquire: %v", err)
	}
	if string(after) != string(before) {
		t.Fatalf("the contender rewrote a live holder's lock file: before %s, after %s", before, after)
	}
	// The budget is generous on purpose: what must fail here is a retry LOOP, not a
	// slow filesystem. A blocking implementation with any backoff at all overshoots it.
	if elapsed > 500*time.Millisecond {
		t.Fatalf("the contender waited %v; it must skip immediately", elapsed)
	}

	// The same-desktop path, which is what actually refuses a second ams-store on
	// Windows: with the lock genuinely held by this process, the named mutex is the
	// first gate and it is just as immediate.
	if err := os.Remove(opt.Path); err != nil {
		t.Fatal(err)
	}
	mine, err := Acquire(opt)
	if err != nil {
		t.Fatalf("Acquire on a free lock: %v", err)
	}
	defer mine.Release()
	start = time.Now()
	third, err := Acquire(opt)
	if err != ErrHeld {
		if third != nil {
			_ = third.Release()
		}
		t.Fatalf("second in-process Acquire got err=%v, want ErrHeld", err)
	}
	if d := time.Since(start); d > 500*time.Millisecond {
		t.Fatalf("the second in-process contender waited %v; it must skip immediately", d)
	}
}

// TestLock_FreeLockRuns is the counterpart of MemoryCompact.Tests.ps1:238 - "the lock is
// free, so the run proceeds and receipts". Acquire succeeds, the file records this
// process, and after Release the next contender gets in.
func TestLock_FreeLockRuns(t *testing.T) {
	opt := isolate(t, Options{Reason: "sync"})

	l, err := Acquire(opt)
	if err != nil {
		t.Fatalf("Acquire on a free lock: %v", err)
	}
	h, err := ReadHolder(opt.Path)
	if err != nil || h == nil {
		t.Fatalf("ReadHolder after Acquire: %v, %v", h, err)
	}
	if h.PID != os.Getpid() {
		t.Fatalf("lock file records pid %d, want this process %d", h.PID, os.Getpid())
	}
	if h.Reason != "sync" {
		t.Fatalf("lock file records reason %q, want %q", h.Reason, "sync")
	}
	if err := l.Release(); err != nil {
		t.Fatalf("Release: %v", err)
	}
	if _, err := os.Stat(opt.Path); !os.IsNotExist(err) {
		t.Fatalf("the lock file survived Release: %v", err)
	}
	again, err := Acquire(opt)
	if err != nil {
		t.Fatalf("Acquire after Release: %v", err)
	}
	if err := again.Release(); err != nil {
		t.Fatalf("second Release: %v", err)
	}
}

// TestLock_StaleAfterTenMinutes pins the staleness window (DESIGN:189). It is the test
// the mutation "set staleness to 0" must turn red (blueprint section 10.2).
func TestLock_StaleAfterTenMinutes(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, FileName)
	now := time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)

	// A holder that IS alive - this very process - so only the age can free the lock.
	// A dead-pid fixture would pass with the staleness check deleted.
	live := Holder{
		PID:           os.Getpid(),
		StartTimeUnix: SelfStartTimeUnix(),
		Host:          "test",
		Reason:        "derive",
	}

	live.AcquiredAt = now.Add(-9 * time.Minute)
	if !IsLive(live, now, 0) {
		t.Fatal("a live holder 9 minutes in must still own the lock")
	}
	live.AcquiredAt = now.Add(-10 * time.Minute)
	if IsLive(live, now, 0) {
		t.Fatal("a holder at exactly 10 minutes must be stale")
	}

	// End to end: an aged file is broken and taken, and the new holder is recorded.
	live.AcquiredAt = now.Add(-11 * time.Minute)
	writeHolder(t, path, live)
	opt := isolate(t, Options{Path: path, Reason: "sync", Now: now})
	l, err := Acquire(opt)
	if err != nil {
		t.Fatalf("Acquire over a stale lock: %v", err)
	}
	defer l.Release()
	h, err := ReadHolder(path)
	if err != nil || h == nil {
		t.Fatalf("ReadHolder after breaking a stale lock: %v, %v", h, err)
	}
	if !h.AcquiredAt.Equal(now) {
		t.Fatalf("the re-taken lock records acquired_at %v, want %v", h.AcquiredAt, now)
	}
}

// TestLock_DeadHolderIsNotLive is the recycled-PID guard: a lock file naming a live PID
// with the WRONG start time belongs to a process that no longer exists.
func TestLock_DeadHolderIsNotLive(t *testing.T) {
	now := time.Now().UTC()
	self := SelfStartTimeUnix()
	if self == 0 {
		t.Skip("this platform cannot read a process start time; the recycled-PID guard degrades to existence")
	}
	h := Holder{PID: os.Getpid(), StartTimeUnix: self - 3600, AcquiredAt: now, Reason: "derive"}
	if IsLive(h, now, 0) {
		t.Fatal("a holder whose recorded start time does not match the PID's must read as dead")
	}
	h.StartTimeUnix = self
	if !IsLive(h, now, 0) {
		t.Fatal("a holder with the matching start time must read as live")
	}
}

// TestLock_UnreadableLockIsNeverTaken: a lock file that exists but does not parse is
// "I could not tell", which must mean "do not touch it".
func TestLock_UnreadableLockIsNeverTaken(t *testing.T) {
	path := filepath.Join(t.TempDir(), FileName)
	if err := os.WriteFile(path, []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := Acquire(isolate(t, Options{Path: path, Reason: "derive"})); err != ErrHeld {
		t.Fatalf("Acquire over a corrupt lock returned %v, want ErrHeld", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("the corrupt lock file was removed: %v", err)
	}
}

// TestLock_BreakReportsTheHolder: `lock break` is operator-only and prints who it took
// the lock from, so a break is never silent.
func TestLock_BreakReportsTheHolder(t *testing.T) {
	opt := isolate(t, Options{Reason: "gate"})
	l, err := Acquire(opt)
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	h, err := Break(opt.Path)
	if err != nil {
		t.Fatalf("Break: %v", err)
	}
	if h == nil || h.Reason != "gate" {
		t.Fatalf("Break reported holder %+v, want the gate holder", h)
	}
	if _, err := os.Stat(opt.Path); !os.IsNotExist(err) {
		t.Fatal("Break left the lock file behind")
	}
	// Releasing a broken lock must not delete a lock someone else has since taken.
	other := Holder{PID: os.Getpid(), StartTimeUnix: SelfStartTimeUnix(), AcquiredAt: time.Now().UTC(), Reason: "sync"}
	writeHolder(t, opt.Path, other)
	if err := l.Release(); err != nil {
		t.Fatalf("Release after a break: %v", err)
	}
	if _, err := os.Stat(opt.Path); err != nil {
		t.Fatal("Release deleted a lock file this process no longer owned")
	}
}

// TestLock_InspectReportsAgeAndStaleness backs `ams-store lock status`.
func TestLock_InspectReportsAgeAndStaleness(t *testing.T) {
	path := filepath.Join(t.TempDir(), FileName)
	now := time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)

	st, err := Inspect(path, now, 0)
	if err != nil || st.Present {
		t.Fatalf("Inspect on an absent lock: %+v, %v", st, err)
	}
	writeHolder(t, path, Holder{PID: os.Getpid(), StartTimeUnix: SelfStartTimeUnix(), AcquiredAt: now.Add(-20 * time.Minute), Reason: "derive"})
	st, err = Inspect(path, now, 0)
	if err != nil {
		t.Fatalf("Inspect: %v", err)
	}
	if !st.Present || !st.Stale || st.Live {
		t.Fatalf("Inspect on a 20-minute-old lock: %+v", st)
	}
	if st.AgeSecs != 1200 {
		t.Fatalf("age %d s, want 1200", st.AgeSecs)
	}
}

// TestSingleton_SecondInstanceIsRefused is the watcher's singleton primitive: one per
// PC, a second instance exits 0 silently.
func TestSingleton_SecondInstanceIsRefused(t *testing.T) {
	dir := t.TempDir()
	opt := SingletonOptions{
		Name: `Local\ams-store-watch-test-` + t.Name(),
		Path: filepath.Join(dir, WatchFileName),
	}
	first, ok, err := AcquireSingleton(opt)
	if err != nil || !ok {
		t.Fatalf("first AcquireSingleton: ok=%v err=%v", ok, err)
	}
	second, ok, err := AcquireSingleton(opt)
	if err != nil {
		t.Fatalf("second AcquireSingleton errored: %v", err)
	}
	if ok {
		second.Release()
		first.Release()
		t.Fatal("two watchers claimed the singleton at once")
	}
	first.Release()
	third, ok, err := AcquireSingleton(opt)
	if err != nil || !ok {
		t.Fatalf("AcquireSingleton after Release: ok=%v err=%v", ok, err)
	}
	third.Release()
}

func writeHolder(t *testing.T, path string, h Holder) {
	t.Helper()
	b := []byte(`{"pid":` + itoa(h.PID) + `,"start_time_unix":` + itoa64(h.StartTimeUnix) +
		`,"host":"` + h.Host + `","acquired_at":"` + h.AcquiredAt.UTC().Format(time.RFC3339Nano) +
		`","reason":"` + h.Reason + `"}`)
	if err := os.WriteFile(path, b, 0o644); err != nil {
		t.Fatalf("write holder: %v", err)
	}
}

func itoa(i int) string     { return itoa64(int64(i)) }
func itoa64(i int64) string { return formatInt(i) }

func formatInt(i int64) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var buf [24]byte
	pos := len(buf)
	for i > 0 {
		pos--
		buf[pos] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		pos--
		buf[pos] = '-'
	}
	return string(buf[pos:])
}

// TestLock_LiveHolderFileRefusesWithoutTheMutex exercises the FILE half on its own.
//
// On Windows the named mutex refuses a contender before the file is ever read, so a
// mutation that disables the liveness check still passes the contender test there - the
// file lock would be untested on the platform the operator runs. Here the two Acquires
// use different mutex names, so only the PID+start-time file can say no. Off Windows the
// mutex is a no-op and this is simply the same guard tested twice.
func TestLock_LiveHolderFileRefusesWithoutTheMutex(t *testing.T) {
	path := filepath.Join(t.TempDir(), FileName)
	writeHolder(t, path, Holder{
		PID:           os.Getpid(),
		StartTimeUnix: SelfStartTimeUnix(),
		Host:          "test",
		AcquiredAt:    time.Now().UTC(),
		Reason:        "derive",
	})
	opt := Options{
		Path:            path,
		Reason:          "sync",
		MutexName:       `Local\ams-store-test-nomutex-` + t.Name(),
		LegacyMutexName: "-",
	}
	if _, err := Acquire(opt); err != ErrHeld {
		t.Fatalf("Acquire over a LIVE holder returned %v, want ErrHeld", err)
	}
	h, err := ReadHolder(path)
	if err != nil || h == nil || h.Reason != "derive" {
		t.Fatalf("the live holder's file was overwritten: %+v (%v)", h, err)
	}
}
