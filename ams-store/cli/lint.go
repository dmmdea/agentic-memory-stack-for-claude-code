package cli

// lintUsage is the --help block for `ams-store lint`, blueprint section 1.1.
const lintUsage = `usage: ams-store lint [--all] [--workspace <slug>] [--json] [--quiet]
                      [--summary-out <path>]

Report on every store without touching one: orphan, dangling, dup-slug, long-line,
oversized-file, no-frontmatter, near-budget, over-sync-limit, over-inject-limit, plus
the stateful findings read from the receipts.

  --all                  every populated store under the projects root
  --workspace <slug>     restrict to one workspace slug
  --json                 one JSON document on stdout, nothing else
  --quiet                suppress the human summary on stderr
  --summary-out <path>   also write the machine summary to this path

Exit: 0 always for findings; 2 bad invocation. lint never mutates a store.`

func lintCommand() command {
	return command{
		Name:    "lint",
		Summary: "report on every store without touching one",
		Usage:   lintUsage,
		Run:     notImplemented("lint"),
	}
}
