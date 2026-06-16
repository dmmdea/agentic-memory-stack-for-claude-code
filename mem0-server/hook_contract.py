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
from typing import Optional

log = logging.getLogger("mem0-server")

# v0.19 M15: do NOT pre-whitelist future versions ('18.0' was pre-whitelisted
# in v0.18, which made the first real drift — a bumped hook against a stale
# server — invisible by design). This set is extended in the SAME commit that
# bumps $HookContractVersion in the Windows hooks, so a stale server then
# WARNs as designed.
# v0.20 A.3: '20.0' added in the same commit that bumps user-prompt-extract.ps1
# to the batched /v1/context/bundle contract. pre-tool-check.ps1 stays '17.0'
# (its search wire contract is unchanged).
KNOWN_HOOK_CONTRACT_VERSIONS = {"17.0", "20.0"}

# v0.19 M10: drift made readable — incremented in warn_hook_contract_version,
# exposed by /health/deep as checks.hook_contract (in-process, zero I/O).
hook_contract_stats: dict = {"missing": 0, "unknown": 0, "last_unknown": None}


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
