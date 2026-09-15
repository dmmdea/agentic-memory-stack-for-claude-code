// Package lint reports on every store without touching one.
//
// READ-ONLY BY CONTRACT. It never writes inside a store. Every finding is recomputed
// from disk on every run - a store holds at most a couple of hundred small files, so a
// full scan is milliseconds - and there is deliberately no flag ledger or audited-keys
// watermark: a monotone watermark would suppress the recurrence of an issue that was
// fixed and then came back.
package lint

import (
	"bufio"
	"encoding/json"
	"os"
	"strings"
	"time"
)

// ReceiptFile is the compactor/maintenance receipts JSONL under STATE_ROOT.
const ReceiptFile = "compact-receipts.jsonl"

// TailLines is how much of a receipts file the per-store history reads (LIB:522).
const TailLines = 600

// UnproductiveTailLines is the window the compactor-unproductive rule scans (LINT:92).
//
// 400, not 40: the tail is shared across every store on the PC, so a small window on a
// busy fleet never lets any ONE store reach three receipts and the finding never fires.
const UnproductiveTailLines = 400

// Receipt statuses that count as reaching a decision (LIB:535).
var productiveStatuses = map[string]bool{
	"applied":                  true,
	"applied-unrecorded":       true,
	"no-op":                    true,
	"protected-set-overflow":   true,
}

// goodStatuses is the narrower set compactor-unproductive accepts (LINT:109). It does
// NOT include protected-set-overflow: a store whose whole over-cap set is doctrine keeps
// reporting that forever, and calling it "good" would hide a store nothing can help.
var goodStatuses = map[string]bool{
	"applied":            true,
	"applied-unrecorded": true,
	"no-op":              true,
}

// NeutralJudgeSkip is the status that is neither good nor bad (LINT:106).
//
// The compactor withheld the judge because it already ran inside the 20 h window, and a
// catch-up sweep re-visits every over-trigger store while any store is starved, so one
// of these lands per session start. It says "waiting", not "stuck": it is EXCLUDED from
// the window rather than counted as good, so three rejected runs with these between them
// still fire.
const NeutralJudgeSkip = "skipped-judge-attempted-today"

// SkipLiveSession is the status a live-session skip writes.
const SkipLiveSession = "skipped-live-session"

// ReceiptRow is the subset of a maintenance receipt the lint reads.
type ReceiptRow struct {
	TS          time.Time `json:"ts"`
	Workspace   string    `json:"workspace"`
	Status      string    `json:"status"`
	DryRun      bool      `json:"dry_run"`
	JudgeCalled bool      `json:"judge_called"`
	SkipStreak  int       `json:"skip_streak"`
}

// RunHistory is what the receipts say about ONE store.
//
// It exists because two watchdogs missed the same store. compactor-silent keys on the
// receipts FILE's age, and a live-session skip WRITES a receipt, so a store skipped every
// night looked alive to it; compactor-unproductive needs three consecutive bad receipts,
// and the store that went over the sync limit had applied, skipped, skipped. The skip
// streak is the per-store signal both of them lacked.
type RunHistory struct {
	SkipStreak        int
	LastStatus        string
	LastProductiveUTC *time.Time
	// LastJudgeUTC is when this store last had a judge CALL, whatever the outcome. A
	// rejected result is a receipt, not a retry.
	LastJudgeUTC *time.Time
}

// ReadRunHistory reads the tail of a receipts file and summarizes one workspace.
// It never fails: a missing, unreadable or half-written receipts file yields a zero
// history, because "I cannot read the receipts" must not be reported as "this store is
// healthy" OR crash the banner.
func ReadRunHistory(path, workspace string) RunHistory {
	out := RunHistory{}
	rows := readRows(path, TailLines)
	mine := make([]ReceiptRow, 0, len(rows))
	for _, r := range rows {
		if r.Workspace != workspace || r.DryRun {
			continue
		}
		mine = append(mine, r)
	}
	if len(mine) == 0 {
		return out
	}
	out.LastStatus = mine[len(mine)-1].Status
	for i := len(mine) - 1; i >= 0; i-- {
		if mine[i].Status != SkipLiveSession {
			break
		}
		out.SkipStreak++
	}
	for i := len(mine) - 1; i >= 0; i-- {
		if productiveStatuses[mine[i].Status] {
			t := mine[i].TS.UTC()
			out.LastProductiveUTC = &t
			break
		}
	}
	for i := len(mine) - 1; i >= 0; i-- {
		if mine[i].JudgeCalled {
			t := mine[i].TS.UTC()
			out.LastJudgeUTC = &t
			break
		}
	}
	return out
}

// Unproductive reports whether a store's last three non-neutral, non-dry-run runs all
// failed to reach a decision (LINT:85-119).
//
// A FRESH receipt file is not proof of progress: a compactor that aborts every night
// writes a receipt every night.
func Unproductive(path, workspace string) (bool, []string) {
	rows := readRows(path, UnproductiveTailLines)
	var mine []ReceiptRow
	for _, r := range rows {
		if r.Workspace != workspace || r.DryRun || r.Status == NeutralJudgeSkip {
			continue
		}
		mine = append(mine, r)
	}
	if len(mine) < 3 {
		return false, nil
	}
	recent := mine[len(mine)-3:]
	statuses := make([]string, 0, 3)
	good := 0
	for _, r := range recent {
		statuses = append(statuses, r.Status)
		if goodStatuses[r.Status] {
			good++
		}
	}
	return good == 0, statuses
}

// FileAgeHours is how old a file's last write is, or nil when it is absent.
func FileAgeHours(path string, now time.Time) *float64 {
	fi, err := os.Stat(path)
	if err != nil {
		return nil
	}
	h := now.UTC().Sub(fi.ModTime().UTC()).Hours()
	h = float64(int(h*10+0.5)) / 10
	return &h
}

func readRows(path string, tail int) []ReceiptRow {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	var lines []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		l := strings.TrimSpace(sc.Text())
		if l == "" {
			continue
		}
		lines = append(lines, l)
		if tail > 0 && len(lines) > tail {
			lines = lines[1:]
		}
	}
	if err := sc.Err(); err != nil {
		return nil
	}
	out := make([]ReceiptRow, 0, len(lines))
	for _, l := range lines {
		var r ReceiptRow
		// One torn append must not blind the reader to the rest of the history.
		if err := json.Unmarshal([]byte(l), &r); err != nil {
			continue
		}
		out = append(out, r)
	}
	return out
}
