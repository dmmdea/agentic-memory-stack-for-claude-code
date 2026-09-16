package derive

import (
	"strings"
	"testing"
	"unicode/utf8"
)

// MemoryStoreLib.Tests.ps1:119 - byte truncation never splits a surrogate pair.
//
// The PowerShell original walks UTF-16 code units and has to drop a trailing high
// surrogate by hand; Go walks runes, so the pair cannot be split - but the assertion
// stays, because the failure it names (a lone high surrogate encodes as EF BF BD and the
// index silently gains the replacement character) is a property of the OUTPUT, not of the
// loop that produced it.
func TestTruncate_SurrogateSafe(t *testing.T) {
	const emoji = "\U0001F600" // 4 bytes in UTF-8
	in := strings.Repeat("x", 97) + emoji + "tail"

	out := TruncateToBytes(in, 99)

	if strings.ContainsRune(out, '\uFFFD') {
		t.Errorf("truncation produced the replacement character: %q", out)
	}
	if !utf8.ValidString(out) {
		t.Errorf("truncation produced invalid UTF-8: %q", out)
	}
	if n := len(out); n > 99 {
		t.Errorf("truncated to %d bytes, want <= 99", n)
	}
	// 97 x's + a 4-byte emoji is 101 bytes, so the emoji must be gone entirely rather
	// than half-written.
	if strings.Contains(out, emoji) {
		t.Errorf("the emoji survived a 99-byte budget it cannot fit in: %q", out)
	}
}

func TestTruncate_WithinBudgetIsUnchanged(t *testing.T) {
	in := "short enough"
	if got := TruncateToBytes(in, 100); got != in {
		t.Errorf("TruncateToBytes(%q, 100) = %q, want it unchanged", in, got)
	}
}

// LIB:629-631: the last space becomes the cut point ONLY past 60% of the truncated
// string. A single long token keeps its byte-exact cut, or a hook that is one long path
// would be truncated to nothing.
func TestTruncate_WordBoundaryOnlyPastSixtyPercent(t *testing.T) {
	// "ab " then a long run: the space sits at index 2, far under 60%, so it must NOT be
	// used as the cut point.
	in := "ab " + strings.Repeat("c", 200)
	got := TruncateToBytes(in, 40)
	if got == "ab" {
		t.Fatalf("cut at a space under 60%% of the string: %q", got)
	}
	if len(got) != 40 {
		t.Errorf("len = %d, want an exact 40-byte cut when no late space exists: %q", len(got), got)
	}

	// A space past 60% IS the cut point, and the result is right-trimmed.
	in2 := strings.Repeat("word ", 8) + "tailtailtail"
	got2 := TruncateToBytes(in2, 30)
	if strings.HasSuffix(got2, " ") {
		t.Errorf("a word-boundary cut must be right-trimmed: %q", got2)
	}
	if !strings.HasSuffix(got2, "word") {
		t.Errorf("got %q, want the cut to fall on the last space past 60%%", got2)
	}
}

func TestTruncate_EmptyAndZeroBudget(t *testing.T) {
	if got := TruncateToBytes("", 10); got != "" {
		t.Errorf("empty input = %q, want empty", got)
	}
	if got := TruncateToBytes("abc", 0); got != "" {
		t.Errorf("zero budget = %q, want empty", got)
	}
}
