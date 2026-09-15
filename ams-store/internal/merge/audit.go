package merge

import (
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
)

// auditPaths picks the paths the deletion table (section 4.3) must decide for itself
// instead of accepting what `merge-tree` made of them.
//
// It is not only merge-tree's conflicted list, and the reason is rename detection.
//
// DESIGN turns renames off so that a judge migration is never paired with an unrelated
// new fact file. `merge.renames=false` is set on every history repo - and it is NOT
// enough. Measured on two installed gits while building this:
//
//	git 2.55  merge.renames=false is honoured; `-X no-renames` also works
//	git 2.43  NOTHING turns rename detection off for `merge-tree --write-tree`:
//	          not merge.renames, not diff.renames, not merge.renameLimit, and `-X`
//	          is accepted in silence and ignored
//
// On that git, a PC that re-homes a fact under a new slug has its remove+add read as a
// rename: the judge's version of the old path is folded into the new one and DISAPPEARS
// from the merged tree, with no conflicted path, no report, and nothing for the deletion
// table to rule on. A fact two PCs disagreed about would vanish quietly - the one outcome
// the whole design is built to prevent - and whether it happened would depend on which
// git a given PC had installed.
//
// So git's rename heuristic is not trusted to decide presence. Every path where the two
// sides DISAGREE about existence is ruled on by the table, on every git. What is left to
// merge-tree is what it is good at: merging content for paths both sides still have.
//
// The audit is cheap because it decides on tree entries, not bytes: a path is only
// returned - and its blobs only read - when the OIDs cannot settle it.
func auditPaths(trees map[string]map[string]gitx.TreeEntry, baseTree, oursTree, theirsTree string, mt gitx.MergeTreeOutput) []string {
	conflicted := make(map[string]bool, len(mt.Conflicted))
	for _, p := range mt.Conflicted {
		conflicted[p] = true
	}

	universe := make(map[string]bool)
	for _, t := range []string{baseTree, oursTree, theirsTree, mt.Tree} {
		for p := range trees[t] {
			universe[p] = true
		}
	}

	out := make([]string, 0, len(mt.Conflicted))
	for p := range universe {
		if conflicted[p] {
			out = append(out, p)
			continue
		}
		ourE, inOurs := trees[oursTree][p]
		theirE, inTheirs := trees[theirsTree][p]
		if inOurs && inTheirs {
			// Both sides still have it. This is a content question, and merge-tree
			// answered it without conflict; the table has nothing to add.
			continue
		}
		mergedE, inMerged := trees[mt.Tree][p]
		baseE, inBase := trees[baseTree][p]

		if !inBase {
			// Added on exactly one side, or on neither. The table keeps the adding
			// side's bytes; anything else in the merged tree is git having paired this
			// path with something.
			switch {
			case !inOurs && !inTheirs:
				if inMerged {
					out = append(out, p)
				}
			case inOurs:
				if !inMerged || mergedE.OID != ourE.OID {
					out = append(out, p)
				}
			default:
				if !inMerged || mergedE.OID != theirE.OID {
					out = append(out, p)
				}
			}
			continue
		}

		// Present at the base and gone from at least one side: a deletion row.
		switch {
		case !inOurs && !inTheirs:
			// Both deleted it. Deleted, and only worth a look if it is somehow back.
			if inMerged {
				out = append(out, p)
			}
		case inOurs && ourE.OID == baseE.OID, inTheirs && theirE.OID == baseE.OID:
			// The surviving side is byte-identical to the base, so nobody modified it
			// and the deletion stands. No normalized comparison needed: identical bytes
			// are identical under any normalization.
			if inMerged {
				out = append(out, p)
			}
		default:
			// The surviving side differs from the base. Whether that is a real
			// modification or only `hook:`/`modified:`/CRLF churn is exactly what the
			// table's normalized comparison is for, and it needs the bytes.
			out = append(out, p)
		}
	}
	return out
}
