package frontmatter

// Field-aware three-way merge of a fact file's leading --- block.
//
// A fact file's frontmatter is not prose and must not be merged as prose. Two PCs that
// each harvested their own `hook:` produce a one-line difference that a line merge calls
// a conflict; merged BY FIELD it is not a conflict at all, because `hook:` has a rule
// ("the judge's value unless the local side changed it") and `modified:` is advisory.
// Merging the block by field is what keeps `conflict-in-history` meaning "two people
// really wrote different prose" instead of "two people both ran derive".

import (
	"regexp"
	"strings"
)

// Field names with rules of their own (blueprint section 4.5).
const (
	FieldHook     = "hook"
	FieldModified = "modified"
	FieldMigrated = "migrated"
)

// reKeyLine matches a frontmatter line that declares a key at ANY indentation, which is
// how `type:` and `modified:` appear (nested under `metadata:`) in every real fact file.
var reKeyLine = regexp.MustCompile(`^(\s*)([A-Za-z0-9_.\-]+)\s*:(.*)$`)

// blockLine is one line of a frontmatter block. key is "" for a line that declares no
// key (a list item, a comment, a wrapped value).
type blockLine struct {
	key string
	raw string
}

// splitBlock decomposes a block into lines, keying each declared field.
//
// A key that appears twice gets a distinct slot ("name", "name#2", ...) so a malformed
// file with a duplicate key still merges deterministically instead of collapsing the two
// occurrences into one.
func splitBlock(block string) []blockLine {
	if block == "" {
		return nil
	}
	seen := map[string]int{}
	raws := strings.Split(strings.ReplaceAll(block, "\r\n", "\n"), "\n")
	out := make([]blockLine, 0, len(raws))
	for _, raw := range raws {
		m := reKeyLine.FindStringSubmatch(raw)
		if m == nil {
			out = append(out, blockLine{raw: raw})
			continue
		}
		key := strings.ToLower(m[2])
		seen[key]++
		if n := seen[key]; n > 1 {
			key = key + "#" + string(rune('0'+n%10))
		}
		out = append(out, blockLine{key: key, raw: raw})
	}
	return out
}

func keyMap(lines []blockLine) map[string]string {
	m := make(map[string]string, len(lines))
	for _, l := range lines {
		if l.key != "" {
			m[l.key] = l.raw
		}
	}
	return m
}

// MergeInput is one three-way frontmatter merge.
type MergeInput struct {
	Base, Ours, Theirs string
	// OursNewer is the winner of the commit-time (then machine-id) race. It decides
	// every field both sides changed differently, and it is NEVER derived from the
	// `modified:` stamp - that stamp is model-written and advisory.
	OursNewer bool
	// BasePresent distinguishes "the file is new on both sides" (no base to diff
	// against) from "the base had an empty block".
	BasePresent bool
}

// MergeBlocks merges three frontmatter blocks field by field.
//
// Key order is deterministic - ours' order, then keys only theirs has, appended - so two
// PCs merging the same pair produce the same bytes and MEMORY.md stays re-derivable
// rather than mergeable.
func MergeBlocks(in MergeInput) (merged string, conflicted []string) {
	ourLines := splitBlock(in.Ours)
	theirLines := splitBlock(in.Theirs)
	baseMap := keyMap(splitBlock(in.Base))
	ourMap := keyMap(ourLines)
	theirMap := keyMap(theirLines)

	if in.Ours == in.Theirs {
		return in.Ours, nil
	}

	out := make([]string, 0, len(ourLines)+4)
	emitted := map[string]bool{}

	resolve := func(key string) (raw string, keep bool, conflict bool) {
		ourRaw, hasOurs := ourMap[key]
		theirRaw, hasTheirs := theirMap[key]
		baseRaw, hasBase := baseMap[key]
		if !in.BasePresent {
			hasBase = false
		}

		base := strings.ToLower(strings.SplitN(key, "#", 2)[0])
		switch base {
		case FieldMigrated:
			// Carried: if either side has it and the other does not, keep it. The id is
			// what lets the judge UPDATE a re-created slug instead of filing a fresh
			// variant every night, so losing it is losing the dedup.
			switch {
			case hasOurs && !hasTheirs:
				return ourRaw, true, false
			case !hasOurs && hasTheirs:
				return theirRaw, true, false
			case hasOurs && hasTheirs && ourRaw != theirRaw:
				if in.OursNewer {
					return ourRaw, true, true
				}
				return theirRaw, true, true
			case hasOurs:
				return ourRaw, true, false
			}
			return "", false, false

		case FieldHook:
			// The judge's value, UNLESS the local side changed hook: itself. If both
			// changed it, the local value stands: a PC's harvest is the authority on
			// its own store's hook text until the judge shortens one nobody has touched.
			ourChanged := hasOurs != hasBase || (hasOurs && hasBase && ourRaw != baseRaw)
			theirChanged := hasTheirs != hasBase || (hasTheirs && hasBase && theirRaw != baseRaw)
			switch {
			case ourChanged && hasOurs:
				return ourRaw, true, false
			case theirChanged && hasTheirs:
				return theirRaw, true, false
			case ourChanged && !hasOurs, theirChanged && !hasTheirs:
				return "", false, false
			case hasBase:
				return baseRaw, true, false
			case hasOurs:
				return ourRaw, true, false
			case hasTheirs:
				return theirRaw, true, false
			}
			return "", false, false

		case FieldModified:
			// Advisory only: never a tiebreak, never a conflict. Take the
			// newer-COMMITTING side's value, and drop the key when that side has none.
			if in.OursNewer {
				return ourRaw, hasOurs, false
			}
			return theirRaw, hasTheirs, false
		}

		// Every other key, `name:`, `description:`, the nested `type:` and any key this
		// tool does not know about: plain three-way.
		switch {
		case hasOurs && hasTheirs && ourRaw == theirRaw:
			return ourRaw, true, false
		case !hasOurs && !hasTheirs:
			return "", false, false
		}
		ourChanged := hasOurs != hasBase || (hasOurs && hasBase && ourRaw != baseRaw)
		theirChanged := hasTheirs != hasBase || (hasTheirs && hasBase && theirRaw != baseRaw)
		switch {
		case ourChanged && !theirChanged:
			return ourRaw, hasOurs, false
		case theirChanged && !ourChanged:
			return theirRaw, hasTheirs, false
		case !ourChanged && !theirChanged:
			return baseRaw, hasBase, false
		}
		if in.OursNewer {
			return ourRaw, hasOurs, true
		}
		return theirRaw, hasTheirs, true
	}

	for _, l := range ourLines {
		if l.key == "" {
			out = append(out, l.raw)
			continue
		}
		emitted[l.key] = true
		raw, keep, conflict := resolve(l.key)
		if conflict {
			conflicted = append(conflicted, l.key)
		}
		if keep {
			out = append(out, raw)
		}
	}
	for _, l := range theirLines {
		if l.key == "" || emitted[l.key] {
			continue
		}
		emitted[l.key] = true
		raw, keep, conflict := resolve(l.key)
		if conflict {
			conflicted = append(conflicted, l.key)
		}
		if keep {
			out = append(out, raw)
		}
	}
	return strings.Join(out, "\n"), conflicted
}
