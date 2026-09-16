package cli

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/judge"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// judgeApplyUsage is the --help block for `ams-store judge-apply`, blueprint section
// 1.1 and section 13 Q1. Hub-only: the Codex call itself stays in the Python chain, and
// this verb consumes the plan file that call produces.
const judgeApplyUsage = `usage: ams-store judge-apply --plan <file> --store <dir> [--dry-run]
                            [--max-migrations 5] [--force] [--hub] [--candidates]

Apply a judge plan to one store under every apply-guard: strict decrease, anchor
retention, the seal, the line round-trip, migration write-then-verify, the blast cap
and protected-set overflow. Hub-only.

  --plan <file>          the plan file to apply
  --store <dir>          the store (memory directory) to apply it to
  --workspace <slug>     the workspace id; default: the store directory's parent name
  --dry-run              report what would be applied; write nothing, post nothing
  --max-migrations <n>   cap the migrations one plan may perform (default 5)
  --force                bypass the 20 h one-attempt-per-store window, and only that
  --hub                  assert the hub role before <state-root>/role is seeded
  --candidates           print the offer set for this store and apply nothing
  --mem0-url <url>       the corpus authority (env AMS_MEM0_URL, MEM0_URL)
  --mem0-user <id>       the corpus user id (env MEM0_USER_ID)
  --json                 machine output on stdout

The corpus API key is read from the environment only (MEM0_API_KEY, AMS_MEM0_KEY): a
key on a command line reaches the process list and the shell history.

Exit: 0 applied or nothing to do, 2 bad invocation, 3 refused (not the hub).`

func judgeApplyCommand() command {
	return command{
		Name:    "judge-apply",
		Summary: "apply a judge plan under every apply-guard (hub-only)",
		Usage:   judgeApplyUsage,
		Run:     runJudgeApply,
	}
}

type judgeApplyFlags struct {
	plan          string
	storeDir      string
	workspace     string
	dryRun        bool
	force         bool
	hub           bool
	candidates    bool
	maxMigrations int
	mem0URL       string
	mem0User      string
	stateRoot     string
	projectsRoot  string
	now           string
	asJSON        bool
	verbose       bool
}

func runJudgeApply(env Env, args []string) int {
	var f judgeApplyFlags
	fs := flag.NewFlagSet("judge-apply", flag.ContinueOnError)
	fs.SetOutput(env.Stderr)
	fs.Usage = func() { fmt.Fprintln(env.Stderr, judgeApplyUsage) }
	fs.StringVar(&f.plan, "plan", "", "the plan file to apply")
	fs.StringVar(&f.storeDir, "store", "", "the store (memory directory)")
	fs.StringVar(&f.workspace, "workspace", "", "the workspace id")
	fs.BoolVar(&f.dryRun, "dry-run", false, "write nothing, post nothing")
	fs.BoolVar(&f.force, "force", false, "bypass the judge window")
	fs.BoolVar(&f.hub, "hub", false, "assert the hub role")
	fs.BoolVar(&f.candidates, "candidates", false, "print the offer set and apply nothing")
	fs.IntVar(&f.maxMigrations, "max-migrations", judge.DefaultMaxMigrations, "cap the judge's migrations")
	fs.StringVar(&f.mem0URL, "mem0-url", "", "the corpus authority")
	fs.StringVar(&f.mem0User, "mem0-user", "", "the corpus user id")
	fs.StringVar(&f.stateRoot, "state-root", "", "override the maintainer state root")
	fs.StringVar(&f.projectsRoot, "projects-root", "", "override the projects root")
	fs.StringVar(&f.now, "now", "", "deterministic clock, RFC3339")
	fs.BoolVar(&f.asJSON, "json", false, "machine output on stdout")
	fs.BoolVar(&f.verbose, "verbose", false, "human log on stderr")
	if err := fs.Parse(args); err != nil {
		return ExitUsage
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: unexpected argument %q\n", fs.Arg(0))
		return ExitUsage
	}

	roots, err := store.DefaultRoots()
	if err != nil && (f.stateRoot == "" || f.projectsRoot == "") {
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: %v\n", err)
		return ExitUsage
	}
	if f.stateRoot != "" {
		roots.StateRoot = f.stateRoot
	}
	if f.projectsRoot != "" {
		roots.ProjectsRoot = f.projectsRoot
	}

	// The hub guard comes FIRST, before the plan is even read: a PC must not be able to
	// learn what the nightly decided by pointing this verb at a plan file, and a second
	// judge on the same store is the concurrency bug of v1 in another form.
	if err := judge.RequireHub(roots.StateRoot, f.hub); err != nil {
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: %v\n", err)
		return ExitRefused
	}

	if f.storeDir == "" {
		fmt.Fprintln(env.Stderr, "ams-store judge-apply: --store is required")
		return ExitUsage
	}
	storeDir, err := filepath.Abs(f.storeDir)
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: --store: %v\n", err)
		return ExitUsage
	}
	workspace := f.workspace
	if workspace == "" {
		workspace = filepath.Base(filepath.Dir(storeDir))
	}
	if workspace == "" || workspace == "." || workspace == string(filepath.Separator) {
		fmt.Fprintln(env.Stderr, "ams-store judge-apply: cannot infer a workspace from --store; pass --workspace")
		return ExitUsage
	}

	now := time.Time{}
	if f.now != "" {
		now, err = time.Parse(time.RFC3339, f.now)
		if err != nil {
			fmt.Fprintf(env.Stderr, "ams-store judge-apply: --now: %v\n", err)
			return ExitUsage
		}
	}

	if f.candidates {
		return runJudgeCandidates(env, roots, storeDir, workspace, f.asJSON)
	}

	if f.plan == "" {
		fmt.Fprintln(env.Stderr, "ams-store judge-apply: --plan is required")
		return ExitUsage
	}
	plan, err := judge.LoadPlan(f.plan)
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: %v\n", err)
		return ExitUsage
	}
	sp, ok := plan.Store(workspace)
	if !ok {
		// Not an error: the nightly may have had nothing to say about this store. The
		// deterministic work still runs, so the run is reported, not refused.
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: the plan names no decisions for %q; running the deterministic work only\n", workspace)
	}

	// Decision Q9: judge-apply takes the SAME per-PC lock every other writer takes, plus
	// the legacy compactor mutex, for as long as both exist. The hub runs this from a
	// timer while the same checkout may be syncing; without the lock a nightly apply and
	// a merge materialize can be inside one store at the same moment.
	l, lockErr := lock.Acquire(lockOptions(LockPath(roots.StateRoot), "judge-apply", now))
	if lockErr != nil {
		if isHeld(lockErr) {
			fmt.Fprintln(env.Stderr, "ams-store judge-apply: the per-PC lock is held; skipping")
			return ExitLocked
		}
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: %v\n", lockErr)
		return ExitRefused
	}
	defer func() { _ = l.Release() }()

	st := storeAt(storeDir)
	st.Workspace = workspace
	opt := judge.Options{
		Roots:         roots,
		Dir:           storeDir,
		Workspace:     workspace,
		Plan:          sp,
		DryRun:        f.dryRun,
		Force:         f.force,
		MaxMigrations: f.maxMigrations,
		Now:           now,
		Mem0:          mem0Client(env, f),
		History:       judge.HistoryRepo{GitDir: roots.HistoryGitDir(), WorkTree: roots.ProjectsRoot},
		// The judge's projected-index guard asserts the index STRICTLY shrinks, so it has
		// to measure the bytes derive will write - the derived order, doctrine first, with
		// the injection stop - not a verbatim re-render. Left unwired, the guard passes a
		// run that grows the file derive then produces, and the index the judge leaves
		// behind is a whole-file diff away from the next derive's output.
		RenderIndex: judgeRenderIndex(roots, storeDir, now),
	}
	if f.verbose {
		opt.Log = env.Stderr
	}

	res, err := judge.Apply(context.Background(), opt)
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: %v\n", err)
		return ExitUsage
	}

	if f.asJSON {
		out := map[string]any{
			"workspace":    res.Workspace,
			"status":       res.Status,
			"note":         res.Note,
			"shortened":    res.Shortened,
			"migrated":     res.Migrated,
			"line_floored": res.LineFloored,
			"before_bytes": res.BeforeBytes,
			"after_bytes":  res.AfterBytes,
			"before_lines": res.BeforeLines,
			"after_lines":  res.AfterLines,
			"judge_called": res.JudgeCalled,
			"productive":   res.Productive,
			"commit":       res.Commit,
			"mem0":         res.Mem0,
			"mem0_orphan":  res.Mem0Orphan,
		}
		enc := json.NewEncoder(env.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(out); err != nil {
			fmt.Fprintf(env.Stderr, "ams-store judge-apply: %v\n", err)
			return ExitUsage
		}
		return ExitOK
	}

	fmt.Fprintf(env.Stderr, "judge-apply %s: %s (shortened=%d migrated=%d line_floored=%d, %d -> %d B)\n",
		res.Workspace, res.Status, res.Shortened, res.Migrated, res.LineFloored, res.BeforeBytes, res.AfterBytes)
	if res.Note != "" {
		fmt.Fprintf(env.Stderr, "  %s\n", res.Note)
	}
	for _, o := range res.Mem0Orphan {
		fmt.Fprintf(env.Stderr, "  orphan: %s\n", o)
	}
	return ExitOK
}

func runJudgeCandidates(env Env, roots store.Roots, dir, workspace string, asJSON bool) int {
	wsState, err := roots.WorkspaceStateDir(workspace)
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: %v\n", err)
		return ExitUsage
	}
	seal, err := judge.LoadSeal(judge.SealPath(wsState))
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: %v\n", err)
		return ExitUsage
	}
	st, err := judge.Load(dir, workspace)
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store judge-apply: %v\n", err)
		return ExitUsage
	}
	cs := st.Candidates(seal)
	if asJSON {
		enc := json.NewEncoder(env.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(map[string]any{"workspace": workspace, "shorten": cs.Shorten, "migrate": cs.Migrate}); err != nil {
			fmt.Fprintf(env.Stderr, "ams-store judge-apply: %v\n", err)
			return ExitUsage
		}
		return ExitOK
	}
	for _, c := range cs.Shorten {
		fmt.Fprintf(env.Stdout, "- slug: %s | type: %s | %d B\n  current: %s\n", c.Slug, c.Type, c.Bytes, c.Hook)
	}
	for _, c := range cs.Migrate {
		fmt.Fprintf(env.Stdout, "- slug: %s | type: %s | migrate-candidate\n  current: %s\n", c.Slug, c.Type, c.Hook)
	}
	return ExitOK
}

// mem0Client builds the corpus client, or nil when no authority is configured - in which
// case migrations are reported as not performed rather than silently counted.
//
// The API key comes from the environment only. A key passed as an argument is visible in
// the process list to every user on the box and lands in shell history.
func mem0Client(env Env, f judgeApplyFlags) judge.Mem0Client {
	url := strings.TrimSpace(f.mem0URL)
	if url == "" {
		url = strings.TrimSpace(firstEnv("AMS_MEM0_URL", "MEM0_URL"))
	}
	if url == "" {
		fmt.Fprintln(env.Stderr, "ams-store judge-apply: no corpus authority configured; migrations will be reported as not performed")
		return nil
	}
	user := strings.TrimSpace(f.mem0User)
	if user == "" {
		user = strings.TrimSpace(firstEnv("MEM0_USER_ID", "AMS_MEM0_USER"))
	}
	if user == "" {
		// The authority refuses an empty partition per request, so a client built without a
		// user turns every migration into its own HTTP 500 while the night still exits 0.
		// A missing partition is the same class of misconfiguration as a missing authority
		// and is reported the same way: once, before any write is attempted.
		fmt.Fprintln(env.Stderr, "ams-store judge-apply: no corpus user configured; migrations will be reported as not performed")
		return nil
	}
	return &judge.HTTPMem0{
		BaseURL: url,
		APIKey:  strings.TrimSpace(firstEnv("MEM0_API_KEY", "AMS_MEM0_KEY")),
		UserID:  user,
	}
}

func firstEnv(names ...string) string {
	for _, n := range names {
		if v := os.Getenv(n); v != "" {
			return v
		}
	}
	return ""
}
