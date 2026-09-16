package frontmatter_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

const em = "\u2014"

func write(t *testing.T, name, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

// MemoryStoreLib.Tests.ps1:144. `type` is NESTED under metadata: in every real fact
// file; a top-level ^type: matches zero files, so the lookup must be lenient about
// indentation and take the first match at ANY depth.
func TestFrontmatter_NestedMetadataType(t *testing.T) {
	// The description carries the two shapes real fact files carry and a naive parser
	// breaks on: an em-dash and inner double quotes inside an already-quoted value.
	p := write(t, "fb.md", testutil.FactFile("x", "Owner "+em+" \"quoted\" inner", "feedback", "body text"))
	fm := frontmatter.ParseFile(p)
	if fm == nil {
		t.Fatal("ParseFile returned nil for a file with a frontmatter block")
	}
	if fm.Type != "feedback" {
		t.Errorf("Type = %q, want feedback", fm.Type)
	}
	if fm.Name != "x" {
		t.Errorf("Name = %q, want x", fm.Name)
	}
	if !strings.Contains(fm.Description, "quoted") {
		t.Errorf("Description = %q, want it to carry the inner quotes", fm.Description)
	}
	if fm.Modified != "2026-08-01" {
		t.Errorf("Modified = %q, want 2026-08-01", fm.Modified)
	}
}

// MemoryStoreLib.Tests.ps1:154
func TestFrontmatter_AbsentBlockIsNil(t *testing.T) {
	if fm := frontmatter.ParseFile(write(t, "nofm.md", "just text\n")); fm != nil {
		t.Errorf("ParseFile of a plain-prose file = %+v, want nil (that nil is what makes lint emit no-frontmatter)", fm)
	}
	if fm := frontmatter.ParseText("--- not really a block\n"); fm != nil {
		t.Errorf("a leading --- that does not close is not a block: got %+v", fm)
	}
}

func TestFrontmatter_ReadsHookAndMigrated(t *testing.T) {
	text := "---\nname: n\ndescription: \"d\"\nhook: \"the short hook\"\nmigrated: 8f3c0011\nmetadata:\n  type: project\n  modified: 2026-08-01\n---\n\nbody\n"
	fm := frontmatter.ParseText(text)
	if fm == nil {
		t.Fatal("ParseText returned nil")
	}
	if fm.Hook != "the short hook" {
		t.Errorf("Hook = %q, want %q", fm.Hook, "the short hook")
	}
	if fm.Migrated != "8f3c0011" {
		t.Errorf("Migrated = %q, want 8f3c0011", fm.Migrated)
	}
}

func TestFrontmatter_StripsExactlyOneOuterQuotePair(t *testing.T) {
	text := "---\nname: \"outer \"inner\" outer\"\ndescription: 'single'\nhook: bare\n---\n\nb\n"
	fm := frontmatter.ParseText(text)
	if fm == nil {
		t.Fatal("ParseText returned nil")
	}
	if fm.Name != "outer \"inner\" outer" {
		t.Errorf("Name = %q, want the inner quotes untouched", fm.Name)
	}
	if fm.Description != "single" {
		t.Errorf("Description = %q, want single", fm.Description)
	}
	if fm.Hook != "bare" {
		t.Errorf("Hook = %q, want bare", fm.Hook)
	}
}

func TestFrontmatter_BodyAndByteCountsAreUTF8(t *testing.T) {
	text := "---\nname: n\n---\n\nbody " + em + "\n"
	fm := frontmatter.ParseText(text)
	if fm == nil {
		t.Fatal("ParseText returned nil")
	}
	if fm.Body != "\nbody "+em+"\n" {
		t.Errorf("Body = %q", fm.Body)
	}
	if fm.FileBytes != len(text) {
		t.Errorf("FileBytes = %d, want %d", fm.FileBytes, len(text))
	}
	if fm.BodyBytes != len(fm.Body) {
		t.Errorf("BodyBytes = %d, want %d", fm.BodyBytes, len(fm.Body))
	}
	if !fm.Present {
		t.Error("Present must be true for a parsed block")
	}
}

// --------------------------------------------------------------- doctrine
// MemoryStoreLib.Tests.ps1:160 - kept in lock-step with imperative_canary.py.

func TestImperative_CanaryPositives(t *testing.T) {
	for _, s := range []string{
		"You MUST use X for all calls",
		"NEVER bind port 80",
		"DO NOT redeploy without approval",
		"RULE: do Y",
		"Ollama is decommissioned.\nNEVER re-register it.",
		"It is retired. Always re-register it.",
	} {
		if !frontmatter.IsImperative(s) {
			t.Errorf("IsImperative(%q) = false, want true", s)
		}
	}
}

// MemoryStoreLib.Tests.ps1:166
func TestImperative_CanaryNegatives(t *testing.T) {
	for _, s := range []string{
		"The reserved ports are 80 and 443",
		"Ollama :11434 is decommissioned",
		"Postiz is a retired, forbidden social scheduler",
		"The operator must approve deploys",
		"over-long inputs defer.",
	} {
		if frontmatter.IsImperative(s) {
			t.Errorf("IsImperative(%q) = true, want false", s)
		}
	}
}

func TestAttributed_NameColonIsAStandingOrderButNotEveryColon(t *testing.T) {
	for _, s := range []string{"Owner: keep the lease", "Owner (CANONICAL): keep the lease"} {
		if !frontmatter.IsAttributed(s) {
			t.Errorf("IsAttributed(%q) = false, want true", s)
		}
	}
	// A space before the colon, or an all-caps word, is not a name.
	for _, s := range []string{"Open for X: do this", "RECURRING: spacey path", "", "   "} {
		if frontmatter.IsAttributed(s) {
			t.Errorf("IsAttributed(%q) = true, want false", s)
		}
	}
}

// MemoryStoreLib.Tests.ps1:172
func TestDoctrine_FeedbackType(t *testing.T) {
	fm := &frontmatter.Frontmatter{Type: "feedback", Description: "x", Present: true}
	if !frontmatter.IsDoctrine("on any key: write to the store now", fm) {
		t.Error("metadata.type=feedback is doctrine whatever the summary says")
	}
}

// MemoryStoreLib.Tests.ps1:178
func TestDoctrine_ImperativeSummary(t *testing.T) {
	fm := &frontmatter.Frontmatter{Type: "project", Description: "", Present: true}
	if !frontmatter.IsDoctrine("NEVER pin a launcher to a versioned path", fm) {
		t.Error("an imperative summary is doctrine even when the type is project")
	}
}

// MemoryStoreLib.Tests.ps1:183
func TestDoctrine_PlainFactEligible(t *testing.T) {
	fm := &frontmatter.Frontmatter{Type: "reference", Description: "where the PDFs are", Present: true}
	if frontmatter.IsDoctrine("pricing PDFs live at the workspace root", fm) {
		t.Error("a declarative location fact is eligible, not doctrine")
	}
}

func TestDoctrine_NilFrontmatterStillTestsTheSummary(t *testing.T) {
	if !frontmatter.IsDoctrine("NEVER do that", nil) {
		t.Error("a missing file yields nil frontmatter and the summary tests still apply")
	}
	if frontmatter.IsDoctrine("a plain fact", nil) {
		t.Error("a plain summary with no frontmatter is not doctrine")
	}
}

func TestDoctrine_ImperativeDescriptionAlone(t *testing.T) {
	fm := &frontmatter.Frontmatter{Type: "project", Description: "NEVER re-register it", Present: true}
	if !frontmatter.IsDoctrine("a bland hook", fm) {
		t.Error("an imperative description is doctrine even when the hook is bland")
	}
}

// --------------------------------------------------------------- harvest
// Blueprint section 3.2: the only fact-file write derive makes.

func TestHarvest_InsertsHookAfterDescription(t *testing.T) {
	p := write(t, "f.md", testutil.FactFile("n", "the description", "project", "body text"))
	changed, err := frontmatter.Harvest(p, "the index hook")
	if err != nil {
		t.Fatalf("Harvest: %v", err)
	}
	if !changed {
		t.Fatal("Harvest reported no change on a file with no hook:")
	}
	got := read(t, p)
	lines := strings.Split(got, "\n")
	descAt, hookAt := -1, -1
	for i, l := range lines {
		if strings.HasPrefix(l, "description:") {
			descAt = i
		}
		if strings.HasPrefix(l, "hook:") {
			hookAt = i
		}
	}
	if descAt < 0 || hookAt != descAt+1 {
		t.Errorf("hook: must sit immediately after description:\n%s", got)
	}
	fm := frontmatter.ParseText(got)
	if fm == nil || fm.Hook != "the index hook" {
		t.Errorf("re-parsed Hook = %+v, want %q", fm, "the index hook")
	}
}

func TestHarvest_IsIdempotent(t *testing.T) {
	p := write(t, "f.md", testutil.FactFile("n", "d", "project", "body text"))
	if _, err := frontmatter.Harvest(p, "first"); err != nil {
		t.Fatal(err)
	}
	before := read(t, p)
	changed, err := frontmatter.Harvest(p, "second")
	if err != nil {
		t.Fatal(err)
	}
	if changed {
		t.Error("a file that already carries hook: must never be rewritten")
	}
	if read(t, p) != before {
		t.Error("Harvest rewrote a file it reported unchanged")
	}
}

func TestHarvest_LeavesAFrontmatterlessFileAlone(t *testing.T) {
	p := write(t, "raw.md", "no frontmatter here\n")
	changed, err := frontmatter.Harvest(p, "hook")
	if err != nil {
		t.Fatalf("Harvest: %v", err)
	}
	if changed {
		t.Error("harvest must not add a frontmatter block: that would change the no-frontmatter lint population")
	}
	if read(t, p) != "no frontmatter here\n" {
		t.Error("the file was rewritten")
	}
}

func TestHarvest_InsertsBeforeMetadataWhenThereIsNoDescription(t *testing.T) {
	p := write(t, "f.md", "---\nname: n\nmetadata:\n  type: project\n---\n\nbody\n")
	if _, err := frontmatter.Harvest(p, "hook text"); err != nil {
		t.Fatal(err)
	}
	got := read(t, p)
	want := "---\nname: n\nhook: \"hook text\"\nmetadata:\n  type: project\n---\n\nbody\n"
	if got != want {
		t.Errorf("Harvest\n got %q\nwant %q", got, want)
	}
}

func TestHarvest_PreservesTheBodyByteForByte(t *testing.T) {
	body := "line one " + em + "\n\n  indented\ttab\n"
	p := write(t, "f.md", "---\nname: n\ndescription: \"d\"\n---\n"+body)
	if _, err := frontmatter.Harvest(p, "hook"); err != nil {
		t.Fatal(err)
	}
	got := read(t, p)
	if !strings.HasSuffix(got, body) {
		t.Errorf("the body must be byte-preserved\n got %q\nwant suffix %q", got, body)
	}
}

func TestHarvest_QuotesEmbeddedQuotesAndBackslashes(t *testing.T) {
	p := write(t, "f.md", testutil.FactFile("n", "d", "project", "body text"))
	hook := `a "quoted" hook with \ backslash and an ` + em
	if _, err := frontmatter.Harvest(p, hook); err != nil {
		t.Fatal(err)
	}
	fm := frontmatter.ParseText(read(t, p))
	if fm == nil {
		t.Fatal("re-parse returned nil")
	}
	if !strings.Contains(read(t, p), `\"quoted\"`) {
		t.Errorf("the inner quotes must be YAML-escaped:\n%s", read(t, p))
	}
	if !strings.Contains(read(t, p), `\\`) {
		t.Errorf("the backslash must be YAML-escaped:\n%s", read(t, p))
	}
}

func TestHarvest_QuoteYAMLEscapes(t *testing.T) {
	if got, want := frontmatter.QuoteYAML(`a "b" c\d`), `"a \"b\" c\\d"`; got != want {
		t.Errorf("QuoteYAML = %q, want %q", got, want)
	}
}

func read(t *testing.T, p string) string {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}
