"""B1 Phase 2 tests: PreCompact conversation-query capture (precompact_capture.py).

At PreCompact a REAL query exists (the conversation), unlike cold boot. This capture tails the
transcript, distills the last few turns into a query, redacts secrets, and stashes a freshness-
stamped marker that the immediately-following post-compact SessionStart helper consumes. PreCompact
cannot inject context itself (Claude Code hook spec) — it is capture-only.

Pure logic is unit-tested here; the stdin/marker-write I/O is exercised by the live e2e. Run:
  python -m pytest claude-config/tests/test_precompact_capture.py -v
"""
import importlib.util
import json
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_MOD = _HERE.parent / "precompact_capture.py"
_spec = importlib.util.spec_from_file_location("precompact_capture", _MOD)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


# --- win_to_wsl_path: the transcript_path arrives as a Windows path ---

def test_win_to_wsl_path_c_drive():
    assert pc.win_to_wsl_path(r"C:\Users\youruser\.claude\x.jsonl") == "/mnt/c/Users/youruser/.claude/x.jsonl"


def test_win_to_wsl_path_d_drive_lowercased():
    assert pc.win_to_wsl_path(r"D:\My Drive\a.jsonl") == "/mnt/d/My Drive/a.jsonl"


def test_win_to_wsl_path_passthrough_if_already_posix():
    assert pc.win_to_wsl_path("/mnt/c/already/wsl.jsonl") == "/mnt/c/already/wsl.jsonl"


# --- extract_turns_text: parse JSONL transcript records into "[role] text" ---

def _line(role, text):
    import json
    return json.dumps({"message": {"role": role, "content": text}})


def test_extract_turns_text_string_content():
    jsonl = "\n".join([_line("user", "first ask"), _line("assistant", "a reply"), _line("user", "second ask")])
    out = pc.extract_turns_text(jsonl, max_turns=6)
    assert "second ask" in out and "first ask" in out
    assert "[user]" in out and "[assistant]" in out


def test_extract_turns_text_list_content_blocks():
    import json
    rec = json.dumps({"message": {"role": "assistant", "content": [
        {"type": "text", "text": "block one"}, {"type": "tool_use", "name": "x"}, {"type": "text", "text": "block two"}]}})
    out = pc.extract_turns_text(rec, max_turns=6)
    assert "block one" in out and "block two" in out


def test_extract_turns_text_caps_to_max_turns():
    jsonl = "\n".join(_line("user", f"turn{i}") for i in range(10))
    out = pc.extract_turns_text(jsonl, max_turns=3)
    assert "turn9" in out and "turn7" in out
    assert "turn0" not in out


def test_extract_turns_text_skips_garbage_lines():
    jsonl = "not json\n" + _line("user", "good turn") + "\n{broken"
    out = pc.extract_turns_text(jsonl, max_turns=6)
    assert "good turn" in out


# --- redact: same canonical credential-shape set, applied before the marker hits disk ---

def test_redact_credential_shapes():
    out = pc.redact("deploy sk-ABCD1234567890efgh and api_key=supersecret123")
    assert "sk-ABCD1234567890efgh" not in out and "supersecret123" not in out and "REDACTED" in out


def test_redact_keeps_benign_prose():
    for safe in ("the password reset email", "token bucket algorithm"):
        assert pc.redact(safe) == safe


# --- build_query: redacted, bounded precis of recent turns ---

def test_build_query_redacts_and_caps():
    jsonl = "\n".join(_line("user", "A" * 2000))
    q = pc.build_query(jsonl, max_turns=6, max_chars=400)
    assert len(q) <= 400


def test_build_query_empty_on_no_turns():
    assert pc.build_query("garbage\n{bad", max_turns=6, max_chars=400) == ""


# --- the shared cross-runtime redaction fixture -------------------------------------------
# The same tests/fixtures/redaction-cases.jsonl that mem0-server/tests/test_redact.py and
# scripts/windows/tests/MemoryCommon.Tests.ps1 iterate. This copy of the pattern set is one of
# FOUR (the fourth, SkillOpt, lives on the offline replica host and is not covered here) and
# they had already drifted once with no test to catch it. Substring assertions only; `|+|` is a
# split marker stripped at load time so realistic credential prefixes never sit contiguously on
# disk and a secret scanner cannot flag the fixture. Conventions are documented in full in
# mem0-server/tests/test_redact.py.
_JOIN = "|+|"
_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "redaction-cases.jsonl"


def _unsplit(s):
    return s.replace(_JOIN, "")


_CASES = [
    json.loads(ln) for ln in _FIXTURE.read_text(encoding="ascii").splitlines() if ln.strip()
]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_redaction_fixture_case(case):
    out = pc.redact(_unsplit(case["text"]))
    for needle in case["must_redact"]:
        assert _unsplit(needle) not in out, "%s: leaked %r -> %r" % (case["name"], needle, out)
    for needle in case["must_keep"]:
        assert _unsplit(needle) in out, "%s: lost %r -> %r" % (case["name"], needle, out)


def test_marker_query_is_redacted_end_to_end():
    """The pattern set must apply through build_query, not just through redact()."""
    # Split so no SOURCE line carries a `KEY=<16 alnum>` shape: secret scanners
    # (gitleaks generic-api-key, GitHub push protection) match it regardless of
    # context, and a redaction suite is the one place such literals must live.
    # The runtime string is byte-identical to the un-split form.
    secret = "abcd1234" + "efgh5678"
    rec = json.dumps({"message": {"role": "user", "content": "export MEM0_API_KEY=" + secret}})
    q = pc.build_query(rec, max_turns=6, max_chars=800)
    assert secret not in q
    assert "MEM0_API_KEY=[REDACTED]" in q
