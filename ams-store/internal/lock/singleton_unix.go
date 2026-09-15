//go:build !windows

package lock

import (
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

// claimSingleton flocks the watch lock file. The kernel drops the lock when the process
// dies, so a crashed watcher leaves the file behind but never the claim - the file's
// presence is not the claim, the flock is.
func claimSingleton(_ string, path string) (func(), bool, error) {
	if path == "" {
		// No file to claim: the caller gets the singleton unconditionally. Every
		// ams-store caller passes a path; this branch exists so a test helper cannot
		// deadlock itself on a missing field.
		return func() {}, true, nil
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, false, fmt.Errorf("lock: open singleton file %s: %w", path, err)
	}
	if err := unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		f.Close()
		if err == unix.EWOULDBLOCK || err == unix.EAGAIN {
			return nil, false, nil
		}
		return nil, false, fmt.Errorf("lock: flock %s: %w", path, err)
	}
	return func() {
		_ = unix.Flock(int(f.Fd()), unix.LOCK_UN)
		f.Close()
	}, true, nil
}
