package atomic_test

import (
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
)

const em = "\u2014"

// MemoryStoreLib.Tests.ps1:24. Read the file, write it straight back, and require the
// bytes to be identical: no BOM, no newline rewrite, no em-dash mangling, and no
// *.am-tmp left behind inside a directory the harness syncs and agents glob.
func TestIO_RoundTripBomlessLFWithEmDash(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "idx.md")
	line := "- [Title](t.md) " + em + " caf\u00e9 rule"
	in := []byte(line + "\n" + line + "\n")
	if err := os.WriteFile(p, in, 0o644); err != nil {
		t.Fatal(err)
	}

	got, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if err := atomic.Write(p, string(got)); err != nil {
		t.Fatalf("atomic.Write: %v", err)
	}

	out, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if len(out) != len(in) {
		t.Fatalf("length = %d, want %d", len(out), len(in))
	}
	if hex.EncodeToString(out) != hex.EncodeToString(in) {
		t.Errorf("bytes differ\n got %s\nwant %s", hex.EncodeToString(out), hex.EncodeToString(in))
	}
	if out[0] != 0x2D {
		t.Errorf("first byte = %#x, want 0x2D: no BOM may be prepended", out[0])
	}
	assertNoTempFiles(t, dir)
}

func TestAtomic_WriteCreatesThenReplaces(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "MEMORY.md")
	if err := atomic.Write(p, "first\n"); err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := atomic.Write(p, "second\n"); err != nil {
		t.Fatalf("replace: %v", err)
	}
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != "second\n" {
		t.Errorf("content = %q, want %q", b, "second\n")
	}
	assertNoTempFiles(t, dir)
}

// A failed swap must never leave a full copy of the index behind in the store.
func TestAtomic_AFailedSwapRemovesTheTempFile(t *testing.T) {
	dir := t.TempDir()
	// The target is a directory: the rename cannot replace it on any platform.
	p := filepath.Join(dir, "MEMORY.md")
	if err := os.Mkdir(p, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := atomic.Write(p, "text\n"); err == nil {
		t.Fatal("atomic.Write onto a directory must fail")
	}
	assertNoTempFiles(t, dir)
}

func TestAtomic_HashIsLowercaseHexSHA256(t *testing.T) {
	// SHA-256 of the empty string.
	const wantEmpty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
	if got := atomic.Hash(nil); got != wantEmpty {
		t.Errorf("Hash(nil) = %q, want %q", got, wantEmpty)
	}
	if strings.ToLower(wantEmpty) != wantEmpty {
		t.Fatal("fixture is not lowercase")
	}

	p := filepath.Join(t.TempDir(), "f")
	if err := os.WriteFile(p, []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := atomic.FileHash(p)
	if err != nil {
		t.Fatal(err)
	}
	const wantABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
	if got != wantABC {
		t.Errorf("FileHash = %q, want %q", got, wantABC)
	}
}

func TestAtomic_FileHashOfAMissingFileIsAnError(t *testing.T) {
	if _, err := atomic.FileHash(filepath.Join(t.TempDir(), "nope")); err == nil {
		t.Fatal("FileHash of a missing file must error: absent and unreadable are not 'unchanged'")
	}
}

// MemoryStoreLib.Tests.ps1:112. An EMPTY file is present-but-unparseable, not absent: a
// truncated seal file read as "no seals" is the same disarm as corruption.
func TestState_EmptyFileErrorsNotEmptyState(t *testing.T) {
	dir := t.TempDir()
	empty := filepath.Join(dir, "empty.json")
	if err := os.WriteFile(empty, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	var v map[string]any
	if _, err := atomic.ReadJSONFile(empty, &v); err == nil {
		t.Error("an empty state file must error, never read as 'no state'")
	}

	whitespace := filepath.Join(dir, "ws.json")
	if err := os.WriteFile(whitespace, []byte("   \n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := atomic.ReadJSONFile(whitespace, &v); err == nil {
		t.Error("a whitespace-only state file must error")
	}

	corrupt := filepath.Join(dir, "corrupt.json")
	if err := os.WriteFile(corrupt, []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := atomic.ReadJSONFile(corrupt, &v); err == nil {
		t.Error("a corrupt state file must error")
	}

	found, err := atomic.ReadJSONFile(filepath.Join(dir, "absent.json"), &v)
	if err != nil {
		t.Errorf("a path that does not exist at all must not error: %v", err)
	}
	if found {
		t.Error("found must be false for a file that does not exist")
	}
}

func TestState_JSONRoundTrip(t *testing.T) {
	p := filepath.Join(t.TempDir(), "state.json")
	in := map[string]any{"a": "b"}
	if err := atomic.WriteJSONFile(p, in); err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	found, err := atomic.ReadJSONFile(p, &out)
	if err != nil || !found {
		t.Fatalf("ReadJSONFile: found=%v err=%v", found, err)
	}
	if out["a"] != "b" {
		t.Errorf("round trip = %v, want map[a:b]", out)
	}
}

func assertNoTempFiles(t *testing.T, dir string) {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".am-tmp") {
			t.Errorf("%s was left behind: the temp file is swapped in, never left in a synced, globbed directory", e.Name())
		}
	}
}
