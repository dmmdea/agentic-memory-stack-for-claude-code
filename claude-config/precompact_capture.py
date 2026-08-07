#!/usr/bin/env python3
"""PreCompact conversation-query capture (B1 Phase 2).

At PreCompact a REAL query exists — the live conversation — unlike cold SessionStart (no query yet).
This capture tails the transcript, distills the last few turns into a redacted query, and stashes a
freshness-stamped marker (`~/.mem0/precompact-query.json`) that the IMMEDIATELY-following post-compact
SessionStart helper consumes to retrieve a conversation-relevant ranked bundle. PreCompact itself
CANNOT inject context (Claude Code hook spec) — this is capture-only.

Runs in WSL (invoked by a PreCompact `wsl.exe` hook); stdin is the hook JSON. `transcript_path`
arrives as a WINDOWS path and is translated to `/mnt/<drive>/`. Dependency-free, operator-agnostic,
fail-silent (any error writes no marker and exits 0). The query is redacted so no credential-shaped
text lands in the marker file, even transiently.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

MARKER_NAME = "precompact-query.json"
DEFAULT_MAX_TURNS = 6
DEFAULT_MAX_CHARS = 800
_TAIL_BYTES = 262144  # read only the last 256 KB — bounds the pathological giant-line transcript case

# Canonical credential-shape set. FOUR copies exist: this one, mem0-server/redact.py, the L1a
# reader (scripts/windows/memory-common.ps1 Redact-Secrets), and the SkillOpt reader on the offline
# replica host. The pattern STRINGS below are byte-identical to the other copies (only the
# backreference syntax differs -- \1 here, $1 in PowerShell); the three in-repo copies are pinned to
# each other by tests/fixtures/redaction-cases.jsonl, which all three suites iterate.
# Rationale for each shape (left boundary vs \b, prefix tolerance, [ \t] vs \s, the bounded
# newline-free value class, no bare-hex / no bare-Bearer) lives in mem0-server/redact.py's docstring.
_SECRET_PATTERNS = tuple(
    (re.compile(p), r)
    for p, r in (
        (r"(?i)(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{10,}", "[REDACTED_OPENAI_KEY]"),
        (r"(?i)(Authorization[ \t]*:[ \t]*Bearer[ \t]+)[^\s\"']+", r"\1[REDACTED]"),
        (r"(?i)(Authorization[ \t]*:[ \t]*Basic[ \t]+)[^\s\"']+", r"\1[REDACTED]"),
        (r"(?i)((?<![A-Za-z0-9])[A-Za-z0-9]*[_-]?(?:api[_-]?key|token|password|passwd|secret)"
         r"[ \t]*[:=][ \t]*)"
         r"(?:[\"'](?:\\.|[^\\\r\n]){4,200}[\"']|[\"'](?:\\.|[^\"'\\\r\n]){4,200}"
         r"|(?=[^\s\"'\r\n]{4})(?:[^\s\"'\r\n]{16,200}|[^\s\"'\r\n]{0,200}[0-9][^\s\"'\r\n]{0,200}))",
         r"\1[REDACTED]"),
        (r"(?im)^([ \t]*(?:[-*+][ \t]+)?(?:value|key|api[_-]?key|token|password|passwd|secret)"
         r"[ \t]*[:=][ \t]*)[\"'\x60]?[A-Za-z0-9]{32,}[A-Za-z0-9+/=_-]*", r"\1[REDACTED]"),
        (r"(?-i:(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36})", "[REDACTED_GITHUB_TOKEN]"),
        (r"(?-i:(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{60,})", "[REDACTED_GITHUB_TOKEN]"),
        (r"(?-i:(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,})", "[REDACTED_SLACK_TOKEN]"),
        (r"(?-i:(?<![A-Za-z0-9])nvapi-[A-Za-z0-9_-]{60,})", "[REDACTED_NVIDIA_KEY]"),
        (r"(?-i:(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16})", "[REDACTED_AWS_KEY]"),
        (r"(?-i:(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)?)",
         "[REDACTED_JWT]"),
        (r"(?i)(?<![A-Za-z0-9+.-])([a-z][a-z0-9+.-]{0,31}://[^\s:@/]{1,64}):[^\s:@/]{1,256}@", r"\1:[REDACTED]@"),
        (r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
         "[REDACTED_PRIVATE_KEY]"),
    )
)


def win_to_wsl_path(p: str) -> str:
    """Translate a Windows path (C:\\a\\b) to its WSL mount (/mnt/c/a/b). POSIX paths pass through."""
    p = p or ""
    if len(p) >= 2 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")
    return p.replace("\\", "/")


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def extract_turns_text(jsonl_text: str, max_turns: int = DEFAULT_MAX_TURNS) -> str:
    """Parse JSONL transcript records -> the last `max_turns` user/assistant turns as '[role] text'."""
    turns = []
    for line in (jsonl_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        if isinstance(content, str):
            text = content
        else:
            text = "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        text = (text or "").strip()
        if text:
            turns.append(f"[{role}] {text}")
    if not turns:
        return ""
    return "\n\n".join(turns[-max_turns:])


def build_query(jsonl_text: str, max_turns: int = DEFAULT_MAX_TURNS, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Redacted, bounded precis of the recent turns — the post-compaction retrieval query."""
    q = redact(extract_turns_text(jsonl_text, max_turns))
    if len(q) > max_chars:
        q = q[-max_chars:]  # keep the most-recent tail
    return q


def read_tail(path: str, max_bytes: int = _TAIL_BYTES) -> str:
    """Read only the last `max_bytes` of a (possibly huge) transcript — never the whole file."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            raw = fh.read()
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw:
            return 0
        evt = json.loads(raw)
        tpath = win_to_wsl_path(evt.get("transcript_path") or "")
        if not tpath or not os.path.isfile(tpath):
            return 0
        query = build_query(read_tail(tpath))
        if not query:
            return 0
        mem0 = os.path.join(os.path.expanduser("~"), ".mem0")
        if not os.path.isdir(mem0):
            return 0
        marker = {"query": query, "session_id": str(evt.get("session_id") or ""), "ts": int(time.time())}
        tmp = os.path.join(mem0, MARKER_NAME + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(marker, fh)
        os.replace(tmp, os.path.join(mem0, MARKER_NAME))  # atomic publish
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
