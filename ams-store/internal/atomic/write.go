// Package atomic is the single door every file write in ams-store goes through.
//
// The shape is the PowerShell original's (LIB:77-96): write <path>.am-tmp beside the
// target, then swap it in. A crash mid-write leaves the original untouched and a reader
// never sees a torn file. When the swap fails, the temp file is removed before the error
// is returned - never leave a full copy of the index behind in a directory the harness
// syncs and agents glob.
package atomic

import (
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// renameAttempts and renameBackoff harden the swap against a transient hold on the
// destination (an antivirus scanner, an editor, the harness itself). The sibling
// harness measured one lost rename in three on a hot path and answered with the same
// retry; the difference here is that ams-store does NOT fall back to an in-place write.
// Losing a progress tick to a torn write is survivable; a torn MEMORY.md is the index
// every session reads.
const (
	renameAttempts = 20
	renameBackoff  = 5 * time.Millisecond
)

// Write writes text as UTF-8 without a BOM through a temp file beside the target, then
// verifies the bytes on disk by SHA-256 before returning.
//
// On Windows the swap is MoveFileEx(MOVEFILE_REPLACE_EXISTING): it replaces an existing
// destination, but - per os.Rename's own documentation - "even within the same directory,
// on non-Unix platforms Rename is not an atomic operation". A reader can therefore
// observe the destination missing for an instant on Windows, which is why the temp file
// always lives in the destination directory (no cross-volume move) and why the readback
// exists.
func Write(path, text string) error { return WriteBytes(path, []byte(text)) }

// WriteBytes is Write for bytes.
func WriteBytes(path string, data []byte) error {
	tmp := path + store.TempSuffix
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return fmt.Errorf("write temp %s: %w", tmp, err)
	}
	if err := swap(tmp, path); err != nil {
		// Best effort: the error being returned is the one that matters, but a leftover
		// temp file in a synced, globbed store is its own incident.
		_ = os.Remove(tmp)
		return err
	}
	want := Hash(data)
	got, err := FileHash(path)
	if err != nil {
		return fmt.Errorf("readback %s: %w", path, err)
	}
	if got != want {
		return fmt.Errorf("readback of %s does not match what was written: sha256 %s, wrote %s", path, got, want)
	}
	return nil
}

func swap(tmp, path string) error {
	var last error
	for i := 0; i < renameAttempts; i++ {
		if err := os.Rename(tmp, path); err == nil {
			return nil
		} else {
			last = err
		}
		// A destination that is a directory will never become renameable; stop early
		// rather than spend a hundred milliseconds proving it.
		if fi, statErr := os.Lstat(path); statErr == nil && fi.IsDir() {
			break
		}
		time.Sleep(renameBackoff)
	}
	if last == nil {
		last = errors.New("rename failed for an unrecorded reason")
	}
	return fmt.Errorf("swap %s into %s: %w", tmp, path, last)
}
