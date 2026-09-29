"""C3 brand map: the Python resolver (scripts/wsl/brand_routing.py) run over the shared corpus
tests/fixtures/brand-routing-cases.jsonl (the PowerShell and Go resolvers run the same file).
Pure functions and tmp_path files only; no network, nothing under the real HOME."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "wsl"))
import brand_routing  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "brand-routing-cases.jsonl"
CASES = [json.loads(ln) for ln in FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch, tmp_path):
    brand_routing._WARNED.clear()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for v in ("MEM0_BRAND_MAP", "MEM0_SHARED_BRANDS"):
        monkeypatch.delenv(v, raising=False)


@pytest.mark.parametrize("case", CASES, ids=[f"{i:02d}" for i in range(len(CASES))])
def test_shared_corpus(case):
    got = brand_routing.resolve(case["map"], case["path"], case["text"])
    assert got == case["expect"], (case["path"], case["text"])


def test_brand_map_missing_is_neutral(tmp_path, capsys):
    """Review focus #3: a map that routes nothing (missing file) leaves behavior brand-neutral."""
    assert brand_routing.load_brand_map(str(tmp_path / "absent.json")) == {}
    assert brand_routing.resolve(brand_routing.load_brand_map(str(tmp_path / "absent.json")),
                                 "g--My-Drive-Projects-ClientA", "text") is None
    assert brand_routing.resolve(None, "anything", "text") is None
    assert capsys.readouterr().err == "", "a missing file is the normal unconfigured state: no warning"


def test_malformed_json_is_neutral_with_one_warning(tmp_path, capsys):
    p = tmp_path / "brands.json"
    p.write_text("{ not json", encoding="utf-8")
    for _ in range(3):
        assert brand_routing.load_brand_map(str(p)) == {}
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "brand map" in err[0] and str(p) in err[0]


def test_non_object_json_is_neutral(tmp_path):
    p = tmp_path / "brands.json"
    p.write_text("[1, 2]", encoding="utf-8")
    assert brand_routing.load_brand_map(str(p)) == {}


def test_map_path_precedence(tmp_path, monkeypatch):
    home_map = tmp_path / ".claude" / "scripts" / "brands.json"
    home_map.parent.mkdir(parents=True)
    home_map.write_text(json.dumps({"rules": [{"pattern": "x", "brand": "from-home"}]}), encoding="utf-8")
    assert brand_routing.load_brand_map()["rules"][0]["brand"] == "from-home"
    env_map = tmp_path / "env-map.json"
    env_map.write_text(json.dumps({"rules": [{"pattern": "x", "brand": "from-env"}]}), encoding="utf-8")
    monkeypatch.setenv("MEM0_BRAND_MAP", str(env_map))
    assert brand_routing.load_brand_map()["rules"][0]["brand"] == "from-env"
    assert brand_routing.load_brand_map(str(home_map))["rules"][0]["brand"] == "from-home"


def test_map_path_from_stack_env(tmp_path):
    (tmp_path / ".mem0").mkdir()
    m = tmp_path / "stack-map.json"
    m.write_text(json.dumps({"rules": [{"pattern": "x", "brand": "from-stack-env"}]}), encoding="utf-8")
    (tmp_path / ".mem0" / "stack.env").write_text(f"MEM0_BRAND_MAP={m}\n", encoding="utf-8")
    assert brand_routing.load_brand_map()["rules"][0]["brand"] == "from-stack-env"


def test_shared_brands_unions_map_and_env(tmp_path, monkeypatch):
    bm = {"shared_brands": ["Shared-A"]}
    assert brand_routing.shared_brands(bm) == {"shared-a"}
    monkeypatch.setenv("MEM0_SHARED_BRANDS", "shared-b, Shared-C,,")
    assert brand_routing.shared_brands(bm) == {"shared-a", "shared-b", "shared-c"}
    assert brand_routing.shared_brands(None) == {"shared-b", "shared-c"}


def test_routable_brands():
    bm = {"rules": [{"pattern": "a", "brand": "brand-a"}],
          "content_rules": [{"pattern": "b", "brand": "brand-b"}],
          "shared_brands": ["shared-a"]}
    assert brand_routing.routable_brands(bm) == {"brand-a", "brand-b", "shared-a"}
    assert brand_routing.routable_brands({}) == set()


def test_separator_class_pattern_compiles_without_a_regex_warning():
    """A pattern spelling the separator class `[\\/ -]` normalizes to a class of dashes; that must
    not reach re.compile as `[----]` (a FutureWarning today, an error in a later Python)."""
    import warnings
    m = {"rules": [{"pattern": "alpha[\\/ -]+shop", "brand": "alpha"}]}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert brand_routing.resolve(m, r"C:\Work\alpha shop\x") == "alpha"
        assert brand_routing.resolve(m, "C--Work-alpha-shop") == "alpha"
        assert brand_routing.resolve(m, "C--Work-alphashop") is None
