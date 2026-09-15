// Package cli is the whole command surface of ams-store. cmd/ams-store holds no logic;
// it hands argv to Run and exits with what Run returns.
package cli

import (
	"fmt"
	"io"
	"os"
	"runtime"
	"strings"
)

// Exit codes, blueprint section 1.2. They are a contract: a caller can tell "skipped"
// from "done" from "refused" without parsing text.
const (
	// ExitOK covers per-store skips, deferrals and "nothing to do".
	ExitOK = 0
	// ExitUnconverged means an index is still at or above the sync limit after the floor.
	ExitUnconverged = 1
	// ExitUsage is a bad invocation.
	ExitUsage = 2
	// ExitRefused means the tool declined to run: history unavailable, or a remote whose
	// host is not the hub.
	ExitRefused = 3
	// ExitLocked means another process holds the per-PC lock; a contender skips.
	ExitLocked = 4
	// ExitNetwork means the hub was unreachable or refused. sync only.
	ExitNetwork = 5
	// ExitConflict means a merge conflict was recorded in history. Advisory-loud.
	ExitConflict = 6
	// ExitNotImplemented is the scaffold's placeholder for a verb whose engine is not
	// built yet. It is EX_USAGE from sysexits.h, chosen so no caller can mistake a
	// missing engine for a successful run.
	ExitNotImplemented = 64
)

// Build is the version stamp cmd/ams-store hands in.
type Build struct {
	Version string
	Commit  string
	Built   string
}

// BuildInfo is set by main before Run.
var BuildInfo = Build{Version: "dev", Commit: "none", Built: "unknown"}

// Env is the injectable process environment a verb runs against. Tests drive the CLI
// in-process through RunWith rather than spawning a binary.
type Env struct {
	Stdout io.Writer
	Stderr io.Writer
	Stdin  io.Reader
}

// command is one verb: its name, its one-line summary, its usage block and the function
// that runs it. Every verb in this scaffold is a stub whose run returns
// ExitNotImplemented; the engines land in the tasks that follow.
type command struct {
	Name    string
	Summary string
	Usage   string
	Run     func(env Env, args []string) int
}

// commands returns the verb table in help order.
func commands() []command {
	return []command{
		deriveCommand(),
		lintCommand(),
		gateCommand(),
		syncCommand(),
		lockCommand(),
		judgeApplyCommand(),
		harvestCommand(),
	}
}

// Verbs lists the command surface in help order.
func Verbs() []string {
	cmds := commands()
	out := make([]string, 0, len(cmds))
	for _, c := range cmds {
		out = append(out, c.Name)
	}
	return out
}

// notImplemented is the shared stub body: it reports the verb on stderr and returns
// ExitNotImplemented, leaving stdout empty so no caller can parse a stub as a product.
func notImplemented(name string) func(Env, []string) int {
	return func(env Env, _ []string) int {
		fmt.Fprintf(env.Stderr, "ams-store %s: not implemented yet in this build\n", name)
		return ExitNotImplemented
	}
}

// Usage renders the top-level help text.
func Usage() string {
	var b strings.Builder
	b.WriteString("usage: ams-store <verb> [flags]\n\n")
	b.WriteString("Maintain Claude Code's native per-workspace auto-memory stores.\n\n")
	b.WriteString("Verbs:\n")
	width := 0
	for _, c := range commands() {
		if len(c.Name) > width {
			width = len(c.Name)
		}
	}
	for _, c := range commands() {
		fmt.Fprintf(&b, "  %-*s  %s\n", width, c.Name, c.Summary)
	}
	b.WriteString("\nGlobal flags available on every verb:\n")
	b.WriteString("  --json                 machine output on stdout, nothing else on stdout\n")
	b.WriteString("  --verbose              human log on stderr\n")
	b.WriteString("  --state-root <dir>     override the maintainer state root (test injection)\n")
	b.WriteString("  --projects-root <dir>  override the projects root (test injection)\n")
	b.WriteString("  --now <RFC3339>        deterministic clock (test injection)\n")
	b.WriteString("  --machine-id <s>       override this PC's machine id\n")
	b.WriteString("\nRun `ams-store <verb> --help` for a verb's own flags.\n")
	b.WriteString("Exit codes: 0 normal, 1 unconverged, 2 bad invocation, 3 refused,\n")
	b.WriteString("4 lock held, 5 network, 6 conflict recorded in history.\n")
	return b.String()
}

// VersionLine renders the --version output.
func VersionLine() string {
	return fmt.Sprintf("ams-store %s (%s, %s, %s, %s/%s)",
		BuildInfo.Version, BuildInfo.Commit, BuildInfo.Built,
		runtime.Version(), runtime.GOOS, runtime.GOARCH)
}

// Run dispatches argv (without the program name) and returns the process exit code.
func Run(args []string) int {
	return RunWith(Env{Stdout: os.Stdout, Stderr: os.Stderr, Stdin: os.Stdin}, args)
}

// RunWith is Run against an injected environment, for tests.
func RunWith(env Env, args []string) int {
	if env.Stdout == nil {
		env.Stdout = io.Discard
	}
	if env.Stderr == nil {
		env.Stderr = io.Discard
	}
	if env.Stdin == nil {
		env.Stdin = strings.NewReader("")
	}

	if len(args) == 0 {
		fmt.Fprint(env.Stderr, Usage())
		return ExitUsage
	}

	switch args[0] {
	case "--help", "-h", "help":
		fmt.Fprint(env.Stdout, Usage())
		return ExitOK
	case "--version", "-V", "version":
		fmt.Fprintln(env.Stdout, VersionLine())
		return ExitOK
	}

	for _, c := range commands() {
		if c.Name != args[0] {
			continue
		}
		rest := args[1:]
		if wantsHelp(rest) {
			fmt.Fprintln(env.Stdout, c.Usage)
			return ExitOK
		}
		return c.Run(env, rest)
	}

	fmt.Fprintf(env.Stderr, "ams-store: unknown verb %q\n\n", args[0])
	fmt.Fprint(env.Stderr, Usage())
	return ExitUsage
}

func wantsHelp(args []string) bool {
	for _, a := range args {
		if a == "--help" || a == "-h" {
			return true
		}
	}
	return false
}
