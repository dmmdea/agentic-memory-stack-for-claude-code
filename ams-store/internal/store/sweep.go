package store

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// SweepResult reports what a temp-file sweep removed and what it could not.
type SweepResult struct {
	Removed []string
	Failed  []string
}

// SweepTempFiles removes orphaned *.am-tmp files from a store directory.
//
// A crashed atomic write can leave a full copy of the index as <name>.am-tmp INSIDE the
// store, where the harness syncs it and agents glob it. A leftover is evidence of a
// previously failed write, so the caller logs what it removed - and a temp file that
// could NOT be removed is a full copy of the index sitting in a synced, globbed
// directory: the case to shout about, never one to swallow.
func SweepTempFiles(dir string) SweepResult {
	res := SweepResult{Removed: []string{}, Failed: []string{}}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return res
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), TempSuffix) {
			continue
		}
		if err := os.Remove(filepath.Join(dir, e.Name())); err != nil {
			res.Failed = append(res.Failed, e.Name()+": "+err.Error())
			continue
		}
		res.Removed = append(res.Removed, e.Name())
	}
	sort.Strings(res.Removed)
	sort.Strings(res.Failed)
	return res
}
