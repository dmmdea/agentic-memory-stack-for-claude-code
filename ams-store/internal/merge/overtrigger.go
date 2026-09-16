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
// crossed the trigger and stayed over it, plus the tombstone that makes a CLEAR
// representable.
//
// The tombstone is not bookkeeping. A store that converges back under the trigger loses
// its stamp, and losing it is an ABSENCE - which a union reducer cannot tell apart from
// "that PC has not re-derived yet". Without `cleared_at` the stamp came straight back
// from whichever PC still had it and the G7 clock could never reset, so the 24 h alarm
// would keep firing on a store that was fixed hours ago.
type OverTrigger struct {
	OverTriggerSince map[string]string `json:"over_trigger_since"`
	// ClearedAt is when each workspace was last seen back UNDER the trigger. A stamp is
	// dead once it is not strictly newer than its workspace's clear; a PC that re-crosses
	// afterwards writes a fresh stamp, which is newer than the clear and therefore lives.
	ClearedAt map[string]string `json:"cleared_at"`
}

// ParseOverTrigger decodes the stamp file. Empty input is an empty stamp, not an error:
// the file legitimately does not exist until some store crosses for the first time.
func ParseOverTrigger(b []byte) (OverTrigger, error) {
	out := OverTrigger{OverTriggerSince: map[string]string{}, ClearedAt: map[string]string{}}
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
	for k, v := range raw.ClearedAt {
		out.ClearedAt[k] = v
	}
	return out, nil
}

// RenderOverTrigger encodes the stamp file deterministically: sorted keys, LF, one
// trailing newline. Two PCs merging the same pair must produce the same bytes or the
// file conflicts with itself forever.
func RenderOverTrigger(t OverTrigger) []byte {
	var b []byte
	b = append(b, `{"over_trigger_since":`...)
	b = appendStampObject(b, t.OverTriggerSince)
	b = append(b, `,"cleared_at":`...)
	b = appendStampObject(b, t.ClearedAt)
	b = append(b, "}\n"...)
	return b
}

// appendStampObject writes one sorted string map. Both halves of the file go through the
// same code so they can never drift apart in key order or spacing.
func appendStampObject(b []byte, m map[string]string) []byte {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	b = append(b, '{')
	for i, k := range keys {
		if i > 0 {
			b = append(b, ',')
		}
		kj, _ := json.Marshal(k)
		vj, _ := json.Marshal(m[k])
		b = append(b, kj...)
		b = append(b, ':')
		b = append(b, vj...)
	}
	return append(b, '}')
}

// MergeOverTrigger reduces two stamp files: the LATEST clear wins, then MIN over the
// stamps that survive it.
//
// The earliest surviving crossing is the answer because the metric the stamp feeds is
// "how long has this store been over the trigger without a decision" - taking the later
// stamp would reset that clock on every sync and make a starved store look freshly over.
//
// But the clear has to win first. A stamp that is not strictly newer than its workspace's
// newest cleared_at describes a crossing that has already been resolved, and carrying it
// forward is how a converged store stayed alarmed forever: a union over keys read the
// absence on the side that cleared as "no news" and copied the stale stamp straight back.
// A PC that re-crosses after a clear stamps a time NEWER than the clear, so its clock
// starts fresh instead of resuming the dead one.
//
// The tombstone is kept even when no stamp survives it, because it IS the record of the
// clear - dropping it would let the next merge with a lagging PC undo the clear again.
// Its own reduction is MAX, which is monotone, so two PCs reducing the same pair in
// either order reach the same bytes.
//
// An unparseable timestamp loses to a parseable one rather than poisoning the reduction;
// a workspace only one side knows about is kept.
func MergeOverTrigger(ours, theirs []byte) ([]byte, error) {
	o, err := ParseOverTrigger(ours)
	if err != nil {
		o = OverTrigger{OverTriggerSince: map[string]string{}, ClearedAt: map[string]string{}}
	}
	t, err := ParseOverTrigger(theirs)
	if err != nil {
		t = OverTrigger{OverTriggerSince: map[string]string{}, ClearedAt: map[string]string{}}
	}
	out := OverTrigger{OverTriggerSince: map[string]string{}, ClearedAt: map[string]string{}}
	for k, v := range o.ClearedAt {
		out.ClearedAt[k] = v
	}
	for k, tv := range t.ClearedAt {
		if ov, have := out.ClearedAt[k]; have {
			out.ClearedAt[k] = later(ov, tv)
			continue
		}
		out.ClearedAt[k] = tv
	}
	for k, ov := range o.OverTriggerSince {
		if StampIsLive(ov, out.ClearedAt[k]) {
			out.OverTriggerSince[k] = ov
		}
	}
	for k, tv := range t.OverTriggerSince {
		if !StampIsLive(tv, out.ClearedAt[k]) {
			continue
		}
		ov, have := out.OverTriggerSince[k]
		if !have {
			out.OverTriggerSince[k] = tv
			continue
		}
		out.OverTriggerSince[k] = earlier(ov, tv)
	}
	return RenderOverTrigger(out), nil
}

// StampIsLive reports whether a crossing stamp is still the current clock, given its
// workspace's tombstone.
//
// An empty tombstone means the workspace was never cleared. An unparseable time on either
// side KEEPS the stamp: this feeds a starvation alarm, and a garbled tombstone must not be
// able to silence one. Only a clear that is provably at or after the crossing kills it.
func StampIsLive(since, clearedAt string) bool {
	if since == "" {
		return false
	}
	if clearedAt == "" {
		return true
	}
	st, errS := time.Parse(time.RFC3339, since)
	ct, errC := time.Parse(time.RFC3339, clearedAt)
	if errS != nil || errC != nil {
		return true
	}
	return st.After(ct)
}

// later is earlier's twin, for the tombstone: the most recent clear is the one that
// counts, because it names the most recent time the store was seen converged.
func later(a, b string) string {
	if earlier(a, b) == a {
		return b
	}
	return a
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
