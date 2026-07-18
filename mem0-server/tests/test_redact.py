"""Unit tests for server-side secret redaction (the stored-text chokepoint).

prompt_text is persisted in the in_progress episode checkpoint from BOTH /v1/episodes/checkpoint
and /v1/context/bundle (via _checkpoint_core). Credentials pasted into a prompt must be scrubbed
before storage. Mirrors the L1a/SkillOpt reader pattern set — one canonical set, three runtimes.
"""
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD = os.path.normpath(os.path.join(_HERE, "..", "redact.py"))
_spec = importlib.util.spec_from_file_location("redact", _MOD)
redact = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redact)


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
