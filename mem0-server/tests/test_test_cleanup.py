"""The live suites' cleanup helper signs what the server verifies and refuses to swallow a failure.

Headless: the HTTP layer is a fake, and the signature is checked against the server's own
validator (security_invariants.validate_hmac_user_direct), not against a re-implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # security_invariants lives in mem0-server/

import _test_cleanup as tc  # noqa: E402

KEY = "unit-test-canonical-key"
MID = "11111111-2222-3333-4444-555555555555"
BASE = {"X-API-Key": "k"}


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class _Http:
    """A fake httpx module: records calls, answers DELETE/GET from the given statuses."""

    def __init__(self, delete_status=200, get_status=404):
        self.delete_status, self.get_status, self.calls = delete_status, get_status, []

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url, kw))
        return _Resp(self.delete_status, "refused" if self.delete_status >= 400 else "")

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return _Resp(self.get_status)


def test_signed_delete_headers_verify_against_the_servers_own_validator(monkeypatch):
    si = pytest.importorskip("security_invariants")
    monkeypatch.setattr(si, "_get_canonical_key", lambda: KEY)
    monkeypatch.setattr(si, "_check_and_record_nonce", lambda nonce, ts: True)
    h = tc.sign_delete_headers(KEY, MID, "unit cleanup")
    assert set(h) == {"X-User-Direct-Token", "X-User-Direct-Ts", "X-User-Direct-Nonce"}
    # returns None on success; raises HTTPException(403) on a missing nonce or a wrong payload
    si.validate_hmac_user_direct(MID, "delete", "unit cleanup", h["X-User-Direct-Token"],
                                 h["X-User-Direct-Ts"], x_user_direct_nonce=h["X-User-Direct-Nonce"])
    with pytest.raises(Exception, match="mismatch|nonce"):
        si.validate_hmac_user_direct(MID, "delete", "another reason", h["X-User-Direct-Token"],
                                     h["X-User-Direct-Ts"], x_user_direct_nonce=h["X-User-Direct-Nonce"])


def test_delete_memory_signs_the_request_and_reads_the_point_back():
    http = _Http()
    tc.delete_memory("http://x", BASE, MID, canonical_key=KEY, reason="r", http=http)
    (verb1, url1, kw1), (verb2, url2, _kw2) = http.calls
    assert (verb1, url1) == ("DELETE", f"http://x/v1/memories/{MID}")
    assert kw1["params"] == {"actor": "user-direct", "reason": "r"}
    assert kw1["headers"]["X-User-Direct-Nonce"] and kw1["headers"]["X-API-Key"] == "k"
    assert (verb2, url2) == ("GET", f"http://x/v1/memories/{MID}")


def test_delete_memory_without_a_key_sends_no_token():
    http = _Http()
    tc.delete_memory("http://x", BASE, MID, http=http)
    assert "X-User-Direct-Token" not in http.calls[0][2]["headers"]


@pytest.mark.parametrize("status", [403, 500])
def test_a_refused_delete_fails_the_test_that_leaked(status):
    with pytest.raises(AssertionError, match="may still be in the store"):
        tc.delete_memory("http://x", BASE, MID, canonical_key=KEY, http=_Http(delete_status=status))


def test_a_delete_that_left_the_point_readable_fails():
    with pytest.raises(AssertionError, match="still readable"):
        tc.delete_memory("http://x", BASE, MID, canonical_key=KEY, http=_Http(get_status=200))


def test_a_point_the_test_already_removed_is_not_a_failure():
    tc.delete_memory("http://x", BASE, MID, canonical_key=KEY, http=_Http(delete_status=404, get_status=404))


def test_a_404_delete_of_a_point_that_is_still_there_fails():
    with pytest.raises(AssertionError, match="still readable"):
        tc.delete_memory("http://x", BASE, MID, canonical_key=KEY, http=_Http(delete_status=404, get_status=200))
