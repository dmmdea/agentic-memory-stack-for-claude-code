package merge

import (
	"encoding/json"
	"fmt"
	"sort"
	"time"
)

// OverTriggerPath is where the shared `over_trigger_since` stamp rides, relative to
// PROJECTS_ROOT (decision Q13).
//
// It is OUTSIDE every store on purpose: nothing but MEMORY.md and fact files may live
// inside a store, because the store is synced by the harness and globbed by agents, and
// a maintenance file in there resurfaces in every agent glob. It is in the synced TREE
// rather than on an orphan ref so it merges with the same engine as everything else.
const OverTriggerPath = ".ams/over-trigger.json"

// OverTrigger is the shared stamp file: per workspace, the instant that store FIRST
// crossed the trigger and stayed over it.
type OverTrigger struct {
	OverTriggerSince map[string]string `json:"over_trigger_since"`
}

// ParseOverTrigger decodes the stamp file. Empty input is an empty stamp, not an error:
// the file legitimately does not exist until some store crosses for the first time.
func ParseOverTrigger(b []byte) (OverTrigger, error) {
	out := OverTrigger{OverTriggerSince: map[string]string{}}
	if len(b) == 0 {
		return out, nil
	}
	var raw OverTrigger
	if err := json.Unmarshal(b, &raw); err != nil {
		return out, fmt.Errorf("parse %s: %w", OverTriggerPath, err)
	}
	for k, v := range raw.OverTriggerSince {
		out.OverTriggerSince[k] = v
	}
	return out, nil
}

// RenderOverTrigger encodes the stamp file deterministically: sorted keys, LF, one
// trailing newline. Two PCs merging the same pair must produce the same bytes or the
// file conflicts with itself forever.
func RenderOverTrigger(t OverTrigger) []byte {
	keys := make([]string, 0, len(t.OverTriggerSince))
	for k := range t.OverTriggerSince {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b []byte
	b = append(b, `{"over_trigger_since":{`...)
	for i, k := range keys {
		if i > 0 {
			b = append(b, ',')
		}
		kj, _ := json.Marshal(k)
		vj, _ := json.Marshal(t.OverTriggerSince[k])
		b = append(b, kj...)
		b = append(b, ':')
		b = append(b, vj...)
	}
	b = append(b, "}}\n"...)
	return b
}

// MergeOverTrigger reduces two stamp files with MIN.
//
// The earliest crossing is the answer because the metric the stamp feeds is "how long has
// this store been over the trigger without a decision" - taking the later stamp would
// reset that clock on every sync and make a starved store look freshly over.
//
// An unparseable timestamp loses to a parseable one rather than poisoning the reduction;
// a workspace only one side knows about is kept.
func MergeOverTrigger(ours, theirs []byte) ([]byte, error) {
	o, err := ParseOverTrigger(ours)
	if err != nil {
		o = OverTrigger{OverTriggerSince: map[string]string{}}
	}
	t, err := ParseOverTrigger(theirs)
	if err != nil {
		t = OverTrigger{OverTriggerSince: map[string]string{}}
	}
	out := OverTrigger{OverTriggerSince: map[string]string{}}
	for k, v := range o.OverTriggerSince {
		out.OverTriggerSince[k] = v
	}
	for k, tv := range t.OverTriggerSince {
		ov, have := out.OverTriggerSince[k]
		if !have {
			out.OverTriggerSince[k] = tv
			continue
		}
		out.OverTriggerSince[k] = earlier(ov, tv)
	}
	return RenderOverTrigger(out), nil
}

func earlier(a, b string) string {
	ta, errA := time.Parse(time.RFC3339, a)
	tb, errB := time.Parse(time.RFC3339, b)
	switch {
	case errA != nil && errB != nil:
		if a <= b {
			return a
		}
		return b
	case errA != nil:
		return b
	case errB != nil:
		return a
	case ta.Before(tb):
		return a
	default:
		return b
	}
}
