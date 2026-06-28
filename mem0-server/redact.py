"""Server-side secret redaction for STORED text.

Strip credential-shaped substrings before they land in a persistent, queryable store. Applied at
the checkpoint chokepoint (_checkpoint_core) so prompt_text from BOTH /v1/episodes/checkpoint and
/v1/context/bundle (the daemon hot-path) is scrubbed regardless of which client POSTed it.

One canonical pattern set across three runtimes — this module, the L1a reader
(scripts/windows/memory-common.ps1 Redact-Secrets), and the SkillOpt reader
(skillopt harvest.redact_secrets). Only the [:=] assignment shape is treated as a secret: a bare
'token <word>' / 'password <word>' rule over-redacts ordinary prose ("password reset email").
"""
from __future__ import annotations

import re
from typing import Optional

_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{10,}"), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"(?i)(Authorization:\s*Bearer\s+)[^\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(Authorization:\s*Basic\s+)[^\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\b(\s*[:=]\s*)[^\s\"']+"), r"\1\2[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
     "[REDACTED_PRIVATE_KEY]"),
)


def redact_secrets(text: Optional[str]) -> Optional[str]:
    """Replace credential-shaped substrings with markers. None/empty pass through unchanged; safe
    prose is untouched (only key=val / key: val assignments + known token/PEM shapes are scrubbed)."""
    if not text:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
