//go:build !windows

package lock

// mutexHandle is a no-op off Windows: there is no desktop-scoped named mutex, and the
// PID+start-time file plus flock cover the same ground. The type is kept so the shared
// code path does not need a build tag of its own.
type mutexHandle = struct{}

// acquireMutex always succeeds off Windows. The legacy Local\ams-memory-compact mutex
// (decision Q9) has no Linux counterpart to contend with: the PowerShell compactor it
// belongs to has never run there.
func acquireMutex(string) (mutexHandle, bool, error) { return mutexHandle{}, true, nil }

func releaseMutex(mutexHandle) {}
