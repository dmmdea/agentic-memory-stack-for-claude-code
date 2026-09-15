package judge

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// Meta is everything the guards need to know about one indexed slug.
type Meta struct {
	Slug string
	FM   *frontmatter.Frontmatter
	// Doctrine is the five-way hard rule, resolved ONCE per slug at load time and
	// re-checked at apply time. A nil FM still yields a verdict: the summary-based
	// imperative and attributed tests apply to a file that could not be read.
	Doctrine bool
}

// State is one store as the judge sees it: the index verbatim, the fact files on disk,
// and the doctrine verdict per slug.
type State struct {
	Workspace string
	Dir       string
	IndexPath string
	Text      string
	Index     *index.Index
	Files     []store.FactFile
	OnDisk    map[string]bool
	Meta      map[string]*Meta
}

// Load reads a store.
//
// Fact-file enumeration FAILS CLOSED (LIB:188-201): an unreadable directory returns an
// error, never an empty set. With the two collapsed, a caller comparing the index against
// "nothing there" concludes every line is dangling, every downstream guard agrees, and
// the whole index is wiped behind a receipt reporting success.
func Load(dir, workspace string) (*State, error) {
	indexPath := filepath.Join(dir, store.IndexName)
	b, err := os.ReadFile(indexPath)
	if err != nil {
		return nil, fmt.Errorf("read index %s: %w", indexPath, err)
	}
	files, err := store.FactFiles(dir)
	if err != nil {
		return nil, err
	}
	st := &State{
		Workspace: workspace,
		Dir:       dir,
		IndexPath: indexPath,
		Text:      string(b),
		Index:     index.Parse(string(b)),
		Files:     files,
		OnDisk:    make(map[string]bool, len(files)),
		Meta:      map[string]*Meta{},
	}
	for _, f := range files {
		st.OnDisk[f.Name] = true
	}
	for _, r := range st.Index.Entries() {
		if _, seen := st.Meta[r.Slug]; seen {
			continue
		}
		var fm *frontmatter.Frontmatter
		if st.OnDisk[r.Slug] {
			fm = frontmatter.ParseFile(filepath.Join(dir, r.Slug))
		}
		st.Meta[r.Slug] = &Meta{
			Slug:     r.Slug,
			FM:       fm,
			Doctrine: frontmatter.IsDoctrine(r.Summary, fm),
		}
	}
	return st, nil
}

// FileNames is every fact file currently on disk, for the compare-and-swap check.
func (s *State) FileNames() []string {
	out := make([]string, 0, len(s.Files))
	for _, f := range s.Files {
		out = append(out, f.Name)
	}
	return out
}

// Candidate is one entry offered to the judge.
type Candidate struct {
	Slug        string
	Title       string
	Hook        string
	Type        string
	Description string
	Bytes       int
}

// CandidateSet is the delta a judge call is allowed to see.
type CandidateSet struct {
	// Shorten is every over-cap line that may be rewritten.
	Shorten []Candidate
	// Migrate is every pullable fact that may be moved to the corpus.
	Migrate []Candidate
}

// Candidates computes what this store offers the judge.
//
// It is the OFFER surface, and it is half of two guards, not a convenience: doctrine is
// never offered (so no plan can even name it), and a sealed line is never offered again
// (one judge rewrite per line, ever). Both facts are asserted by tests that read this
// set, because a guard that only rejects at apply time still lets the model spend a
// night's attempt re-deciding a line it may not touch.
//
// It lives beside the apply guards on purpose: the Python chain reads this set to build
// its prompt, so the filter that decides what may be judged and the filter that decides
// what may be applied are one implementation.
func (s *State) Candidates(seal Seal) CandidateSet {
	var cs CandidateSet
	for _, r := range s.Index.Entries() {
		m := s.Meta[r.Slug]
		if m == nil || m.Doctrine {
			continue
		}
		if !s.OnDisk[r.Slug] {
			continue
		}
		c := Candidate{Slug: r.Slug, Title: r.Title, Hook: r.Summary, Bytes: r.Bytes}
		if m.FM != nil {
			c.Type = m.FM.Type
			c.Description = m.FM.Description
		}
		if r.Bytes > store.LineByteCap && !seal.Sealed(r.Slug) {
			cs.Shorten = append(cs.Shorten, c)
		}
		if IsMigratable(m) {
			cs.Migrate = append(cs.Migrate, c)
		}
	}
	return cs
}

// IsMigratable reports whether a fact may be moved to the corpus at all.
//
// Three conditions, each earned:
//   - not doctrine - a standing order is needed BEFORE the agent knows to ask for it,
//     so moving it out of every session prompt is exactly what must never happen;
//   - typed project or reference - a pullable lookup, not a judgement;
//   - a body within the server's storage cap - a larger body is rejected by the server
//     on every attempt, nightly, forever ("returned no id; line kept", three nights
//     running, until someone read the log).
func IsMigratable(m *Meta) bool {
	if m == nil || m.Doctrine || m.FM == nil {
		return false
	}
	switch strings.ToLower(m.FM.Type) {
	case "project", "reference":
	default:
		return false
	}
	if m.FM.Body == "" {
		return false
	}
	return !TooLargeToMigrate(MigrationText(m.FM.Description, m.FM.Body))
}
