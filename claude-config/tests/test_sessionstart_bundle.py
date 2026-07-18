"""B1 tests: SessionStart durable/evidence bundle enrichment (sessionstart_bundle.py).

The SessionStart banner already surfaces canonical + open goals + recent episodes, but NOT the
ranked durable/evidence facts the (now-dead) per-prompt UserPromptSubmit hook used to inject. B1
enriches the banner with a thin, brand+initiative-scoped, recency-pseudo-query-ranked, K<=1,
DISTILLED precis of those facts, reusing the live /v1/context/bundle pipeline (checkpoint:false,
tier:small). Frontier-grounded (scope-first/rank-second; precision-over-recall; distill-not-dump).

These unit-test the PURE logic (query construction, distillation, K cap, render). The bundle HTTP
call + sqlite read live in main() and are exercised by the live e2e, not here. Run:
  python -m pytest claude-config/tests/test_sessionstart_bundle.py -v
"""
import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_MOD = _HERE.parent / "sessionstart_bundle.py"
_spec = importlib.util.spec_from_file_location("sessionstart_bundle", _MOD)
ssb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssb)

SCRIPT = _HERE.parent / "storage-cap-check.sh"


def _mem(text, score=0.75):
    return {"id": "x", "memory": text, "score": score}


# --- build_boot_query: recency goal is the primary signal, scope is the fallback ---

def test_build_boot_query_uses_recent_goal():
    q = ssb.build_boot_query("Fix the influencer invite flow", "brand-a", "brand-a-platform")
    assert "influencer invite flow" in q


def test_build_boot_query_falls_back_to_scope_tokens():
    q = ssb.build_boot_query(None, "ai-ecosystem", "agentic-memory-stack")
    assert "ai-ecosystem" in q and "agentic-memory-stack" in q


def test_build_boot_query_empty_when_no_signal():
    assert ssb.build_boot_query(None, None, None) == ""
    assert ssb.build_boot_query("   ", "", "") == ""


# --- distill: a thin precis, never a dump (length tax) ---

def test_distill_truncates_long_text_to_limit():
    assert len(ssb.distill("A" * 200, limit=120)) <= 120


def test_distill_keeps_short_text():
    assert ssb.distill("short fact", limit=120) == "short fact"


# --- select_facts: K cap, blank-skip, distillation ---

def test_select_facts_caps_to_k():
    mems = [_mem("a"), _mem("b"), _mem("c")]
    assert len(ssb.select_facts(mems, k=1)) == 1


def test_select_facts_skips_blank_memory():
    mems = [_mem("   "), _mem("real fact")]
    facts = ssb.select_facts(mems, k=2)
    assert facts == ["real fact"]


def test_select_facts_distills_long_text():
    facts = ssb.select_facts([_mem("Z" * 300)], k=1)
    assert len(facts[0]) <= 120


def test_select_facts_empty_input():
    assert ssb.select_facts([], k=1) == []


# --- format_block: advisory header + bullets; silent when empty ---

def test_format_block_renders_header_and_bullets():
    out = ssb.format_block(["fact one"])
    assert ssb.HEADER in out
    assert "  - [recall] fact one" in out


def test_format_block_silent_on_empty():
    assert ssb.format_block([]) == ""


# --- header wording: advisory, never imperative (mirrors the Phase-2a frame rule) ---

def test_header_is_advisory_not_imperative():
    assert "verify" in ssb.HEADER.lower()
    upper = ssb.HEADER.upper().lstrip()
    for kw in ("MUST ", "NEVER ", "ALWAYS ", "DO NOT", "DON'T", "YOU MUST"):
        assert not upper.startswith(kw), f"header must be advisory, not imperative: {ssb.HEADER!r}"


# --- integration: the helper must actually be invoked by the hook script ---

def test_helper_invoked_by_script():
    # The HEADER is printed by THIS helper, not echoed by bash; the real wiring guard is that the
    # script invokes the helper. (Drift guard against the call being removed.)
    assert "sessionstart_bundle.py" in SCRIPT.read_text(encoding="utf-8"), (
        "storage-cap-check.sh does not invoke sessionstart_bundle.py — B1 enrichment not wired in."
    )


def _to_wsl_path(p: Path) -> str:
    s = p.as_posix()  # e.g. D:/repos/...
    if len(s) > 1 and s[1] == ":":
        s = "/mnt/" + s[0].lower() + s[2:]
    return s


def test_bash_syntax_ok():
    # storage-cap-check.sh is a WSL script; validate it in WSL (the deploy runtime), not the
    # ambient `bash` (which may be Git Bash or WSL and mishandles Windows paths). Skip if no WSL.
    import shutil
    import subprocess
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        import pytest
        pytest.skip("wsl not available")
    r = subprocess.run([wsl, "-e", "bash", "-lc", f"bash -n '{_to_wsl_path(SCRIPT)}'"], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()


# --- I/O: brand-scoped recent goal + abstention (the recency pseudo-query source) ---

def test_recent_goal_for_brand_scopes_and_abstains(tmp_path):
    import sqlite3 as sq
    db = tmp_path / "ep.db"
    con = sq.connect(db)
    con.execute("CREATE TABLE episodes (session_id TEXT, goal_text TEXT, ended_at TEXT)")
    con.execute("CREATE TABLE sessions (session_id TEXT, brand TEXT)")
    con.executemany("INSERT INTO episodes VALUES (?,?,?)", [
        ("s1", "ai goal newest", "2026-06-28T03:00:00"),
        ("s2", "brand-a goal", "2026-06-28T02:00:00"),
    ])
    con.executemany("INSERT INTO sessions VALUES (?,?)", [("s1", "ai-ecosystem"), ("s2", "brand-a")])
    con.commit()
    con.close()
    assert ssb.recent_goal_for_brand(str(db), "ai-ecosystem") == "ai goal newest"
    assert ssb.recent_goal_for_brand(str(db), "brand-a") == "brand-a goal"
    # brand with no episode -> abstain, NOT a cross-brand fallback
    assert ssb.recent_goal_for_brand(str(db), "nonexistent") is None
    # brandless -> global most-recent
    assert ssb.recent_goal_for_brand(str(db), None) == "ai goal newest"
    # missing db -> None (fail-silent)
    assert ssb.recent_goal_for_brand(str(tmp_path / "nope.db"), "x") is None


# --- deploy guard: the installer MUST copy the helper or B1 no-ops in real installs ---

def test_installer_deploys_helper():
    inst = _HERE.parent.parent / "install" / "2-windows-config.ps1"
    assert "sessionstart_bundle.py" in inst.read_text(encoding="utf-8"), (
        "install/2-windows-config.ps1 must copy sessionstart_bundle.py beside storage-cap-check.sh "
        "or B1 silently no-ops in real installs."
    )


# --- Phase 2: marker-driven query selection (conversation query post-compaction vs recency) ---

def test_choose_query_prefers_marker_with_frontier_k2():
    q, tier, k = ssb.choose_query_and_params("conversation query", "recency goal")
    assert q == "conversation query" and tier == "frontier" and k == 2


def test_choose_query_falls_back_to_recency_small_k1():
    q, tier, k = ssb.choose_query_and_params(None, "recency goal")
    assert q == "recency goal" and tier == "small" and k == 1
    # empty marker string is treated as no marker
    q2, tier2, k2 = ssb.choose_query_and_params("   ", "recency goal")
    assert q2 == "recency goal" and tier2 == "small" and k2 == 1


def test_choose_query_empty_when_no_signal():
    q, _tier, _k = ssb.choose_query_and_params(None, "")
    assert not q


def test_load_and_consume_marker_fresh(tmp_path):
    import json
    m = tmp_path / "precompact-query.json"
    m.write_text(json.dumps({"query": "what was I doing", "ts": 1000, "session_id": "s"}), encoding="utf-8")
    got = ssb.load_and_consume_marker(str(m), now=1010, max_age=300)
    assert got == "what was I doing"
    assert not m.exists()  # consume-once


def test_load_and_consume_marker_stale_is_dropped(tmp_path):
    import json
    m = tmp_path / "precompact-query.json"
    m.write_text(json.dumps({"query": "old", "ts": 1000, "session_id": "s"}), encoding="utf-8")
    got = ssb.load_and_consume_marker(str(m), now=9999, max_age=300)
    assert got is None
    assert not m.exists()  # a stale marker is cleaned up, not left to linger


def test_load_and_consume_marker_missing(tmp_path):
    assert ssb.load_and_consume_marker(str(tmp_path / "nope.json"), now=10, max_age=300) is None
