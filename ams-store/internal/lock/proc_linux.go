//go:build linux

package lock

import (
	"os"
	"strconv"
	"strings"
)

// ProcessAlive reports whether pid is running AND was started at startUnix, read from
// /proc/<pid>/stat field 22 (starttime, in clock ticks since boot) offset by
// /proc/stat's btime.
//
// See the Windows file for why the start time, and not the PID alone, is the identity.
func ProcessAlive(pid int, startUnix int64) bool {
	if pid <= 0 {
		return false
	}
	if _, err := os.Stat("/proc/" + strconv.Itoa(pid)); err != nil {
		return false
	}
	if startUnix == 0 {
		return true
	}
	got, ok := procStartUnix(pid)
	if !ok {
		return true
	}
	// One second of slack: starttime is in clock ticks and btime in whole seconds, so
	// the derived value can differ by a tick's worth of rounding between two readers.
	d := got - startUnix
	return d >= -1 && d <= 1
}

// SelfStartTimeUnix is this process's start time in unix seconds, or 0 when unreadable.
func SelfStartTimeUnix() int64 {
	v, ok := procStartUnix(os.Getpid())
	if !ok {
		return 0
	}
	return v
}

// clockTicks is the kernel's USER_HZ. It is 100 on every Linux ams-store targets;
// sysconf(_SC_CLK_TCK) needs cgo, and this file must build with CGO_ENABLED=0.
const clockTicks = 100

func procStartUnix(pid int) (int64, bool) {
	b, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return 0, false
	}
	s := string(b)
	// The comm field is parenthesised and may itself contain spaces and parentheses, so
	// the fields are counted from the LAST ')' - splitting the whole line on spaces
	// misreads every process whose name has a space in it.
	close := strings.LastIndex(s, ")")
	if close < 0 || close+2 >= len(s) {
		return 0, false
	}
	fields := strings.Fields(s[close+2:])
	// After comm, field 3 is state; starttime is field 22 overall, i.e. index 19 here.
	const startIdx = 19
	if len(fields) <= startIdx {
		return 0, false
	}
	ticks, err := strconv.ParseInt(fields[startIdx], 10, 64)
	if err != nil {
		return 0, false
	}
	btime, ok := bootTimeUnix()
	if !ok {
		return 0, false
	}
	return btime + ticks/clockTicks, true
}

func bootTimeUnix() (int64, bool) {
	b, err := os.ReadFile("/proc/stat")
	if err != nil {
		return 0, false
	}
	for _, line := range strings.Split(string(b), "\n") {
		if !strings.HasPrefix(line, "btime ") {
			continue
		}
		v, err := strconv.ParseInt(strings.TrimSpace(strings.TrimPrefix(line, "btime ")), 10, 64)
		if err != nil {
			return 0, false
		}
		return v, true
	}
	return 0, false
}
