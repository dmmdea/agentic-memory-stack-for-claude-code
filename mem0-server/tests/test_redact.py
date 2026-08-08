"""Unit tests for server-side secret redaction (the stored-text chokepoint).

prompt_text is persisted in the in_progress episode checkpoint from BOTH /v1/episodes/checkpoint
and /v1/context/bundle (via _checkpoint_core). Credentials pasted into a prompt must be scrubbed
before storage. Mirrors the L1a/PreCompact/SkillOpt reader pattern set — one canonical set, FOUR
copies (three in this repo, plus SkillOpt on the offline replica host). The three in-repo copies
are pinned to each other by the shared fixture iterated at the bottom of this file.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD = os.path.normpath(os.path.join(_HERE, "..", "redact.py"))
_spec = importlib.util.spec_from_file_location("redact", _MOD)
redact = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redact)


def test_count_redactions_counts_without_mutating():
    """W5 T6.1: count-only telemetry — runtime-derived keys, zero mutation."""
    text = ("deploy sk-ABCD1234567890efgh and a token gh" "p_" +
            "A" * 36 + " plus api_key=supersecret123")
    before = text
    counts = redact.count_redactions(text)
    assert text == before                       # count NEVER mutates
    assert counts.get("REDACTED_OPENAI_KEY") == 1
    assert counts.get("REDACTED_GITHUB_TOKEN") == 1
    assert sum(counts.values()) >= 3            # + the assignment rule
    # generic [REDACTED] rules get positional keys — never the bare marker
    assert "REDACTED" not in counts
    assert redact.count_redactions("plain prose, no secrets") == {}
    assert redact.count_redactions("") == {}
    assert redact.count_redactions(None) == {}


def test_scrubs_credential_shapes():
    out = redact.redact_secrets(
        "deploy sk-ABCD1234567890efgh; Authorization: Bearer tok_xyz; api_key=supersecret123"
    )
    assert "sk-ABCD1234567890efgh" not in out
    assert "tok_xyz" not in out
    assert "supersecret123" not in out
    assert "REDACTED" in out


def test_keeps_benign_prose_with_trigger_words():
    # regression guard: only the [:=] assignment shape is a secret — bare trigger words survive
    for safe in ("the password reset email", "token bucket algorithm", "the secret sauce"):
        assert redact.redact_secrets(safe) == safe


def test_none_and_empty_pass_through():
    assert redact.redact_secrets(None) is None
    assert redact.redact_secrets("") == ""


# --------------------------------------------------------------------------------------
# The shared cross-runtime fixture.
#
# ONE file, THREE suites: this one, claude-config/tests/test_precompact_capture.py, and
# scripts/windows/tests/MemoryCommon.Tests.ps1. That is the whole structural fix for AMS-12:
# the four copies of the pattern set were born identical in a single commit with NO binding
# test, and had already drifted on case sensitivity before anyone noticed.
#
# Fixture conventions, each one load-bearing:
#   * field name is `text`, never `input` — Pester's `-ForEach` collides with `$input`.
#   * ASCII-only — PS 5.1 `Get-Content` defaults to ANSI, so any non-ASCII byte would decode
#     differently in the PowerShell suite than in the Python ones.
#   * assertions are SUBSTRINGS, never exact output equality — that absorbs `$1` vs `\1` and
#     any future marker rewording without turning the fixture into a tripwire.
#   * `|+|` is a split marker stripped at load time. Realistic credential prefixes written
#     contiguously make the fixture unpushable (gitleaks flags them, and this repo's commit
#     path runs a gitleaks pre-commit hook); splitting them keeps the file scanner-clean while
#     the assembled string still exercises the real quantifiers.
# --------------------------------------------------------------------------------------
_JOIN = "|+|"
_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "redaction-cases.jsonl"


def _unsplit(s):
    return s.replace(_JOIN, "")


def _load_cases():
    return [json.loads(ln) for ln in _FIXTURE.read_text(encoding="ascii").splitlines() if ln.strip()]


_CASES = _load_cases()


def test_fixture_is_present_ascii_and_populated():
    _FIXTURE.read_bytes().decode("ascii")  # raises if a non-ASCII byte ever sneaks in
    assert len(_CASES) >= 20
    for c in _CASES:
        assert set(c) == {"name", "text", "must_redact", "must_keep"}


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_redaction_fixture_case(case):
    out = redact.redact_secrets(_unsplit(case["text"]))
    for needle in case["must_redact"]:
        assert _unsplit(needle) not in out, "%s: leaked %r -> %r" % (case["name"], needle, out)
    for needle in case["must_keep"]:
        assert _unsplit(needle) in out, "%s: lost %r -> %r" % (case["name"], needle, out)
