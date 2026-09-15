package cli

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"path/filepath"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gate"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/lock"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	amsync "github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/sync"
)

// gateUsage is the --help block for `ams-store gate`, blueprint section 1.1.
const gateUsage = `usage: ams-store gate [--stdin-payload]

The PostToolUse hook. Reads the harness's hook JSON on stdin, and when the write
touched a store's MEMORY.md reports its size against the caps, normalizes an index that
has crossed the sync limit, commits the store locally and marks the tree dirty. The
advisory block is the product on stdout.

  --stdin-payload        read the hook payload from stdin (the default)
  --engage-at <bytes>    floor engage threshold (default: the harness sync limit)
  --stop-below <bytes>   floor stop threshold (default: the compactor trigger)

The two floor thresholds are decision Q2's hysteresis: the gate advises below
--engage-at and normalizes at or above it, down to --stop-below. The Phase 4 flip to
an unconditional-to-trigger floor is a change of this flag's DEFAULT, so it can be
rehearsed on one PC before it is decided for the fleet.

NETWORK IS NEVER ON A HOOK'S CRITICAL PATH. This verb commits locally and touches the
dirty marker; the watcher is what carries the change to the hub.

Exit: 0, always. The gate is fail-open by contract: a gate that can fail a tool call is
a gate that can stop the operator working, so every error path is swallowed and the
worst case is silence.`

// maxPayloadBytes bounds what the gate will read from stdin. A hook payload is a few
// hundred bytes; anything larger is a caller error or a wedged pipe, and reading it
// unbounded would hang the tool call the gate exists to stay out of the way of.
const maxPayloadBytes = 1 << 20

func gateCommand() command {
	return command{
		Name:    "gate",
		Summary: "the PostToolUse hook; advise, normalize, commit locally, mark dirty",
		Usage:   gateUsage,
		Run:     runGate,
	}
}

// runGate always returns ExitOK. Every failure below is swallowed deliberately: the gate
// runs inside the operator's edit loop, and a hook that can fail a Write is a hook that
// can stop them working.
func runGate(env Env, args []string) (code int) {
	defer func() {
		// Even a panic is a silent zero. There is no failure of this verb worth taking a
		// tool call down for.
		if r := recover(); r != nil {
			fmt.Fprintf(env.Stderr, "ams-store gate: recovered: %v\n", r)
			code = ExitOK
		}
	}()

	var g globalOpts
	fs := newFlagSet("gate")
	g.bind(fs)
	var stdinPayload bool
	var stopBelow int
	var engageAt int
	fs.BoolVar(&stdinPayload, "stdin-payload", true, "read the hook payload from stdin")
	fs.IntVar(&stopBelow, "stop-below", 0, "floor stop threshold in bytes")
	fs.IntVar(&engageAt, "engage-at", 0, "floor engage threshold in bytes")
	if _, err := parseArgs(fs, args); err != nil {
		// Even a bad invocation is a zero here: the harness spawned us, and refusing its
		// argv would surface as a failed Write to the operator.
		fmt.Fprintf(env.Stderr, "ams-store gate: %v\n", err)
		return ExitOK
	}

	roots, err := g.roots()
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store gate: %v\n", err)
		return ExitOK
	}
	now, err := g.now()
	if err != nil {
		fmt.Fprintf(env.Stderr, "ams-store gate: %v\n", err)
		return ExitOK
	}

	// The payload is read ONCE into memory and replayed twice: the gate engine consumes
	// it to decide the advisory, and the bookkeeping pass consumes it to learn which
	// store was written. A pipe cannot be rewound, which is why this is a buffer.
	payload, err := io.ReadAll(io.LimitReader(env.Stdin, maxPayloadBytes))
	if err != nil {
		return ExitOK
	}

	ctx := context.Background()
	gate.Run(ctx, gate.Options{
		Roots:     roots,
		StopBelow: stopBelow,
		EngageAt:  engageAt,
		// The ONE floor in this binary (blueprint 12.1). A nil here leaves the gate an
		// advisory printer: it says the index is over the limit and does nothing about it.
		Floor: floorAdapter{},
		TryLock: func() (func(), bool) {
			l, err := lock.Acquire(lockOptions(LockPath(roots.StateRoot), "gate", now))
			if err != nil {
				return func() {}, false
			}
			return func() { _ = l.Release() }, true
		},
		Now:     now,
		Version: BuildInfo.Version,
		Stdout:  env.Stdout,
		Stderr:  env.Stderr,
	}, bytes.NewReader(payload))

	// Blueprint section 6: derive + local commit + dirty marker, never network. The
	// marker is written for ANY write to a store index, not only for one the gate itself
	// normalized - the harness's Write is what changed the file, and a change nobody
	// marked sits on this PC until something unrelated happens to sync.
	markAndCommit(ctx, env, g, roots, payload, now)
	return ExitOK
}

// markAndCommit records that a store changed: a local commit plus the dirty marker.
//
// Both halves are best-effort and INDEPENDENT. The marker is what the watcher wakes on,
// so losing the commit must not lose the marker; and the commit is what keeps offline
// history, so a missing git must not lose that either. Neither ever touches the network.
func markAndCommit(ctx context.Context, env Env, g globalOpts, roots store.Roots, payload []byte, now time.Time) {
	path, ok := gate.IndexPathFromPayload(bytes.NewReader(payload))
	if !ok {
		return // not a store index; nothing happened that anyone needs to know about
	}
	storeDir := filepath.Dir(path)
	workspace, ok := workspaceOfStoreDir(roots.ProjectsRoot, storeDir)
	if !ok {
		// A MEMORY.md that matches the shape but lives outside the projects root is not
		// this tool's business, and staging it would put a stranger's file in history.
		return
	}

	if err := amsync.MarkDirty(roots.StateRoot); err != nil {
		fmt.Fprintf(env.Stderr, "ams-store gate: dirty marker: %v\n", err)
	}

	l, err := lock.Acquire(lockOptions(LockPath(roots.StateRoot), "gate", now))
	if err != nil {
		// A contender skips. The marker is already down, so the work is not lost - the
		// holder's own pass, or the next one, will commit it.
		return
	}
	defer func() { _ = l.Release() }()

	repo := amsync.NewRepo(roots)
	if err := repo.Initialize(ctx); err != nil {
		fmt.Fprintf(env.Stderr, "ams-store gate: history: %v\n", err)
		return
	}
	if err := repo.Stage(ctx, workspace); err != nil {
		fmt.Fprintf(env.Stderr, "ams-store gate: stage: %v\n", err)
		return
	}
	staged, err := repo.HasStagedChanges(ctx, workspace)
	if err != nil || !staged {
		return // an empty commit says nothing and clutters the history a human reads
	}
	machineID := g.machineID
	if machineID == "" {
		if id, err := amsync.MachineID(roots.StateRoot); err == nil {
			machineID = id
		}
	}
	msg := "gate " + workspace + ": index written"
	if _, err := repo.Commit(ctx, msg, machineID, "local"); err != nil {
		fmt.Fprintf(env.Stderr, "ams-store gate: commit: %v\n", err)
	}
}

// workspaceOfStoreDir maps <PROJECTS_ROOT>/<workspace>/memory back to <workspace>, and
// reports false for any path that is not under the projects root.
func workspaceOfStoreDir(projectsRoot, storeDir string) (string, bool) {
	rel, err := filepath.Rel(projectsRoot, storeDir)
	if err != nil {
		return "", false
	}
	rel = filepath.ToSlash(rel)
	if strings.HasPrefix(rel, "../") || rel == ".." {
		return "", false
	}
	parts := strings.Split(rel, "/")
	if len(parts) != 2 || parts[1] != "memory" || parts[0] == "" {
		return "", false
	}
	return parts[0], true
}
