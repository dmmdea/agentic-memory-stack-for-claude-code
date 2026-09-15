package cli

// gateUsage is the --help block for `ams-store gate`, blueprint section 1.1.
const gateUsage = `usage: ams-store gate [--stdin-payload]

The PostToolUse hook. Reads the harness's hook JSON on stdin, and when the write
touched a store's MEMORY.md reports its size against the caps and normalizes an index
that has crossed the sync limit. The advisory block is the product on stdout.

  --stdin-payload        read the hook payload from stdin (the default)

Exit: 0, always. The gate is fail-open by contract: a gate that can fail a tool call is
a gate that can stop the operator working, so every error path is swallowed and the
worst case is silence.`

func gateCommand() command {
	return command{
		Name:    "gate",
		Summary: "the PostToolUse hook; advise, and normalize over the limit",
		Usage:   gateUsage,
		Run:     notImplemented("gate"),
	}
}
