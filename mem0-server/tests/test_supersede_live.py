"""Live: the supersede door (1.32.4) against a running server.

Not in the CI list: it needs the stack. Run on the authority after a deploy:
  cd mem0-server && MEM0_KEY=$(cat ~/.mem0/api-key) MEM0_URL=<authority url> \
    AMS_ALLOW_LIVE_PROD_TESTS=1 <venv>/python -m pytest -q tests/test_supersede_live.py
Every record it creates carries user_id test-supersede and is deleted in a finally block.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac as _hmac
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx
import pytest

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ.get("MEM0_KEY") or (Path.home() / ".mem0" / "api-key").read_text().strip()
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
USER = "test-supersede"

from canonical_key_provider import CanonicalKeyProvider  # noqa: E402
from _test_cleanup import delete_memory  # noqa: E402

CANONICAL_KEY: Optional[str] = CanonicalKeyProvider().get_key()


def _add(text: str, user: str = USER, **md_extra) -> str:
    md = {"tier": "evidence", "source": "test-supersede", "user_id": user}
    md.update(md_extra)
    r = httpx.post(f"{URL}/v1/memories", headers=H, timeout=30,
                   json={"messages": text, "user_id": user, "infer": False, "metadata": md})
    r.raise_for_status()
    return r.json()["results"][0]["id"]


def _get(mid: str) -> dict:
    r = httpx.get(f"{URL}/v1/memories/{mid}", headers=H, timeout=15)
    r.raise_for_status()
    return r.json()


def _meta(mid: str) -> dict:
    body = _get(mid)
    return body.get("metadata") or body


def _supersede(mid: str, winner: str, **kw):
    return httpx.post(f"{URL}/v1/memories/{mid}/supersede", headers=H, timeout=15,
                      json={"winner_id": winner, "source": "test-supersede-live", **kw})


def _ids(query: str, query_class: str) -> set:
    r = httpx.post(f"{URL}/v1/memories/search", headers=H, timeout=120, json={
        "query": query, "filters": {"user_id": USER}, "limit": 10, "threshold": 0.0,
        "rerank": False, "query_class": query_class})
    r.raise_for_status()
    body = r.json()
    rows = body.get("results", body) if isinstance(body, dict) else body
    return {row.get("id") for row in rows}


def _promote(mid: str) -> None:
    if CANONICAL_KEY is None:
        pytest.skip("canonical key not configured on this box")
    ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    nonce = str(uuid.uuid4())
    msg = f"{ts}|{nonce}|promote|{mid}|test-supersede setup".encode()
    token = base64.b64encode(_hmac.new(CANONICAL_KEY.encode(), msg, hashlib.sha256).digest()).decode()
    r = httpx.patch(f"{URL}/v1/memories/{mid}/tier", timeout=15,
                    json={"tier": "canonical", "actor": "user-direct", "reason": "test-supersede setup"},
                    headers={**H, "X-User-Direct-Token": token, "X-User-Direct-Ts": ts,
                             "X-User-Direct-Nonce": nonce})
    r.raise_for_status()


@pytest.fixture
def made():
    ids: list = []
    yield ids
    for mid in ids:
        delete_memory(URL, H, mid, canonical_key=CANONICAL_KEY, reason="test-supersede cleanup")


def test_full_supersession_hides_the_loser_until_cleared(made):
    tag = uuid.uuid4().hex[:10]
    loser = _add(f"supersede-live {tag}: the fleet node shares the desktop console")
    winner = _add(f"supersede-live {tag}: the fleet node runs detached with its own console")
    made += [loser, winner]
    query = f"supersede-live {tag} fleet node console"
    assert loser in _ids(query, "durable")

    r = _supersede(loser, winner, reason="live test")
    assert r.status_code == 200, r.text
    assert r.json()["hidden"] is True and r.json()["noop"] is False
    meta = _meta(loser)
    assert meta.get("superseded_by") == winner and meta.get("superseded_at")
    assert loser not in _ids(query, "durable")
    assert loser in _ids(query, "history")

    again = _supersede(loser, winner)
    assert again.status_code == 200 and again.json()["noop"] is True

    cleared = httpx.delete(f"{URL}/v1/memories/{loser}/supersede", headers=H, timeout=15,
                           params={"scope": "full", "reason": "live test"})
    assert cleared.status_code == 200 and cleared.json()["noop"] is False, cleared.text
    assert not _meta(loser).get("superseded_by")
    assert loser in _ids(query, "durable")


def test_partial_supersession_annotates_and_stays_visible(made):
    tag = uuid.uuid4().hex[:10]
    loser = _add(f"supersede-live {tag}: the release is 1.20.4 and the store has 7 steps")
    winner = _add(f"supersede-live {tag}: the release is 1.32.4")
    made += [loser, winner]
    r = _supersede(loser, winner, scope="partial", detail="the release figure")
    assert r.status_code == 200 and r.json()["hidden"] is False, r.text
    entries = _meta(loser).get("partially_superseded_by")
    assert entries and entries[-1]["winner_id"] == winner
    assert loser in _ids(f"supersede-live {tag} release store steps", "durable")


def test_refusals(made):
    tag = uuid.uuid4().hex[:10]
    a = _add(f"supersede-live {tag}: a")
    b = _add(f"supersede-live {tag}: b")
    other = _add(f"supersede-live {tag}: other tenant", user="test-supersede-other")
    made += [a, b, other]
    assert _supersede(a, b, scope="partial").status_code == 400            # detail required
    assert _supersede(a, "not-an-id").status_code == 400
    assert _supersede(a, a).status_code == 400                             # self
    assert _supersede(a, other).status_code == 403                         # cross-tenant
    assert _supersede(a, str(uuid.uuid4())).status_code == 404            # winner missing
    assert _supersede(b, a).status_code == 200
    assert _supersede(str(uuid.uuid4()), b).status_code in (404, 409)     # loser missing / winner superseded
    c = _add(f"supersede-live {tag}: c")
    made.append(c)
    assert _supersede(c, b).status_code == 409                             # winner b is superseded


def test_a_canonical_record_is_refused_by_the_door_and_by_patch(made):
    tag = uuid.uuid4().hex[:10]
    canon = _add(f"supersede-live {tag}: canonical")
    winner = _add(f"supersede-live {tag}: newer")
    made += [canon, winner]
    _promote(canon)
    assert _supersede(canon, winner).status_code == 403
    for actor, key, value in (("supersession-resolve-v030", "superseded_by", winner),
                              ("contradiction-sweep-v019", "contradicts_canonical", winner)):
        r = httpx.patch(f"{URL}/v1/memories/{canon}/metadata", headers=H, timeout=15,
                        json={"metadata": {key: value}, "actor": actor, "reason": "F1 probe"})
        assert r.status_code == 403, (actor, r.status_code, r.text)
    meta = _meta(canon)
    assert not meta.get("superseded_by") and not meta.get("contradicts_canonical")


def test_the_old_resolve_actor_string_cannot_hide_an_evidence_record(made):
    tag = uuid.uuid4().hex[:10]
    a = _add(f"supersede-live {tag}: a")
    b = _add(f"supersede-live {tag}: b")
    made += [a, b]
    r = httpx.patch(f"{URL}/v1/memories/{a}/metadata", headers=H, timeout=15,
                    json={"metadata": {"superseded_by": b}, "actor": "supersession-resolve-v030",
                          "reason": "F1 probe"})
    assert r.status_code == 403, r.text
    assert not _meta(a).get("superseded_by")


def test_a_hand_written_marker_gets_a_note(made):
    tag = uuid.uuid4().hex[:10]
    a = _add(f"supersede-live {tag}: old fact")
    b = _add(f"supersede-live {tag}: new fact")
    made += [a, b]
    r = httpx.put(f"{URL}/v1/memories/{a}", headers=H, timeout=30,
                  json={"text": f"supersede-live {tag}: old fact\n\nSUPERSEDED 2026-10-01 by mem0 {b}: new."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("supersede_marker", {}).get("kind") == "full"
    assert "memory_supersede" in body.get("supersede_note", "")
    assert not _meta(a).get("superseded_by"), "a marker never hides by itself"


def test_a_superseded_record_is_never_promoted_into_canonical(made):
    """Security review, 1.32.4: a superseded canonical would be hidden and reachable by an unsigned
    cascade delete through its supersession link, so the promotion is refused until it is cleared."""
    tag = uuid.uuid4().hex[:10]
    old = _add(f"supersede-live {tag}: old")
    new = _add(f"supersede-live {tag}: new")
    made += [old, new]
    assert _supersede(old, new).status_code == 200
    if CANONICAL_KEY is None:
        pytest.skip("canonical key not configured on this box")
    with pytest.raises(httpx.HTTPStatusError) as e:
        _promote(old)
    assert e.value.response.status_code == 409 and "superseded-record" in e.value.response.text
    assert _meta(old).get("tier") == "evidence"
