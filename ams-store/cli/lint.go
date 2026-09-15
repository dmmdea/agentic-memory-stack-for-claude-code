package cli

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lint"
	amsync "github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/sync"
)

// lintUsage is the --help block for `ams-store lint`, blueprint section 1.1.
const lintUsage = `usage: ams-store lint [--all] [--workspace <slug>] [--json] [--quiet]
                      [--summary-out <path>] [--nightly-unit <name>] [--hub-host <name>]

Report on every store without touching one: orphan, dangling, dup-slug, long-line,
oversized-file, no-frontmatter, near-budget, over-sync-limit, over-inject-limit, plus
the stateful findings read from the receipts and from the sync history.

  --all                  every populated store under the projects root (the default)
  --workspace <slug>     restrict to one workspace slug; repeatable
  --json                 one JSON document on stdout, nothing else
  --quiet                suppress the human summary AND bypass the 6 h throttle
  --summary-out <path>   also write the machine summary to this path
  --nightly-unit <name>  the unit compactor-silent names; empty on a PC, where the
                         finding does not exist because there is no local nightly
  --hub-host <name>      the MagicDNS name the history-remote rule pins the hub to

Exit: 0 always for findings; 2 bad invocation. lint never mutates a store.`

// LintThrottleFile is the stamp lint's own 6 h throttle marks, under STATE_ROOT.
const LintThrottleFile = "lint-throttle"

// LintThrottle is how often the UNATTENDED path rescans (LINT:29). A burst of session
// starts costs one scan at worst.
const LintThrottle = 6 * time.Hour

func lintCommand() command {
	return command{
		Name:    "lint",
		Summary: "report on every store without touching one",
		Usage:   lintUsage,
		Run:     runLint,
	}
}

// stringList collects a repeatable flag.
type stringList []string

func (s *stringList) String() string     { return fmt.Sprint(*s) }
func (s *stringList) Set(v string) error { *s = append(*s, v); return nil }

// runLint fails open. Spawned detached from SessionStart, a failure here can never block
// a session, so every error path below reports on stderr and still exits 0.
func runLint(env Env, args []string) int {
	var g globalOpts
	fs := newFlagSet("lint")
	g.bind(fs)
	var all, quiet bool
	var workspaces stringList
	var summaryOut, nightlyUnit, hubHost string
	fs.BoolVar(&all, "all", false, "every populated store")
	fs.BoolVar(&quiet, "quiet", false, "no human summary, and no throttle")
	fs.Var(&workspaces, "workspace", "restrict to a workspace slug")
	fs.StringVar(&summaryOut, "summary-out", "", "also write the summary here")
	fs.StringVar(&nightlyUnit, "nightly-unit", "", "the unit compactor-silent names")
	fs.StringVar(&hubHost, "hub-host", "", "the MagicDNS name of the hub")
	if _, err := parseArgs(fs, args); err != nil {
		return usageError(env, "lint", err)
	}

	roots, err := g.roots()
	if err != nil {
		return usageError(env, "lint", err)
	}
	now, err := g.now()
	if err != nil {
		return usageError(env, "lint", err)
	}

	// Own 6 h throttle, independent of the other SessionStart children. --quiet is the
	// operator's and the test's door past it, and a --quiet run does NOT mark it: an
	// explicit scan must not starve the next scheduled one.
	throttle := filepath.Join(roots.StateRoot, LintThrottleFile)
	if !quiet && throttled(throttle, now, LintThrottle) {
		return ExitOK
	}

	sum, err := lint.Run(context.Background(), lint.Options{
		Roots:       roots,
		NightlyUnit: nightlyUnit,
		Policy:      amsync.RemotePolicy{ExpectedHost: hubHost},
		Workspaces:  workspaces,
		Now:         now,
	})
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store lint: %v\n", err)
		return ExitOK
	}

	if err := lint.Write(roots.StateRoot, sum); err != nil {
		fmt.Fprintf(env.Stderr, "ams-store lint: summary: %v\n", err)
		return ExitOK
	}
	if summaryOut != "" {
		if err := os.MkdirAll(filepath.Dir(summaryOut), 0o755); err != nil {
			fmt.Fprintf(env.Stderr, "ams-store lint: summary-out: %v\n", err)
		} else if err := atomic.WriteJSONFile(summaryOut, sum); err != nil {
			fmt.Fprintf(env.Stderr, "ams-store lint: summary-out: %v\n", err)
		}
	}

	if g.json {
		if err := writeJSON(env.Stdout, sum); err != nil {
			fmt.Fprintf(env.Stderr, "ams-store lint: %v\n", err)
		}
	} else if !quiet {
		fmt.Fprintf(env.Stderr, "ams-store lint: scanned %d store(s): %d finding(s), %d actionable\n",
			len(sum.Stores), sum.Counts.Total, sum.Counts.Actionable)
	}

	// Marked only on SUCCESS (LINT:152): a run that failed must not buy six hours of
	// silence for the failure.
	if !quiet {
		markThrottle(throttle, now)
	}
	return ExitOK
}

// throttled reports whether the last successful run is younger than the window. An
// unreadable or absent stamp means "run": failing open on the stamp costs one extra
// scan, failing closed costs every scan.
func throttled(path string, now time.Time, window time.Duration) bool {
	fi, err := os.Stat(path)
	if err != nil {
		return false
	}
	return now.Sub(fi.ModTime()) < window
}

func markThrottle(path string, now time.Time) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return
	}
	// The CONTENT is the stamp a human reads; the mtime is the stamp the check reads.
	// Writing both means a stamp whose mtime was clobbered by a backup restore is still
	// explicable.
	_ = os.WriteFile(path, []byte(now.UTC().Format(time.RFC3339)+"\n"), 0o644)
	_ = os.Chtimes(path, now, now)
}
