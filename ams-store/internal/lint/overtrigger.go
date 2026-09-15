package lint

import (
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/atomic"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
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

// Stamps maps a workspace slug to when it first crossed the trigger, and carries the
// tombstone that says when it was last seen back under it.
//
// ClearedAt exists because a clear is an ABSENCE, and an absence is exactly what a union
// reducer cannot distinguish from "that PC has not re-derived yet". Without it the cleared
// stamp came straight back from the first PC that still carried it.
type Stamps struct {
	OverTriggerSince map[string]time.Time `json:"over_trigger_since"`
	ClearedAt        map[string]time.Time `json:"cleared_at"`
}

// StampPath is the stamp file under a projects root.
func StampPath(projectsRoot string) string {
	return filepath.Join(projectsRoot, StampDir, StampFile)
}

// ReadStamps reads the stamp file. A missing file is an empty set, not an error; a file
// that exists but does not parse IS an error, because "absent" and "corrupt" must not
// collapse into the same answer.
func ReadStamps(projectsRoot string) (Stamps, error) {
	s := emptyStamps()
	found, err := atomic.ReadJSONFile(StampPath(projectsRoot), &s)
	if err != nil {
		return emptyStamps(), err
	}
	if !found || s.OverTriggerSince == nil {
		s.OverTriggerSince = map[string]time.Time{}
	}
	if s.ClearedAt == nil {
		s.ClearedAt = map[string]time.Time{}
	}
	return s, nil
}

func emptyStamps() Stamps {
	return Stamps{OverTriggerSince: map[string]time.Time{}, ClearedAt: map[string]time.Time{}}
}

// IsLive reports whether a workspace's crossing stamp is still the current clock rather
// than one a later clear already resolved. It is the typed twin of merge.StampIsLive and
// must answer the same question the reducer does, or lint and the merge would disagree
// about the same file.
func (s Stamps) IsLive(workspace string) bool {
	since, ok := s.OverTriggerSince[workspace]
	if !ok {
		return false
	}
	cleared, ok := s.ClearedAt[workspace]
	if !ok {
		return true
	}
	return since.UTC().After(cleared.UTC())
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
		s = emptyStamps()
	}
	prev, had := s.OverTriggerSince[workspace]
	live := s.IsLive(workspace)
	switch {
	case !overTrigger:
		// The clear is a WRITE, not a deletion: the tombstone is what carries it to the
		// other PCs, which still have the stamp and would otherwise hand it back.
		if !had && s.clearedNotBefore(workspace, now) {
			return s, nil // already cleared at or after this instant; nothing new to say
		}
		delete(s.OverTriggerSince, workspace)
		if !s.clearedNotBefore(workspace, now) {
			s.ClearedAt[workspace] = now.UTC()
		}
	case !live:
		// No live stamp - either none at all, or one a clear already resolved. Either way
		// this is a FRESH crossing and it gets today's clock, never the dead one back.
		s.OverTriggerSince[workspace] = now.UTC()
	case now.UTC().Before(prev):
		s.OverTriggerSince[workspace] = now.UTC()
	default:
		return s, nil // already stamped, and the stamp is older: nothing to do
	}
	if err := WriteStamps(projectsRoot, s); err != nil {
		return s, err
	}
	return s, nil
}

// clearedNotBefore reports whether this workspace already carries a tombstone at or after
// now. Re-stamping the same clear on every maintenance pass would rewrite a tracked file
// - and cost a commit - for a store nobody touched.
func (s Stamps) clearedNotBefore(workspace string, now time.Time) bool {
	c, ok := s.ClearedAt[workspace]
	if !ok {
		return false
	}
	return !c.UTC().Before(now.UTC())
}

// WriteStamps writes the stamp file through the ONE renderer that owns those bytes,
// merge.RenderOverTrigger.
//
// The producer and the merge reducer write the same tracked file. When they render it
// differently the file flips format on every change - each stamp costs an extra commit,
// and the two PCs disagree on the bytes until a merge has run. atomic.WriteJSONFile is
// deliberately NOT used here: its indented encoding is the second renderer this replaces.
func WriteStamps(projectsRoot string, s Stamps) error {
	if err := os.MkdirAll(filepath.Dir(StampPath(projectsRoot)), 0o755); err != nil {
		return err
	}
	return atomic.WriteBytes(StampPath(projectsRoot), merge.RenderOverTrigger(s.toMerge()))
}

// toMerge projects the typed stamps onto the merge engine's string-keyed view, which is
// the shape RenderOverTrigger encodes. Times are written at RFC3339 second precision:
// that is what the reducer parses, and a nanosecond tail no PC can reproduce would make
// the same crossing render differently on two machines.
func (s Stamps) toMerge() merge.OverTrigger {
	out := merge.OverTrigger{
		OverTriggerSince: rfc3339Map(s.OverTriggerSince),
		ClearedAt:        rfc3339Map(s.ClearedAt),
	}
	// A stamp a clear already resolved must never be written back out: the reducer would
	// drop it on the next merge anyway, and leaving it in the file makes the producer's
	// bytes differ from the reducer's for the same content.
	for k := range out.OverTriggerSince {
		if !merge.StampIsLive(out.OverTriggerSince[k], out.ClearedAt[k]) {
			delete(out.OverTriggerSince, k)
		}
	}
	return out
}

func rfc3339Map(m map[string]time.Time) map[string]string {
	out := make(map[string]string, len(m))
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		out[k] = m[k].UTC().Format(time.RFC3339)
	}
	return out
}

// MergeStamps is the typed twin of merge.MergeOverTrigger: the latest clear wins, then
// MIN over the stamps that survive it. It must answer exactly what the byte reducer
// answers - a second rule here is a second rule the fleet would have to agree with.
func MergeStamps(a, b Stamps) Stamps {
	out := emptyStamps()
	for _, src := range []Stamps{a, b} {
		for k, v := range src.ClearedAt {
			if cur, ok := out.ClearedAt[k]; !ok || v.UTC().After(cur) {
				out.ClearedAt[k] = v.UTC()
			}
		}
	}
	for _, src := range []Stamps{a, b} {
		for k, v := range src.OverTriggerSince {
			if c, ok := out.ClearedAt[k]; ok && !v.UTC().After(c) {
				continue // a crossing the clear already resolved
			}
			if cur, ok := out.OverTriggerSince[k]; !ok || v.UTC().Before(cur) {
				out.OverTriggerSince[k] = v.UTC()
			}
		}
	}
	return out
}

// HoursOverTrigger is how long a workspace has been over the trigger, or nil when it has
// no stamp.
func (s Stamps) HoursOverTrigger(workspace string, now time.Time) *float64 {
	since, ok := s.OverTriggerSince[workspace]
	if !ok || !s.IsLive(workspace) {
		return nil
	}
	h := now.UTC().Sub(since.UTC()).Hours()
	h = float64(int(h*10+0.5)) / 10
	return &h
}
