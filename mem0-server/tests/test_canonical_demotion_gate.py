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
    ("insight", "evidence", None),
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
    env = {"HOME": str(home), "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
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
