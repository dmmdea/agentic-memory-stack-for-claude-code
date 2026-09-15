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
