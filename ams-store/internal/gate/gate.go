// Package gate is the PostToolUse hook that runs after every Write|Edit.
//
// It is FAIL-OPEN BY CONTRACT. A gate that can fail a tool call is a gate that can stop
// the operator working, so every error path here is swallowed and the worst case is
// silence: Run recovers from a panic, ignores every error it cannot act on, and always
// returns 0. The PowerShell original says the same thing three ways -
// $ErrorActionPreference='SilentlyContinue', the whole body in try{}catch{} with an empty
// catch, and a bare `exit 0` outside it.
//
// Its job is narrow on purpose: advise always, and ACT only when the index has crossed
// the harness's sync limit. Network is never on a hook's critical path, so nothing here
// fetches, pushes or resolves a hostname.
package gate

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// ReceiptFile is the gate's own receipts JSONL under STATE_ROOT.
const ReceiptFile = "write-gate-receipts.jsonl"

// FloorResult is what one convergence-floor pass reports.
type FloorResult struct {
	// Floored is how many entry lines were truncated.
	Floored int
	// Bytes is the projected size of the regenerated index.
	Bytes int
}

// Floorer is the convergence floor, declared as an interface because derive owns the
// implementation and derive is built in parallel.
//
// There is exactly ONE floor in this binary. The gate and the nightly derive must
// truncate identically, or an index converges at write time and diverges again at
// night - so this interface is a seam for wiring, never a licence for a second
// implementation.
//
// Contract (LIB:634-688): engage only at or above engageAt; then truncate non-doctrine
// entry hooks IN PLACE, longest rendered line first, until the projected index is under
// stopBelow; never touch a doctrine line; reject any truncation that does not shrink the
// line or that fails the round-trip check.
type Floorer interface {
	Floor(records []*index.Record, storeDir, newline string, engageAt, stopBelow int) (FloorResult, error)
}

// Options configures one gate invocation.
type Options struct {
	Roots store.Roots
	// Floor is the convergence floor. A nil Floor means the gate advises and never
	// mutates - which is a safe degradation, not a broken one.
	Floor Floorer
	// StopBelow is the floor's stop threshold. Zero means the compactor trigger, which
	// is the legacy hysteresis: engage at the sync limit, stop below the trigger.
	StopBelow int
	// EngageAt is the size at or above which the gate normalizes at all. Zero means the
	// sync limit - decision Q2's legacy hysteresis, and the value Phase 4 lowers to the
	// trigger. It is the gate's own threshold as well as the floor's: below it the gate
	// advises and never rewrites, so the two must move together or the gate would
	// silently skip a floor it was told to run.
	EngageAt int
	// TryLock takes the per-PC lock. It returns ok=false when another process holds it,
	// and the gate then does nothing at all: a contender skips, the gate NEVER waits.
	// Nil means no locking.
	TryLock func() (release func(), ok bool)
	// OnWrite is called after the gate has rewritten an index, with the store
	// directory. The CLI wires it to the local commit and the dirty marker. It must
	// never touch the network.
	OnWrite func(ctx context.Context, storeDir string) error
	// Now is the injected clock.
	Now time.Time
	// Version is stamped into the receipt.
	Version string
	// Stdout is the advisory block - the product. Stderr is the log.
	Stdout io.Writer
	Stderr io.Writer
}

// Payload is the subset of the harness's hook JSON the gate reads.
type Payload struct {
	HookEventName string `json:"hook_event_name"`
	ToolName      string `json:"tool_name"`
	ToolInput     struct {
		FilePath string `json:"file_path"`
	} `json:"tool_input"`
}

// Receipt is one row of write-gate-receipts.jsonl.
type Receipt struct {
	TS          time.Time `json:"ts"`
	Index       string    `json:"index"`
	BeforeBytes int       `json:"before_bytes"`
	AfterBytes  int       `json:"after_bytes"`
	Floored     int       `json:"floored"`
	Converged   bool      `json:"converged"`
	Version     string    `json:"ams_store_version,omitempty"`
}

// reIndexPath is GATE:32, widened to accept either separator so one rule serves both
// OSes. It is ANCHORED at the end: a file named MEMORY.md somewhere else in the tree, or
// a memory/MEMORY.md.bak, is not a store index and must not be touched.
var reIndexPath = regexp.MustCompile(`[\\/]memory[\\/]MEMORY\.md$`)

// reIndexPathFold is the case-insensitive form used on Windows, whose filesystem folds
// case. Off Windows the spelling is the identity and folding would make the gate act on
// a genuinely different file.
var reIndexPathFold = regexp.MustCompile(`(?i)[\\/]memory[\\/]MEMORY\.md$`)

// Run reads the hook payload from stdin and returns the process exit code, which is
// always 0.
func Run(ctx context.Context, opt Options, stdin io.Reader) (code int) {
	defer func() {
		// The hook contract in one line: whatever went wrong, the tool call succeeds.
		_ = recover()
		code = 0
	}()
	run(ctx, opt, stdin)
	return 0
}

func run(ctx context.Context, opt Options, stdin io.Reader) {
	out := opt.Stdout
	if out == nil {
		out = io.Discard
	}
	errw := opt.Stderr
	if errw == nil {
		errw = io.Discard
	}
	now := opt.Now
	if now.IsZero() {
		now = time.Now()
	}
	stopBelow := opt.StopBelow
	if stopBelow <= 0 {
		stopBelow = store.TriggerBytes
	}
	engageAt := opt.EngageAt
	if engageAt <= 0 {
		engageAt = store.SyncLimitBytes
	}
	// Asking the floor to stop below a size it is not allowed to engage at is a request
	// to engage there; derive.Floor reconciles the pair the same way.
	if stopBelow > engageAt {
		engageAt = stopBelow
	}

	path, ok := IndexPathFromPayload(stdin)
	if !ok {
		return
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}

	// The compare-and-swap currency: the hash as it stood when the gate read the file.
	// A live session writing the index back between this read and the write must lose
	// the gate's edit, never the other way round.
	hashAtRead := atomic.Hash(data)
	text := string(data)
	bytes := len(data)
	ix := index.Parse(text)
	lineCount := CountNonBlank(text)

	var long []*index.Record
	for _, r := range ix.Entries() {
		if r.Bytes > store.LineByteCap {
			long = append(long, r)
		}
	}

	// GATE:44 - silent exit when nothing is wrong. Silence is the product here: a hook
	// that speaks on every write trains the operator to ignore it.
	if len(long) == 0 && bytes < engageAt && lineCount < store.InjectLimitLines {
		return
	}

	if opt.TryLock != nil {
		release, got := opt.TryLock()
		if !got {
			// A contender skips immediately. Maintenance that queues behind maintenance
			// runs under a live session by the time it gets in.
			return
		}
		defer release()
	}

	for _, line := range AdvisoryHeader(bytes, lineCount, len(long)) {
		writeLine(out, line)
	}

	if bytes < engageAt {
		return // advise only: below the engage threshold the gate never rewrites
	}
	if opt.Floor == nil {
		return
	}

	res, err := opt.Floor.Floor(ix.Records, filepath.Dir(path), ix.Newline, engageAt, stopBelow)
	if err != nil {
		return
	}
	if res.Floored == 0 {
		// Doctrine is the hard rule. If every over-cap line is a standing order there is
		// nothing this gate may shorten, and saying so is the whole value of the run.
		writeLine(out, AdvisoryNothingNormalizable)
		return
	}

	current, err := atomic.FileHash(path)
	if err != nil {
		return
	}
	if current != hashAtRead {
		writeLine(out, AdvisoryChangedUnderGate)
		return
	}

	newText := index.RenderVerbatim(ix.Records, ix.Newline)
	if err := atomic.Write(path, newText); err != nil {
		return
	}
	after := len(newText)

	appendReceipt(opt.Roots.StateRoot, Receipt{
		TS:          now.UTC(),
		Index:       path,
		BeforeBytes: bytes,
		AfterBytes:  after,
		Floored:     res.Floored,
		Converged:   after < store.SyncLimitBytes,
		Version:     opt.Version,
	})

	writeLine(out, AdvisoryNormalized(res.Floored, bytes, after))
	if after >= store.SyncLimitBytes {
		writeLine(out, AdvisoryStillOver)
	}

	if opt.OnWrite != nil {
		if err := opt.OnWrite(ctx, filepath.Dir(path)); err != nil {
			// The commit and the dirty marker are bookkeeping. Failing them must not
			// undo a write that already made the index loadable again.
			writeLine(errw, "ams-store gate: "+err.Error())
		}
	}
}

// IndexPathFromPayload decodes the hook JSON and returns the store index path it names.
// Every rejection is silent: an unparsable payload, a payload for another tool, a path
// that is not a store index, or a file that is not there.
func IndexPathFromPayload(stdin io.Reader) (string, bool) {
	if stdin == nil {
		return "", false
	}
	raw, err := io.ReadAll(io.LimitReader(stdin, 1<<20))
	if err != nil || len(strings.TrimSpace(string(raw))) == 0 {
		return "", false
	}
	var p Payload
	if err := json.Unmarshal(raw, &p); err != nil {
		return "", false
	}
	path := strings.TrimSpace(p.ToolInput.FilePath)
	if path == "" {
		return "", false
	}
	if !MatchesIndexPath(path) {
		return "", false
	}
	if _, err := os.Stat(path); err != nil {
		return "", false
	}
	return path, true
}

// MatchesIndexPath reports whether a path is a store's MEMORY.md.
func MatchesIndexPath(path string) bool {
	if runtime.GOOS == "windows" {
		return reIndexPathFold.MatchString(path)
	}
	return reIndexPath.MatchString(path)
}

// CountNonBlank is the GATE's own line rule: non-blank lines only (GATE:41).
//
// This is deliberately NOT index.LineCount, which counts every line and discounts one
// trailing blank (LIB:411-412). The two rules disagree by however many blank lines a
// store has, and unifying them would move one of the two surfaces' thresholds without
// anyone deciding to - the gate's advisory and lint's over-inject-limit would then say
// different things about the same file.
func CountNonBlank(text string) int {
	n := 0
	for _, l := range strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n") {
		if strings.TrimSpace(l) != "" {
			n++
		}
	}
	return n
}

func appendReceipt(stateRoot string, r Receipt) {
	// Wrapped in its own best-effort block: a receipt that cannot be written must never
	// fail a write that already succeeded.
	defer func() { _ = recover() }()
	if stateRoot == "" {
		return
	}
	if err := os.MkdirAll(stateRoot, 0o755); err != nil {
		return
	}
	b, err := json.Marshal(r)
	if err != nil {
		return
	}
	f, err := os.OpenFile(filepath.Join(stateRoot, ReceiptFile), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.Write(append(b, '\n'))
}

func writeLine(w io.Writer, s string) {
	_, _ = io.WriteString(w, s+"\n")
}
