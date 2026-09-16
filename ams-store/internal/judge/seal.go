package judge

import (
	"fmt"
	"path/filepath"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
)

// SealFileName is the per-workspace seal file (COMPACT:237).
const SealFileName = "sealed-lines.json"

// Seal is slug -> the RFC3339 UTC instant the judge's rewrite of that line was applied.
//
// One judge rewrite per line, EVER. Without the seal the same line is offered every
// night, re-worded every night, and an index drifts away from what its facts say by a
// thousand small edits nobody reviewed. The stamp is kept rather than a bare bool so the
// file reads as an audit trail.
type Seal map[string]string

// SealPath is the seal file for one workspace's state directory.
func SealPath(workspaceStateDir string) string {
	return filepath.Join(workspaceStateDir, SealFileName)
}

// LoadSeal reads the seal file.
//
// A missing file is an empty seal set and no error. A file that EXISTS but is empty or
// unparseable is an ERROR, and the caller must not proceed seal-less: an empty seal set
// re-arms the judge on every already-shortened line, and the save that follows would
// overwrite the file with only this run's seals, discarding the history permanently.
// "Absent" and "corrupt" are different answers and this is the call site that proves it.
func LoadSeal(path string) (Seal, error) {
	s := Seal{}
	found, err := atomic.ReadJSONFile(path, &s)
	if err != nil {
		return nil, fmt.Errorf("seal: %w", err)
	}
	if !found {
		return Seal{}, nil
	}
	if s == nil {
		s = Seal{}
	}
	return s, nil
}

// Stamp records a slug as sealed at t.
func (s Seal) Stamp(slug string, t time.Time) {
	s[slug] = t.UTC().Format(time.RFC3339Nano)
}

// Sealed reports whether a slug has already had its one judge rewrite.
func (s Seal) Sealed(slug string) bool {
	_, ok := s[slug]
	return ok
}

// SaveSeal writes the seal file. The caller reports a failure in the receipt rather than
// swallowing it: an unsaved seal silently re-arms the judge on those lines next run.
func SaveSeal(path string, s Seal) error {
	if s == nil {
		s = Seal{}
	}
	if err := atomic.WriteJSONFile(path, s); err != nil {
		return fmt.Errorf("seal: %w", err)
	}
	return nil
}
