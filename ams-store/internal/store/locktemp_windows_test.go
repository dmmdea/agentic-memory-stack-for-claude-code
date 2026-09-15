//go:build windows

package store_test

import (
	"syscall"
	"testing"
)

// lockTempFile creates path and holds it open with no sharing at all, which is what
// makes os.Remove fail with a sharing violation. This is the Go equivalent of the
// Pester fixture's [System.IO.File]::Open(..., FileShare.None): Go's own os.Open asks
// for FILE_SHARE_DELETE, so it would NOT reproduce the case.
func lockTempFile(t *testing.T, path string) (func(), bool) {
	t.Helper()
	p, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return nil, false
	}
	h, err := syscall.CreateFile(
		p,
		syscall.GENERIC_READ|syscall.GENERIC_WRITE,
		0, // dwShareMode: no sharing
		nil,
		syscall.CREATE_ALWAYS,
		syscall.FILE_ATTRIBUTE_NORMAL,
		0,
	)
	if err != nil {
		return nil, false
	}
	return func() { syscall.CloseHandle(h) }, true
}
