package merge

import (
	"bytes"
	"context"
)

// Op is what a resolution does to a path.
type Op string

const (
	OpReplace Op = "replace"
	OpDelete  Op = "delete"
	// OpNone means the merged tree already carries the right blob and nothing needs
	// restaging.
	OpNone Op = "none"
)

// resolvePath applies blueprint section 4.3's three-way deletion table and, for the rows
// that need it, the field-aware frontmatter merge and the three-way body merge.
//
// "Unchanged" here is a NORMALIZED comparison: line endings collapsed and the `hook:` and
// `modified:` lines ignored. Without that, a PC that merely harvested its own hook looks
// like it modified the file and resurrects a deletion the judge made on purpose.
func (e *Engine) resolvePath(ctx context.Context, p string, base, ours, theirs sideBlob, rc resolveContext) (resolution, error) {
	res := resolution{path: p, op: OpNone}

	// The shared state stamp is not a fact file and does not follow the table: the first
	// crossing is what the starvation metric measures, so the earliest stamp wins.
	if p == OverTriggerPath {
		merged, err := MergeOverTrigger(ours.data, theirs.data)
		if err != nil {
			return res, err
		}
		res.op = OpReplace
		res.content = merged
		return res, nil
	}

	switch {
	case base.present && ours.present && !theirs.present:
		if NormalizedEqual(base.data, ours.data) {
			res.op = OpDelete
			return res, nil
		}
		// Modified here, deleted there: keep the modified side and say so.
		res.op = OpReplace
		res.content = toLF(ours.data)
		res.resurrected = true
		return res, nil

	case base.present && !ours.present && theirs.present:
		if NormalizedEqual(base.data, theirs.data) {
			res.op = OpDelete
			return res, nil
		}
		res.op = OpReplace
		res.content = toLF(theirs.data)
		res.resurrected = true
		return res, nil

	case !ours.present && !theirs.present:
		res.op = OpDelete
		return res, nil

	case ours.present && !theirs.present:
		// Added on our side only.
		res.op = OpReplace
		res.content = toLF(ours.data)
		return res, nil

	case !ours.present && theirs.present:
		res.op = OpReplace
		res.content = toLF(theirs.data)
		return res, nil
	}

	// Both sides have the file. Even when the bytes differ only in line endings or in
	// `hook:`, the content merge is what produces the right OUTPUT - Canon is a
	// comparison form and writing it out would delete the hook the index renders from.
	if bytes.Equal(ours.data, theirs.data) {
		res.op = OpReplace
		res.content = toLF(ours.data)
		return res, nil
	}
	merged, conflicts, err := e.mergeContent(ctx, p, base, ours, theirs, rc)
	if err != nil {
		return res, err
	}
	res.op = OpReplace
	res.content = merged
	res.conflicts = conflicts
	return res, nil
}
