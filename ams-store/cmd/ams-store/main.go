// Command ams-store maintains Claude Code's native per-workspace auto-memory stores.
//
// This file is deliberately thin: it carries the ldflags version stamp into the cli
// package and exits with what cli.Run returns. No logic lives here.
package main

import (
	"os"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/cli"
)

// Set at link time:
//
//	-ldflags "-s -w -X main.version=... -X main.commit=... -X main.built=..."
var (
	version = "dev"
	commit  = "none"
	built   = "unknown"
)

func main() {
	cli.BuildInfo = cli.Build{Version: version, Commit: commit, Built: built}
	os.Exit(cli.Run(os.Args[1:]))
}
