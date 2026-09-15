//go:build windows

package lock

import (
	"time"

	"golang.org/x/sys/windows"
)

// ProcessAlive reports whether pid is running AND was started at startUnix.
//
// The start time is the whole point. A PID is recycled within minutes on a busy box, so
// "a process with this PID exists" is not "the holder is alive" - it is how a dead
// holder's lock survives a reboot and blocks maintenance forever. A startUnix of 0 means
// the writer could not read its own start time; the check then degrades to existence
// only, which is still better than assuming death.
func ProcessAlive(pid int, startUnix int64) bool {
	if pid <= 0 {
		return false
	}
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(h)
	if startUnix == 0 {
		return true
	}
	got, err := processStartUnix(h)
	if err != nil {
		return true
	}
	return got == startUnix
}

// SelfStartTimeUnix is this process's start time in unix seconds, or 0 when it cannot be
// read.
func SelfStartTimeUnix() int64 {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(windows.GetCurrentProcessId()))
	if err != nil {
		return 0
	}
	defer windows.CloseHandle(h)
	v, err := processStartUnix(h)
	if err != nil {
		return 0
	}
	return v
}

func processStartUnix(h windows.Handle) (int64, error) {
	var creation, exit, kernel, user windows.Filetime
	if err := windows.GetProcessTimes(h, &creation, &exit, &kernel, &user); err != nil {
		return 0, err
	}
	return time.Unix(0, creation.Nanoseconds()).Unix(), nil
}

// StartTimeUnix is the start time of an ARBITRARY process, or 0 when it cannot be read.
//
// SelfStartTimeUnix answers it for this process; a lock written on behalf of another
// process - and the tests that stage one - need it for that process. 0 means "existence
// only", which is how ProcessAlive already degrades.
func StartTimeUnix(pid int) int64 {
	if pid <= 0 {
		return 0
	}
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return 0
	}
	defer windows.CloseHandle(h)
	v, err := processStartUnix(h)
	if err != nil {
		return 0
	}
	return v
}
