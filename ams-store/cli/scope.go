package cli

import (
	"fmt"
	"path/filepath"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

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
