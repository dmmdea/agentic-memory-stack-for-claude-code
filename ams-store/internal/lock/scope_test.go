package lock

import (
	"errors"
	"path/filepath"
	"runtime"
	"testing"
)

// The production mutex names are scoped to the state root (2026-09-23). Before, the Windows
// named mutexes Local\ams-store and Local\ams-memory-compact were global to the logon session
// whatever the state root, so a test run or a scratch HOME held the LIVE store's lock: the real
// `ams-store sync --once` exited 4 ("the per-PC lock is held") while a Pester run's sandbox
// compactor held the legacy mutex, and install/3-verify.ps1 failed on it.
//
// These tests use the PRODUCTION names on purpose (no isolate()): with the scoping in place a
// t.TempDir() root can never collide with the operator's store, and without it the first test
// below goes red.

func prodOptions(root string) Options {
	return Options{Path: filepath.Join(root, FileName), Reason: "test"}
}

func TestScopedLock_TwoStateRootsHoldAtOnce(t *testing.T) {
	a, err := Acquire(prodOptions(t.TempDir()))
	if err != nil {
		t.Fatalf("first root: %v", err)
	}
	defer a.Release()
	b, err := Acquire(prodOptions(t.TempDir()))
	if err != nil {
		t.Fatalf("a second state root must not contend with the first (the mutex names are per root): %v", err)
	}
	defer b.Release()
}

func TestScopedLock_SameStateRootStillExcludes(t *testing.T) {
	root := t.TempDir()
	a, err := Acquire(prodOptions(root))
	if err != nil {
		t.Fatalf("first: %v", err)
	}
	defer a.Release()
	// A second holder of the same store is refused - by the mutex on Windows, and by the
	// PID + start-time file everywhere (this process is live, so its own file is not stale).
	if _, err := Acquire(prodOptions(root)); !errors.Is(err, ErrHeld) {
		t.Fatalf("same root: want ErrHeld, got %v", err)
	}
}

func TestScopedLock_SameStateRootExcludesByMutexAlone(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("named mutexes exist on Windows only")
	}
	root := t.TempDir()
	h, ok, err := acquireMutex(ScopedName(MutexName, root))
	if err != nil || !ok {
		t.Fatalf("pre-hold the scoped primary mutex: ok=%v err=%v", ok, err)
	}
	defer releaseMutex(h)
	if _, err := Acquire(prodOptions(root)); !errors.Is(err, ErrHeld) {
		t.Fatalf("the scoped mutex alone must exclude a second holder of the same root, got %v", err)
	}
}

func TestScopedLock_LegacyMutexIsScopedTheWayTheCompactorNamesIt(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("named mutexes exist on Windows only")
	}
	// memory-compact.ps1 (GUARD 0) takes Get-AmStoreMutexName 'Local\ams-memory-compact' over its
	// state root. A compactor holding it must still stop a Go pass on the SAME root (decision Q9)
	// and must not stop one on another root.
	root := t.TempDir()
	h, ok, err := acquireMutex(ScopedName(LegacyMutexName, root))
	if err != nil || !ok {
		t.Fatalf("pre-hold the scoped legacy mutex: ok=%v err=%v", ok, err)
	}
	defer releaseMutex(h)
	if _, err := Acquire(prodOptions(root)); !errors.Is(err, ErrHeld) {
		t.Fatalf("a compactor on the same root must exclude the Go pass, got %v", err)
	}
	other, err := Acquire(prodOptions(t.TempDir()))
	if err != nil {
		t.Fatalf("a compactor on another root must not exclude the Go pass: %v", err)
	}
	other.Release()
}

func TestScopedName_GoldenVectorSharedWithPowerShell(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("the canonical form (backslashes, lower case) is the Windows one")
	}
	// MemoryStoreLib.Tests.ps1 pins the SAME vector for Get-AmStoreMutexName: both sides must
	// derive byte-identical names or the compactor and the binary stop excluding each other.
	want := `Local\ams-memory-compact-f870ee63903ab820`
	for _, root := range []string{
		`D:\Stores\Example\state\automemory`,
		`d:/stores/EXAMPLE/state/automemory/`,
		`D:\Stores\Example\state\automemory\`,
	} {
		if got := ScopedName(LegacyMutexName, root); got != want {
			t.Errorf("ScopedName(%q) = %q, want %q", root, got, want)
		}
	}
}

func TestScopedSingleton_TwoStateRootsBothRun(t *testing.T) {
	a, ok, err := AcquireSingleton(SingletonOptions{Path: filepath.Join(t.TempDir(), WatchFileName)})
	if err != nil || !ok {
		t.Fatalf("first watcher: ok=%v err=%v", ok, err)
	}
	defer a.Release()
	b, ok, err := AcquireSingleton(SingletonOptions{Path: filepath.Join(t.TempDir(), WatchFileName)})
	if err != nil || !ok {
		t.Fatalf("a watcher on another state root must start: ok=%v err=%v", ok, err)
	}
	defer b.Release()
	root := t.TempDir()
	c, ok, err := AcquireSingleton(SingletonOptions{Path: filepath.Join(root, WatchFileName)})
	if err != nil || !ok {
		t.Fatalf("third watcher: ok=%v err=%v", ok, err)
	}
	defer c.Release()
	if _, ok, _ := AcquireSingleton(SingletonOptions{Path: filepath.Join(root, WatchFileName)}); ok {
		t.Fatal("a second watcher on the SAME state root must not start")
	}
}
