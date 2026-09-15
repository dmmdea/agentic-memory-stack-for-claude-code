package gate

import (
	"strconv"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// The advisory block is the gate's product on stdout, and it is a byte-for-byte port of
// GATE:46-55 and :74-81.
//
// The wording is not decoration. The bash advisory this gate replaced printed a warning
// and did nothing, and live sessions ignored it; one store reached 27.4 KB with 126 of
// 180 lines over the cap while the harness silently loaded only part of it. Every line
// here tells the model what happened AND what to do about it, which is why the
// normalization line says the in-context copy is stale rather than just reporting bytes.

// AdvisoryNothingNormalizable is printed when every over-cap line is doctrine.
const AdvisoryNothingNormalizable = "  Nothing normalizable (every over-cap line is doctrine) - re-home doctrine detail into topic files by hand."

// AdvisoryStillOver is printed when the index is still at or over the sync limit after a
// normalization that did shorten something.
const AdvisoryStillOver = "  STILL OVER THE SYNC LIMIT: the remaining over-cap lines are doctrine (never truncated) - re-home doctrine detail into topic files by hand."

// AdvisoryChangedUnderGate is printed when the compare-and-swap found the index had
// moved since the gate read it. Skipping is correct: a live session's whole-file write
// must win over a maintenance truncation, and the next write or the nightly retries.
const AdvisoryChangedUnderGate = "  (index changed under the gate - normalization skipped this time; the next write or the nightly compactor will retry)"

// AdvisoryHeader is the size line plus whichever cap warnings apply.
//
// The byte warning and the line warning are an if/ELSE: a store over the sync limit is
// already being told the harness will not load it, and adding "and the tail is not
// injected" underneath buries the worse problem.
func AdvisoryHeader(bytes, lines, longLines int) []string {
	out := []string{
		"[auto-memory index] " + itoa(bytes) + " B / " + itoa(lines) + " lines (caps: " +
			itoa(store.SyncLimitBytes) + " B sync, " + itoa(store.InjectLimitLines) + " lines injected)",
	}
	if bytes >= store.SyncLimitBytes {
		out = append(out, "  OVER THE SYNC LIMIT: the harness loads only part of this file until it is under the limit.")
	} else if lines >= store.InjectLimitLines {
		out = append(out, "  OVER THE INJECTION CAP: entries past line "+itoa(store.InjectLimitLines)+" are not loaded into context.")
	}
	if longLines > 0 {
		out = append(out, "  "+itoa(longLines)+" index line(s) over "+itoa(store.LineByteCap)+
			" B. The index format is one pointer per entry: \"- [Title](file.md) - hook\". "+
			"Keep the hook to the trigger for opening the file; detail belongs in the fact file.")
	}
	return out
}

// AdvisoryNormalized reports a completed normalization.
func AdvisoryNormalized(floored, before, after int) string {
	return "  NORMALIZED: " + itoa(floored) + " over-cap hook(s) truncated to the line cap; index " +
		itoa(before) + " -> " + itoa(after) + " B. Your in-context copy of MEMORY.md is now STALE - " +
		"re-read it before the next edit, and keep new hooks under " + itoa(store.LineByteCap) +
		" B. Full text of every entry is still in its fact file."
}

func itoa(i int) string { return strconv.Itoa(i) }
