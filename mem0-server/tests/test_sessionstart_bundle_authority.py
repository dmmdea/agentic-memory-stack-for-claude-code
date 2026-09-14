"""v1.23 P2-3 (spec §7): the SessionStart bundle resolves its authority from the per-host file
first and says where its block came from."""
from __future__ import annotations
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MOD = REPO_ROOT / "claude-config" / "sessionstart_bundle.py"


def _load():
    spec = importlib.util.spec_from_file_location("ssb_ut", MOD)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_file_wins_over_env_and_loopback_is_last(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setenv("MEM0_URL", "http://env.invalid:18791")
    (tmp_path / ".mem0").mkdir()
    (tmp_path / ".mem0" / "authority-url").write_text("# c\nhttp://brain.invalid:18791/\n", encoding="utf-8")
    assert mod.resolve_authority_url(str(tmp_path)) == "http://brain.invalid:18791"
    (tmp_path / ".mem0" / "authority-url").unlink()
    assert mod.resolve_authority_url(str(tmp_path)) == "http://env.invalid:18791"
    monkeypatch.delenv("MEM0_URL")
    assert mod.resolve_authority_url(str(tmp_path)) == "http://127.0.0.1:18791"


def test_block_header_names_the_source():
    mod = _load()
    hdr = mod.format_block(["x"], source="authority:brain.invalid:18791").splitlines()[0]
    assert hdr == "Recently-relevant memory (verify before acting; source=authority:brain.invalid:18791):"
    assert mod.format_block(["x"]).splitlines()[0] == "Recently-relevant memory (verify before acting):"
    assert mod.format_block([]) == ""
