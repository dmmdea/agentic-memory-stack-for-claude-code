package lint

import (
	"context"
	"fmt"
	"os"
	"sort"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	amsync "github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/sync"
)

// Finding kinds.
const (
	KindOrphan         = "orphan"
	KindDangling       = "dangling"
	KindDupSlug        = "dup-slug"
	KindLongLine       = "long-line"
	KindOversizedFile  = "oversized-file"
	KindNoFrontmatter  = "no-frontmatter"
	KindNearBudget     = "near-budget"
	KindOverSyncLimit  = "over-sync-limit"
	KindOverInjectCap  = "over-inject-limit"
	KindScanError      = "scan-error"
	KindStarved        = "compactor-starved"
	KindSilent         = "compactor-silent"
	KindUnproductive   = "compactor-unproductive"
	KindHistoryRemote  = "history-remote"
	KindResurrected    = "resurrected"
	KindConflictInHist = "conflict-in-history"
)

// actionableKinds is what reaches the session-start banner (LINT:146, plus the two v2
// merge findings).
//
// Membership is not cosmetic: the banner renders ONLY these kinds, so a finding left out
// of this set reaches no surface at all. That is how a store that could not be read once
// became completely invisible - scan-error was missing from the list.
var actionableKinds = map[string]bool{
	KindOrphan:         true,
	KindDangling:       true,
	KindDupSlug:        true,
	KindOverSyncLimit:  true,
	KindOverInjectCap:  true,
	KindSilent:         true,
	KindUnproductive:   true,
	KindStarved:        true,
	KindHistoryRemote:  true,
	KindScanError:      true,
	KindResurrected:    true,
	KindConflictInHist: true,
}

// Actionable reports whether a kind reaches the banner.
func Actionable(kind string) bool { return actionableKinds[kind] }

// Finding is one lint result.
type Finding struct {
	Store  string `json:"store"`
	Kind   string `json:"kind"`
	File   string `json:"file"`
	Detail string `json:"detail"`
}

// Stats is one store's measured shape.
type Stats struct {
	Bytes       int
	Lines       int
	Entries     int
	Files       int
	OverTrigger bool
}

// Options configures a lint run.
type Options struct {
	Roots store.Roots
	// NightlyUnit is what compactor-silent NAMES when it fires (decision Q10). On a PC
	// it is EMPTY and the finding does not exist at all, because there is no local
	// nightly to be silent; on the hub it is the systemd timer's unit name. Hard-coding
	// a Windows scheduled-task name here is what the Linux port could not carry.
	NightlyUnit string
	// Policy is the remote policy the history-remote rule applies (the Y3 amendment).
	Policy amsync.RemotePolicy
	// Now is the injected clock.
	Now time.Time
}

// StoreRow is one row of the summary's stores[].
type StoreRow struct {
	Workspace        string   `json:"workspace"`
	Bytes            int      `json:"bytes"`
	Lines            int      `json:"lines"`
	Entries          int      `json:"entries"`
	Files            int      `json:"files"`
	OverTrigger      bool     `json:"over_trigger"`
	SkipStreak       int      `json:"skip_streak"`
	LastStatus       string   `json:"last_status,omitempty"`
	OverTriggerHours *float64 `json:"over_trigger_hours"`
}

// MeasureStore reads a store and returns its stats.
//
// Fact-file enumeration is fail-closed: an unreadable directory is an ERROR, never an
// empty set. When the two collapsed, a caller comparing the index against that empty set
// concluded every line was dangling, every downstream guard agreed, and the whole index
// was wiped with a receipt reporting success.
func MeasureStore(s store.Store) (Stats, error) {
	data, err := os.ReadFile(s.IndexPath)
	if err != nil {
		return Stats{}, fmt.Errorf("read %s: %w", s.IndexPath, err)
	}
	files, err := store.FactFiles(s.Dir)
	if err != nil {
		return Stats{}, err
	}
	ix := index.Parse(string(data))
	lines := ix.LineCount()
	return Stats{
		Bytes:       len(data),
		Lines:       lines,
		Entries:     len(ix.Entries()),
		Files:       len(files),
		OverTrigger: len(data) >= store.TriggerBytes || lines >= store.TriggerLines,
	}, nil
}

// StoreFindings is the stateless per-store rule set (LIB:437-454). Every finding is
// recomputed from disk; nothing here is remembered between runs.
func StoreFindings(s store.Store) ([]Finding, error) {
	data, err := os.ReadFile(s.IndexPath)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", s.IndexPath, err)
	}
	files, err := store.FactFiles(s.Dir)
	if err != nil {
		return nil, err
	}
	ix := index.Parse(string(data))

	onDisk := make(map[string]bool, len(files))
	for _, f := range files {
		onDisk[f.Name] = true
	}
	// Reachability is decided by ANY link on ANY line, not only by parsed entries. A line
	// whose title contains "]" does not match the entry regex, so counting only entries
	// reported its file as an ORPHAN - and the compactor then appended a SECOND pointer
	// to a file that was already indexed, growing a store it exists to shrink.
	linked := index.LinkedSlugs(ix.Records)

	var out []Finding
	add := func(kind, file, detail string) {
		out = append(out, Finding{Store: s.Workspace, Kind: kind, File: file, Detail: detail})
	}

	seen := map[string]bool{}
	for _, e := range ix.Entries() {
		if seen[e.Slug] {
			add(KindDupSlug, e.Slug, fmt.Sprintf("linked again at line %d", e.Index+1))
		} else {
			seen[e.Slug] = true
		}
		if !onDisk[e.Slug] {
			add(KindDangling, e.Slug, fmt.Sprintf("index line %d points at a missing file", e.Index+1))
		}
		if e.Bytes > store.LineByteCap {
			add(KindLongLine, e.Slug, fmt.Sprintf("%d B (cap %d)", e.Bytes, store.LineByteCap))
		}
	}
	for _, f := range files {
		if !linked[f.Name] {
			add(KindOrphan, f.Name, "on disk but not indexed (never loaded)")
		}
		if f.Size >= store.OversizedFactBytes {
			add(KindOversizedFile, f.Name, fmt.Sprintf("%d B (flag only; bodies are never edited)", f.Size))
		}
		if frontmatter.ParseFile(f.Path) == nil {
			add(KindNoFrontmatter, f.Name, "no leading --- block")
		}
	}

	bytes := len(data)
	lines := ix.LineCount()
	// Byte and line budgets are independent, but WITHIN each the worse finding wins: a
	// store told it is over the sync limit does not also need to hear it is near budget.
	if bytes >= store.SyncLimitBytes {
		add(KindOverSyncLimit, store.IndexName, fmt.Sprintf("%d B >= %d (harness refuses to sync)", bytes, store.SyncLimitBytes))
	} else if bytes >= store.TriggerBytes {
		add(KindNearBudget, store.IndexName, fmt.Sprintf("%d B >= trigger %d", bytes, store.TriggerBytes))
	}
	if lines >= store.InjectLimitLines {
		add(KindOverInjectCap, store.IndexName, fmt.Sprintf("%d lines >= %d (tail not injected)", lines, store.InjectLimitLines))
	} else if lines >= store.TriggerLines {
		add(KindNearBudget, store.IndexName, fmt.Sprintf("%d lines >= trigger %d", lines, store.TriggerLines))
	}
	return out, nil
}

// StarvationFindings applies the per-store starvation rules to an already-measured store.
//
// Rule (a) is LINT:60-76: two consecutive live-session skips above trigger, or ONE skip
// on a store already at the sync limit. Rule (b) is the G7 metric (DESIGN:257-258): hours
// over trigger without an applied DECISION. They are one finding kind on purpose - the
// banner renders kinds, and a new kind nobody taught it about reaches no surface.
func StarvationFindings(row StoreRow, h RunHistory, now time.Time) []Finding {
	if !row.OverTrigger {
		return nil
	}
	atLimit := row.Bytes >= store.SyncLimitBytes
	limitNote := " (above trigger)"
	if atLimit {
		limitNote = fmt.Sprintf(" >= the %d B sync limit (harness refuses to sync)", store.SyncLimitBytes)
	}

	if h.SkipStreak >= 2 || (atLimit && h.LastStatus == SkipLiveSession) {
		return []Finding{{
			Store: row.Workspace, Kind: KindStarved, File: store.IndexName,
			Detail: fmt.Sprintf("skipped as live-session %d run(s) in a row while %d B%s - the nightly is running, this store is not getting it",
				h.SkipStreak, row.Bytes, limitNote),
		}}
	}

	// G7: a skip is not a decision. Receipt age says the maintainer ran; this says the
	// store got better, and only the second one ends starvation.
	decided := h.LastProductiveUTC != nil && now.UTC().Sub(*h.LastProductiveUTC) < AlarmHours*time.Hour
	if decided {
		return nil
	}
	if row.OverTriggerHours != nil && *row.OverTriggerHours < AlarmHours {
		return nil
	}
	last := "never"
	if h.LastProductiveUTC != nil {
		last = fmt.Sprintf("%.1fh ago", now.UTC().Sub(*h.LastProductiveUTC).Hours())
	}
	over := "unknown"
	if row.OverTriggerHours != nil {
		over = fmt.Sprintf("%.1fh", *row.OverTriggerHours)
	}
	return []Finding{{
		Store: row.Workspace, Kind: KindStarved, File: store.IndexName,
		Detail: fmt.Sprintf("%d B%s, over trigger for %s with no applied decision (last: %s)",
			row.Bytes, limitNote, over, last),
	}}
}

// SilentFinding is the watch-the-watcher rule (LINT:55-59, :77-84), parameterised per
// decision Q10.
//
// On a PC it does not exist: the nightly was REMOVED from the PCs, so there is nothing
// local to be silent, and a finding naming a job that should not be running is noise that
// teaches the operator to ignore the banner. On the hub it names the timer that owns the
// nightly chain.
func SilentFinding(nightlyUnit string, overTriggerCount int, receiptAgeHours *float64) []Finding {
	if nightlyUnit == "" || overTriggerCount == 0 {
		return nil
	}
	if receiptAgeHours != nil && *receiptAgeHours <= 48 {
		return nil
	}
	age := "never"
	if receiptAgeHours != nil {
		age = fmt.Sprintf("%.1fh ago", *receiptAgeHours)
	}
	return []Finding{{
		Store: "(fleet)", Kind: KindSilent, File: nightlyUnit,
		Detail: fmt.Sprintf("%d store(s) above trigger, last maintenance receipt: %s", overTriggerCount, age),
	}}
}

// RemoteFindings is the Y3-amended history-remote rule (DESIGN:170, blueprint 7.2).
//
// Before Y3 ANY remote was a finding, because a push would publish stores that hold
// credentials and private brand facts. Y3 narrows that to: exactly one remote, named hub,
// over SSH, to the tailnet MagicDNS name. Everything else is still a finding, and the
// detail says which rule the remote broke so the operator does not have to guess.
func RemoteFindings(ctx context.Context, repo amsync.Repo, p amsync.RemotePolicy) []Finding {
	checks, err := p.Check(ctx, repo)
	if err != nil {
		return []Finding{{
			Store: "(fleet)", Kind: KindScanError, File: "history.git",
			Detail: "could not read the history repo's remotes: " + err.Error(),
		}}
	}
	var out []Finding
	for _, c := range checks {
		if c.OK {
			continue
		}
		out = append(out, Finding{
			Store: "(fleet)", Kind: KindHistoryRemote, File: "history.git",
			Detail: fmt.Sprintf("remote %q: %s", c.Name, c.Reason),
		})
	}
	return out
}

// MergeFindings turns what the merge engine reported in the sync receipts into findings
// (DESIGN:209, :217).
//
// Both are ACTIONABLE. A deliberate deletion that came back is a decision a human makes,
// not the tool; a body conflict's loser is still in history and only a human can decide
// whether to recover it - which is why the finding carries the commit id rather than just
// saying a conflict happened.
func MergeFindings(stateRoot string, since time.Time) []Finding {
	rows, err := amsync.ReadReceipts(amsync.ReceiptPath(stateRoot), TailLines)
	if err != nil || len(rows) == 0 {
		return nil
	}
	var out []Finding
	for _, r := range rows {
		if !since.IsZero() && r.TS.Before(since) {
			continue
		}
		for _, p := range r.Resurrected {
			out = append(out, Finding{
				Store: workspaceOf(p), Kind: KindResurrected, File: p,
				Detail: "kept by the modify/delete rule: one side edited it while the other deleted it",
			})
		}
		for _, c := range r.ConflictsInHistory {
			out = append(out, Finding{
				Store: workspaceOf(c.Path), Kind: KindConflictInHist, File: c.Path,
				Detail: fmt.Sprintf("a body conflict was resolved; the losing side is commit %s", c.Commit),
			})
		}
	}
	return out
}

// workspaceOf extracts the workspace slug from a work-tree-relative path.
func workspaceOf(p string) string {
	for i := 0; i < len(p); i++ {
		if p[i] == '/' || p[i] == '\\' {
			return p[:i]
		}
	}
	return p
}

// sortFindings gives the summary a stable order so two runs over an unchanged fleet
// produce byte-identical output and a diff of the file means something.
func sortFindings(f []Finding) {
	sort.SliceStable(f, func(i, j int) bool {
		if f[i].Store != f[j].Store {
			return f[i].Store < f[j].Store
		}
		if f[i].Kind != f[j].Kind {
			return f[i].Kind < f[j].Kind
		}
		return f[i].File < f[j].File
	})
}
