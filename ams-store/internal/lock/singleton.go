package lock

import (
	"fmt"
	"os"
	"path/filepath"
)

// SingletonOptions configures a whole-PC "only one of me" claim.
type SingletonOptions struct {
	// Name is the Windows named mutex. Empty means WatchMutexName.
	Name string
	// Path is the file flocked off Windows. Empty means no file claim, which on Linux
	// means the claim always succeeds - so a caller that can run on Linux must set it.
	Path string
}

// Singleton is a held singleton claim.
type Singleton struct {
	release func()
	held    bool
}

// AcquireSingleton claims the singleton, reporting ok=false when another instance
// already holds it.
//
// The watcher is ONE per PC, not one per session. A second instance exits 0 silently:
// it is not an error for a session to start while the watcher is already running, it is
// the normal case, and anything louder would print on every session start.
//
// Windows uses a named mutex, the same primitive the mem0 hook daemon and the
// PowerShell compactor use. Linux uses flock(LOCK_EX|LOCK_NB) on the lock file, which
// the kernel releases when the process dies - so a crashed watcher never wedges the PC,
// which is exactly what a named mutex gives on Windows too.
func AcquireSingleton(opt SingletonOptions) (*Singleton, bool, error) {
	name := opt.Name
	if name == "" {
		name = WatchMutexName
	}
	if opt.Path != "" {
		if err := os.MkdirAll(filepath.Dir(opt.Path), 0o755); err != nil {
			return nil, false, fmt.Errorf("lock: singleton dir: %w", err)
		}
	}
	rel, ok, err := claimSingleton(name, opt.Path)
	if err != nil {
		return nil, false, err
	}
	if !ok {
		return nil, false, nil
	}
	return &Singleton{release: rel, held: true}, true, nil
}

// Release drops the claim. Idempotent.
func (s *Singleton) Release() {
	if s == nil || !s.held {
		return
	}
	s.held = false
	if s.release != nil {
		s.release()
	}
}
