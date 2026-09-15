package cli

// lockUsage is the --help block for `ams-store lock`, blueprint section 1.1.
const lockUsage = `usage: ams-store lock status
       ams-store lock acquire --for <dur> --reason <s>
       ams-store lock release
       ams-store lock break

Inspect and hold the per-PC cross-process lock (PID + process start time, stale after
10 minutes). A contender skips immediately rather than queueing - maintenance that
waits behind maintenance is maintenance that runs under a live session.

  status                 print the holder, its reason and its age
  acquire                take the lock for a bounded window
  release                release a lock this process holds
  break                  force-release a lock (stale holders only)

Exit: 0 acquired or released, 2 bad invocation, 4 held by another process.`

func lockCommand() command {
	return command{
		Name:    "lock",
		Summary: "inspect and hold the per-PC cross-process lock",
		Usage:   lockUsage,
		Run:     notImplemented("lock"),
	}
}
