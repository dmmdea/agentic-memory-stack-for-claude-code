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
