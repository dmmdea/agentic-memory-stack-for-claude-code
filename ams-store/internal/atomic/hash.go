package atomic

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
)

// Hash is the lowercase hex SHA-256 of data. It is the compare-and-swap currency: the
// hash is taken at entry and re-taken immediately before a write, and on drift the write
// is abandoned rather than allowed to clobber what a live session just wrote.
func Hash(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// FileHash is the lowercase hex SHA-256 of a file's bytes. A missing or unreadable file
// is an error, never a hash that happens to compare unequal: "I could not read it" and
// "it changed" must stay distinguishable at the call site.
func FileHash(path string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("hash %s: %w", path, err)
	}
	return Hash(b), nil
}
