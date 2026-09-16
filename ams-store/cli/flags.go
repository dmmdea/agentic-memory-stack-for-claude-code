package cli

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// globalOpts are the flags every verb accepts (blueprint section 1.1's global block).
//
// The three injection flags are not a testing convenience bolted on afterwards: the
// binary runs as a hook, as a timer job and by hand, and each of those resolves its
// roots differently. Making the roots a parameter rather than a global lookup is what
// lets a test drive the real command surface instead of a reimplementation of it.
type globalOpts struct {
	stateRoot    string
	projectsRoot string
	machineID    string
	nowRaw       string
	json         bool
	verbose      bool
}

func (g *globalOpts) bind(fs *flag.FlagSet) {
	fs.StringVar(&g.stateRoot, "state-root", "", "override the maintainer state root")
	fs.StringVar(&g.projectsRoot, "projects-root", "", "override the projects root")
	fs.StringVar(&g.machineID, "machine-id", "", "override this PC's machine id")
	fs.StringVar(&g.nowRaw, "now", "", "deterministic clock, RFC3339")
	fs.BoolVar(&g.json, "json", false, "machine output on stdout, nothing else on stdout")
	fs.BoolVar(&g.verbose, "verbose", false, "human log on stderr")
}

// roots resolves the two roots, honouring the overrides.
//
// A partial override is deliberately allowed: the hub's own checkout overrides the
// projects root alone, and a test that only cares about state overrides that alone.
func (g *globalOpts) roots() (store.Roots, error) {
	r := store.Roots{ProjectsRoot: g.projectsRoot, StateRoot: g.stateRoot}
	if r.ProjectsRoot == "" || r.StateRoot == "" {
		def, err := store.DefaultRoots()
		if err != nil {
			return store.Roots{}, err
		}
		if r.ProjectsRoot == "" {
			r.ProjectsRoot = def.ProjectsRoot
		}
		if r.StateRoot == "" {
			r.StateRoot = def.StateRoot
		}
	}
	return r, nil
}

// now returns the injected clock, or the wall clock. A bad --now is a usage error, not a
// silent fallback: a run that believes it is a different time writes receipts nobody can
// reconcile.
func (g *globalOpts) now() (time.Time, error) {
	if g.nowRaw == "" {
		return time.Now(), nil
	}
	t, err := time.Parse(time.RFC3339, g.nowRaw)
	if err != nil {
		return time.Time{}, fmt.Errorf("--now %q is not RFC3339: %w", g.nowRaw, err)
	}
	return t, nil
}

// logWriter is where the human log goes: stderr when asked for, nowhere otherwise.
// stdout is the product and never carries a log line, so --json output is always
// parseable without filtering.
func (g *globalOpts) logWriter(env Env) io.Writer {
	if g.verbose {
		return env.Stderr
	}
	return io.Discard
}

// newFlagSet builds a silent flag set. The flag package's own error text goes to the
// caller's stderr through our own message so a usage error reads the same whichever verb
// produced it.
func newFlagSet(name string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	return fs
}

// parseArgs parses a flag set over args in which flags and positional arguments may be
// interspersed.
//
// Go's flag package stops at the first non-flag argument, which would make
// `ams-store lock --state-root X status` and `ams-store lock status --state-root X`
// behave differently. A hook's argv is assembled by a config file and a human's argv is
// typed; both spellings have to work.
func parseArgs(fs *flag.FlagSet, args []string) ([]string, error) {
	var positional []string
	for {
		if err := fs.Parse(args); err != nil {
			return nil, err
		}
		rest := fs.Args()
		if len(rest) == 0 {
			return positional, nil
		}
		positional = append(positional, rest[0])
		args = rest[1:]
	}
}

// usageError reports a bad invocation on stderr and returns the exit code for one.
// stdout stays empty: a caller parsing stdout must never receive an error message there.
func usageError(env Env, verb string, err error) int {
	fmt.Fprintf(env.Stderr, "ams-store %s: %v\n", verb, err)
	return ExitUsage
}

// writeJSON emits one JSON document on stdout, the whole product of a --json run.
func writeJSON(w io.Writer, v any) error {
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}
