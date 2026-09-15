package cli

// harvestUsage is the --help block for `ams-store harvest`, blueprint section 1.1. The
// step is part of derive; the explicit verb exists because harvest must run on every PC
// before that PC's first push, so `hook:`-only differences never reach the merge.
const harvestUsage = `usage: ams-store harvest --store <dir>

Copy each index entry's hook text into its fact file's frontmatter as hook:. Idempotent:
a file that already carries hook: is never rewritten, and a file with no frontmatter
block is left alone.

  --store <dir>          the store (memory directory) to harvest

Exit: 0 normal, 2 bad invocation, 4 lock held.`

func harvestCommand() command {
	return command{
		Name:    "harvest",
		Summary: "copy index hook text into fact-file frontmatter",
		Usage:   harvestUsage,
		Run:     notImplemented("harvest"),
		Stub:    true,
	}
}
