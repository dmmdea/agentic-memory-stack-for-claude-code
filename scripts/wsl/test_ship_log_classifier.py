#!/usr/bin/env python3
"""Tests for is_ship_log() — parity with MemoryCommon.Tests.ps1 'Test-IsShipLog' block.

Run with:
  python3 scripts/wsl/test_ship_log_classifier.py
  # or: pytest scripts/wsl/test_ship_log_classifier.py

All 11 cases mirror the Pester truth table exactly (same inputs, same expected verdicts).
Pester uses -imatch (case-insensitive); Python re.IGNORECASE is the equivalent.
"""
from __future__ import annotations

import sys
import os

# Allow running from any directory
sys.path.insert(0, os.path.dirname(__file__))

from ship_log_reclassify import is_ship_log  # noqa: E402 — import after sys.path patch


# ---------------------------------------------------------------------------
# Helper for standalone __main__ execution (no pytest required)
# ---------------------------------------------------------------------------

_RESULTS: list[tuple[str, bool, bool]] = []  # (label, expected, got)


def _check(label: str, text: str, expected: bool) -> None:
    got = is_ship_log(text)
    _RESULTS.append((label, expected, got))


# ---------------------------------------------------------------------------
# 11 Pester cases from 'Test-IsShipLog keep/route classifier'
# ---------------------------------------------------------------------------

def test_routes_long_x_block() -> None:
    """routes a long dated checkpoint (>=150 chars) — Pester: 'x' * 900 -> True"""
    assert is_ship_log('x' * 900) is True


def test_routes_short_dated_status_line() -> None:
    """routes a short-ish dated status line"""
    assert is_ship_log(
        'Shipped the canonical fix and fixed surfacing on 2026-06-15, deployed to prod.'
    ) is True


def test_keeps_atomic_config_fact() -> None:
    """KEEPS an atomic config fact — 'set to' atomicMarker, short -> Rule 1 KEEP"""
    assert is_ship_log('APIFY_MAX_USD on Railway is set to $20') is False


def test_keeps_terse_version_fact() -> None:
    """KEEPS a terse version fact — short, no atomicMarker, no shipSignal -> Rule 4 KEEP"""
    assert is_ship_log('v0.17 final pytest result was 97 PASS and 1 SKIP') is False


def test_keeps_comma_heavy_atomic_ports() -> None:
    """KEEPS a comma-heavy atomic (ports) — short + port atomicMarker (:\\d{2,5}) -> Rule 1 KEEP"""
    assert is_ship_log('The reserved ports are 80, 443, 3000, 5000, 8000, 6443') is False


def test_keeps_empty_whitespace() -> None:
    """KEEPS empty/whitespace"""
    assert is_ship_log('   ') is False


def test_keeps_short_dated_status_with_url() -> None:
    """KEEPS a short dated-status line that carries a value marker (over-KEEP tie-break)"""
    assert is_ship_log(
        'The prod webhook was added on 2026-01-15 at https://api.x.com/hook'
    ) is False


def test_keeps_long_credential_fact() -> None:
    """KEEPS a long credential fact with no ship-signal (value-marker beats length)"""
    assert is_ship_log(
        'The Hermes OAuth client secret is X9z-kL2mPq8vRt7wNy3dBs6jFh1cAe4uGi5oUp0'
        ' and must never be rotated without updating all three callers'
        ' (Brain, Zora, and the mem0 sidecar).'
    ) is False


def test_keeps_long_path_fact() -> None:
    """KEEPS a long path fact with no ship-signal"""
    assert is_ship_log(
        r'C:\path\to\agentic-memory-stack-for-claude-code\scripts\windows\memory-common.ps1'
        ' is the canonical location for all shared PowerShell helpers'
        ' used by the L1a and L1b extractors.'
    ) is False


def test_routes_realistic_long_dated_ship_log() -> None:
    """routes a realistic long dated ship-log (status verbs + date)"""
    assert is_ship_log(
        'Shipped the canonical-surfacing fix and deployed storage-cap-check.sh'
        ' on 2026-06-19; verified 7 of 7 facts surface and updated'
        ' Test-MemoryStack with the R-surface invariant.'
    ) is True


def test_routes_long_ship_log_with_port_mention() -> None:
    """routes a long dated ship-log even though it mentions a port (ship-signal beats marker at length)"""
    assert is_ship_log(
        'Deployed the API gateway and migrated all traffic on 2026-06-15;'
        ' the new service binds port 8080, the old one was removed, and we verified'
        ' latency across all three regions before cutover.'
    ) is True


# ---------------------------------------------------------------------------
# Standalone runner — prints a parity table, no pytest needed
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    cases = [
        ("routes long x*900 (len>=150, no atomicMarker)", 'x' * 900, True),
        ("routes short dated status", 'Shipped the canonical fix and fixed surfacing on 2026-06-15, deployed to prod.', True),
        ("KEEPS atomic config ('set to')", 'APIFY_MAX_USD on Railway is set to $20', False),
        ("KEEPS terse version fact (short, no marker)", 'v0.17 final pytest result was 97 PASS and 1 SKIP', False),
        ("KEEPS comma-heavy ports (port atomicMarker)", 'The reserved ports are 80, 443, 3000, 5000, 8000, 6443', False),
        ("KEEPS whitespace", '   ', False),
        ("KEEPS short dated+url (over-KEEP tie-break)", 'The prod webhook was added on 2026-01-15 at https://api.x.com/hook', False),
        ("KEEPS long credential (secret atomicMarker, no shipSignal)",
         'The Hermes OAuth client secret is X9z-kL2mPq8vRt7wNy3dBs6jFh1cAe4uGi5oUp0'
         ' and must never be rotated without updating all three callers'
         ' (Brain, Zora, and the mem0 sidecar).', False),
        ("KEEPS long path (.ps1 atomicMarker, no shipSignal)",
         r'C:\path\to\agentic-memory-stack-for-claude-code\scripts\windows\memory-common.ps1'
         ' is the canonical location for all shared PowerShell helpers'
         ' used by the L1a and L1b extractors.', False),
        ("routes long dated ship-log with .sh (ship-signal wins)",
         'Shipped the canonical-surfacing fix and deployed storage-cap-check.sh'
         ' on 2026-06-19; verified 7 of 7 facts surface and updated'
         ' Test-MemoryStack with the R-surface invariant.', True),
        ("routes long ship-log with port mention (ship-signal beats atomicMarker)",
         'Deployed the API gateway and migrated all traffic on 2026-06-15;'
         ' the new service binds port 8080, the old one was removed, and we verified'
         ' latency across all three regions before cutover.', True),
    ]

    passed = 0
    failed = 0
    print("=== is_ship_log() parity check vs Pester MemoryCommon.Tests.ps1 ===\n")
    for label, text, expected in cases:
        got = is_ship_log(text)
        ok = got == expected
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  [{status}] {label}")
        if not ok:
            print(f"         expected={expected}  got={got}")
            print(f"         text={text[:80]!r}")
    print(f"\n{passed}/{len(cases)} matching Pester truth table", end="")
    if failed:
        print(f"  ({failed} FAILED)")
        sys.exit(1)
    else:
        print("  — full parity")
        sys.exit(0)
