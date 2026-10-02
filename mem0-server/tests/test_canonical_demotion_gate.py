"""Canonical demotion needs the operator's signed token.

The canonical write gate (security_invariants.assert_writable) makes PUT, DELETE and
metadata PATCH on a canonical record require an HMAC user-direct token. PATCH /tier only
gated promotions INTO canonical, so any API-key holder could demote a canonical record to
evidence and then PUT or DELETE it with no token: a two-step bypass of the whole gate.

Demotion OUT of canonical now signs its own action word ("demote"), so a promote token
cannot be replayed as a demotion and no unsigned client can take a record out of the
canonical tier. These tests pin the policy as a pure function (the app.py handler calls it)
and the signing contract of mem0-canonize.sh. Headless: no live stack, no network.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import security_invariants as si  # noqa: E402

fastapi = pytest.importorskip("fastapi")


@pytest.mark.parametrize("current,target,expected", [
    (None, "canonical", "promote"),
    ("evidence", "canonical", "promote"),
    ("stable", "canonical", "promote"),
    ("canonical", "canonical", "promote"),
    ("canonical", "evidence", "demote"),
    ("canonical", "stable", "demote"),
    ("canonical", "temporal", "demote"),
    ("canonical", "insight", "demote"),
    # 1.32.5: a move OUT of insight signs "demote" too (the insight two-step hole).
    ("insight", "evidence", "demote"),
    ("insight", "stable", "demote"),
    ("insight", "temporal", "demote"),
    ("insight", "insight", None),
    ("evidence", "stable", None),
    ("stable", "evidence", None),
    (None, "evidence", None),
])
def test_tier_change_hmac_action_matrix(current, target, expected):
    assert si.tier_change_hmac_action(current, target) == expected


def test_demote_is_a_signed_action():
    assert "demote" in si.VALID_HMAC_ACTIONS


def _sign(key: str, ts: str, nonce: str, action: str, mid: str, reason: str) -> str:
    msg = f"{ts}|{nonce}|{action}|{mid}|{reason}".encode("utf-8")
    return base64.b64encode(hmac.new(key.encode("utf-8"), msg, hashlib.sha256).digest()).decode("ascii")


@pytest.fixture
def signing(monkeypatch):
    key = "k" * 43
    seen = set()

    def _record(nonce, ts):
        if nonce in seen:
            return False
        seen.add(nonce)
        return True

    monkeypatch.setattr(si, "_get_canonical_key", lambda: key)
    monkeypatch.setattr(si, "_check_and_record_nonce", _record)
    return key


SERVICE_KEY = "s" * 64


@pytest.fixture
def service_key(monkeypatch):
    """1.32.5: the authority's service key, so a consolidator label is a PROVEN claim."""
    monkeypatch.setattr(si, "_get_service_key", lambda: SERVICE_KEY)
    return SERVICE_KEY


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_signed_demote_is_accepted(signing):
    ts, nonce = _now(), str(uuid.uuid4())
    tok = _sign(signing, ts, nonce, "demote", "mid-1", "stale fact")
    si.validate_hmac_user_direct("mid-1", "demote", "stale fact", tok, ts, x_user_direct_nonce=nonce)


def test_promote_token_cannot_be_replayed_as_demote(signing):
    ts, nonce = _now(), str(uuid.uuid4())
    tok = _sign(signing, ts, nonce, "promote", "mid-1", "stale fact")
    with pytest.raises(fastapi.HTTPException) as e:
        si.validate_hmac_user_direct("mid-1", "demote", "stale fact", tok, ts, x_user_direct_nonce=nonce)
    assert e.value.status_code == 403


def test_unsigned_demote_is_refused(signing):
    with pytest.raises(fastapi.HTTPException) as e:
        si.validate_hmac_user_direct("mid-1", "demote", "stale fact", None, None, x_user_direct_nonce=None)
    assert e.value.status_code == 403
    assert "--action demote" in str(e.value.detail)


# ---- mem0-canonize.sh --action demote: the operator's signed path -------------------------

CANON = REPO_ROOT / "scripts" / "wsl" / "mem0-canonize.sh"
bash_required = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("openssl") is None,
                                   reason="bash and openssl required")


def _sandbox(tmp_path: Path) -> dict:
    """A HOME that holds a FAKE api key and canonical key, a fake curl that records its argv,
    and a MEM0_URL that cannot resolve: nothing here can reach a real server."""
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    (home / ".mem0" / "api-key").write_text("fake-api-key\n", encoding="utf-8")
    (home / ".mem0" / "canonical-key").write_text("k" * 43, encoding="utf-8")
    (home / ".mem0" / "role").write_text("brain\n", encoding="utf-8")
    rt = home / "runtime"
    rt.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    rec = tmp_path / "curl.args"
    curl = bindir / "curl"
    curl.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > " + str(rec) + "\necho '{\"ok\": true}'\n",
                    encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    env = {"HOME": str(home), "USERPROFILE": str(home),
           "HOMEDRIVE": os.path.splitdrive(str(home))[0], "HOMEPATH": os.path.splitdrive(str(home))[1],
           "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
           "XDG_RUNTIME_DIR": str(rt), "MEM0_URL": "http://authority.invalid:1"}
    return {"env": env, "rec": rec}


def _run(sb, *argv):
    return subprocess.run(["bash", str(CANON), *argv], env=sb["env"], capture_output=True, text=True)


@bash_required
def test_canonize_demote_signs_the_demote_action(tmp_path):
    sb = _sandbox(tmp_path)
    r = _run(sb, "--action", "demote", "mid-9", "gate bypass promotion")
    assert r.returncode == 0, r.stderr
    args = sb["rec"].read_text(encoding="utf-8").splitlines()
    assert "PATCH" in args and any(a.endswith("/v1/memories/mid-9/tier") for a in args), args
    body = json.loads(args[args.index("-d") + 1])
    assert body == {"tier": "evidence", "actor": "user-direct", "reason": "gate bypass promotion"}
    hdr = {a.split(": ", 1)[0]: a.split(": ", 1)[1] for a in args if ": " in a}
    ts, nonce, tok = hdr["X-User-Direct-Ts"], hdr["X-User-Direct-Nonce"], hdr["X-User-Direct-Token"]
    assert tok == _sign("k" * 43, ts, nonce, "demote", "mid-9", "gate bypass promotion")


@bash_required
def test_canonize_demote_honours_target_tier_and_refuses_canonical(tmp_path):
    sb = _sandbox(tmp_path)
    r = _run(sb, "--action", "demote", "--tier", "stable", "mid-9", "why")
    assert r.returncode == 0, r.stderr
    args = sb["rec"].read_text(encoding="utf-8").splitlines()
    assert json.loads(args[args.index("-d") + 1])["tier"] == "stable"
    r2 = _run(sb, "--action", "demote", "--tier", "canonical", "mid-9", "why")
    assert r2.returncode != 0 and "canonical" in r2.stderr


@bash_required
def test_canonize_signs_the_stripped_reason(tmp_path):
    """The server verifies the HMAC over reason.strip(); the CLI must sign the same string."""
    sb = _sandbox(tmp_path)
    r = _run(sb, "--action", "demote", "mid-9", "  padded reason \n")
    assert r.returncode == 0, r.stderr
    args = sb["rec"].read_text(encoding="utf-8").splitlines()
    assert json.loads(args[args.index("-d") + 1])["reason"] == "padded reason"
    hdr = {a.split(": ", 1)[0]: a.split(": ", 1)[1] for a in args if ": " in a}
    ts, nonce, tok = hdr["X-User-Direct-Ts"], hdr["X-User-Direct-Nonce"], hdr["X-User-Direct-Token"]
    assert tok == _sign("k" * 43, ts, nonce, "demote", "mid-9", "padded reason")


@bash_required
def test_canonize_help_prints_the_whole_header(tmp_path):
    sb = _sandbox(tmp_path)
    r = _run(sb, "--help")
    assert r.returncode == 0, r.stderr
    block = []
    for ln in CANON.read_text(encoding="utf-8").splitlines()[1:]:   # after the shebang
        if not ln.startswith("#"):
            break
        block.append(ln)
    assert len(r.stdout.splitlines()) == len(block), "help must print the whole leading comment block"
    assert "Irreversible." in r.stdout   # the line a fixed 50-line cap used to cut


# ---- the handler wiring itself (app.py update_tier), headless -------------------------------
# The pure policy above can be right while the handler stops calling it. These tests pin the
# call site: first by source order, then by running the real update_tier (extracted from app.py
# with ast, decorators dropped) against a fake vector store.

def _update_tier_src() -> str:
    src = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    t = src.find("def update_tier(")
    assert t != -1
    return src[t:src.find("\n@app.", t + 10)]


def test_update_tier_wiring_order_is_pinned():
    body = _update_tier_src()
    i_policy = body.find("tier_change_hmac_action(")
    i_val = body.find("validate_hmac_user_direct(", i_policy)
    i_word = body.find('"demote"', i_val)
    i_lock = body.find("_mid_write_lock(")
    i_reread = body.find("fetch_current_tier(", i_lock)
    i_intent = body.find('"tier-change-intent"')
    i_set = body.find("set_payload(")
    assert -1 not in (i_policy, i_val, i_word, i_lock, i_reread, i_intent, i_set), body[:200]
    assert i_word - i_val < 80, "the validator call right after the policy must sign 'demote'"
    assert i_policy < i_val < i_lock < i_reread < i_intent < i_set, (
        "order must be: policy -> signed demote -> write lock -> tier re-read -> intent -> write")
    assert "raise _TierRaced" in body
    i_http = body.find("except HTTPException:\n        raise")
    i_generic = body.find('log.exception("tier-update failed")')
    assert i_http != -1 and i_generic != -1 and i_http < i_generic, (
        "HTTPExceptions raised under the lock (503/409) must pass through, not become 5xx upstream errors")


_MISSING = object()   # a point with no tier field
_ABSENT = object()    # no point at all


class _FakeStore:
    """retrieve() answers from `tiers`, one entry per call (the last one repeats); an index in
    `fail_on` raises instead. set_payload() is recorded."""

    def __init__(self, tiers, fail_on=()):
        self.tiers, self.fail_on, self.n, self.writes = list(tiers), set(fail_on), 0, []

    def retrieve(self, collection_name, ids, with_payload=True, with_vectors=False):
        i, self.n = self.n, self.n + 1
        if i in self.fail_on:
            raise RuntimeError("store down")
        t = self.tiers[min(i, len(self.tiers) - 1)]
        if t is _ABSENT:
            return []
        payload = {"data": "a fact"} if t is _MISSING else {"data": "a fact", "tier": t}
        return [type("Rec", (), {"id": ids[0], "payload": payload})()]

    def set_payload(self, collection_name, payload, points):
        self.writes.append((payload, points))


def _handler(store: _FakeStore, ledger_fail: bool = False):
    import ast
    import logging
    import threading
    import types
    from typing import Optional

    from fastapi import Header, HTTPException
    from pydantic import BaseModel

    tree = ast.parse((SERVER_DIR / "app.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name == "update_tier")
             or (isinstance(n, ast.ClassDef) and n.name == "TierIn")]
    assert len(nodes) == 2
    for n in nodes:
        n.decorator_list = []
    ledger: list = []

    def append_ledger(record: dict) -> None:
        if ledger_fail:
            raise OSError("ledger disk full")
        ledger.append(record)

    ns = {
        "HTTPException": HTTPException, "Header": Header, "BaseModel": BaseModel,
        "Optional": Optional, "_dt": dt, "log": logging.getLogger("test-update-tier"),
        "auth": lambda key: None,
        "PROMOTE_ALLOWED_TIERS": {"evidence", "stable", "canonical", "insight", "temporal"},
        "CANONICAL_REQUIRES_USER_DIRECT": True,
        "CANONICAL_AUTOPROMOTE_ALLOWED": {"dream-autopromote"},
        "INSIGHT_REQUIRES_C1": True,
        # 1.32.5: one copy, security_invariants' (app.py imports it).
        "INSIGHT_ALLOWED_ACTORS": si.INSIGHT_ALLOWED_ACTORS,
        "is_imperative_canonical": lambda text: False,
        "mem": types.SimpleNamespace(vector_store=types.SimpleNamespace(client=store, collection_name="memories")),
        "_append_ledger": append_ledger,
        "_mid_write_lock": lambda mid: threading.Lock(),
        "_upstream_error": lambda e: HTTPException(502, f"upstream: {e}"),
        # 1.32.4: the into-insight/canonical supersession refusal reads the point; never superseded here.
        "_supersede_read": lambda mid: {"data": "a fact"},
    }
    # dont_inherit: this test module's `from __future__ import annotations` would otherwise turn
    # TierIn's annotations into strings pydantic cannot resolve outside app.py's namespace.
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py:update_tier", "exec", dont_inherit=True), ns)
    tier_in, fn = ns["TierIn"], ns["update_tier"]

    def call(tier, actor="claude-autonomous", reason="why", token=None, ts=None, nonce=None, service_key=None):
        return fn("mid-1", tier_in(tier=tier, actor=actor, reason=reason), x_api_key="k",
                  x_user_direct_token=token, x_user_direct_ts=ts, x_user_direct_nonce=nonce,
                  x_ams_service_key=service_key)

    return call, ledger


def _status(call, *a, **kw) -> int:
    with pytest.raises(fastapi.HTTPException) as e:
        call(*a, **kw)
    return e.value.status_code


def _intents(ledger) -> list:
    return [r for r in ledger if r.get("event") == "tier-change-intent"]


def test_handler_refuses_unsigned_demotion_of_a_canonical(signing):
    store = _FakeStore(["canonical"])
    call, ledger = _handler(store)
    assert _status(call, "evidence") == 403
    assert store.writes == [] and _intents(ledger) == []


def test_handler_treats_a_tierless_record_as_canonical(signing):
    store = _FakeStore([_MISSING])
    call, ledger = _handler(store)
    assert _status(call, "stable") == 403
    assert store.writes == [] and ledger == []


def test_handler_answers_404_for_a_missing_record(signing):
    store = _FakeStore([_ABSENT])
    call, ledger = _handler(store)
    assert _status(call, "evidence") == 404
    assert store.writes == [] and ledger == []


def test_handler_store_outage_before_the_lock_is_503_without_an_intent(signing):
    store = _FakeStore(["stable"], fail_on={0})
    call, ledger = _handler(store)
    assert _status(call, "evidence") == 503
    assert store.writes == [] and ledger == []


def test_handler_refuses_a_record_promoted_mid_flight_with_409(signing):
    store = _FakeStore(["evidence", "canonical"])
    call, ledger = _handler(store)
    assert _status(call, "stable") == 409
    assert store.writes == [], "an unsigned change must not land on a record that is canonical now"
    assert _intents(ledger) == [], "a refused change must not leave an unpaired intent line"


def test_handler_store_outage_on_the_reread_is_503(signing):
    store = _FakeStore(["evidence", "evidence"], fail_on={1})
    call, ledger = _handler(store)
    assert _status(call, "stable") == 503
    assert store.writes == []


def test_handler_allows_an_unsigned_move_between_non_canonical_tiers(signing):
    store = _FakeStore(["stable", "stable"])
    call, ledger = _handler(store)
    out = call("evidence")
    assert out["ok"] is True and out["tier"] == "evidence"
    assert store.writes and store.writes[0][0]["tier"] == "evidence"
    assert [r["event"] for r in ledger] == ["tier-change-intent", "tier-change"]


def test_handler_accepts_a_signed_demotion(signing):
    store = _FakeStore(["canonical"])
    call, ledger = _handler(store)
    ts, nonce = _now(), str(uuid.uuid4())
    tok = _sign(signing, ts, nonce, "demote", "mid-1", "stale fact")
    out = call("evidence", actor="user-direct", reason="stale fact", token=tok, ts=ts, nonce=nonce)
    assert out["ok"] is True
    assert store.writes and store.writes[0][0]["tier"] == "evidence"
    assert _intents(ledger) and _intents(ledger)[0]["transport"] == "cli-user-direct"


def test_handler_answers_404_for_a_record_deleted_mid_flight(signing):
    store = _FakeStore(["evidence", _ABSENT])
    call, ledger = _handler(store)
    assert _status(call, "stable") == 404
    assert store.writes == [] and ledger == []


def test_handler_refuses_the_change_when_the_intent_line_cannot_be_written(signing):
    store = _FakeStore(["stable", "stable"])
    call, ledger = _handler(store, ledger_fail=True)
    assert _status(call, "evidence") == 503
    assert store.writes == [], "no audit intent, no mutation"


def test_handler_demotion_needs_a_reason(signing):
    store = _FakeStore(["canonical"])
    call, ledger = _handler(store)
    ts, nonce = _now(), str(uuid.uuid4())
    tok = _sign(signing, ts, nonce, "demote", "mid-1", "")
    assert _status(call, "evidence", actor="user-direct", reason="", token=tok, ts=ts, nonce=nonce) == 400
    assert store.writes == []


@pytest.mark.parametrize("target", ["stable", "temporal", "insight"])
def test_handler_every_target_out_of_canonical_needs_the_token(signing, service_key, target):
    # The consolidator label is PROVEN here (service key), so the 403 is the demote gate's own.
    store = _FakeStore(["canonical"])
    call, ledger = _handler(store)
    assert _status(call, target, actor="c1-consolidator", service_key=service_key) == 403
    assert store.writes == [] and ledger == []


def test_lock_key_is_the_canonical_uuid_spelling():
    """Qdrant resolves every spelling of one UUID to the same point, so the per-record write lock
    must too, or a differently spelled request escapes the serialization."""
    import ast
    import uuid as _uuid_mod
    tree = ast.parse((SERVER_DIR / "app.py").read_text(encoding="utf-8"))
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_mid_lock_key"]
    assert len(fn) == 1
    ns = {"_uuid": _uuid_mod}
    exec(compile(ast.Module(body=fn, type_ignores=[]), "app.py:_mid_lock_key", "exec", dont_inherit=True), ns)
    key = ns["_mid_lock_key"]
    u = _uuid_mod.uuid4()
    spellings = [str(u), str(u).upper(), u.hex, "{" + str(u) + "}", "urn:uuid:" + str(u), " " + str(u) + " "]
    assert {key(s) for s in spellings} == {str(u)}
    assert key("not-a-uuid") == "not-a-uuid" and key(42) == "42"
    body = _update_tier_src()
    assert "_mid_write_lock(" in body
    src = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    lk = src[src.find("def _mid_write_lock("):]
    assert "_mid_lock_key(mid)" in lk[:200], "the write lock must key on the normalized id"


def test_handler_refuses_a_promote_token_for_a_demotion(signing):
    store = _FakeStore(["canonical"])
    call, ledger = _handler(store)
    ts, nonce = _now(), str(uuid.uuid4())
    tok = _sign(signing, ts, nonce, "promote", "mid-1", "stale fact")
    assert _status(call, "evidence", actor="user-direct", reason="stale fact", token=tok, ts=ts, nonce=nonce) == 403
    assert store.writes == []


# ---- 1.32.5: insight is protected like canonical; a job label needs the service key ----------

@pytest.mark.parametrize("target", ["evidence", "stable", "temporal"])
def test_handler_refuses_an_unsigned_move_out_of_insight(signing, target):
    """The two-step hole: insight -> evidence used to need nothing, after which PUT/DELETE were ungated."""
    store = _FakeStore(["insight"])
    call, ledger = _handler(store)
    assert _status(call, target) == 403
    assert store.writes == [] and _intents(ledger) == []


def test_a_proven_consolidator_label_does_not_exempt_an_insight_demotion(signing, service_key):
    """No job label demotes an insight: only the operator's signed demote moves it out."""
    store = _FakeStore(["insight"])
    call, ledger = _handler(store)
    assert _status(call, "evidence", actor="dream-consolidator", service_key=service_key) == 403
    assert store.writes == []


def test_handler_accepts_a_signed_demotion_of_an_insight(signing):
    store = _FakeStore(["insight"])
    call, ledger = _handler(store)
    ts, nonce = _now(), str(uuid.uuid4())
    tok = _sign(signing, ts, nonce, "demote", "mid-1", "wrong insight")
    out = call("evidence", actor="user-direct", reason="wrong insight", token=tok, ts=ts, nonce=nonce)
    assert out["ok"] is True and store.writes[0][0]["tier"] == "evidence"


def test_handler_refuses_a_record_that_became_insight_mid_flight_with_409(signing):
    store = _FakeStore(["evidence", "insight"])
    call, ledger = _handler(store)
    with pytest.raises(fastapi.HTTPException) as e:
        call("stable")
    assert e.value.status_code == 409 and "became insight" in str(e.value.detail)
    assert store.writes == [] and _intents(ledger) == []


def test_promotion_into_insight_with_an_unproven_consolidator_label_is_refused(signing):
    store = _FakeStore(["evidence"])
    call, ledger = _handler(store)
    with pytest.raises(fastapi.HTTPException) as e:
        call("insight", actor="dream-consolidator")
    assert e.value.status_code == 403 and "service-credential-required" in str(e.value.detail)
    assert store.writes == []


def test_promotion_into_insight_with_a_proven_consolidator_label_lands(signing, service_key):
    store = _FakeStore(["evidence", "evidence"])
    call, ledger = _handler(store)
    out = call("insight", actor="dream-consolidator", service_key=service_key)
    assert out["ok"] is True and store.writes[0][0]["tier"] == "insight"


def test_a_wrong_service_key_is_no_proof(signing, service_key):
    store = _FakeStore(["evidence"])
    call, ledger = _handler(store)
    assert _status(call, "insight", actor="dream-consolidator", service_key="s" * 63 + "t") == 403
    assert store.writes == []
