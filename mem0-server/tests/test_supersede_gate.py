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


SVC = "s" * 64


@pytest.fixture(autouse=True)
def _canonical_key(monkeypatch):
    """A key exists (as on a provisioned box), so a missing token is a 403, not a 503. 1.32.5: so
    does the authority's service key, which a server-side job label needs to count."""
    monkeypatch.setattr(si, "_get_canonical_key", lambda: "k" * 43)
    monkeypatch.setattr(si, "_get_service_key", lambda: SVC)


class _Rec:
    def __init__(self, payload):
        self.payload = payload


class _Client:
    """Just enough of QdrantClient for fetch_current_tier."""

    def __init__(self, tier):
        self._tier = tier

    def retrieve(self, collection_name, ids, with_payload=True, with_vectors=False):
        return [_Rec({"tier": self._tier, "data": "x"})]


def _patch(tier, actor, keys, token=None, service_key=None):
    """Run the PATCH /metadata authorisation exactly as the handler does (no token: no HMAC):
    1.32.5 the label gate first, then the tier gate and the key policy with its verdict."""
    verified = si.require_service_credential(actor, service_key)
    current = si.assert_writable(_Client(tier), "memories", MID, "patch_metadata",
                                 token, None, actor=actor, reason="r", x_user_direct_nonce=None,
                                 service_verified=verified)
    si.authorize_metadata_patch(current, actor, keys, service_verified=verified)


def _refused(tier, actor, keys, service_key=None):
    with pytest.raises(fastapi.HTTPException) as e:
        _patch(tier, actor, keys, service_key=service_key)
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
    # A server-side job's label is proven by the service key (the unit loads it).
    _patch(tier, actor, keys, service_key=SVC)


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
    # ...and the service key does not open them either: it proves a label, it is not a pass.
    assert _refused(tier, actor, keys, service_key=SVC) == 403


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
    assert ("authorize_metadata_patch(current_tier, b.actor, b.metadata.keys(), "
            "service_verified=_service_verified)") in body
    assert "FORBIDDEN_KEYS = {" not in body, "the key policy lives in security_invariants only"


# ---- the supersede door (app.py source pins; app.py cannot be imported headless) -----------------

def _handler(name: str, end_marker: str) -> str:
    src = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    start = src.index(f"def {name}(")
    return src[start:src.index(end_marker, start)]


def test_supersede_endpoint_runs_the_transaction_under_both_locks():
    """The order (read both, precheck, intent line, write) lives in supersession.run_supersede and is
    executed headless in test_supersession.py; the endpoint must call it under both locks."""
    body = _handler("supersede_memory", "@app.delete(")
    assert body.index("auth(x_api_key)") < body.index("_supersede_locks(mid, b.winner_id)")
    lock_at = body.index("with _supersede_locks(mid, b.winner_id):")
    assert lock_at < body.index("_supersede_call(") < body.index("_ss.run_supersede")
    assert "shared_brands=_shared_brands_from_env()" in body
    assert "reason=b.reason" in body and "detail=b.detail" in body
    assert body.index("_supersede_call(") < body.index("return _supersede_finish(out)")


def test_supersede_body_has_no_actor_field():
    """The ledger actor is stamped by the server; the request model cannot carry one."""
    src = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    model = src[src.index("class SupersedeIn(BaseModel):"):]
    model = model[:model.index("\nclass ")]
    assert "actor" not in model.split('"""')[0].replace("ledger actor", "")


def test_unsupersede_endpoint_runs_the_transaction_under_the_lock():
    body = _handler("unsupersede_memory", "# v0.16: Goal endpoints")
    assert body.index("auth(x_api_key)") < body.index("with _supersede_locks(mid):")
    assert body.index("with _supersede_locks(mid):") < body.index("_ss.run_unsupersede")
    assert "reason=reason" in body


def test_supersede_call_maps_refusals_and_a_missing_ledger():
    body = _handler("_supersede_call", "def _supersede_finish(")
    assert "except _ss.Refused as e:" in body and "e.refusal.status" in body
    assert "except _ss.LedgerUnavailable" in body and "503" in body
    assert "fn(_SupersedeStore(), _append_ledger, **kw)" in body


def test_cascade_never_deletes_a_protected_member_through_a_supersession_link():
    src = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    start = src.index('@app.delete("/v1/memories/{mid}")')
    nxt = src.find("@app.", start + 10)
    body = src[start:] if nxt < 0 else src[start:nxt]
    loop = body[body.index("for _linked_id in chain_ids:"):]
    assert loop.index("_ss.cascade_protected(_linked_payload)") < loop.index("mem.delete(memory_id=_linked_id)")
    assert "cascade_skipped.append(_linked_id)" in loop
    assert '"cascade_skipped_protected"' in body


def test_a_plain_delete_names_the_records_it_leaves_superseded():
    src = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    start = src.index('@app.delete("/v1/memories/{mid}")')
    nxt = src.find("@app.", start + 10)
    body = src[start:] if nxt < 0 else src[start:nxt]
    tail = body[body.index("if not cascade:"):]
    assert '"superseded_by"' in tail and '"orphaned_supersessions"' in tail
    assert "except Exception:" in tail, "the report is fail-soft: it may never fail a delete"


def test_a_superseded_record_is_never_promoted_into_a_protected_tier():
    body = _handler("update_tier", "def update_metadata(")
    locked = body[body.index("with _mid_write_lock(mid):"):]
    assert locked.index("_ss.promotion_refusal(_supersede_read(mid), b.tier)") < locked.index('"tier-change-intent"')


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


# ---- 1.32.5: a server-side job label is a claim the service key must prove --------------------------

PRIVILEGED_WRITES = [
    ("evidence", "system", {"tier_actor"}),
    ("evidence", "decay-scan", {"expires_at"}),
    ("evidence", "backfill-apply-v013", {"retrievable"}),
    ("evidence", "contradiction-sweep-v019", {"contradicts_canonical", "contradiction_checked_at"}),
    ("insight", "contradiction-sweep-v019", {"contradicts_canonical"}),
    ("insight", "dream-consolidator", {"custom_tag"}),
    ("canonical", "stamp-retired-v013", {"retired_at"}),
    ("canonical", "contradiction-sweep-v019", {"contradiction_checked_at"}),
]


@pytest.mark.parametrize("tier,actor,keys", PRIVILEGED_WRITES)
def test_a_job_label_without_the_service_key_is_refused(tier, actor, keys):
    """THE 1.32.5 hole: any API-key holder could send these labels. Without the key: 403."""
    with pytest.raises(fastapi.HTTPException) as e:
        _patch(tier, actor, keys)
    assert e.value.status_code == 403 and "service-credential-required" in str(e.value.detail)


@pytest.mark.parametrize("tier,actor,keys", PRIVILEGED_WRITES)
def test_the_policy_functions_fail_closed_without_the_proof(tier, actor, keys):
    """Defence in depth: a handler that FORGETS require_service_credential must still be refused,
    so the pure policy honours a label only when told it was proven."""
    with pytest.raises(fastapi.HTTPException) as e:
        current = si.assert_writable(_Client(tier), "memories", MID, "patch_metadata",
                                     None, None, actor=actor, reason="r", x_user_direct_nonce=None)
        si.authorize_metadata_patch(current, actor, keys)
    assert e.value.status_code == 403


@pytest.mark.parametrize("label", [
    "contradiction-sweep-v019", "  Contradiction-Sweep-V019  ", "DREAM-CONSOLIDATOR",
    "bac\u212aFill-apply-v013",  # KELVIN SIGN lower()s to "k": still the privileged label
])
def test_label_normalisation_cannot_dodge_the_gate(label):
    assert si.is_privileged_label(label)
    with pytest.raises(fastapi.HTTPException) as e:
        si.require_service_credential(label, None)
    assert e.value.status_code == 403


@pytest.mark.parametrize("presented", [None, "", "   ", "s" * 63, "s" * 65, SVC.upper(), "\u00e9" * 64, 42])
def test_only_the_exact_key_proves_a_label(presented):
    with pytest.raises(fastapi.HTTPException):
        si.require_service_credential("dream-consolidator", presented)


def test_the_exact_key_proves_a_label_and_surrounding_whitespace_is_tolerated():
    assert si.require_service_credential("dream-consolidator", SVC) is True
    assert si.require_service_credential("dream-consolidator", " " + SVC + "\n") is True


@pytest.mark.parametrize("label", [None, "", "claude-autonomous", "user-direct", "rest-api", 42])
def test_an_ordinary_label_needs_no_key(label):
    assert si.require_service_credential(label, None) is False


def test_a_server_without_the_key_accepts_no_job_label_and_says_why(monkeypatch):
    monkeypatch.setattr(si, "_get_service_key", lambda: None)
    for presented in (None, "", SVC):
        with pytest.raises(fastapi.HTTPException) as e:
            si.require_service_credential("contradiction-sweep-v019", presented)
        assert e.value.status_code == 403 and "holds no service key" in str(e.value.detail)


def test_every_privileged_table_is_covered():
    labels = set(si.TRUSTED_PATCH_ACTORS) | set(si.LEGACY_PATCH_ACTOR_KEYS) | set(si.INSIGHT_ALLOWED_ACTORS)
    assert labels and all(si.is_privileged_label(x) for x in labels)


HANDLERS = {
    "add": ("def add(", "require_service_credential(src, x_ams_service_key", None),
    "update": ("def update(", "require_service_credential(actor, x_ams_service_key)", "assert_writable("),
    "update_tier": ("def update_tier(", "require_service_credential(actor, x_ams_service_key)", "tier_change_hmac_action("),
    "update_metadata": ("def update_metadata(", "require_service_credential(b.actor, x_ams_service_key)", "assert_writable("),
    "delete": ("def delete(", "require_service_credential(actor, x_ams_service_key)", "assert_writable("),
}


@pytest.mark.parametrize("name", sorted(HANDLERS))
def test_every_write_handler_gates_its_label_before_the_policy(name):
    """app.py cannot be imported headless: pin that each write handler declares the header and
    calls the gate BEFORE it hands the label to the policy, and passes the proof on."""
    src = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    start, gate, policy = HANDLERS[name]
    i = src.index(start)
    end = src.find("\n@app.", i + 10)
    body = src[i:end if end != -1 else len(src)]  # delete() is the last route in app.py
    assert 'alias="X-AMS-Service-Key"' in body, name
    assert gate in body, name
    if policy:
        assert body.index(gate) < body.index(policy), f"{name}: the label gate must run before the policy"
    if name in ("update", "update_metadata", "delete"):
        assert "service_verified=_service_verified" in body, name
