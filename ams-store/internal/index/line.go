package index

import "github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"

// EntryLine constructs a canonical index line (LIB:271-281). The em-dash separator is
// appended ONLY when the summary is non-empty, so a bare pointer stays a bare pointer.
func EntryLine(title, slug, summary, indent string) string {
	line := indent + "- [" + title + "](" + slug + ")"
	if summary != "" {
		line += " " + store.EmDash + " " + summary
	}
	return line
}

// RecordLine rebuilds an entry record's line with a new summary, keeping the marker the
// record carries. Every caller that rewrites an existing entry goes through here, so a
// shortened or re-rendered line never loses its decoration.
func RecordLine(r *Record, summary string) string {
	line := r.Indent + "- " + r.Prefix + "[" + r.Title + "](" + r.Slug + ")"
	if summary != "" {
		line += " " + store.EmDash + " " + summary
	}
	return line
}
