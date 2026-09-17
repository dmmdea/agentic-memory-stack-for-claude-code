package derive

import (
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// Meta is the per-slug classification derive computes once, before anything is rendered or
// truncated, and re-uses everywhere doctrine is asked about (LIB's $meta cache).
type Meta struct {
	FM       *frontmatter.Frontmatter
	Doctrine bool
}

// HygieneInput is one store's material for the hygiene passes.
type HygieneInput struct {
	Records  []*index.Record
	StoreDir string
	OnDisk   map[string]bool
	Files    []string
	Meta     map[string]*Meta
	// ResolveHook answers "what hook does this kept entry carry after hygiene". It runs
	// BEFORE the dead-extra-link repair, because the hook a harvested file carries is a
	// copy of the index line's - dead link included - and repairing the line before
	// resolving the hook would put the dead link straight back.
	ResolveHook func(rec *index.Record) string
	// ProjectBytes renders a candidate record set and returns its byte size. The orphan
	// pass measures against it, so the headroom rule is measured on the file that will be
	// written rather than on the one that was read.
	ProjectBytes func(records []*index.Record) int
	Log          func(string)
}

func (in HygieneInput) logf(msg string) {
	if in.Log != nil {
		in.Log(msg)
	}
}

// HygieneResult is what the four passes did.
type HygieneResult struct {
	Keep          []*index.Record
	Dedangled     int
	DedupSlug     int
	Reindexed     int
	LeftUnindexed []string
	Note          string
	// Dangling names the entries pass B dropped because their file is gone - the
	// LINE removals, as opposed to the dead-extra-link repairs Dedangled also counts.
	// The blast cap reads it to tell a wipe in progress from the judge's migrations
	// arriving: a slug the history says was migrated is not counted against the cap.
	Dangling []string
}

var (
	// reLeftoverParens, reMultiSpace, reTrailingAnd and reTrailingComma tidy what removing
	// a dead link leaves behind: "see also [x](a.md) and [y](gone.md)" must not become
	// "see also [x](a.md) and".
	reLeftoverParens = regexp.MustCompile(`\(\s*(?:also|see also|see|and)?\s*\)`)
	reMultiSpace     = regexp.MustCompile(`\s{2,}`)
	reTrailingAnd    = regexp.MustCompile(`\s+and\s*$`)
	reTrailingComma  = regexp.MustCompile(`\s*,\s*$`)
	reBracketParen   = regexp.MustCompile(`[\[\]()]`)
	reEOL            = regexp.MustCompile(`\r?\n`)
	reMarkdownLink   = regexp.MustCompile(`\[([^\]]*)\]\([^)]*\)`)
	reListPrefix     = regexp.MustCompile(`^[-*]\s+`)
	reWhitespaceRun  = regexp.MustCompile(`\s+`)
	reSafeID         = regexp.MustCompile(`^[A-Za-z0-9_.:@+-]+$`)
)

// BlastCap bounds one hygiene pass's removals: max(1, floor(entries * 0.2)) (COMPACT:548).
// The floor of 1 keeps a four-entry store repairable; the cap keeps a mass-dangling index
// reported rather than silently gutted, because the one removal loop that can empty an
// entire index used to be unbounded.
func BlastCap(entryCount int) int {
	c := int(math.Floor(float64(entryCount) * store.BlastCapFraction))
	if c < 1 {
		return 1
	}
	return c
}

// Hygiene runs passes A (duplicate slug), B (dangling removal, with the ambiguity
// exemption), the hook resolution, and C (dead extra-link repair) over the parsed records
// in order. Non-entry lines are carried through untouched, verbatim.
func Hygiene(in HygieneInput) *HygieneResult {
	res := &HygieneResult{Keep: make([]*index.Record, 0, len(in.Records))}
	seen := make(map[string]bool, len(in.Records))

	for _, r := range in.Records {
		if r.Kind != index.KindEntry {
			res.Keep = append(res.Keep, r)
			continue
		}
		// A - the second and later pointer at one slug is duplication.
		if seen[r.Slug] {
			res.DedupSlug++
			continue
		}
		// B - an entry whose file is not on disk points nowhere.
		if !in.OnDisk[r.Slug] {
			// A title containing "]" is an ambiguous shape: "- [x] task with [link](f.md)"
			// is a checkbox, not a pointer. Such a line still counts for reachability, but
			// the DESTRUCTIVE step is reserved for the unambiguous form.
			if strings.Contains(r.Title, "]") {
				in.logf("ambiguous line left alone (bracketed title, missing target " + r.Slug + ")")
				res.Keep = append(res.Keep, r)
				continue
			}
			res.Dedangled++
			res.Dangling = append(res.Dangling, r.Slug)
			continue
		}
		seen[r.Slug] = true

		if in.ResolveHook != nil {
			applyHook(r, in.ResolveHook(r))
		}

		// C - a second link on a kept line pointing at a missing file is a ghost the
		// post-write invariant fails on forever while hygiene never fixes it: the index
		// would be restored every night and every run discarded.
		repairDeadExtras(in, res, r)

		res.Keep = append(res.Keep, r)
	}
	return res
}

// applyHook re-points a record at the hook derive resolved for it. A candidate line that
// does not parse back as exactly this entry is refused and the record keeps what it had -
// a hook that injects a phantom second slug is a ghost nothing can repair.
func applyHook(r *index.Record, hook string) {
	if hook == r.Summary {
		return
	}
	line := index.EntryLine(r.Title, r.Slug, hook, r.Indent)
	parsed := index.Parse(line).Entries()
	if len(parsed) != 1 || parsed[0].Slug != r.Slug {
		return
	}
	r.Summary = parsed[0].Summary
	r.ExtraSlugs = parsed[0].ExtraSlugs
	r.Bytes = index.ByteCount(line)
	r.Dirty = true
}

func repairDeadExtras(in HygieneInput, res *HygieneResult, r *index.Record) {
	var dead, live []string
	for _, e := range r.ExtraSlugs {
		if in.OnDisk[e] {
			live = append(live, e)
			continue
		}
		dead = append(dead, e)
	}
	if len(dead) == 0 {
		return
	}
	summary := r.Summary
	for _, dx := range dead {
		summary = regexp.MustCompile(`\s*\[[^\]]*\]\(`+regexp.QuoteMeta(dx)+`\)`).ReplaceAllString(summary, "")
	}
	summary = reMultiSpace.ReplaceAllString(reLeftoverParens.ReplaceAllString(summary, ""), " ")
	summary = reTrailingComma.ReplaceAllString(reTrailingAnd.ReplaceAllString(summary, ""), "")
	summary = strings.TrimSpace(summary)

	candidate := index.EntryLine(r.Title, r.Slug, summary, r.Indent)
	// Set EQUALITY on the extras, never subset: a repair that drops a DEAD extra link must
	// keep a LIVE one, or the repair is rejected and the dead link stays forever.
	if !index.LineRoundTrips(candidate, r.Slug, live) {
		in.logf("could not repair dead extra link(s) on " + r.Slug + " (" + strings.Join(dead, ",") + "); left as is")
		return
	}
	r.Summary = summary
	r.ExtraSlugs = live
	r.Bytes = index.ByteCount(candidate)
	r.Dirty = true
	res.Dedangled++
}

// ReindexOrphans is hygiene pass D: a fact on disk that nothing links to is invisible to
// every session, so it gets a pointer back.
//
// Link accounting runs over EVERY line, parsed entry or not (index.LinkedSlugs): treating
// only entries as linked reported an indexed file as an ORPHAN and appended a SECOND
// pointer to it, growing a store the maintainer exists to shrink.
func ReindexOrphans(in HygieneInput, res *HygieneResult) {
	linked := index.LinkedSlugs(res.Keep)
	running := 0
	if in.ProjectBytes != nil {
		running = in.ProjectBytes(res.Keep)
	}

	for _, name := range in.Files {
		if linked[name] {
			continue
		}
		path := filepath.Join(in.StoreDir, name)
		fm := frontmatter.ParseFile(path)

		title := strings.TrimSuffix(name, filepath.Ext(name))
		if fm != nil && fm.Name != "" {
			title = fm.Name
		}
		hook := ""
		switch {
		case fm != nil && fm.Hook != "":
			hook = fm.Hook
		case fm != nil && fm.Description != "":
			hook = fm.Description
		default:
			// No frontmatter at all (a session re-created a migrated slug with only its
			// addendum): the hook is the file's first line of prose, never a placeholder
			// that tells the reader nothing.
			hook = SynthesizedHook(path, fm)
		}
		hook = TruncateToBytes(hook, store.OrphanHookBudget)

		line := index.EntryLine(title, name, hook, "")
		if !index.LineRoundTrips(line, name, nil) {
			// A title containing "]" or a hook containing a link regenerates a line that
			// does not parse back, so the file reads as an orphan again next run, forever.
			safeTitle := strings.TrimSpace(reBracketParen.ReplaceAllString(title, " "))
			if safeTitle == "" {
				safeTitle = strings.TrimSuffix(name, filepath.Ext(name))
			}
			safeHook := strings.TrimSpace(reBracketParen.ReplaceAllString(hook, " "))
			title, hook = safeTitle, safeHook
			line = index.EntryLine(title, name, hook, "")
			if !index.LineRoundTrips(line, name, nil) {
				in.logf("cannot build a parseable index line for orphan " + name + "; left unindexed")
				res.LeftUnindexed = append(res.LeftUnindexed, name)
				continue
			}
		}

		bytes := index.ByteCount(line)
		// Re-indexing is the ONE hygiene action that adds bytes. It must never be the
		// reason a store crosses the hard sync limit - that is the exact failure this
		// system exists to prevent, and causing it while repairing something else would be
		// perverse.
		if running+bytes+1 >= store.SyncLimitBytes {
			in.logf("orphan " + name + " left unindexed - re-indexing it would push the index over the sync limit")
			res.Note = "one or more orphans left unindexed: no byte headroom (compact first)"
			res.LeftUnindexed = append(res.LeftUnindexed, name)
			continue
		}
		running += bytes + 1

		rec := &index.Record{
			Index: -1, Kind: index.KindEntry,
			// Raw carries the CONSTRUCTED line so a receipt row for this entry can never
			// be blank: the audit trail must be able to say how the index read before a
			// later removal.
			Raw: line, Title: title, Slug: name, Summary: hook,
			ExtraSlugs: []string{}, Bytes: bytes, Dirty: true,
		}
		res.Keep = append(res.Keep, rec)
		if in.Meta != nil {
			in.Meta[name] = &Meta{FM: fm, Doctrine: frontmatter.IsDoctrine(hook, fm)}
		}
		res.Reindexed++
	}
}

// SynthesizedHook builds a hook for a fact file that carries no usable description
// (LIB:599-616): the first real line of prose, with emphasis, backticks, list markers and
// LINKS stripped. The link strip is the load-bearing part - a link left in the prose would
// become a second slug on the constructed index line, i.e. a ghost hygiene cannot repair.
func SynthesizedHook(path string, fm *frontmatter.Frontmatter) string {
	text := ""
	if fm != nil {
		text = fm.Body
	} else if b, err := os.ReadFile(path); err == nil {
		text = string(b)
	}
	for _, line := range reEOL.Split(text, -1) {
		t := strings.TrimSpace(line)
		if t == "" {
			continue
		}
		if strings.HasPrefix(t, "#") || strings.HasPrefix(t, "---") || strings.HasPrefix(t, "|") ||
			strings.HasPrefix(t, "```") || strings.HasPrefix(t, "<") {
			continue
		}
		t = reMarkdownLink.ReplaceAllString(t, "${1}")
		t = strings.ReplaceAll(t, "**", "")
		t = strings.ReplaceAll(t, "`", "")
		t = reListPrefix.ReplaceAllString(t, "")
		t = reWhitespaceRun.ReplaceAllString(t, " ")
		if t = strings.TrimSpace(t); t != "" {
			return t
		}
	}
	return "recovered orphan; no description"
}
