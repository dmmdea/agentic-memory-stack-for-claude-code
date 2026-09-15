package merge

import (
	"os"
	"path/filepath"
	"strings"
	"time"
)

// LivenessWindow is how recently a transcript must have been written for a workspace to
// count as live (ported from Test-AmWorkspaceLive's 30-minute default).
const LivenessWindow = 30 * time.Minute

// SessionStartFloor clamps how far back a session is assumed to have started. A
// transcript left open for days would otherwise make every file in the store look
// "touched by the session".
const SessionStartFloor = 24 * time.Hour

// Liveness answers "is a Claude session live in this workspace right now".
//
// The signal is a *.jsonl transcript written under the workspace directory within the
// window - not an OS process list, which is not portable and which the transcript
// already proves.
//
// In v2 this no longer gates maintenance: derive always runs. It survives as the ONE
// guard on materialize, which is the only place a merge touches files a live session may
// be holding.
type Liveness struct {
	ProjectsRoot string
	// Within overrides LivenessWindow.
	Within time.Duration
	// Now is injectable for tests.
	Now func() time.Time
	// ProbeDirs overrides the directories probed for a workspace. The default is the
	// workspace directory itself; the store enumerator supplies ALIAS directories too,
	// because a session running under a junction path writes its transcript into the
	// alias directory and a probe on the canonical name alone would miss it.
	ProbeDirs func(workspace string) []string
}

func (l Liveness) now() time.Time {
	if l.Now != nil {
		return l.Now()
	}
	return time.Now()
}

func (l Liveness) within() time.Duration {
	if l.Within > 0 {
		return l.Within
	}
	return LivenessWindow
}

func (l Liveness) dirs(workspace string) []string {
	if l.ProbeDirs != nil {
		return l.ProbeDirs(workspace)
	}
	if l.ProjectsRoot == "" || workspace == "" {
		return nil
	}
	return []string{filepath.Join(l.ProjectsRoot, workspace)}
}

// Probe reports whether a session is live in the workspace, and when the oldest live
// transcript in it was last written - the instant materialize treats as the session's
// start.
//
// It FAILS CLOSED. With nothing to probe, or when the enumeration itself errors, the
// answer is LIVE: "I could not tell" must mean "do not touch it", never "go ahead". A
// probe directory that simply does not exist is skipped, because a workspace with no
// transcript directory has no session by definition.
func (l Liveness) Probe(workspace string) (live bool, sessionStart time.Time) {
	now := l.now()
	cutoff := now.Add(-l.within())
	floor := now.Add(-SessionStartFloor)

	dirs := l.dirs(workspace)
	if len(dirs) == 0 {
		return true, floor
	}

	oldest := time.Time{}
	for _, dir := range dirs {
		entries, err := os.ReadDir(dir)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			// Unreadable is not "empty".
			return true, floor
		}
		for _, ent := range entries {
			if ent.IsDir() || !strings.HasSuffix(strings.ToLower(ent.Name()), ".jsonl") {
				continue
			}
			info, err := ent.Info()
			if err != nil {
				return true, floor
			}
			mt := info.ModTime()
			if mt.Before(cutoff) {
				continue
			}
			live = true
			if oldest.IsZero() || mt.Before(oldest) {
				oldest = mt
			}
		}
	}
	if !live {
		return false, time.Time{}
	}
	if oldest.Before(floor) {
		oldest = floor
	}
	return true, oldest
}
