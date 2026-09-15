package cli

// deriveUsage is the --help block for `ams-store derive`, blueprint section 1.1.
const deriveUsage = `usage: ams-store derive [--store <dir>|--all] [--workspace <slug>] [--dry-run] [--json]
                        [--no-harvest] [--stop-below <bytes>] [--projects-root <dir>]

Re-derive MEMORY.md from the fact files of one store or of every store. derive is the
single deterministic writer: it harvests index hooks into frontmatter, runs hygiene,
renders doctrine first and newest next, applies the convergence floor and writes the
index atomically as LF.

  --store <dir>          one store (the memory directory) instead of every store
  --all                  every populated store under the projects root
  --workspace <slug>     restrict to one workspace slug
  --dry-run              report what would change; write nothing
  --json                 one JSON document on stdout, nothing else
  --no-harvest           skip the frontmatter harvest step
  --stop-below <bytes>   floor stop threshold (default: the compactor trigger)
  --projects-root <dir>  override the projects root (test injection)

Exit: 0 normal or skipped, 1 still over the sync limit after the floor, 2 bad
invocation, 3 refused, 4 lock held by another process.`

func deriveCommand() command {
	return command{
		Name:    "derive",
		Summary: "re-derive MEMORY.md from the fact files",
		Usage:   deriveUsage,
		Run:     notImplemented("derive"),
	}
}
