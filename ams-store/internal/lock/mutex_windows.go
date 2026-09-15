//go:build windows

package lock

import (
	"errors"

	"golang.org/x/sys/windows"
)

// mutexHandle is a live Windows named-mutex handle.
type mutexHandle = windows.Handle

// acquireMutex opens the named mutex and reports whether THIS process is the first to
// hold it open.
//
// Ownership is deliberately not requested. A Win32 mutex is owned by a THREAD, and Go
// moves goroutines between threads, so an owned mutex could be released by the runtime
// out from under us. What the singleton check actually needs is the kernel object's
// existence: CreateMutexW returns ERROR_ALREADY_EXISTS when a handle to that name is
// already open somewhere on the desktop, which is precisely "another instance is
// running". The handle is then held for the lock's lifetime and closed on release.
func acquireMutex(name string) (mutexHandle, bool, error) {
	p, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return 0, false, err
	}
	h, err := windows.CreateMutex(nil, false, p)
	if errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
		// The handle IS valid on ERROR_ALREADY_EXISTS; close it or the name is pinned
		// open by the very process that just declined to take it.
		if h != 0 {
			windows.CloseHandle(h)
		}
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return h, true, nil
}

func releaseMutex(h mutexHandle) {
	if h != 0 {
		windows.CloseHandle(h)
	}
}
