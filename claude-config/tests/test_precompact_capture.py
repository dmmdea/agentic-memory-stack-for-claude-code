"""B1 Phase 2 tests: PreCompact conversation-query capture (precompact_capture.py).

At PreCompact a REAL query exists (the conversation), unlike cold boot. This capture tails the
transcript, distills the last few turns into a query, redacts secrets, and stashes a freshness-
stamped marker that the immediately-following post-compact SessionStart helper consumes. PreCompact
cannot inject context itself (Claude Code hook spec) — it is capture-only.

Pure logic is unit-tested here; the stdin/marker-write I/O is exercised by the live e2e. Run:
  python -m pytest claude-config/tests/test_precompact_capture.py -v
"""
import importlib.util
from pathlib import Path

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
