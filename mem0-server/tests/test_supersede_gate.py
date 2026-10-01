"""The metadata-write decision for hide keys, end to end and headless.

PATCH /v1/memories/{id}/metadata runs security_invariants.assert_writable (tier gate) and then
security_invariants.authorize_metadata_patch (key policy). Two keys HIDE a record from default
retrieval in the admission gate: superseded_by and contradicts_canonical.

Before 1.32.4 the only guard on those keys was the body's free-text `actor`: the trusted-actor
early return in assert_writable skips the canonical HMAC check, so any API-key holder sending
actor="supersession-resolve-v030" (or the sweep's actor) could hide ANY record, canonical
included. superseded_by now has exactly one writer, the supersede endpoint, whose policy the
server enforces; and no PATCH may put a hide key on a canonical record.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

fastapi = pytest.importorskip("fastapi")
import security_invariants as si  # noqa: E402

MID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def _canonical_key(monkeypatch):
    """A key exists (as on a provisioned box), so a missing token is a 403, not a 503."""
    monkeypatch.setattr(si, "_get_canonical_key", lambda: "k" * 43)


class _Rec:
    def __init__(self, payload):
        self.payload = payload


class _Client:
    """Just enough of QdrantClient for fetch_current_tier."""

    def __init__(self, tier):
        self._tier = tier

    def retrieve(self, collection_name, ids, with_payload=True, with_vectors=False):
        return [_Rec({"tier": self._tier, "data": "x"})]


def _patch(tier, actor, keys, token=None):
    """Run the PATCH /metadata authorisation exactly as the handler does (no token: no HMAC)."""
    current = si.assert_writable(_Client(tier), "memories", MID, "patch_metadata",
                                 token, None, actor=actor, reason="r", x_user_direct_nonce=None)
    si.authorize_metadata_patch(current, actor, keys)


def _refused(tier, actor, keys):
    with pytest.raises(fastapi.HTTPException) as e:
        _patch(tier, actor, keys)
    return e.value.status_code


# ---- the policy that must NOT change -------------------------------------------------------------

@pytest.mark.parametrize("tier,actor,keys", [
    ("evidence", "", {"custom_tag"}),
    ("evidence", "system", {"tier_actor"}),
    ("evidence", "system", {"expires_at", "tier_actor"}),
    ("evidence", "decay-scan", {"expires_at"}),
    ("evidence", "backfill-apply-v013", {"retrievable"}),
    ("evidence", "contradiction-sweep-v019", {"contradicts_canonical", "contradiction_checked_at"}),
    ("evidence", "contradiction-sweep-v019", {"contradicts_canonical_pending"}),
    ("insight", "contradiction-sweep-v019", {"contradiction_checked_at"}),
    ("canonical", "stamp-retired-v013", {"retired_at"}),
    ("canonical", "contradiction-sweep-v019", {"contradiction_checked_at"}),
])
def test_allowed_metadata_writes_stay_allowed(tier, actor, keys):
    _patch(tier, actor, keys)


@pytest.mark.parametrize("tier,actor,keys", [
    ("evidence", "", {"superseded_by"}),
    ("evidence", "claude-autonomous", {"contradicts_canonical"}),
    ("evidence", "", {"retrievable"}),
    ("evidence", "system", {"tier_actor", "superseded_by"}),          # smuggling
    ("evidence", "contradiction-sweep-v019", {"retired_at"}),
    ("evidence", "stamp-retired-v013", {"retired_at", "custom_tag"}),
    ("canonical", "", {"custom_tag"}),                                # canonical needs the token
    ("insight", "", {"custom_tag"}),                                  # insight needs a consolidator
])
def test_refused_metadata_writes_stay_refused(tier, actor, keys):
    assert _refused(tier, actor, keys) == 403


# ---- F1: the actor string is not a credential for hiding a record ---------------------------------

@pytest.mark.parametrize("tier", ["evidence", "stable", "temporal", "insight", "canonical"])
def test_superseded_by_has_no_patch_writer(tier):
    """The supersede endpoint is the only writer; the old resolve actor string opens nothing."""
    assert _refused(tier, "supersession-resolve-v030", {"superseded_by"}) == 403


def test_no_trusted_actor_lists_superseded_by():
    assert not any("superseded_by" in keys for keys in si.TRUSTED_PATCH_ACTORS.values())


def test_a_canonical_record_cannot_be_hidden_by_the_sweep_actor_string():
    """contradicts_canonical on a canonical record would hide it; the sweep never stamps canonicals."""
    assert _refused("canonical", "contradiction-sweep-v019", {"contradicts_canonical"}) == 403
    assert _refused("canonical", "contradiction-sweep-v019",
                    {"contradicts_canonical", "contradiction_checked_at"}) == 403


def test_hide_keys_on_canonical_are_refused_whatever_the_actor():
    for actor in ("", "system", "contradiction-sweep-v019", "supersession-resolve-v030",
                  "stamp-retired-v013", "user-direct"):
        for key in sorted(si.RETRIEVAL_HIDE_KEYS):
            assert _refused("canonical", actor, {key}) == 403, (actor, key)


def test_retrieval_hide_keys_match_what_the_admission_gate_enforces():
    """Every key the gate rejects on (outside the forensic class) is a hide key here."""
    import admission_gate
    src = Path(admission_gate.__file__).read_text(encoding="utf-8")
    assert 'meta.get("superseded_by")' in src and 'meta.get("contradicts_canonical")' in src
    assert si.RETRIEVAL_HIDE_KEYS == frozenset({"superseded_by", "contradicts_canonical"})


def test_the_handler_uses_the_pure_policy():
    """app.py cannot be imported headless: pin that update_metadata calls both gates."""
    src = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    body = src[src.index("def update_metadata("):src.index("# v0.16: Goal endpoints")]
    assert "assert_writable(" in body
    assert "authorize_metadata_patch(current_tier, b.actor, b.metadata.keys())" in body
    assert "FORBIDDEN_KEYS = {" not in body, "the key policy lives in security_invariants only"


# ---- the supersede door (app.py source pins; app.py cannot be imported headless) -----------------

def _handler(name: str, end_marker: str) -> str:
    src = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    start = src.index(f"def {name}(")
    return src[start:src.index(end_marker, start)]


def test_supersede_endpoint_enforces_the_policy_under_both_locks_and_audits_first():
    body = _handler("supersede_memory", "@app.delete(")
    assert body.index("auth(x_api_key)") < body.index("_supersede_locks(mid, b.winner_id)")
    lock_at = body.index("with _supersede_locks(mid, b.winner_id):")
    assert lock_at < body.index("_supersede_read(mid)") < body.index("_ss.precheck(")
    assert body.index("_ss.precheck(") < body.index('"supersede-intent"') < body.index("set_payload(")
    assert '"actor": _ss.ENDPOINT_ACTOR' in body
    assert "shared_brands=_shared_brands_from_env()" in body


def test_supersede_body_has_no_actor_field():
    """The ledger actor is stamped by the server; the request model cannot carry one."""
    src = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    model = src[src.index("class SupersedeIn(BaseModel):"):]
    model = model[:model.index("\nclass ")]
    assert "actor" not in model.split('"""')[0].replace("ledger actor", "")


def test_unsupersede_endpoint_rechecks_and_audits_first():
    body = _handler("unsupersede_memory", "# v0.16: Goal endpoints")
    assert body.index("auth(x_api_key)") < body.index("_ss.clear_precheck(")
    assert body.index("_ss.clear_precheck(") < body.index('"unsupersede-intent"') < body.index("delete_payload(")


def test_put_names_a_hand_written_marker():
    body = _handler("update", '@app.patch("/v1/memories/{mid}/tier")')
    assert "_ss.classify_text(b.text)" in body and "supersede_note" in body


def test_the_door_keys_are_forbidden_on_add_and_patch():
    import ast
    import supersession as ss
    tree = ast.parse((SERVER_DIR / "app.py").read_text(encoding="utf-8"))
    add_set = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign)
                   and any(getattr(t, "id", None) == "_ADD_FORBIDDEN_META" for t in node.targets))
    for key in ss.SUPERSEDE_KEYS:
        assert key in add_set, key
        assert key in si.METADATA_FORBIDDEN_KEYS, key
        assert _refused("evidence", "", {key}) == 403


def test_a_text_update_keeps_the_supersession():
    import supersession as ss
    from payload_carryover import compute_carryover
    payload = {"tier": "evidence", "superseded_by": "w", "superseded_at": "t",
               "superseded_via": "v", "partially_superseded_by": [{"winner_id": "w"}]}
    kept = compute_carryover(payload)
    assert ss.SUPERSEDE_KEYS <= set(kept)


def test_the_ledger_audit_knows_every_supersede_event():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ledger_audit_schema", SERVER_DIR.parent / "scripts" / "wsl" / "ledger-audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    written = {"ts", "event", "memory_id", "winner_id", "scope", "detail", "actor", "source",
               "reason", "prior_tier", "transport", "status", "schema_version"}
    for event in ("supersede", "supersede-intent"):
        allowed = set(mod.SCHEMA[event]["required"]) | set(mod.SCHEMA[event]["optional"])
        assert written <= allowed, (event, written - allowed)
    unwritten = {"ts", "event", "memory_id", "scope", "cleared", "actor", "reason", "prior_tier",
                 "transport", "status", "schema_version"}
    for event in ("unsupersede", "unsupersede-intent"):
        allowed = set(mod.SCHEMA[event]["required"]) | set(mod.SCHEMA[event]["optional"])
        assert unwritten <= allowed, (event, unwritten - allowed)


def test_supersession_module_is_deployed():
    text = (SERVER_DIR.parent / "install" / "1-wsl-services.sh").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("MEM0_MODULES="))
    assert " supersession.py" in line
