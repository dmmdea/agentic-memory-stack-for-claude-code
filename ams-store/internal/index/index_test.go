package index_test

import (
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/index"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

const em = store.EmDash

// --------------------------------------------------------------- byte-exact I/O
// MemoryStoreLib.Tests.ps1:38

func TestIO_ByteCountNotCharCount(t *testing.T) {
	if got := index.ByteCount("a" + em + "b"); got != 5 {
		t.Fatalf("ByteCount(a<emdash>b) = %d, want 5 (an em-dash is 3 UTF-8 bytes)", got)
	}
}

// MemoryStoreLib.Tests.ps1:42. The legacy prevailing-newline rule. derive always writes
// LF (blueprint section 3.4), but the rule still governs every read path, so it is
// ported rather than dropped.
func TestIO_PrevailingNewlineCRLF(t *testing.T) {
	if got := index.Newline("a\r\nb"); got != "\r\n" {
		t.Errorf("Newline(CRLF text) = %q, want CRLF", got)
	}
	if got := index.Newline("a\nb"); got != "\n" {
		t.Errorf("Newline(LF text) = %q, want LF", got)
	}
	if got := index.Newline(""); got != "\n" {
		t.Errorf("Newline(empty) = %q, want LF", got)
	}
	// Majority, not "contains": one stray CRLF in a mostly-LF index must not rewrite
	// every line the job never touched.
	if got := index.Newline("a\r\nb\nc\nd\ne\nf"); got != "\n" {
		t.Errorf("Newline(one stray CRLF among five LF) = %q, want LF", got)
	}
}

// --------------------------------------------------------------- index parsing
// MemoryStoreLib.Tests.ps1:49

func TestIndex_ParseAndRegenerateVerbatim(t *testing.T) {
	text := "# Memory Index\n\n- [A title](a.md) " + em + " hook one\n- [B](b.md)\nnot an entry\n"
	ix := index.Parse(text)
	entries := ix.Entries()
	if len(entries) != 2 {
		t.Fatalf("entries = %d, want 2", len(entries))
	}
	if entries[0].Slug != "a.md" {
		t.Errorf("entries[0].Slug = %q, want a.md", entries[0].Slug)
	}
	if entries[0].Summary != "hook one" {
		t.Errorf("entries[0].Summary = %q, want %q", entries[0].Summary, "hook one")
	}
	if entries[1].Summary != "" {
		t.Errorf("entries[1].Summary = %q, want empty", entries[1].Summary)
	}
	if got := index.RenderVerbatim(ix.Records, ix.Newline); got != text {
		t.Errorf("RenderVerbatim did not reproduce the input\n got %q\nwant %q", got, text)
	}
}

// MemoryStoreLib.Tests.ps1:60
func TestIndex_RebuildsOnlyDirtyRecords(t *testing.T) {
	text := "- [A](a.md) " + em + " long long summary\n- [B](b.md) " + em + " keep\n"
	ix := index.Parse(text)
	ix.Records[0].Dirty = true
	ix.Records[0].Summary = "short"
	want := "- [A](a.md) " + em + " short\n- [B](b.md) " + em + " keep\n"
	if got := index.RenderVerbatim(ix.Records, ix.Newline); got != want {
		t.Errorf("RenderVerbatim\n got %q\nwant %q", got, want)
	}
}

// MemoryStoreLib.Tests.ps1:69
func TestIndex_ExtraLinksCountForReachability(t *testing.T) {
	ix := index.Parse("- [A](a.md) " + em + " merged (also [B](b.md))\n")
	linked := index.LinkedSlugs(ix.Records)
	if !linked["a.md"] {
		t.Error("a.md must be linked")
	}
	if !linked["b.md"] {
		t.Error("b.md (a secondary link inside the hook) must count for orphan accounting")
	}
}

// --------------------------------------------------------------- review primitives
// MemoryStoreLib.Tests.ps1:78

func TestIndex_GhostsFromEntryLinksOnly(t *testing.T) {
	text := "# Index (older notes moved to (archive.md))\n- [A](a.md) " + em + " see [B](b.md) and [dead](dead.md)\n"
	ix := index.Parse(text)
	onDisk := map[string]bool{"a.md": true, "b.md": true}
	got := index.EntryGhosts(ix.Records, onDisk)
	if len(got) != 1 || got[0] != "dead.md" {
		t.Errorf("EntryGhosts = %v, want [dead.md]", got)
	}
	if !index.LinkedSlugs(ix.Records)["archive.md"] {
		t.Error("a file mentioned by a heading is reachable (not an orphan) even though it is never a ghost")
	}
}

// MemoryStoreLib.Tests.ps1:87
func TestIndex_RoundTripExactExtraLinks(t *testing.T) {
	line := "- [A](a.md) " + em + " see [B](b.md)"
	if index.LineRoundTrips(line, "a.md", nil) {
		t.Error("an unexpected extra link must fail the round-trip check")
	}
	if !index.LineRoundTrips(line, "a.md", []string{"b.md"}) {
		t.Error("the exactly-expected extra link must pass")
	}
	if index.LineRoundTrips(line, "a.md", []string{"c.md"}) {
		t.Error("the wrong expected extra must fail: set equality, never subset")
	}
}

// MemoryStoreLib.Tests.ps1:94
func TestIndex_FencedListItemIsNotAnEntry(t *testing.T) {
	text := "- [Real](real.md)\n```\n- [Example](example.md) - illustration\n```\n- [Also real](also.md)\n"
	ix := index.Parse(text)
	var slugs []string
	for _, r := range ix.Entries() {
		slugs = append(slugs, r.Slug)
	}
	want := []string{"real.md", "also.md"}
	if strings.Join(slugs, ",") != strings.Join(want, ",") {
		t.Errorf("entry slugs = %v, want %v", slugs, want)
	}
	if got := index.RenderVerbatim(ix.Records, ix.Newline); got != text {
		t.Errorf("fenced text must round-trip byte-for-byte\n got %q\nwant %q", got, text)
	}
}

// MemoryStoreLib.Tests.ps1:101
func TestIndex_BracketTitleAndIndentedPointerRoundTrip(t *testing.T) {
	text := "- [Title [with] brackets](b.md) " + em + " hook\n  - [Nested](n.md) " + em + " nested\n"
	ix := index.Parse(text)
	e := ix.Entries()
	if len(e) != 2 {
		t.Fatalf("entries = %d, want 2", len(e))
	}
	if e[0].Title != "Title [with] brackets" {
		t.Errorf("Title = %q, want %q", e[0].Title, "Title [with] brackets")
	}
	if e[1].Indent != "  " {
		t.Errorf("Indent = %q, want two spaces", e[1].Indent)
	}
	e[1].Dirty = true
	if got := index.RenderVerbatim(ix.Records, ix.Newline); got != text {
		t.Errorf("a dirty indented entry must keep its indentation\n got %q\nwant %q", got, text)
	}
}

func TestIndex_EntryLineOmitsSeparatorForEmptySummary(t *testing.T) {
	if got := index.EntryLine("A", "a.md", "", ""); got != "- [A](a.md)" {
		t.Errorf("EntryLine with no summary = %q, want %q", got, "- [A](a.md)")
	}
	want := "  - [A](a.md) " + em + " hook"
	if got := index.EntryLine("A", "a.md", "hook", "  "); got != want {
		t.Errorf("EntryLine = %q, want %q", got, want)
	}
}

func TestIndex_TrailingNewlineIsPreservedAsAnEmptyRecord(t *testing.T) {
	ix := index.Parse("- [A](a.md)\n")
	if len(ix.Records) != 2 {
		t.Fatalf("records = %d, want 2 (the entry plus the trailing empty element)", len(ix.Records))
	}
	if ix.Records[1].Raw != "" {
		t.Errorf("trailing record Raw = %q, want empty", ix.Records[1].Raw)
	}
	if ix.LineCount() != 1 {
		t.Errorf("LineCount = %d, want 1 (a single trailing blank is discounted)", ix.LineCount())
	}
}

func TestIndex_SeparatorStripAcceptsHyphenAndColon(t *testing.T) {
	for _, tc := range []struct{ line, want string }{
		{"- [A](a.md) " + em + " hook", "hook"},
		{"- [A](a.md) - hook", "hook"},
		{"- [A](a.md) -- hook", "hook"},
		{"- [A](a.md): hook", "hook"},
		// No separator at all: only the trailing whitespace is trimmed, which is what
		// keeps a hook that happens to start with a space byte-identical on re-render.
		{"- [A](a.md) hook   ", " hook"},
	} {
		ix := index.Parse(tc.line)
		e := ix.Entries()
		if len(e) != 1 {
			t.Fatalf("%q: entries = %d, want 1", tc.line, len(e))
		}
		if e[0].Summary != tc.want {
			t.Errorf("%q: Summary = %q, want %q", tc.line, e[0].Summary, tc.want)
		}
	}
}

func TestIndex_RecordBytesAreCountedAtParseTime(t *testing.T) {
	line := "- [A](a.md) " + em + " hook"
	ix := index.Parse(line)
	if got := ix.Records[0].Bytes; got != len(line) {
		t.Errorf("Bytes = %d, want %d (the UTF-8 length of the raw line)", got, len(line))
	}
}

// --------------------------------------------------------------- derived render
// Blueprint section 3.4. These are net-new behaviour, not ports: the PowerShell
// regenerator re-emits records in parse order with the prevailing newline.

func TestRender_FixedHeadingAlwaysLFAndSlugTiebreak(t *testing.T) {
	ix := index.Parse("# Old heading\r\n\r\n- [B](b.md) - two\r\n- [A](a.md) - one\r\n")
	res := index.RenderDerived(ix.Records, index.RenderOptions{})
	want := "# Memory Index\n\n- [A](a.md) - one\n- [B](b.md) - two\n"
	if res.Text != want {
		t.Errorf("RenderDerived\n got %q\nwant %q", res.Text, want)
	}
	if strings.Contains(res.Text, "\r") {
		t.Error("a derived render is always LF")
	}
	if res.Text[0] != '#' {
		t.Errorf("first byte = %#x, want 0x23 (no BOM, ever)", res.Text[0])
	}
}

func TestRender_DoctrineFirstThenNewestCommitFirst(t *testing.T) {
	ix := index.Parse("- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n")
	res := index.RenderDerived(ix.Records, index.RenderOptions{
		Doctrine: func(r *index.Record) bool { return r.Slug == "a.md" },
		CommitTime: func(slug string) (int64, bool) {
			switch slug {
			case "b.md":
				return 100, true
			case "c.md":
				return 200, true
			}
			return 0, false
		},
		Now: 50,
	})
	want := "# Memory Index\n\n- [A](a.md)\n- [C](c.md)\n- [B](b.md)\n"
	if res.Text != want {
		t.Errorf("doctrine first, then commit time descending\n got %q\nwant %q", res.Text, want)
	}
}

func TestRender_UncommittedFileSortsAsNewest(t *testing.T) {
	ix := index.Parse("- [A](a.md)\n- [B](b.md)\n")
	res := index.RenderDerived(ix.Records, index.RenderOptions{
		CommitTime: func(slug string) (int64, bool) {
			if slug == "a.md" {
				return 100, true
			}
			return 0, false // b.md has no commit yet
		},
		Now: 999,
	})
	want := "# Memory Index\n\n- [B](b.md)\n- [A](a.md)\n"
	if res.Text != want {
		t.Errorf("a file with no commit yet sorts as now\n got %q\nwant %q", res.Text, want)
	}
}

func TestRender_TwoHundredLineStopOmitsAndReports(t *testing.T) {
	var lines []string
	for i := 0; i < 20; i++ {
		lines = append(lines, index.EntryLine("F", string(rune('a'+i))+".md", "", ""))
	}
	ix := index.Parse(strings.Join(lines, "\n") + "\n")
	res := index.RenderDerived(ix.Records, index.RenderOptions{InjectLimitLines: 7})
	rendered := strings.Count(res.Text, "](")
	if rendered != 5 {
		t.Errorf("rendered entries = %d, want 5 (the heading and its blank line count toward the cap)", rendered)
	}
	if len(res.Omitted) != 15 {
		t.Errorf("Omitted = %d, want 15", len(res.Omitted))
	}
	if res.ProtectedOverflow {
		t.Error("ProtectedOverflow must be false when the omitted entries are not doctrine")
	}
}

func TestRender_DoctrineIsNeverDroppedAndOverflowIsReported(t *testing.T) {
	var lines []string
	for i := 0; i < 10; i++ {
		lines = append(lines, index.EntryLine("F", string(rune('a'+i))+".md", "", ""))
	}
	ix := index.Parse(strings.Join(lines, "\n") + "\n")
	res := index.RenderDerived(ix.Records, index.RenderOptions{
		InjectLimitLines: 5,
		Doctrine:         func(r *index.Record) bool { return true },
	})
	if got := strings.Count(res.Text, "]("); got != 10 {
		t.Errorf("rendered entries = %d, want all 10: doctrine is never dropped", got)
	}
	if len(res.Omitted) != 0 {
		t.Errorf("Omitted = %v, want none", res.Omitted)
	}
	if !res.ProtectedOverflow {
		t.Error("doctrine alone past the cap must be reported as protected-set overflow")
	}
}

func TestRender_NonEntryLinesKeepTheirOrderAfterTheHeading(t *testing.T) {
	ix := index.Parse("# Old\n\n## Section\n- [B](b.md)\nprose\n- [A](a.md)\n")
	res := index.RenderDerived(ix.Records, index.RenderOptions{})
	want := "# Memory Index\n\n## Section\nprose\n- [A](a.md)\n- [B](b.md)\n"
	if res.Text != want {
		t.Errorf("non-entry lines keep their relative order after the fixed heading\n got %q\nwant %q", res.Text, want)
	}
}

func TestRender_DirtyEntriesAreRebuiltOthersEmitRaw(t *testing.T) {
	ix := index.Parse("- [A](a.md) - raw text\n")
	ix.Records[0].Dirty = true
	ix.Records[0].Summary = "rebuilt"
	res := index.RenderDerived(ix.Records, index.RenderOptions{})
	want := "# Memory Index\n\n- [A](a.md) " + em + " rebuilt\n"
	if res.Text != want {
		t.Errorf("RenderDerived\n got %q\nwant %q", res.Text, want)
	}
}
