package cli

// syncUsage is the --help block for `ams-store sync`, blueprint section 1.1.
const syncUsage = `usage: ams-store sync [--once] [--watch] [--timeout <dur>] [--remote <name>] [--json]

Commit every store locally, then fetch, merge and push against the hub. --watch is the
singleton watcher: one per PC, holding watch.lock, waking on the dirty marker.

  --once                 one pass, then exit
  --watch                stay resident and sync on every dirty marker
  --timeout <dur>        bound the network half (default 30s)
  --remote <name>        the hub remote (default origin)
  --json                 one JSON document on stdout, nothing else

Exit: 0 normal, 2 bad invocation, 3 refused (no history repo, or a remote that is not
the hub), 4 lock held, 5 hub unreachable, 6 a conflict was recorded in history.`

func syncCommand() command {
	return command{
		Name:    "sync",
		Summary: "commit, fetch, merge and push against the hub",
		Usage:   syncUsage,
		Run:     notImplemented("sync"),
	}
}
