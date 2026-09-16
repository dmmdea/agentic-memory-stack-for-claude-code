package lint

import (
	"context"
	"path/filepath"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	amsync "github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/sync"
)

// SummaryFile is the artifact the SessionStart banner reads.
const SummaryFile = "lint-summary.json"

// Counts is the summary's counts block. The JSON names are the ones
// claude-config/storage-cap-check.sh already reads; renaming one silently empties the
// banner rather than breaking it, which is the worst kind of change.
type Counts struct {
	Total             int `json:"total"`
	Orphan            int `json:"orphan"`
	Dangling          int `json:"dangling"`
	DupSlug           int `json:"dup_slug"`
	LongLine          int `json:"long_line"`
	Oversized         int `json:"oversized"`
	OverBudget        int `json:"over_budget"`
	ScanError         int `json:"scan_error"`
	Starved           int `json:"starved"`
	Actionable        int `json:"actionable"`
	Resurrected       int `json:"resurrected"`
	ConflictInHistory int `json:"conflict_in_history"`
	OverInjectLimit   int `json:"over_inject_limit"`
}

// Summary is lint-summary.json.
type Summary struct {
	// GeneratedAt is WHOLE SECONDS in Zulu, never RFC3339Nano.
	//
	// The 'o'/nano format emits seven fractional digits, which Python 3.10's
	// fromisoformat rejects - and the banner's staleness guard runs under exactly that
	// runtime, so a nano stamp would make the guard inert and present a week-old summary
	// as this morning's truth.
	GeneratedAt         string     `json:"generated_at"`
	Stores              []StoreRow `json:"stores"`
	Findings            []Finding  `json:"findings"`
	Counts              Counts     `json:"counts"`
	LastReceiptAgeHours *float64   `json:"last_receipt_age_hours"`
}

// SummaryTimeFormat is the whole-second Zulu layout.
const SummaryTimeFormat = "2006-01-02T15:04:05Z"

// SummaryPath is the artifact's path under a state root.
func SummaryPath(stateRoot string) string { return filepath.Join(stateRoot, SummaryFile) }

// Run scans every canonical store and builds the summary. It never mutates a store.
//
// It also never fails the caller: a store that cannot be read becomes a scan-error
// finding - which IS actionable, so an unreadable store reaches the banner instead of
// disappearing from it - and the run continues with the rest of the fleet.
func Run(ctx context.Context, opt Options) (Summary, error) {
	now := opt.Now
	if now.IsZero() {
		now = time.Now()
	}

	stores, _, err := store.Enumerate(opt.Roots.ProjectsRoot)
	if err != nil {
		return Summary{}, err
	}

	stamps, stampErr := ReadStamps(opt.Roots.ProjectsRoot)
	receiptPath := filepath.Join(opt.Roots.StateRoot, ReceiptFile)
	receiptAge := FileAgeHours(receiptPath, now)

	findings := []Finding{}
	rows := []StoreRow{}
	overTriggerCount := 0

	want := map[string]bool{}
	for _, w := range opt.Workspaces {
		want[w] = true
	}

	for _, s := range stores {
		if s.IsAlias {
			continue // an alias is the same physical store; reporting it twice doubles every finding
		}
		if len(want) > 0 && !want[s.Workspace] {
			continue
		}
		stats, err := MeasureStore(s)
		if err != nil {
			findings = append(findings, Finding{
				Store: s.Workspace, Kind: KindScanError, File: store.IndexName, Detail: err.Error(),
			})
			rows = append(rows, StoreRow{Workspace: s.Workspace})
			continue
		}
		f, err := StoreFindings(s)
		if err != nil {
			findings = append(findings, Finding{
				Store: s.Workspace, Kind: KindScanError, File: store.IndexName, Detail: err.Error(),
			})
		} else {
			findings = append(findings, f...)
		}

		h := ReadRunHistory(receiptPath, s.Workspace)
		row := StoreRow{
			Workspace:        s.Workspace,
			Bytes:            stats.Bytes,
			Lines:            stats.Lines,
			Entries:          stats.Entries,
			Files:            stats.Files,
			OverTrigger:      stats.OverTrigger,
			SkipStreak:       h.SkipStreak,
			LastStatus:       h.LastStatus,
			OverTriggerHours: stamps.HoursOverTrigger(s.Workspace, now),
		}
		rows = append(rows, row)
		if stats.OverTrigger {
			overTriggerCount++
			findings = append(findings, StarvationFindings(row, h, now)...)
			if bad, statuses := Unproductive(receiptPath, s.Workspace); bad {
				findings = append(findings, Finding{
					Store: s.Workspace, Kind: KindUnproductive, File: store.IndexName,
					Detail: "above trigger and the last 3 runs all ended: " + join(statuses, ", "),
				})
			}
		}
	}

	if stampErr != nil {
		findings = append(findings, Finding{
			Store: "(fleet)", Kind: KindScanError, File: filepath.Join(StampDir, StampFile),
			Detail: stampErr.Error(),
		})
	}

	findings = append(findings, SilentFinding(opt.NightlyUnit, overTriggerCount, receiptAge)...)
	findings = append(findings, RemoteFindings(ctx, amsync.NewRepo(opt.Roots), opt.Policy)...)
	findings = append(findings, MergeFindings(opt.Roots.StateRoot, time.Time{})...)

	sortFindings(findings)
	return Summary{
		GeneratedAt:         now.UTC().Format(SummaryTimeFormat),
		Stores:              rows,
		Findings:            findings,
		Counts:              count(findings),
		LastReceiptAgeHours: receiptAge,
	}, nil
}

// Write persists the summary through the atomic writer.
func Write(stateRoot string, s Summary) error {
	return atomic.WriteJSONFile(SummaryPath(stateRoot), s)
}

func count(f []Finding) Counts {
	c := Counts{Total: len(f)}
	for _, x := range f {
		switch x.Kind {
		case KindOrphan:
			c.Orphan++
		case KindDangling:
			c.Dangling++
		case KindDupSlug:
			c.DupSlug++
		case KindLongLine:
			c.LongLine++
		case KindOversizedFile:
			c.Oversized++
		case KindScanError:
			c.ScanError++
		case KindStarved:
			c.Starved++
		case KindResurrected:
			c.Resurrected++
		case KindConflictInHist:
			c.ConflictInHistory++
		}
		if x.Kind == KindOverSyncLimit || x.Kind == KindOverInjectCap {
			c.OverBudget++
		}
		if x.Kind == KindOverInjectCap {
			c.OverInjectLimit++
		}
		if Actionable(x.Kind) {
			c.Actionable++
		}
	}
	return c
}

func join(parts []string, sep string) string {
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += sep
		}
		out += p
	}
	return out
}
