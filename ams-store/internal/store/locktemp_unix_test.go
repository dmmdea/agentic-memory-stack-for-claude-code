//go:build !windows

package store_test

import (
	"os"
	"path/filepath"
	"testing"
)

// lockTempFile makes path undeletable by taking write permission off its directory.
// Unix has no mandatory file locking to borrow, and root ignores the mode bits, so this
// reports false when it cannot guarantee the failure.
func lockTempFile(t *testing.T, path string) (func(), bool) {
	t.Helper()
	if os.Geteuid() == 0 {
		return nil, false
	}
	if err := os.WriteFile(path, []byte("leftover"), 0o644); err != nil {
		return nil, false
	}
	dir := filepath.Dir(path)
	if err := os.Chmod(dir, 0o500); err != nil {
		return nil, false
	}
	return func() { _ = os.Chmod(dir, 0o755) }, true
}
