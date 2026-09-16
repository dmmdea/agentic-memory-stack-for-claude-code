package cli

import (
	"encoding/json"
	"errors"
	"fmt"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/derive"
)

// deriveUsage is the --help block for `ams-store derive`, blueprint section 1.1.
const deriveUsage = `usage: ams-store derive [--store <dir>|--all] [--workspace <slug>] [--dry-run] [--json]
                        [--no-harvest] [--engage-at <bytes>] [--stop-below <bytes>]
                        [--projects-root <dir>]

Re-derive MEMORY.md from the fact files of one store or of every store. derive is the
single deterministic writer: it harvests index hooks into frontmatter, runs hygiene,
renders doctrine first and newest next, applies the convergence floor and writes the
index atomically as LF.

  --store <dir>          one store (the memory directory) instead of every store
  --all                  every populated store under the projects root
  --workspace <slug>     restrict to one workspace slug
  --dry-run              report what would change; the store stays untouched and the
                         receipt row is flagged dry_run
  --json                 one JSON document on stdout, nothing else
  --no-harvest           skip the frontmatter harvest step
  --engage-at <bytes>    floor engage threshold (default: the harness sync limit)
  --stop-below <bytes>   floor stop threshold (default: the compactor trigger)
  --projects-root <dir>  override the projects root (test injection)

The two floor thresholds are decision Q2's hysteresis: the floor engages at or above
--engage-at and truncates down to --stop-below. The Phase 4 flip to an
unconditional-to-trigger floor is a change of --engage-at's DEFAULT, so it can be
rehearsed and measured on one PC before it is decided for the fleet.

Exit: 0 normal or skipped, 1 still over the sync limit after the floor, 2 bad
invocation, 3 refused, 4 lock held by another process.`

func deriveCommand() command {
	return command{
		Name:    "derive",
		Summary: "re-derive MEMORY.md from the fact files",
		Usage:   deriveUsage,
		Run:     runDerive,
	}
}

// deriveReport is the --json document. One row per store, plus the two figures a caller
// would otherwise have to re-derive from the rows.
type deriveReport struct {
	Verb        string           `json:"verb"`
	Stores      []*derive.Result `json:"stores"`
	Changed     int              `json:"changed"`
	Unconverged int              `json:"unconverged"`
}

func runDerive(env Env, args []string) int {
	var (
		g         globalOpts
		storeDir  string
		workspace string
		all       bool
		dryRun    bool
		noHarvest bool
		stopBelow int
		engageAt  int
	)
	fs := newFlagSet("derive")
	fs.StringVar(&storeDir, "store", "", "one store (the memory directory)")
	fs.BoolVar(&all, "all", false, "every populated store under the projects root")
	fs.StringVar(&workspace, "workspace", "", "restrict to one workspace slug")
	fs.BoolVar(&dryRun, "dry-run", false, "report what would change; the store stays untouched, the receipt row is flagged dry_run")
	fs.BoolVar(&noHarvest, "no-harvest", false, "skip the frontmatter harvest step")
	fs.IntVar(&stopBelow, "stop-below", 0, "floor stop threshold in bytes")
	fs.IntVar(&engageAt, "engage-at", 0, "floor engage threshold in bytes")
	g.bind(fs)
	if _, err := parseArgs(fs, args); err != nil {
		return usageError(env, fs.Name(), err)
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(env.Stderr, "ams-store derive: unexpected argument %q\n", fs.Arg(0))
		return ExitUsage
	}
	// Scope is REQUIRED, never defaulted to --all. derive is a writer: it harvests into
	// fact files and rewrites MEMORY.md. A bare `ams-store derive` that quietly meant
	// "every populated store on this PC" turned one stray invocation - the scaffold test
	// that ran the whole verb table with no arguments - into a live write across every
	// store on the machine. An explicit scope is the one guard a call site cannot forget.
	if storeDir == "" && !all && workspace == "" {
		fmt.Fprintln(env.Stderr, "ams-store derive: pass --store <dir>, --all, or --workspace <slug>")
		return ExitUsage
	}

	roots, err := g.roots()
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store derive: %v\n", err)
		return ExitUsage
	}
	now, err := g.now()
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store derive: %v\n", err)
		return ExitUsage
	}
	stores, code := resolveScope(env, roots, storeDir, workspace, all)
	if code != ExitOK {
		return code
	}

	commits := derive.NewHistoryCommitTimes(roots)
	// The seams (cli/seams.go). Every one of these is an engine derive drives through a
	// one-method interface, and a nil here is a silent degradation, not a failure: no
	// lock means two derives on one PC race, no lookup means a re-created slug becomes a
	// second corpus record, no committer means offline history is never kept.
	dlock := deriveLock{path: LockPath(roots.StateRoot), now: now}
	migrated := newMigratedLookup(roots)
	committer := deriveCommitter{roots: roots, machineID: g.machineID}
	report := deriveReport{Verb: "derive", Stores: make([]*derive.Result, 0, len(stores))}
	exit := ExitOK

	for _, st := range stores {
		res, err := derive.Run(derive.Options{
			Roots:          roots,
			Store:          st,
			DryRun:         dryRun,
			NoHarvest:      noHarvest,
			StopBelowBytes: stopBelow,
			EngageAtBytes:  engageAt,
			Now:            now,
			Commits:        commits,
			Lock:           dlock,
			Migrated:       migrated,
			Committer:      committer,
			Log:            env.Stderr,
		})
		if res != nil {
			report.Stores = append(report.Stores, res)
			if res.Changed {
				report.Changed++
			}
			if res.Unconverged {
				report.Unconverged++
			}
			// The G7 clock: this is the maintenance path, and the maintenance path is the
			// only writer of the over-trigger stamp.
			recordOverTrigger(roots, res.Workspace, resultBytes(res.BeforeBytes, res.AfterBytes), dryRun, now, env.Stderr)
		}
		if err != nil {
			// One unreadable store must not kill the run: every other store still gets
			// its pass, and the worst code seen wins at the end.
			fmt.Fprintf(env.Stderr, "ams-store derive: %s: %v\n", st.Workspace, err)
			exit = worseExit(exit, exitFor(err))
			continue
		}
		if res != nil && res.Unconverged {
			exit = worseExit(exit, ExitUnconverged)
		}
	}

	if g.json {
		b, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			fmt.Fprintf(env.Stderr, "ams-store derive: %v\n", err)
			return worseExit(exit, ExitUsage)
		}
		fmt.Fprintln(env.Stdout, string(b))
	} else if g.verbose {
		for _, r := range report.Stores {
			fmt.Fprintf(env.Stderr, "%s: %s\n", r.Workspace, r.Status)
		}
	}
	return exit
}

// exitFor maps an engine error onto the exit-code contract of blueprint section 1.2. It
// exists so "the lock was held" cannot arrive at a caller as a generic failure: a contender
// that skipped did no work and broke nothing, and exit 4 is how it says so.
func exitFor(err error) int {
	switch {
	case err == nil:
		return ExitOK
	case errors.Is(err, derive.ErrLocked):
		return ExitLocked
	default:
		return ExitRefused
	}
}

// worseExit keeps the most serious code a multi-store run produced. Refused and locked
// outrank unconverged, which outranks a clean pass.
func worseExit(a, b int) int {
	rank := func(c int) int {
		switch c {
		case ExitOK:
			return 0
		case ExitUnconverged:
			return 1
		case ExitLocked:
			return 2
		case ExitUsage:
			return 3
		case ExitRefused:
			return 4
		default:
			return 5
		}
	}
	if rank(b) > rank(a) {
		return b
	}
	return a
}
