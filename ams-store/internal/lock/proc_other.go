//go:build !windows && !linux

package lock

import (
	"os"
	"syscall"
)

// ProcessAlive degrades to an existence probe on platforms with neither GetProcessTimes
// nor /proc. ams-store ships for windows/amd64 and linux/amd64; this file exists so a
// developer on another unix can still run the suite, and it fails towards "alive" - a
// lock is better held one window too long than broken under a live holder.
func ProcessAlive(pid int, _ int64) bool {
	if pid <= 0 {
		return false
	}
	p, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return p.Signal(syscall.Signal(0)) == nil
}

// SelfStartTimeUnix is unavailable here; 0 means "existence only".
func SelfStartTimeUnix() int64 { return 0 }

// StartTimeUnix is the start time of an ARBITRARY process, or 0 when it cannot be read.
//
// SelfStartTimeUnix answers it for this process; a lock written on behalf of another
// process - and the tests that stage one - need it for that process. 0 means "existence
// only", which is how ProcessAlive already degrades.
func StartTimeUnix(int) int64 { return 0 }
