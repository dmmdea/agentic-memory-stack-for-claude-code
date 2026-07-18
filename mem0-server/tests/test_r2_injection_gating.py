"""v1.0 Phase 3 / R2: abstention-first, entity-side gated injection.

Pins the R2 gate so a careless future edit cannot silently revert it. The R2
levers are (a) memory_cap (K) capped at 1-2 (ReasoningBank k=1 49.7% > k=4 44.4%):
frontier=2, small=1; and (b) the hook's block-level NOOP (no memory clears -> no
block), tested in Pester.

relevance_threshold is KEPT at 0.30 (NOT raised). The CALIBRATE-FIRST probe
(eval/injection-gating/, 2026-06-15) found mem0 2.0.4 does HYBRID search:
score_and_rank() gates each candidate on its SEMANTIC score (raw Qdrant cosine)
but returns the higher combined (semantic+bm25+entity) score. On the SEMANTIC scale
the threshold actually gates, EmbeddingGemma's separation is compressed — off-domain
prompts <=0.12, relevant 0.25-0.57 — so 0.30 already cleanly rejects clearly-irrelevant
and a raise to the research's 0.5-0.6 would drop ~100% of relevant. This test pins
0.30 so the calibration finding can't be silently undone.

STATIC by design — app.py is parsed with `ast` (no import, no server) so it runs
in the unit lane regardless of whether mem0/Qdrant are up (mirrors
test_tier_parity.py)."""
import ast
import json
import pathlib

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_SERVER_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SERVER_DIR.parent
_APP_PY = _SERVER_DIR / "app.py"
_TIERS_JSON = _REPO_ROOT / "claude-config" / "model-tiers.json"


def _server_policy() -> dict:
    tree = ast.parse(_APP_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "TIER_BUNDLE_POLICY":
                    return ast.literal_eval(node.value)
        if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "TIER_BUNDLE_POLICY"
                and node.value is not None):
            return ast.literal_eval(node.value)
    raise AssertionError("TIER_BUNDLE_POLICY assignment not found in app.py")


def _client_tiers() -> dict:
    return json.loads(_TIERS_JSON.read_text(encoding="utf-8"))["tiers"]


def test_r2_relevance_threshold_kept_at_030():
    """Both tiers gate at 0.30 — KEPT, not raised. The calibration found the
    threshold gates the hybrid-search SEMANTIC score whose EmbeddingGemma separation
    is compressed (off-domain <=0.12, relevant 0.25-0.57), so 0.30 is already correct
    and a raise craters recall. Unified across tiers (small was 0.33, which
    over-abstains on this scale); per-tier scaling lives in K, not the threshold."""
    server = _server_policy()
    client = _client_tiers()
    for tier in ("frontier", "small"):
        assert server[tier]["relevance_threshold"] == 0.30, (
            f"{tier} relevance_threshold must be 0.30 (calibration-confirmed; not "
            f"raised), got {server[tier]['relevance_threshold']} (app.py)")
        assert client[tier]["relevance_threshold"] == 0.30, (
            f"{tier} relevance_threshold must be 0.30 in model-tiers.json, got "
            f"{client[tier]['relevance_threshold']}")


def test_r2_memory_cap_k_is_1_to_2():
    """K capped at 1-2: frontier=2, small=1 (fewer, higher-relevance memories)."""
    server = _server_policy()
    client = _client_tiers()
    assert server["frontier"]["memory_cap"] == 2, (
        f"frontier K must be 2, got {server['frontier']['memory_cap']}")
    assert server["small"]["memory_cap"] == 1, (
        f"small K must be 1, got {server['small']['memory_cap']}")
    assert client["frontier"]["memory_cap"] == 2
    assert client["small"]["memory_cap"] == 1


def test_r2_goal_oq_caps_unchanged():
    """R2 is a minimal change: it touches K + threshold + block-level abstention,
    NOT the goal/OQ caps. The block-abstention (no memory -> no block) is what
    drops goal/OQ injection FREQUENCY; their per-fire caps stay as-is."""
    server = _server_policy()
    assert server["frontier"]["goal_cap"] == 5
    assert server["frontier"]["oq_cap"] == 3
    assert server["small"]["goal_cap"] == 3
    assert server["small"]["oq_cap"] == 2
