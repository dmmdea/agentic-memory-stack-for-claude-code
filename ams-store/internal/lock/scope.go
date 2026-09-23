package lock

import (
	"crypto/sha256"
	"encoding/hex"
	"path/filepath"
	"runtime"
	"strings"
)

// ScopedName is a production mutex name bound to ONE store: base + "-" + the first 16 hex
// digits of SHA-256 over the canonical state root.
//
// A Windows named mutex in the Local\ namespace is global to the logon session, not to a
// state root. With the bare names, any second store on the desktop - a test run's sandbox,
// a scratch HOME - held the operator's live lock: the real `ams-store sync --once` exited 4
// while a Pester run's sandbox compactor held Local\ams-memory-compact (2026-09-23). Scoping
// keeps the singleton-per-store rule (the same root always derives the same name) and drops
// the accidental singleton-per-desktop one.
//
// The PowerShell side derives the SAME name (memory-store-lib.ps1 Get-AmStoreMutexName; the
// compactor's GUARD 0 takes it), so a Go pass and a PowerShell compaction of one store still
// exclude each other (decision Q9). Both suites pin one golden vector.
func ScopedName(base, stateRoot string) string {
	return base + "-" + StateRootKey(stateRoot)
}

// StateRootKey is the 16-hex-digit key ScopedName appends.
func StateRootKey(stateRoot string) string {
	sum := sha256.Sum256([]byte(canonicalStateRoot(stateRoot)))
	return hex.EncodeToString(sum[:])[:16]
}

// canonicalStateRoot is the one spelling of a state root both implementations hash: the
// absolute path with no trailing separator and, on Windows, backslashes and lower case
// (NTFS paths compare case-insensitively, and a root reached as C:/Users/... or with a
// trailing slash is the same store). PowerShell: GetFullPath + TrimEnd('\','/') +
// ToLowerInvariant.
func canonicalStateRoot(p string) string {
	if abs, err := filepath.Abs(p); err == nil {
		p = abs
	}
	p = filepath.Clean(p)
	if runtime.GOOS == "windows" {
		p = strings.ToLower(strings.ReplaceAll(p, "/", `\`))
		return strings.TrimRight(p, `\`)
	}
	if len(p) > 1 {
		p = strings.TrimRight(p, "/")
	}
	return p
}
