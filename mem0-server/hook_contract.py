"""hook_contract.py — v0.18 MED-17 hook contract drift detection.

v0.19 M15: extracted from app.py into this side-effect-free module so tests can
import and caplog-assert the WARN directly (app.py cannot be imported in tests —
Memory.from_config at import time needs the live Qdrant/Ollama stack).

v0.19 M10: in-process drift counters (hook_contract_stats) surfaced via
GET /health/deep -> checks.hook_contract, and the missing-field branch is
demoted to INFO — field-less callers (direct API users, Test-MemoryStack
probes, pre-v0.18 hooks) are documented-legitimate and were drowning the real
drift signal (132 WARN lines observed in one day). WARN is reserved for the
one event the mechanism exists for: an UNKNOWN version, i.e. hook/server skew.

Test fingerprint convention (v0.19 M15): tests that deliberately send an
unknown version MUST use a value containing '-test' (e.g. '99.0-test') so the
Test-MemoryStack journal drift row can exclude test-generated WARNs.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger("mem0-server")

# v0.19 M15: do NOT pre-whitelist future versions ('18.0' was pre-whitelisted
# in v0.18, which made the first real drift — a bumped hook against a stale
# server — invisible by design). This set is extended in the SAME commit that
# bumps $HookContractVersion in the Windows hooks, so a stale server then
# WARNs as designed.
# v0.20 A.3: '20.0' added in the same commit that bumps user-prompt-extract.ps1
# to the batched /v1/context/bundle contract. '17.0' is the search wire
# contract — still spoken by the MCP shim's search POSTs (pre-tool-check.ps1
# also spoke it until AMS-16 retired that hook, 2026-08-09), so it stays.
KNOWN_HOOK_CONTRACT_VERSIONS = {"17.0", "20.0"}

# v0.19 M10: drift made readable — incremented in warn_hook_contract_version,
# exposed by /health/deep as checks.hook_contract (in-process, zero I/O).
hook_contract_stats: dict = {"missing": 0, "unknown": 0, "last_unknown": None}


# C10 (2026-09-22): machine turns. Claude Code raises UserPromptSubmit for background task
# notifications as well as for human prompts, and before this gate the [MEMORY CONTEXT] block
# rode ~91% of those notification turns. A notification reaches the hook with its prompt starting
# <task-notification>, both when it opens its own turn and when it was queued behind a running
# turn: the sampled hook inputs matched the transcript's queued_command prompt byte-for-byte, so
# the wrapper is the same. Only leading ASCII whitespace is skipped.
# The same verdict lives in the Windows lib (Test-MachineTurnPrompt) and in the compiled client
# (mem0-hook-client.cs IsMachineTurnStdin). All three are tested against one corpus,
# scripts/windows/tests/fixtures/machine-turn-prompts.json, so they cannot drift apart.
MACHINE_TURN_MARKER = "<task-notification>"
_MACHINE_TURN_LEADING_WS = " \t\r\n\f\v"


def is_machine_turn_prompt(prompt: Optional[str]) -> bool:
    """True when the prompt is a background task notification, not something a person typed.
    None, empty and non-string input read as a human prompt, the side that keeps today's
    behavior."""
    if not isinstance(prompt, str) or not prompt:
        return False
    return prompt.lstrip(_MACHINE_TURN_LEADING_WS).startswith(MACHINE_TURN_MARKER)


# 1.32.4: a message another agent session sends to this one arrives as the user turn, opening with
# the <cross-session-message wrapper or with its one-line announcement followed by the wrapper. It is
# that agent speaking, not the person. C10 deliberately keeps such a turn human-shaped for the memory
# block (is_machine_turn_prompt above stays exactly as it was, and the corpus keeps machine_turn=false
# for it), so this is a separate verdict: the episode running summary uses it, through
# is_non_human_turn, to keep both kinds of turn out of what the recent-sessions view shows.
# It is a port of Test-RelayedAgentMessage in scripts/windows/user-prompt-lib.ps1, pinned to the same
# corpus through each case's relayed_agent_message field. The wrapper must be followed by a whitespace
# character or '>': .NET's \s is [\f\n\r\t\v\x85\p{Z}], spelled out here because Python's \s also
# takes the C0 separators \x1c-\x1f.
RELAYED_MESSAGE_ANNOUNCEMENT = "Another Claude session sent a message:"
_RELAYED_WRAPPER = re.compile(
    "<cross-session-message[\f\n\r\t\v\x85 \xa0  -     　>]"
)


def is_relayed_agent_message(prompt: Optional[str]) -> bool:
    """True when the prompt is a message another agent session sent to this one. Same rule as the
    Windows lib: after the six leading whitespace characters, the prompt either starts with the
    wrapper, or starts with the announcement line and carries the wrapper after it. A prompt that
    only quotes the wrapper mid-text does not match. None, empty and non-string input read as
    human; never raises."""
    if not isinstance(prompt, str) or not prompt:
        return False
    t = prompt.lstrip(_MACHINE_TURN_LEADING_WS)
    if _RELAYED_WRAPPER.match(t):
        return True
    return t.startswith(RELAYED_MESSAGE_ANNOUNCEMENT) and _RELAYED_WRAPPER.search(t) is not None


def is_non_human_turn(prompt: Optional[str]) -> bool:
    """True when nobody typed the prompt: a background task notification or a message relayed from
    another agent session. The episode running summary records only what a person typed."""
    return is_machine_turn_prompt(prompt) or is_relayed_agent_message(prompt)


def warn_hook_contract_version(endpoint: str, version: Optional[str]) -> None:
    """Log-and-count contract-version validation. NEVER rejects (back-compat:
    pre-v0.18 hooks and direct API callers don't send the field)."""
    if version is None:
        hook_contract_stats["missing"] += 1
        # v0.19 M10: INFO, not WARN — field-less callers are legitimate.
        log.info(
            "MED-17: %s called without hook_contract_version (pre-v0.18 hook or direct API call)",
            endpoint,
        )
    elif str(version) not in KNOWN_HOOK_CONTRACT_VERSIONS:
        hook_contract_stats["unknown"] += 1
        hook_contract_stats["last_unknown"] = str(version)
        log.warning(
            "MED-17: %s called with unknown hook_contract_version=%r (known: %s)",
            endpoint, version, sorted(KNOWN_HOOK_CONTRACT_VERSIONS),
        )
