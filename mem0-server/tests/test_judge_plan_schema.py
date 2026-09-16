"""The judge plan's schema side of the cross-language contract (register P4-1b).

`dream-consolidate.py` writes the nightly store-judge plan and validates it against
docs/schemas/judge-plan.schema.json BEFORE writing; `ams-store judge-apply` decodes it with
judge.ParsePlan, which is the authority. This test runs the schema over the SAME corpus the
Go decoder test runs (ams-store/internal/judge/testdata/plan-corpus.json) and asserts:

  1. the schema's verdict on every case matches the corpus's `schema` column, and
  2. the subset invariant: anything the schema rejects, the decoder rejects too.

(2) is the one that matters in production. A schema stricter than the decoder makes the
producer refuse to write a plan the consumer would have applied - the nightly silently
stops deciding for that store, with no failure anywhere. JSON Schema cannot express
uniqueness across array items, so the schema is a strict subset by design and the two
duplicate cases in the corpus are marked decoder-only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema", reason="jsonschema is the authority's validator")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPO_ROOT / "docs" / "schemas" / "judge-plan.schema.json"
CORPUS = REPO_ROOT / "ams-store" / "internal" / "judge" / "testdata" / "plan-corpus.json"


def _validator():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def _cases():
    return json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]


def test_schema_file_exists_and_is_a_valid_2020_12_schema():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    # Generated, never hand-edited: the generator's test fails when the file is stale.
    assert "scripts/planschema" in schema["description"]
    _validator()


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_schema_verdict_matches_the_corpus(case):
    v = _validator()
    errors = sorted(v.iter_errors(case["doc"]), key=lambda e: list(e.path))
    accepted = not errors
    want = case["schema"] == "accept"
    if want and not accepted:
        pytest.fail(
            f"the schema REJECTED a case it must accept ({case['why']}): "
            + "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:3])
        )
    if not want and accepted:
        pytest.fail(f"the schema ACCEPTED a case it must reject ({case['why']})")


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_schema_rejections_are_a_subset_of_the_decoder_s(case):
    """A schema stricter than the decoder is a producer that stops writing plans."""
    if case["schema"] == "reject":
        assert case["decoder"] == "reject", (
            f"case {case['name']!r} is rejected by the schema but accepted by the decoder; "
            "the schema must stay a SUBSET of judge.Plan.Validate"
        )


def test_the_corpus_covers_both_verdicts_on_both_sides():
    """A corpus that only ever accepts proves nothing."""
    cases = _cases()
    assert sum(1 for c in cases if c["schema"] == "accept") >= 3
    assert sum(1 for c in cases if c["schema"] == "reject") >= 10
    assert sum(1 for c in cases if c["decoder"] == "reject" and c["schema"] == "accept") >= 1, (
        "at least one decoder-only rejection must be covered, or the subset invariant is vacuous"
    )
