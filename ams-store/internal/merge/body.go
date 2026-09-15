package merge

import (
	"context"
	"fmt"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/frontmatter"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
)

// mergeContent merges a fact file: frontmatter by field, body three-way by lines.
func (e *Engine) mergeContent(ctx context.Context, p string, base, ours, theirs sideBlob, rc resolveContext) ([]byte, []Conflict, error) {
	oursWon, tiebreak := rc.oursWins()
	winner, loser := rc.winnerLoser(oursWon)

	ourFM, ourBody := splitFile(ours.data)
	theirFM, theirBody := splitFile(theirs.data)
	baseFM, baseBody := splitFile(base.data)

	var conflicts []Conflict

	mergedFM, fieldConflicts := frontmatter.MergeBlocks(frontmatter.MergeInput{
		Base:        baseFM,
		Ours:        ourFM,
		Theirs:      theirFM,
		OursNewer:   oursWon,
		BasePresent: base.present,
	})
	for _, key := range fieldConflicts {
		conflicts = append(conflicts, Conflict{
			Path: p, Kind: "field:" + key,
			WinnerCommit: winner, LoserCommit: loser, Tiebreak: tiebreak,
		})
	}

	var mergedBody string
	switch {
	case ourBody == theirBody:
		mergedBody = ourBody
	default:
		mf, err := gitx.MergeFile(ctx, e.opt(),
			[]byte(ourBody), []byte(baseBody), []byte(theirBody),
			"ours", "base", "theirs")
		if err != nil {
			return nil, nil, fmt.Errorf("body merge of %s: %w", p, err)
		}
		if !mf.Conflict {
			mergedBody = string(mf.Merged)
			break
		}
		// A real body conflict: one side wins whole, the loser stays in history, and no
		// conflict marker is ever written into a store.
		if oursWon {
			mergedBody = ourBody
		} else {
			mergedBody = theirBody
		}
		conflicts = append(conflicts, Conflict{
			Path: p, Kind: "body",
			WinnerCommit: winner, LoserCommit: loser, Tiebreak: tiebreak,
		})
	}

	if mergedFM == "" && !hasFrontmatter(ours.data) && !hasFrontmatter(theirs.data) {
		return toLF([]byte(mergedBody)), conflicts, nil
	}
	return toLF([]byte("---\n" + mergedFM + "\n---\n" + mergedBody)), conflicts, nil
}

func hasFrontmatter(b []byte) bool {
	return frontmatter.ParseText(string(toLF(b))) != nil
}

// splitFile separates a fact file into its frontmatter block and its body. A file with no
// block is all body - and harvest deliberately never adds one, so this is an ordinary
// shape and not an error.
func splitFile(b []byte) (block, body string) {
	text := string(toLF(b))
	fm := frontmatter.ParseText(text)
	if fm == nil {
		return "", text
	}
	// fm.Body begins at the newline after the closing ---, so the blank line every fact
	// file carries between its frontmatter and its prose is part of the body and
	// survives reassembly byte for byte.
	return fm.Raw, fm.Body
}
