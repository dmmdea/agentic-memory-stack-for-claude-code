package cli

import (
	"encoding/json"
	"flag"
	"fmt"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/derive"
)

// harvestUsage is the --help block for `ams-store harvest`, blueprint section 1.1. The
// step is part of derive; the explicit verb exists because harvest must run on every PC
// before that PC's first push, so `hook:`-only differences never reach the merge.
const harvestUsage = `usage: ams-store harvest --store <dir>
       ams-store harvest --all [--workspace <slug>]

Copy each index entry's hook text into its fact file's frontmatter as hook:, and stamp
migrated: <id> onto a slug the history says the judge migrated. Idempotent: a file that
already carries the key is never rewritten, and a file with no frontmatter block is left
alone. Writes no index and renders nothing.

  --store <dir>          the store (memory directory) to harvest
  --all                  every populated store under the projects root
  --workspace <slug>     restrict to one workspace slug
  --json                 one JSON document on stdout, nothing else

Exit: 0 normal, 2 bad invocation, 3 refused, 4 lock held.`

func harvestCommand() command {
	return command{
		Name:    "harvest",
		Summary: "copy index hook text into fact-file frontmatter",
		Usage:   harvestUsage,
		Run:     runHarvest,
	}
}

type harvestReport struct {
	Verb   string           `json:"verb"`
	Stores []*derive.Result `json:"stores"`
	// Harvested and Stamped are the two figures the seed receipt records per PC.
	Harvested int `json:"harvested"`
	Stamped   int `json:"stamped"`
}

func runHarvest(env Env, args []string) int {
	var (
		g         globals
		storeDir  string
		workspace string
		all       bool
	)
	fs := flag.NewFlagSet("harvest", flag.ContinueOnError)
	fs.SetOutput(env.Stderr)
	fs.StringVar(&storeDir, "store", "", "the store (memory directory) to harvest")
	fs.BoolVar(&all, "all", false, "every populated store under the projects root")
	fs.StringVar(&workspace, "workspace", "", "restrict to one workspace slug")
	g.register(fs)
	if err := fs.Parse(args); err != nil {
		return ExitUsage
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(env.Stderr, "ams-store harvest: unexpected argument %q\n", fs.Arg(0))
		return ExitUsage
	}
	if storeDir == "" && !all && workspace == "" {
		fmt.Fprintln(env.Stderr, "ams-store harvest: pass --store <dir>, --all, or --workspace <slug>")
		return ExitUsage
	}

	roots, err := g.roots()
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store harvest: %v\n", err)
		return ExitUsage
	}
	now, err := g.clock()
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store harvest: %v\n", err)
		return ExitUsage
	}
	stores, code := resolveScope(env, roots, storeDir, workspace, all)
	if code != ExitOK {
		return code
	}

	report := harvestReport{Verb: "harvest", Stores: make([]*derive.Result, 0, len(stores))}
	exit := ExitOK
	for _, st := range stores {
		res, err := derive.Harvest(derive.Options{
			Roots: roots,
			Store: st,
			Now:   now,
			// Decision Q8: the id comes from the judge's deletion commit, so harvest is
			// where a re-created slug gets its `migrated:` key back.
			Migrated: newMigratedLookup(roots),
			Log:      env.Stderr,
		})
		if res != nil {
			report.Stores = append(report.Stores, res)
			report.Harvested += res.Harvested
			report.Stamped += res.MigratedStamped
		}
		if err != nil {
			fmt.Fprintf(env.Stderr, "ams-store harvest: %s: %v\n", st.Workspace, err)
			exit = worseExit(exit, exitFor(err))
		}
	}

	if g.JSON {
		b, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			fmt.Fprintf(env.Stderr, "ams-store harvest: %v\n", err)
			return worseExit(exit, ExitUsage)
		}
		fmt.Fprintln(env.Stdout, string(b))
	}
	return exit
}
