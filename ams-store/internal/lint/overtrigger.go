package lint

import (
	"os"
	"path/filepath"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
)

// The over-trigger stamp, DESIGN:257-258 and decision Q13.
//
// It lives at <PROJECTS_ROOT>/.ams/over-trigger.json: inside the SYNCED tree so every PC
// agrees on when a store first crossed the trigger, and OUTSIDE every store because
// nothing but MEMORY.md and fact files may sit in a store - a maintenance file in there
// resurfaces in every agent's glob.
//
// The merge rule is MIN: the earliest crossing any PC saw is the truth. Taking the latest
// would let a PC that only just noticed reset the clock, and the clock is the whole
// metric - "hours over trigger without an applied decision" (G7). A skip cannot satisfy
// it, which is the point: receipt age says the maintainer ran, this says the store got
// better.

// StampDir and StampFile locate the stamp under the projects root.
const (
	StampDir  = ".ams"
	StampFile = "over-trigger.json"
)

// AlarmHours is where G7 raises the alarm.
const AlarmHours = 24.0

// Stamps maps a workspace slug to when it first crossed the trigger.
type Stamps struct {
	OverTriggerSince map[string]time.Time `json:"over_trigger_since"`
}

// StampPath is the stamp file under a projects root.
func StampPath(projectsRoot string) string {
	return filepath.Join(projectsRoot, StampDir, StampFile)
}

// ReadStamps reads the stamp file. A missing file is an empty set, not an error; a file
// that exists but does not parse IS an error, because "absent" and "corrupt" must not
// collapse into the same answer.
func ReadStamps(projectsRoot string) (Stamps, error) {
	s := Stamps{OverTriggerSince: map[string]time.Time{}}
	found, err := atomic.ReadJSONFile(StampPath(projectsRoot), &s)
	if err != nil {
		return Stamps{OverTriggerSince: map[string]time.Time{}}, err
	}
	if !found || s.OverTriggerSince == nil {
		s.OverTriggerSince = map[string]time.Time{}
	}
	return s, nil
}

// RecordOverTrigger updates the stamp for one workspace and returns the stamps as they
// now stand.
//
// It is called by the MAINTENANCE path (derive/sync), never by lint: lint is read-only by
// contract and only reads this file. A store that has fallen back under the trigger loses
// its stamp, so the next crossing starts a fresh clock; a store already stamped keeps the
// EARLIER of the two times, which is the same min reducer the merge uses.
func RecordOverTrigger(projectsRoot, workspace string, overTrigger bool, now time.Time) (Stamps, error) {
	s, err := ReadStamps(projectsRoot)
	if err != nil {
		// A corrupt stamp file must not stop maintenance. Start a clean one and carry on:
		// losing the clock costs one alarm, refusing to maintain costs the store.
		s = Stamps{OverTriggerSince: map[string]time.Time{}}
	}
	prev, had := s.OverTriggerSince[workspace]
	switch {
	case !overTrigger:
		if !had {
			return s, nil
		}
		delete(s.OverTriggerSince, workspace)
	case !had:
		s.OverTriggerSince[workspace] = now.UTC()
	case now.UTC().Before(prev):
		s.OverTriggerSince[workspace] = now.UTC()
	default:
		return s, nil // already stamped, and the stamp is older: nothing to do
	}
	if err := os.MkdirAll(filepath.Dir(StampPath(projectsRoot)), 0o755); err != nil {
		return s, err
	}
	if err := atomic.WriteJSONFile(StampPath(projectsRoot), s); err != nil {
		return s, err
	}
	return s, nil
}

// MergeStamps is the min reducer the synced stamp file is merged with.
func MergeStamps(a, b Stamps) Stamps {
	out := Stamps{OverTriggerSince: map[string]time.Time{}}
	for k, v := range a.OverTriggerSince {
		out.OverTriggerSince[k] = v.UTC()
	}
	for k, v := range b.OverTriggerSince {
		if cur, ok := out.OverTriggerSince[k]; !ok || v.UTC().Before(cur) {
			out.OverTriggerSince[k] = v.UTC()
		}
	}
	return out
}

// HoursOverTrigger is how long a workspace has been over the trigger, or nil when it has
// no stamp.
func (s Stamps) HoursOverTrigger(workspace string, now time.Time) *float64 {
	since, ok := s.OverTriggerSince[workspace]
	if !ok {
		return nil
	}
	h := now.UTC().Sub(since.UTC()).Hours()
	h = float64(int(h*10+0.5)) / 10
	return &h
}
