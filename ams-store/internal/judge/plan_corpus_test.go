package judge_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/judge"
)

// The plan is written by a program in another language. testdata/plan-corpus.json is the
// contract both sides are held to: this test asserts the DECODER's verdict on every case,
// and mem0-server/tests/test_judge_plan_schema.py asserts the SCHEMA's verdict on the same
// file plus the subset invariant. Neither side can move without the other noticing.
type corpusCase struct {
	Name    string          `json:"name"`
	Why     string          `json:"why"`
	Decoder string          `json:"decoder"`
	Schema  string          `json:"schema"`
	Doc     json.RawMessage `json:"doc"`
}

func loadCorpus(t *testing.T) []corpusCase {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", "plan-corpus.json"))
	if err != nil {
		t.Fatalf("read corpus: %v", err)
	}
	var doc struct {
		Cases []corpusCase `json:"cases"`
	}
	if err := json.Unmarshal(b, &doc); err != nil {
		t.Fatalf("parse corpus: %v", err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("the corpus is empty")
	}
	return doc.Cases
}

func TestPlan_CorpusMatchesTheDecoder(t *testing.T) {
	for _, c := range loadCorpus(t) {
		t.Run(c.Name, func(t *testing.T) {
			switch c.Decoder {
			case "accept", "reject":
			default:
				t.Fatalf("corpus case %q has decoder=%q, want accept or reject", c.Name, c.Decoder)
			}
			switch c.Schema {
			case "accept", "reject":
			default:
				t.Fatalf("corpus case %q has schema=%q, want accept or reject", c.Name, c.Schema)
			}
			// The invariant, asserted from this side too so a corpus edit cannot quietly
			// declare a schema-rejects/decoder-accepts case: that shape is a producer
			// writing plans that never apply.
			if c.Schema == "reject" && c.Decoder == "accept" {
				t.Fatalf("corpus case %q says the schema rejects what the decoder accepts;"+
					" the schema must stay a SUBSET of the decoder's rules", c.Name)
			}
			_, err := judge.ParsePlan(c.Doc)
			if c.Decoder == "accept" && err != nil {
				t.Errorf("ParsePlan rejected a case the corpus accepts (%s): %v", c.Why, err)
			}
			if c.Decoder == "reject" && err == nil {
				t.Errorf("ParsePlan ACCEPTED a case the corpus rejects (%s)", c.Why)
			}
		})
	}
}

// The generated schema carries these enums and this pattern. Adding a verb or an outcome
// without adding it here leaves the schema rejecting a plan this build applies, so the
// exhaustiveness lives in a test that must be edited in the same change.
func TestPlan_VerbsAndOutcomesAreExhaustive(t *testing.T) {
	if got, want := judge.Verbs, []judge.Verb{judge.VerbShorten, judge.VerbMigrate, judge.VerbKeep}; !sameVerbs(got, want) {
		t.Errorf("Verbs = %v, want %v (a new verb must also reach scripts/planschema)", got, want)
	}
	want := []judge.Outcome{judge.OutcomeOK, judge.OutcomeUnavailable, judge.OutcomeEmpty, judge.OutcomeParseFail}
	if !sameOutcomes(judge.Outcomes, want) {
		t.Errorf("Outcomes = %v, want %v", judge.Outcomes, want)
	}
	if judge.SlugPattern != `^[^)\s]+\.md$` {
		t.Errorf("SlugPattern = %q; the generated schema carries this literal", judge.SlugPattern)
	}
	if judge.PlanVersion != 1 {
		t.Errorf("PlanVersion = %d; the schema pins it as a const", judge.PlanVersion)
	}
}

func sameVerbs(a, b []judge.Verb) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func sameOutcomes(a, b []judge.Outcome) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
