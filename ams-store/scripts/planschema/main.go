// Command planschema writes the JSON Schema for the judge plan from the Go types that
// define it, so the Python producer and the Go consumer cannot drift apart silently.
//
// The plan is written by dream-consolidate.py on the authority and applied by
// `ams-store judge-apply`. Two programs in two languages share one file format; when they
// disagree, the operator believes a decision was applied that was not. So the schema is
// GENERATED here, checked in at docs/schemas/judge-plan.schema.json, and pinned three ways:
//
//   - scripts/planschema/main_test.go fails when the checked-in file is not what this
//     generator produces (an edit to plan.go that forgets the schema fails CI);
//   - internal/judge/plan_corpus_test.go runs ParsePlan over a shared corpus of documents
//     and asserts the decoder's verdict on each;
//   - mem0-server/tests/test_judge_plan_schema.py runs THIS schema over the SAME corpus and
//     asserts the schema's verdict, plus the subset invariant: anything the schema rejects,
//     the decoder must reject too. A schema that refuses a plan the decoder would have
//     applied is a nightly that silently stops deciding.
//
// The schema is deliberately a SUBSET of Validate: JSON Schema 2020-12 cannot express
// "workspaces are unique" or "slugs are unique within a store", so those stay
// decoder-only and the corpus marks them so. Everything structural IS expressed here.
//
// Usage: go run ./scripts/planschema [-o <path>]   (default: ../docs/schemas/judge-plan.schema.json)
//
// ASCII-only source, like every file in this module.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/judge"
)

// obj is a JSON object. Go marshals map keys in sorted order, so the rendered file is
// byte-deterministic without any ordering machinery.
type obj = map[string]any

// Schema builds the plan schema from the judge package's own constants. Every literal
// below that has a Go counterpart IS that counterpart: the verbs, the outcomes, the
// version and the slug rule are read from internal/judge, never retyped.
func Schema() obj {
	nonEmpty := obj{"type": "string", "minLength": 1, "pattern": `\S`}
	empty := obj{"type": "string", "maxLength": 0}

	decision := obj{
		"type":                 "object",
		"additionalProperties": false,
		"description":          "One index entry's verb. Mirrors judge.Decision.",
		"required":             []any{"slug", "verb"},
		"properties": obj{
			"slug":      obj{"type": "string", "pattern": judge.SlugPattern, "description": "The fact file the decision is about."},
			"verb":      obj{"enum": verbs(), "description": "SHORTEN, MIGRATE or KEEP."},
			"new_hook":  obj{"type": "string", "description": "The rewritten hook. Required for SHORTEN, forbidden otherwise."},
			"mem0_text": obj{"type": "string", "description": "Overrides the text a MIGRATE posts. Normally absent."},
			"metadata":  obj{"type": "object", "additionalProperties": obj{"type": "string"}},
		},
		"allOf": []any{
			obj{
				"if":   obj{"required": []any{"verb"}, "properties": obj{"verb": obj{"const": string(judge.VerbShorten)}}},
				"then": obj{"required": []any{"new_hook"}, "properties": obj{"new_hook": nonEmpty, "mem0_text": empty}},
			},
			obj{
				"if":   obj{"required": []any{"verb"}, "properties": obj{"verb": obj{"const": string(judge.VerbMigrate)}}},
				"then": obj{"properties": obj{"new_hook": empty}},
			},
			obj{
				"if":   obj{"required": []any{"verb"}, "properties": obj{"verb": obj{"const": string(judge.VerbKeep)}}},
				"then": obj{"properties": obj{"new_hook": empty, "mem0_text": empty}},
			},
		},
	}

	storePlan := obj{
		"type":                 "object",
		"additionalProperties": false,
		"description":          "One store's decisions plus the outcome of the call that produced them. Mirrors judge.StorePlan.",
		"required":             []any{"workspace"},
		"properties": obj{
			"workspace": nonEmpty,
			"outcome":   obj{"enum": outcomes(), "description": "Defaults to ok when absent."},
			"note":      obj{"type": "string"},
			"decisions": obj{"type": "array", "items": obj{"$ref": "#/$defs/decision"}},
		},
		"allOf": []any{
			obj{
				"if":   obj{"required": []any{"outcome"}, "properties": obj{"outcome": obj{"enum": nonOKOutcomes()}}},
				"then": obj{"properties": obj{"decisions": obj{"maxItems": 0}}},
			},
		},
	}

	return obj{
		"$schema": "https://json-schema.org/draft/2020-12/schema",
		"$id":     "https://raw.githubusercontent.com/dmmdea/agentic-memory-stack-for-claude-code/main/docs/schemas/judge-plan.schema.json",
		"title":   "ams-store judge plan",
		"description": "The nightly store-judge plan: written by dream-consolidate.py on the authority, " +
			"applied by `ams-store judge-apply`. GENERATED from ams-store/internal/judge/plan.go by " +
			"`go run ./scripts/planschema` - do not edit by hand. It is a SUBSET of the Go decoder's rules: " +
			"uniqueness of workspaces and of slugs within a store cannot be expressed here and is enforced by " +
			"judge.Plan.Validate, which is the authority.",
		"type":                 "object",
		"additionalProperties": false,
		"required":             []any{"version", "stores"},
		"properties": obj{
			"version":      obj{"const": judge.PlanVersion, "description": "The only version this build applies."},
			"generated_at": obj{"type": "string", "description": "RFC3339, advisory only."},
			"stores":       obj{"type": "array", "minItems": 1, "items": obj{"$ref": "#/$defs/storePlan"}},
		},
		"$defs": obj{"storePlan": storePlan, "decision": decision},
	}
}

func verbs() []any {
	out := make([]any, 0, len(judge.Verbs))
	for _, v := range judge.Verbs {
		out = append(out, string(v))
	}
	return out
}

func outcomes() []any {
	out := make([]any, 0, len(judge.Outcomes))
	for _, o := range judge.Outcomes {
		out = append(out, string(o))
	}
	return out
}

// nonOKOutcomes is every outcome that means the call did not answer, which is exactly the
// set Validate refuses decisions for.
func nonOKOutcomes() []any {
	out := make([]any, 0, len(judge.Outcomes)-1)
	for _, o := range judge.Outcomes {
		if o == judge.OutcomeOK {
			continue
		}
		out = append(out, string(o))
	}
	return out
}

// Render is the exact bytes of the checked-in file: indented with two spaces and ending
// in a newline, so `go run ./scripts/planschema` and the test compare byte for byte.
func Render() ([]byte, error) {
	b, err := json.MarshalIndent(Schema(), "", "  ")
	if err != nil {
		return nil, err
	}
	return append(b, '\n'), nil
}

// DefaultOut is the checked-in location, relative to the module root.
const DefaultOut = "../docs/schemas/judge-plan.schema.json"

func main() {
	out := flag.String("o", DefaultOut, "where to write the schema")
	flag.Parse()
	b, err := Render()
	if err != nil {
		fmt.Fprintf(os.Stderr, "planschema: %v\n", err)
		os.Exit(1)
	}
	if err := os.MkdirAll(filepath.Dir(*out), 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "planschema: %v\n", err)
		os.Exit(1)
	}
	if err := os.WriteFile(*out, b, 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "planschema: %v\n", err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "planschema: wrote %s (%d bytes)\n", *out, len(b))
}
