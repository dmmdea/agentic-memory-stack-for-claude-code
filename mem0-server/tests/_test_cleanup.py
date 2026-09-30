"""_test_cleanup.py - delete what a live-stack test seeded, and prove it is gone.

A cleanup that swallows its own status leaves debris and says nothing: 79 canonical test points
and dozens of insight points sat in a production store because the delete a test sent was refused
(403) and nobody looked. Two defects made that happen, and both are closed here:

  * canonical and insight records refuse a plain DELETE, so the delete must be HMAC-signed with
    the same format-2 payload the server verifies (<ts>|<nonce>|delete|<id>|<reason>, nonce
    header REQUIRED since v0.18 MED-7 - a token without a nonce is a 403 that looks like "best
    effort" to a caller who swallows it);
  * the result was never checked. `delete_memory` asserts the delete took and then reads the point back and
    asserts it is absent (404), so a refused or ineffective delete fails the test that leaked.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import uuid
from typing import Mapping, Optional


def sign_delete_headers(key: str, memory_id: str, reason: str) -> dict[str, str]:
    """X-User-Direct-Token/-Ts/-Nonce for DELETE /v1/memories/{id} (format-2, action=delete)."""
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = str(uuid.uuid4())
    msg = f"{ts}|{nonce}|delete|{memory_id}|{reason}".encode("utf-8")
    token = base64.b64encode(hmac.new(key.encode("utf-8"), msg, hashlib.sha256).digest()).decode("ascii")
    return {"X-User-Direct-Token": token.strip(), "X-User-Direct-Ts": ts, "X-User-Direct-Nonce": nonce}


def delete_memory(
    url: str,
    headers: Mapping[str, str],
    memory_id: str,
    *,
    canonical_key: Optional[str] = None,
    reason: str = "test cleanup",
    http=None,
) -> None:
    """Delete one seeded memory through the HMAC path (when a key is given) and assert it worked.

    Raises AssertionError when the delete is refused (anything but 2xx, or 404 for a point the test
    already removed) or the point can still be read back afterwards.
    Without a key only a plain delete is possible, which the server refuses for canonical and
    insight records - the assertion then names that instead of leaving the point behind quietly.
    """
    if http is None:
        import httpx as http  # the suites' own client; injectable so the helper is unit-testable
    hdrs = dict(headers)
    if canonical_key:
        hdrs.update(sign_delete_headers(canonical_key, memory_id, reason))
        params = {"actor": "user-direct", "reason": reason}
    else:
        params = {"actor": "test-cleanup", "reason": reason}
    r = http.delete(f"{url}/v1/memories/{memory_id}", params=params, headers=hdrs, timeout=15)
    # 404 is fine when the test already removed the point itself; the read-back below decides.
    assert 200 <= r.status_code < 300 or r.status_code == 404, (
        f"cleanup DELETE of {memory_id} failed ({r.status_code}): {r.text[:200]} - the seeded "
        f"point may still be in the store"
    )
    g = http.get(f"{url}/v1/memories/{memory_id}", headers=dict(headers), timeout=15)
    assert g.status_code == 404, (
        f"cleanup DELETE of {memory_id} returned {r.status_code} but the point is still readable "
        f"({g.status_code})"
    )
