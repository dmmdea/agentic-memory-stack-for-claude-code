package merge

import "github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"

// Tiebreak names which rule decided a body conflict, so a receipt says WHY one side won.
const (
	TiebreakCommitTime = "commit-time"
	TiebreakMachineID  = "machine-id"
)

// Conflict is one reportable `conflict-in-history` finding.
type Conflict struct {
	Path string `json:"path"`
	// Kind is "body" or "field:<name>".
	Kind string `json:"kind"`
	// WinnerCommit and LoserCommit are the last commits to this path on each side. The
	// loser's version is still reachable at LoserCommit: that is what "the loser stays
	// in history" buys, and lint reports it with the commit id.
	WinnerCommit string `json:"winner_commit"`
	LoserCommit  string `json:"loser_commit"`
	Tiebreak     string `json:"tiebreak,omitempty"`
}

// resolution is one conflicted path's outcome.
type resolution struct {
	path        string
	op          Op
	content     []byte
	resurrected bool
	conflicts   []Conflict
}

// sideBlob is one side's version of a path.
type sideBlob struct {
	present bool
	oid     string
	data    []byte
}

// resolveContext carries everything a single path's resolution needs.
type resolveContext struct {
	oursCommit   gitx.PathCommit
	theirsCommit gitx.PathCommit
	hasOurs      bool
	hasTheirs    bool
}

// oursWins decides a real content conflict: the side whose last COMMIT to that path is
// newer, then the machine id, and never the model-written `modified:` stamp.
//
// The machine-id half is not decoration. Two PCs syncing seconds apart routinely commit
// in the same second, and a non-deterministic tiebreak would have the two of them
// converge on different bytes and then merge each other's results forever.
func (rc resolveContext) oursWins() (bool, string) {
	switch {
	case !rc.hasTheirs:
		return true, TiebreakCommitTime
	case !rc.hasOurs:
		return false, TiebreakCommitTime
	case rc.oursCommit.Unix != rc.theirsCommit.Unix:
		return rc.oursCommit.Unix > rc.theirsCommit.Unix, TiebreakCommitTime
	}
	return rc.oursCommit.Machine > rc.theirsCommit.Machine, TiebreakMachineID
}

func (rc resolveContext) winnerLoser(oursWon bool) (winner, loser string) {
	if oursWon {
		return rc.oursCommit.OID, rc.theirsCommit.OID
	}
	return rc.theirsCommit.OID, rc.oursCommit.OID
}
