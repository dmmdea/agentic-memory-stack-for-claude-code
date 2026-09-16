// Package store owns the on-disk shape of an auto-memory store: the constants every
// other package measures against, store enumeration (fail-closed, alias-deduped) and
// the paths the tool reads and writes.
//
// ASCII-only source, deliberately. The PowerShell original (memory-store-lib.ps1:7-9)
// builds every non-ASCII runtime character from a code point because PS 5.1 reads a
// BOM-less file as ANSI; the Go port keeps the rule so the two implementations can be
// diffed line for line and so no editor ever "helpfully" rewrites a literal em-dash.
package store

// Every constant here is a port of memory-store-lib.ps1:21-30 (LIB) or of a figure the
// compactor hard-codes (COMPACT). Blueprint section 2.6 is the authority for the list.
const (
	// LineByteCap is the index line hook budget for "- [Title](file.md) - hook". LIB:21.
	LineByteCap = 130
	// TriggerBytes is where the compactor fires, and the convergence floor's default
	// StopBelowBytes. LIB:22.
	TriggerBytes = 20000
	// TriggerLines is the line-count trigger. LIB:23.
	TriggerLines = 160
	// TargetBytes is the hysteresis target the byte floor aims at. LIB:24.
	TargetBytes = 17000
	// TargetLines is the hysteresis target the line floor aims at. LIB:25.
	TargetLines = 140
	// SyncLimitBytes is the harness per-file sync limit: at or above this the harness
	// refuses to sync MEMORY.md at all. LIB:26.
	SyncLimitBytes = 25000
	// InjectLimitLines is how many index lines the harness injects into context. LIB:27.
	InjectLimitLines = 200
	// OversizedFactBytes flags a single fact body. Flag only - bodies are never edited. LIB:28.
	OversizedFactBytes = 10000
	// Mem0MaxChars is mem0-server's MAX_MEMORY_CHARS default: a larger body 413s on
	// migration, forever. LIB:29.
	Mem0MaxChars = 4000
	// MinHookBudget is the floor's give-up threshold: a title that alone eats the cap
	// cannot be floored. LIB:674.
	MinHookBudget = 24
	// WordCutFraction is how far into a truncated string a space must sit before it is
	// used as the cut point. LIB:630.
	WordCutFraction = 0.6
	// TruncDefaultMax is Get-AmTruncatedToBytes's default MaxBytes. LIB:620.
	TruncDefaultMax = 100
	// OrphanHookBudget is the byte budget a re-indexed orphan's hook is truncated to.
	// COMPACT:497 spells it as the line cap minus the line's own overhead allowance.
	OrphanHookBudget = LineByteCap - 40
	// BlastCapFraction bounds one hygiene pass: max(1, floor(entries*0.2)). COMPACT:548.
	BlastCapFraction = 0.2
	// ReceiptTailLines is how much of a receipts JSONL the run-history reader tails. LIB:522.
	ReceiptTailLines = 600
	// EmDash is the canonical index separator, built from its code point and never typed
	// as a literal. LIB:30.
	EmDash = "\u2014"
)

// IndexName is the one file in a store that is not a fact file.
const IndexName = "MEMORY.md"

// TempSuffix is the suffix an interrupted atomic write leaves behind inside the store.
const TempSuffix = ".am-tmp"
