"""v0.23 L7: guard TIER_BUNDLE_POLICY (server) against claude-config/model-tiers.json
(the client-side detection config the PowerShell hook reads). The two are maintained
by hand in different files/languages, so they can silently drift — and a drift means
a model gets a bundle the hook never asked for (or the server fails open and ignores
the tier). These tests catch that.

STATIC by design: app.py is parsed with `ast` (no import, no server, no side effects)
so this runs in the unit lane regardless of whether mem0/Qdrant are up."""
import ast
import json
import pathlib

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_SERVER_DIR = _TESTS_DIR.parent                       # mem0-server/
_REPO_ROOT = _SERVER_DIR.parent                       # repo root
_APP_PY = _SERVER_DIR / "app.py"
_TIERS_JSON = _REPO_ROOT / "claude-config" / "model-tiers.json"

# Fields the server actually enforces. model-tiers.json also carries client-only
# fields (match[], format, include_legend) — excluded from the parity comparison.
_SHARED_FIELDS = ("memory_cap", "goal_cap", "oq_cap", "relevance_threshold")


def _server_policy() -> dict:
    """Extract the TIER_BUNDLE_POLICY literal from app.py without importing it."""
    tree = ast.parse(_APP_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        # plain assignment: TIER_BUNDLE_POLICY = {...}
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "TIER_BUNDLE_POLICY":
                    return ast.literal_eval(node.value)
        # annotated assignment: TIER_BUNDLE_POLICY: dict[...] = {...}
        if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "TIER_BUNDLE_POLICY"
                and node.value is not None):
            return ast.literal_eval(node.value)
    raise AssertionError("TIER_BUNDLE_POLICY assignment not found in app.py")


def _client_tiers() -> dict:
    return json.loads(_TIERS_JSON.read_text(encoding="utf-8"))["tiers"]


def test_tier_name_sets_match():
    server = set(_server_policy())
    client = set(_client_tiers())
    assert server == client, (
        f"tier-name drift: app.py TIER_BUNDLE_POLICY has {sorted(server)}, "
        f"claude-config/model-tiers.json has {sorted(client)}")


def test_tier_caps_and_threshold_match():
    server = _server_policy()
    client = _client_tiers()
    for tier in server:
        for field in _SHARED_FIELDS:
            assert server[tier][field] == client[tier][field], (
                f"{tier}.{field} drift: app.py={server[tier][field]} "
                f"vs model-tiers.json={client[tier][field]}")


def test_default_tier_is_frontier():
    """default_tier must be frontier so unknown/legacy model strings (and a stale
    'mid' sidecar) fail open to the richest bundle, never under-serve."""
    cfg = json.loads(_TIERS_JSON.read_text(encoding="utf-8"))
    assert cfg.get("default_tier") == "frontier"
    assert "frontier" in cfg["tiers"]


def test_sonnet_is_frontier_class():
    """v0.23 portfolio: Sonnet (1M flagship) maps to frontier, not a trimmed bucket.
    The hook matches case-insensitive substrings against match[]."""
    tiers = _client_tiers()
    frontier_match = [m.lower() for m in tiers["frontier"]["match"]]
    assert any("sonnet" in m for m in frontier_match), \
        f"'sonnet' not in frontier.match: {frontier_match}"
    small_match = [m.lower() for m in tiers.get("small", {}).get("match", [])]
    assert not any("sonnet" in m for m in small_match), \
        "Sonnet must not be claimed by the small tier"


def test_every_1m_flagship_family_is_frontier_class():
    """All current 1M-context Claude flagships resolve to frontier via substring
    match. Mirrors Resolve-ModelTier (PowerShell) semantics: case-insensitive,
    first-declared tier wins."""
    tiers = _client_tiers()
    # rebuild the ordered match table the way the hook reads it
    ordered = list(tiers.items())

    def resolve(model: str) -> str:
        ml = model.lower()
        for name, spec in ordered:
            if any(sub.lower() in ml for sub in spec.get("match", [])):
                return name
        return "frontier"  # default_tier

    for model in ("claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
                  "claude-fable-5", "claude-sonnet-4-6"):
        assert resolve(model) == "frontier", f"{model} should be frontier-class"
    assert resolve("claude-haiku-4-5") == "small"
