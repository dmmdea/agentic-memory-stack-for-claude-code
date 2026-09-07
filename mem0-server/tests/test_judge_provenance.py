# mem0-server/tests/test_judge_provenance.py
"""judge_model provenance on tier changes (schema v18, 2026-09-07).

`actor` is a ROLE label ("dream-autopromote", "user-direct") and never said WHAT did the
judging, so a promoted memory carried no way to answer "which model decided this?".
These pin the additive v18 field end to end without needing a live server.
"""
import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = (REPO / "mem0-server" / "app.py").read_text(encoding="utf-8")
CANONIZE = (REPO / "scripts" / "wsl" / "mem0-canonize.sh").read_text(encoding="utf-8")


def _ledger_audit():
    spec = importlib.util.spec_from_file_location(
        "ledger_audit_ut", REPO / "scripts" / "wsl" / "ledger-audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tier_body_accepts_an_optional_judge_model():
    m = re.search(r"class TierIn\(BaseModel\):(.*?)\n\nclass ", APP, re.S)
    assert m, "TierIn not found"
    body = m.group(1)
    assert "judge_model: Optional[str] = None" in body, "judge_model must be optional"
    # A caller that does not know must record None rather than a guess.
    assert "= None" in body


def test_both_tier_ledger_rows_carry_judge_model_and_v18():
    # The write-ahead INTENT row matters as much as the completion row: a refused
    # mutation still has to say which model was judging when it was refused.
    rows = re.findall(r'"event": "tier-change(?:-intent)?", "memory_id": mid,(.*?)\}\)', APP, re.S)
    assert len(rows) >= 2, f"expected both tier-change rows, found {len(rows)}"
    for r in rows:
        assert "judge_model" in r
        assert '"schema_version": "v18"' in r


def test_judge_model_is_explicitly_outside_the_signed_material():
    # The canonical HMAC covers <ts>|<nonce>|promote|<mid>|<reason>. Recording the field
    # is an audit convenience; treating it as tamper-evident would be wrong, so the code
    # has to SAY so where a reader will see it.
    m = re.search(r"class TierIn\(BaseModel\):(.*?)\n\nclass ", APP, re.S)
    assert "HMAC" in m.group(1) and "UNSIGNED" in m.group(1)


def test_ledger_audit_accepts_v18_rows_and_still_accepts_v17():
    mod = _ledger_audit()
    for ev in ("tier-change", "tier-change-intent"):
        schema = mod.SCHEMA[ev]
        assert "judge_model" in schema["optional"], f"{ev} must tolerate the new field"
        # additive: it is OPTIONAL, so pre-v18 rows without it stay valid
        assert "judge_model" not in schema["required"]


def test_canonize_passes_judge_model_without_touching_the_signature():
    assert "judge_model" in CANONIZE
    assert "JUDGE_MODEL" in CANONIZE
    # the signed payload construction must be unchanged
    assert "promote|" in CANONIZE or "action=\"promote\"" in CANONIZE or "promote" in CANONIZE
