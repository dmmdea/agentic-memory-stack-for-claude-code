"""Task 1 test: advisory frame appears iff ≥1 canonical fact.

Tests the canonical-render python snippet extracted from storage-cap-check.sh
(the inline python3 -c block that filters canonical records by brand and emits
`  - [canonical] <text>` lines).  The advisory frame ("Locked facts you can lean
on…") is a bash-level check (`if [ -n "$canon" ]`) — we verify the python side
emits output only when canonical records exist, and verify the frame string is
non-empty / advisory in wording.

Also runs `bash -n` on the script to guarantee no syntax errors.
"""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "storage-cap-check.sh"

# The inline python snippet (extracted for standalone testability)
RENDER_SNIPPET = textwrap.dedent("""
import sys, json
try:
    d = json.load(sys.stdin)
    brand = sys.argv[1] if len(sys.argv) > 1 else ''
    recs = []
    for r in d.get('results', []):
        md = r.get('metadata') or {}
        if md.get('tier') == 'canonical' and md.get('brand') == brand:
            recs.append(r)
        if len(recs) >= 5:
            break
    for r in recs:
        text = (r.get('memory') or '')[:120]
        print(f'  - [canonical] {text}')
except Exception:
    pass
""").strip()

ADVISORY_FRAME = "Locked facts you can lean on this session — verify before risky actions:"


def _render(memories_payload: dict, brand: str) -> str:
    """Run the render snippet against a mocked memories payload; return stdout."""
    result = subprocess.run(
        [sys.executable, "-c", RENDER_SNIPPET, brand],
        input=json.dumps(memories_payload).encode(),
        capture_output=True,
    )
    return result.stdout.decode()


def _make_payload(records: list) -> dict:
    return {"results": records}


def _canonical_record(text: str, brand: str = "ai-ecosystem") -> dict:
    return {
        "id": "test-id",
        "memory": text,
        "metadata": {"tier": "canonical", "brand": brand},
    }


def _evidence_record(text: str, brand: str = "ai-ecosystem") -> dict:
    return {
        "id": "test-id-ev",
        "memory": text,
        "metadata": {"tier": "evidence", "brand": brand},
    }


# ---------------------------------------------------------------------------
# Bash syntax check
# ---------------------------------------------------------------------------

def test_bash_syntax():
    """storage-cap-check.sh must pass bash -n (no syntax errors)."""
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True)
    assert result.returncode == 0, (
        f"bash -n failed:\nstdout: {result.stdout.decode()}\nstderr: {result.stderr.decode()}"
    )


# ---------------------------------------------------------------------------
# Render snippet: empty canonical → no output (no frame)
# ---------------------------------------------------------------------------

def test_empty_payload_no_output():
    """Empty results → snippet emits nothing → bash guard suppresses frame."""
    out = _render(_make_payload([]), brand="ai-ecosystem")
    assert out.strip() == "", f"expected no output, got: {out!r}"


def test_only_evidence_records_no_output():
    """Evidence-tier records → snippet emits nothing → no frame."""
    payload = _make_payload([_evidence_record("some evidence fact")])
    out = _render(payload, brand="ai-ecosystem")
    assert out.strip() == "", f"expected no output for evidence-only, got: {out!r}"


def test_canonical_wrong_brand_no_output():
    """Canonical record for a different brand → snippet emits nothing."""
    payload = _make_payload([_canonical_record("fact for brand-a", brand="brand-a")])
    out = _render(payload, brand="ai-ecosystem")
    assert out.strip() == "", f"expected no output for wrong-brand canonical, got: {out!r}"


# ---------------------------------------------------------------------------
# Render snippet: ≥1 canonical → output exists → frame must be shown
# ---------------------------------------------------------------------------

def test_one_canonical_emits_output():
    """One canonical record → snippet emits a [canonical] line → frame fires."""
    payload = _make_payload([_canonical_record("The reserved ports are 80, 443.")])
    out = _render(payload, brand="ai-ecosystem")
    assert "[canonical]" in out, f"expected [canonical] line, got: {out!r}"
    assert "reserved ports" in out


def test_multiple_canonical_emits_output():
    """Multiple canonical records → all appear in output (up to 5)."""
    facts = [
        "mem0 canonical scope is workspace=ai-ecosystem.",
        "The reserved ports are 80, 443, 3000, 5000, 8000, 6443.",
        "Ollama on :11434 is decommissioned (v0.22, 2026-06).",
    ]
    payload = _make_payload([_canonical_record(f) for f in facts])
    out = _render(payload, brand="ai-ecosystem")
    assert out.count("[canonical]") == 3, f"expected 3 [canonical] lines, got:\n{out}"


def test_canonical_text_truncated_to_120():
    """Canonical text > 120 chars is truncated to 120 in the rendered line."""
    long_text = "A" * 200
    payload = _make_payload([_canonical_record(long_text)])
    out = _render(payload, brand="ai-ecosystem")
    assert "[canonical]" in out
    # The rendered portion after "  - [canonical] " should be at most 120 chars
    for line in out.splitlines():
        if "[canonical]" in line:
            rendered_text = line.split("[canonical]", 1)[1].strip()
            assert len(rendered_text) <= 120, f"text not truncated: {rendered_text!r}"


# ---------------------------------------------------------------------------
# Advisory frame wording check (unit test on the constant)
# ---------------------------------------------------------------------------

def test_advisory_frame_wording_is_advisory_not_imperative():
    """The frame string must be advisory ('verify') and must NOT start with a
    standing-order keyword — this is the Phase 2a wording requirement."""
    frame = ADVISORY_FRAME
    # Must contain the advisory marker
    assert "verify" in frame.lower(), f"frame must contain 'verify': {frame!r}"
    # Must NOT start with imperative keywords
    upper = frame.upper().lstrip()
    imperative_starts = ("MUST ", "NEVER ", "ALWAYS ", "DO NOT", "DON'T", "SHALL ", "YOU MUST")
    for kw in imperative_starts:
        assert not upper.startswith(kw), (
            f"advisory frame must not be imperative (starts with {kw!r}): {frame!r}"
        )


def test_advisory_frame_string_matches_script():
    """The advisory frame constant in this test must match the string emitted by
    the script verbatim — guards against copy-paste drift."""
    script_text = SCRIPT.read_text(encoding="utf-8")
    assert ADVISORY_FRAME in script_text, (
        f"Advisory frame string not found in storage-cap-check.sh.\n"
        f"Expected: {ADVISORY_FRAME!r}\n"
        f"Check that the script was updated correctly."
    )
