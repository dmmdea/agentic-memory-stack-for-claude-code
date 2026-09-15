// Package judge applies a judge plan to a store under every apply-guard the PowerShell
// compactor carries. It is HUB-ONLY: the Lenovo judges its own checkout of the hub,
// never a live store, and a PC that tried to apply a plan would be a second judge on the
// same store - the concurrency bug of v1 in another form.
//
// The division of labour (blueprint section 13 Q1) is deliberate: the LLM call stays in
// the Python nightly chain, which writes a plan FILE; this package decides what of that
// plan may be applied and applies it. Every guard here exists because a version without
// it did damage:
//
//   - strict decrease - a "shortening" that grew the index, applied nightly;
//   - the round-trip check - a hook carrying a markdown link injected a phantom slug,
//     a ghost hygiene could never remove, and the store's maintenance died forever;
//   - the anchor rule - a rewrite that kept the topic and dropped the port number;
//   - the seal - the same line re-offered and re-worded every night (drift by a
//     thousand small edits);
//   - the blast cap - one removal loop that could empty an entire index;
//   - protected-set overflow - the hard rule "doctrine is untouchable" must fail LOUD
//     rather than be loosened autonomously;
//   - write-then-verify with an undo - a migration that deleted the file before the
//     corpus record was provably readable back by id.
//
// ASCII-only source, like every file in this module.
package judge

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strings"
)

// Verb is one decision the judge reached about one index entry.
type Verb string

// The only three verbs. Anything else is a plan this package refuses to apply: an
// unknown verb means the producer and the consumer disagree about what a plan says, and
// guessing at that disagreement is how a KEEP becomes a delete.
const (
	// VerbShorten rewrites the entry's hook to fit the 130 B line cap. The new text must
	// be strictly shorter, must round-trip, and must keep an anchor.
	VerbShorten Verb = "SHORTEN"
	// VerbMigrate moves a pullable fact into mem0 and removes it from the store. The
	// file is deleted only after a byte-equal read-back BY ID.
	VerbMigrate Verb = "MIGRATE"
	// VerbKeep leaves the line exactly as it is. The safe default.
	VerbKeep Verb = "KEEP"
)

// Outcome records what the judge CALL did, independently of what its plan says. It is
// carried in the plan file so an attempt that produced no decisions is still an outcome
// with a receipt, never a non-event: "the judge returned nothing" and "the judge had
// nothing to say" are different facts and only one of them is a defect to chase.
type Outcome string

const (
	// OutcomeOK is a judge call that answered and parsed.
	OutcomeOK Outcome = "ok"
	// OutcomeUnavailable is a judge that could not be called at all (timeout, non-zero
	// exit, lock held). There is NO local fallback judge: the deterministic work still
	// runs, the judge-only work waits, and the run is not productive.
	OutcomeUnavailable Outcome = "unavailable"
	// OutcomeEmpty is a judge call that SUCCEEDED and returned whitespace. In the
	// PowerShell original an empty string is falsy, so this outcome wrote no ledger row
	// at all and was invisible to the usage report: a failure the ledger cannot count is
	// a failure nobody fixes.
	OutcomeEmpty Outcome = "empty"
	// OutcomeParseFail is a judge call whose output could not be parsed as a plan.
	OutcomeParseFail Outcome = "parse_fail"
)

// Decision is one entry's verb.
//
// Field names are the wire contract with dream-consolidate.py. They are snake_case
// because every other JSON artifact in this stack is, and they are validated strictly:
// an unknown field is a producer/consumer mismatch, not a nicety to ignore.
type Decision struct {
	// Slug is the fact file the decision is about, e.g. "wsl2-traps.md".
	Slug string `json:"slug"`
	// Verb is SHORTEN, MIGRATE or KEEP.
	Verb Verb `json:"verb"`
	// NewHook is the rewritten hook text. Required for SHORTEN, forbidden otherwise.
	NewHook string `json:"new_hook,omitempty"`
	// Mem0Text overrides the text a MIGRATE posts. Normally empty: the text is built
	// from the fact file VERBATIM (description + blank line + body, trimmed) so a retry
	// produces identical bytes and deduplicates instead of creating a second variant.
	Mem0Text string `json:"mem0_text,omitempty"`
	// Metadata is merged into the mem0 record's metadata on a MIGRATE. tier, origin_slug,
	// workspace and source are set by this package and win over anything here.
	Metadata map[string]string `json:"metadata,omitempty"`
}

// StorePlan is one store's decisions plus the outcome of the call that produced them.
type StorePlan struct {
	// Workspace is the harness slug, which is the workspace id on every PC.
	Workspace string `json:"workspace"`
	// Outcome defaults to OutcomeOK when empty.
	Outcome Outcome `json:"outcome,omitempty"`
	// Note is the producer's free text, carried into the receipt.
	Note string `json:"note,omitempty"`
	// Decisions may be empty: a judge that kept everything is a valid, successful plan.
	Decisions []Decision `json:"decisions"`
}

// Plan is the whole plan file.
type Plan struct {
	// Version is the schema version. 1 is the only version this build applies.
	Version int `json:"version"`
	// GeneratedAt is RFC3339, advisory only. The 20 h judge window is computed from
	// RECEIPTS, never from a timestamp the producer wrote about itself.
	GeneratedAt string      `json:"generated_at,omitempty"`
	Stores      []StorePlan `json:"stores"`
}

// PlanVersion is the schema version this build understands.
const PlanVersion = 1

// reSlug is the slug rule, and the only one there is (LIB:223-224): no whitespace, no
// closing paren, ending in .md. There is no slugification function anywhere in this
// system - slugs are consumed exactly as the harness writes them - so validation is the
// one place a malformed slug can be caught.
var reSlug = regexp.MustCompile(`^[^)\s]+\.md$`)

// LoadPlan reads and validates a plan file.
//
// Decoding is STRICT: unknown fields are rejected rather than ignored. A plan is written
// by a separate program in a different language; a field this build silently drops is a
// decision the operator believes was applied and was not.
func LoadPlan(path string) (*Plan, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read plan %s: %w", path, err)
	}
	return ParsePlan(b)
}

// ParsePlan validates a plan document.
func ParsePlan(data []byte) (*Plan, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		// Absent and corrupt must not collapse, and neither may be read as "an empty
		// plan": an empty plan applies nothing and would be receipted as a clean run.
		return nil, fmt.Errorf("plan is empty (truncated write?)")
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var p Plan
	if err := dec.Decode(&p); err != nil {
		return nil, fmt.Errorf("parse plan: %w", err)
	}
	if err := p.Validate(); err != nil {
		return nil, err
	}
	return &p, nil
}

// Validate applies every structural rule. It returns the FIRST violation, naming the
// store and slug, so a producer bug is one message away from its cause.
func (p *Plan) Validate() error {
	if p.Version != PlanVersion {
		return fmt.Errorf("plan version %d is not supported (this build applies version %d)", p.Version, PlanVersion)
	}
	if len(p.Stores) == 0 {
		return fmt.Errorf("plan names no stores")
	}
	seenWorkspace := make(map[string]bool, len(p.Stores))
	for i := range p.Stores {
		sp := &p.Stores[i]
		if strings.TrimSpace(sp.Workspace) == "" {
			return fmt.Errorf("plan store %d has no workspace", i)
		}
		if seenWorkspace[sp.Workspace] {
			return fmt.Errorf("plan names workspace %q twice", sp.Workspace)
		}
		seenWorkspace[sp.Workspace] = true
		if sp.Outcome == "" {
			sp.Outcome = OutcomeOK
		}
		switch sp.Outcome {
		case OutcomeOK, OutcomeUnavailable, OutcomeEmpty, OutcomeParseFail:
		default:
			return fmt.Errorf("store %q: unknown outcome %q", sp.Workspace, sp.Outcome)
		}
		if sp.Outcome != OutcomeOK && len(sp.Decisions) > 0 {
			return fmt.Errorf("store %q: outcome %q carries %d decision(s); a call that did not answer has none",
				sp.Workspace, sp.Outcome, len(sp.Decisions))
		}
		seenSlug := make(map[string]bool, len(sp.Decisions))
		for j := range sp.Decisions {
			d := &sp.Decisions[j]
			if !reSlug.MatchString(d.Slug) {
				return fmt.Errorf("store %q decision %d: %q is not a slug (no whitespace, no ')', must end .md)", sp.Workspace, j, d.Slug)
			}
			if seenSlug[d.Slug] {
				return fmt.Errorf("store %q: two decisions for %s", sp.Workspace, d.Slug)
			}
			seenSlug[d.Slug] = true
			switch d.Verb {
			case VerbShorten:
				if strings.TrimSpace(d.NewHook) == "" {
					return fmt.Errorf("store %q: SHORTEN %s carries no new_hook", sp.Workspace, d.Slug)
				}
				if d.Mem0Text != "" {
					return fmt.Errorf("store %q: SHORTEN %s carries mem0_text", sp.Workspace, d.Slug)
				}
			case VerbMigrate:
				if d.NewHook != "" {
					return fmt.Errorf("store %q: MIGRATE %s carries new_hook", sp.Workspace, d.Slug)
				}
			case VerbKeep:
				if d.NewHook != "" || d.Mem0Text != "" {
					return fmt.Errorf("store %q: KEEP %s carries an edit", sp.Workspace, d.Slug)
				}
			case "":
				return fmt.Errorf("store %q decision %d (%s): no verb", sp.Workspace, j, d.Slug)
			default:
				return fmt.Errorf("store %q decision %d (%s): unknown verb %q (SHORTEN, MIGRATE or KEEP)", sp.Workspace, j, d.Slug, d.Verb)
			}
		}
	}
	return nil
}

// Store returns the plan for one workspace.
func (p *Plan) Store(workspace string) (*StorePlan, bool) {
	for i := range p.Stores {
		if p.Stores[i].Workspace == workspace {
			return &p.Stores[i], true
		}
	}
	return nil, false
}
