package cli

import (
	"flag"
	"fmt"
	"path/filepath"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// globals are the flags blueprint section 1.1 puts on every verb. They are declared per
// verb rather than parsed ahead of argv, so `ams-store derive --json` and
// `ams-store lint --json` read the same way and no verb has to care where a global sat.
type globals struct {
	JSON         bool
	Verbose      bool
	StateRoot    string
	ProjectsRoot string
	Now          string
	MachineID    string
}

func (g *globals) register(fs *flag.FlagSet) {
	fs.BoolVar(&g.JSON, "json", false, "machine output on stdout, nothing else on stdout")
	fs.BoolVar(&g.Verbose, "verbose", false, "human log on stderr")
	fs.StringVar(&g.StateRoot, "state-root", "", "override the maintainer state root (test injection)")
	fs.StringVar(&g.ProjectsRoot, "projects-root", "", "override the projects root (test injection)")
	fs.StringVar(&g.Now, "now", "", "deterministic clock, RFC3339 (test injection)")
	fs.StringVar(&g.MachineID, "machine-id", "", "override this PC's machine id")
}

// roots resolves the two directory roots, honouring the overrides. A partial override is
// allowed: pointing --projects-root at a sandbox while the state root stays real is how
// the seed's rehearsal runs.
func (g *globals) roots() (store.Roots, error) {
	r, err := store.DefaultRoots()
	if err != nil && g.ProjectsRoot == "" {
		return store.Roots{}, err
	}
	if g.ProjectsRoot != "" {
		r.ProjectsRoot = g.ProjectsRoot
	}
	if g.StateRoot != "" {
		r.StateRoot = g.StateRoot
	}
	if r.ProjectsRoot == "" {
		return store.Roots{}, fmt.Errorf("cannot resolve the projects root; pass --projects-root")
	}
	return r, nil
}

// clock resolves --now. A bad value is a bad invocation, never a silent fall back to the
// wall clock: a test that thinks it pinned the clock and did not would pass for the wrong
// reason.
func (g *globals) clock() (time.Time, error) {
	if g.Now == "" {
		return time.Now(), nil
	}
	t, err := time.Parse(time.RFC3339, g.Now)
	if err != nil {
		return time.Time{}, fmt.Errorf("--now %q is not RFC3339: %w", g.Now, err)
	}
	return t, nil
}

// storeAt builds the row for one store directory named on the command line. The workspace
// id is the harness slug, i.e. the name of the directory the memory folder sits in.
func storeAt(dir string) store.Store {
	dir = filepath.Clean(dir)
	wsDir := filepath.Dir(dir)
	return store.Store{
		Workspace:    filepath.Base(wsDir),
		Dir:          dir,
		IndexPath:    filepath.Join(dir, store.IndexName),
		CanonicalDir: dir,
		WorkspaceDir: wsDir,
		ProbeDirs:    []string{wsDir},
	}
}

// resolveScope turns --store / --all / --workspace into the store rows a verb runs over.
// It returns an exit code rather than an error for a bad invocation, because "you asked
// for a workspace that does not exist" is exit 2 and nothing else.
func resolveScope(env Env, roots store.Roots, storeDir, workspace string, all bool) ([]store.Store, int) {
	if storeDir != "" && all {
		fmt.Fprintln(env.Stderr, "ams-store: --store and --all are mutually exclusive")
		return nil, ExitUsage
	}
	if storeDir != "" {
		st := storeAt(storeDir)
		if workspace != "" && st.Workspace != workspace {
			fmt.Fprintf(env.Stderr, "ams-store: --store %s is workspace %q, not %q\n", storeDir, st.Workspace, workspace)
			return nil, ExitUsage
		}
		return []store.Store{st}, ExitOK
	}

	rows, warnings, err := store.Enumerate(roots.ProjectsRoot)
	if err != nil {
		// Enumeration fails CLOSED: "could not read the projects root" is a refusal, never
		// an empty store set that every downstream guard then agrees with.
		fmt.Fprintf(env.Stderr, "ams-store: %v\n", err)
		return nil, ExitRefused
	}
	for _, w := range warnings {
		fmt.Fprintf(env.Stderr, "ams-store: %s\n", w)
	}

	out := make([]store.Store, 0, len(rows))
	for _, r := range rows {
		// An alias is the same physical store: mutating through it mutates the real one
		// twice in a run, the second pass reading the first pass's output.
		if r.IsAlias {
			continue
		}
		if workspace != "" && r.Workspace != workspace {
			continue
		}
		out = append(out, r)
	}
	if workspace != "" && len(out) == 0 {
		fmt.Fprintf(env.Stderr, "ams-store: no store matches workspace %q\n", workspace)
		return nil, ExitUsage
	}
	return out, ExitOK
}
