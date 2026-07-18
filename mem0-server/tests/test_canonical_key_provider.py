"""v0.18 Phase A: canonical_key_provider tests (DPAPI + plaintext fallback)."""
from __future__ import annotations

import os
import pytest
from pathlib import Path
from unittest.mock import patch


def test_dpapi_roundtrip_user_scope(tmp_path, monkeypatch):
    """DPAPI encrypt → decrypt yields the original key for the same user."""
    from canonical_key_provider import dpapi_encrypt, dpapi_decrypt, _is_windows
    if not _is_windows():
        pytest.skip("DPAPI is Windows-only")
    plaintext = b"v018-test-canonical-key-32bytes!"
    blob = dpapi_encrypt(plaintext)
    assert blob != plaintext, "encrypted blob must differ from plaintext"
    recovered = dpapi_decrypt(blob)
    assert recovered == plaintext


def test_provider_reads_dpapi_blob_when_present(tmp_path, monkeypatch):
    """When ~/.mem0/canonical-key.dpapi exists, provider returns decrypted contents."""
    from canonical_key_provider import CanonicalKeyProvider, _is_windows
    if not _is_windows():
        pytest.skip("DPAPI is Windows-only; provider falls back to plaintext on WSL")
    dpapi_path = tmp_path / "canonical-key.dpapi"
    plaintext_path = tmp_path / "canonical-key"
    from canonical_key_provider import dpapi_encrypt
    dpapi_path.write_bytes(dpapi_encrypt(b"dpapi-preferred"))
    plaintext_path.write_text("plaintext-not-preferred")
    provider = CanonicalKeyProvider(dpapi_path=dpapi_path, plaintext_path=plaintext_path,
                                   runtime_key_path=tmp_path / "absent-runtime-key")
    assert provider.get_key() == "dpapi-preferred"


def test_provider_falls_back_to_plaintext(tmp_path):
    """When DPAPI blob absent, plaintext file is used (WSL/dev path)."""
    from canonical_key_provider import CanonicalKeyProvider
    dpapi_path = tmp_path / "canonical-key.dpapi"  # missing
    plaintext_path = tmp_path / "canonical-key"
    plaintext_path.write_text("plaintext-fallback-key")
    provider = CanonicalKeyProvider(dpapi_path=dpapi_path, plaintext_path=plaintext_path,
                                   runtime_key_path=tmp_path / "absent-runtime-key")
    assert provider.get_key() == "plaintext-fallback-key"


def test_provider_returns_none_when_neither_present(tmp_path):
    """Missing both → None (server runs but canonical writes are 503'd)."""
    from canonical_key_provider import CanonicalKeyProvider
    provider = CanonicalKeyProvider(
        dpapi_path=tmp_path / "canonical-key.dpapi",
        plaintext_path=tmp_path / "canonical-key",
        runtime_key_path=tmp_path / "absent-runtime-key",
    )
    assert provider.get_key() is None


def test_provider_caches_key_across_calls(tmp_path):
    """Same provider instance returns identical bytes without re-reading disk."""
    from canonical_key_provider import CanonicalKeyProvider
    plaintext_path = tmp_path / "canonical-key"
    plaintext_path.write_text("cache-test-key")
    provider = CanonicalKeyProvider(
        dpapi_path=tmp_path / "canonical-key.dpapi",
        plaintext_path=plaintext_path,
        runtime_key_path=tmp_path / "absent-runtime-key",
    )
    k1 = provider.get_key()
    plaintext_path.write_text("changed-on-disk")
    k2 = provider.get_key()
    assert k1 == k2 == "cache-test-key"


def test_dpapi_blob_without_plaintext_on_non_windows_logs_error(tmp_path, caplog):
    """v0.18 fix-pass HIGH: WSL-runnable regression for the broken post-migration
    state — a DPAPI blob with NO plaintext key on a non-Windows host. The provider
    must return None (it can never decrypt DPAPI off-Windows) and log a loud error
    pointing at the recovery path, instead of silently 503ing canonical writes."""
    import logging
    from canonical_key_provider import CanonicalKeyProvider

    dpapi_path = tmp_path / "canonical-key.dpapi"
    dpapi_path.write_bytes(b"\x01\x02\x03not-a-real-dpapi-blob")
    plaintext_path = tmp_path / "canonical-key"  # intentionally missing

    provider = CanonicalKeyProvider(dpapi_path=dpapi_path, plaintext_path=plaintext_path,
                                   runtime_key_path=tmp_path / "absent-runtime-key")
    with patch("canonical_key_provider._is_windows", return_value=False):
        with caplog.at_level(logging.ERROR, logger="canonical_key_provider"):
            assert provider.get_key() is None
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(
        "canonical-key.dpapi present but DPAPI is unavailable" in r.getMessage()
        for r in error_records
    ), f"expected loud DPAPI-orphan error, got: {[r.getMessage() for r in caplog.records]}"


def test_provider_path_not_traversal(tmp_path):
    """Provider rejects a key path outside user home / tmp / runtime dir."""
    from canonical_key_provider import CanonicalKeyProvider
    # An unambiguous ABSOLUTE outside-home path. The old relative fixture
    # ("../../etc/passwd") was cwd-dependent: with pytest's cwd inside home
    # (every CI checkout under /home/runner) it resolved UNDER home, where the
    # guard rightly allows it — the test failed only on such runners.
    outside = Path("C:/Windows/System32/config") if os.name == "nt" else Path("/etc/passwd")
    with pytest.raises((ValueError, OSError)):
        CanonicalKeyProvider(
            dpapi_path=outside,
            plaintext_path=tmp_path / "canonical-key",
        )


# --- v0.19 Phase H: runtime-injected key (tmpfs, ExecStartPre DPAPI fetch) ---


def test_runtime_key_beats_plaintext(tmp_path):
    """Precedence: runtime-injected key (tmpfs) wins over plaintext."""
    from canonical_key_provider import CanonicalKeyProvider
    runtime_path = tmp_path / "run" / "mem0" / "canonical-key"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text("runtime-injected-key\n")
    plaintext_path = tmp_path / "canonical-key"
    plaintext_path.write_text("plaintext-should-lose")
    provider = CanonicalKeyProvider(
        runtime_key_path=runtime_path,
        dpapi_path=tmp_path / "canonical-key.dpapi",
        plaintext_path=plaintext_path,
    )
    assert provider.get_key() == "runtime-injected-key"


def test_runtime_key_beats_dpapi_on_windows(tmp_path):
    """Precedence: runtime-injected key wins over the DPAPI blob even on Windows."""
    from canonical_key_provider import CanonicalKeyProvider, _is_windows
    if not _is_windows():
        pytest.skip("DPAPI branch is Windows-only")
    from canonical_key_provider import dpapi_encrypt
    runtime_path = tmp_path / "run" / "mem0" / "canonical-key"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text("runtime-injected-key")
    dpapi_path = tmp_path / "canonical-key.dpapi"
    dpapi_path.write_bytes(dpapi_encrypt(b"dpapi-should-lose"))
    provider = CanonicalKeyProvider(
        runtime_key_path=runtime_path,
        dpapi_path=dpapi_path,
        plaintext_path=tmp_path / "canonical-key",
    )
    assert provider.get_key() == "runtime-injected-key"


def test_runtime_key_missing_falls_through_to_plaintext(tmp_path):
    """Runtime path absent → existing v0.18 chain unchanged (plaintext on WSL)."""
    from canonical_key_provider import CanonicalKeyProvider
    plaintext_path = tmp_path / "canonical-key"
    plaintext_path.write_text("plaintext-fallback-key")
    provider = CanonicalKeyProvider(
        runtime_key_path=tmp_path / "run" / "mem0" / "canonical-key",  # missing
        dpapi_path=tmp_path / "canonical-key.dpapi",  # missing
        plaintext_path=plaintext_path,
    )
    assert provider.get_key() == "plaintext-fallback-key"


def test_runtime_key_full_precedence_none_when_all_absent(tmp_path):
    """All three sources absent → None (canonical mutations 503)."""
    from canonical_key_provider import CanonicalKeyProvider
    provider = CanonicalKeyProvider(
        runtime_key_path=tmp_path / "run" / "mem0" / "canonical-key",
        dpapi_path=tmp_path / "canonical-key.dpapi",
        plaintext_path=tmp_path / "canonical-key",
    )
    assert provider.get_key() is None


def test_runtime_default_path_from_xdg_runtime_dir(tmp_path, monkeypatch):
    """Default runtime path is $XDG_RUNTIME_DIR/mem0/canonical-key (systemd sets
    XDG_RUNTIME_DIR for user services; RuntimeDirectory=mem0 creates the dir)."""
    from canonical_key_provider import CanonicalKeyProvider
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdgrun"))
    key_path = tmp_path / "xdgrun" / "mem0" / "canonical-key"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("xdg-default-runtime-key")
    provider = CanonicalKeyProvider(
        dpapi_path=tmp_path / "canonical-key.dpapi",
        plaintext_path=tmp_path / "canonical-key",
    )
    assert provider.runtime_key_path == key_path
    assert provider.get_key() == "xdg-default-runtime-key"


def test_runtime_default_none_without_xdg(tmp_path, monkeypatch):
    """No XDG_RUNTIME_DIR (e.g. plain Windows process) → runtime branch disabled,
    chain falls through exactly as v0.18."""
    from canonical_key_provider import CanonicalKeyProvider
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    plaintext_path = tmp_path / "canonical-key"
    plaintext_path.write_text("plaintext-key")
    provider = CanonicalKeyProvider(
        dpapi_path=tmp_path / "canonical-key.dpapi",
        plaintext_path=plaintext_path,
    )
    assert provider.runtime_key_path is None
    assert provider.get_key() == "plaintext-key"


# --- v0.20 Phase D (L1): ''-is-absent semantics for runtime/plaintext keys ---


def test_empty_runtime_key_falls_through_to_plaintext(tmp_path, caplog):
    """L1: an empty runtime file must be treated as ABSENT (WARN + fall through),
    not served as '' — '' would mask the fallback chain and feed hmac.new(b'')."""
    import logging
    from canonical_key_provider import CanonicalKeyProvider
    runtime_path = tmp_path / "run" / "mem0" / "canonical-key"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text("")  # empty — dpapi-fetch-key.sh decode bug artifact
    plaintext_path = tmp_path / "canonical-key"
    plaintext_path.write_text("plaintext-fallback-key")
    provider = CanonicalKeyProvider(
        runtime_key_path=runtime_path,
        dpapi_path=tmp_path / "canonical-key.dpapi",
        plaintext_path=plaintext_path,
    )
    with caplog.at_level(logging.WARNING, logger="canonical_key_provider"):
        assert provider.get_key() == "plaintext-fallback-key"
    assert any("empty/whitespace" in r.getMessage() for r in caplog.records)


def test_whitespace_runtime_key_alone_is_none(tmp_path):
    """L1: whitespace-only runtime file with no other source → None (degraded
    503), never '' (which would 403-storm with an empty HMAC key)."""
    from canonical_key_provider import CanonicalKeyProvider
    runtime_path = tmp_path / "run" / "mem0" / "canonical-key"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text(" \n\t\n")
    provider = CanonicalKeyProvider(
        runtime_key_path=runtime_path,
        dpapi_path=tmp_path / "canonical-key.dpapi",
        plaintext_path=tmp_path / "canonical-key",
    )
    assert provider.get_key() is None


def test_empty_plaintext_key_treated_as_absent(tmp_path, caplog):
    """L1 consistency: the plaintext branch gets the same ''-is-absent rule."""
    import logging
    from canonical_key_provider import CanonicalKeyProvider
    plaintext_path = tmp_path / "canonical-key"
    plaintext_path.write_text("\n")
    provider = CanonicalKeyProvider(
        runtime_key_path=tmp_path / "absent-runtime-key",
        dpapi_path=tmp_path / "canonical-key.dpapi",
        plaintext_path=plaintext_path,
    )
    with caplog.at_level(logging.WARNING, logger="canonical_key_provider"):
        assert provider.get_key() is None
    assert any("empty/whitespace" in r.getMessage() for r in caplog.records)


# --- v0.20 Phase D (L13): mocked-Windows tests so the WSL release gate ---
# --- exercises the runtime>dpapi precedence and dpapi-decrypt branches ---


def test_runtime_key_beats_dpapi_mocked_windows(tmp_path):
    """L13: runtime>dpapi precedence cell, runnable on the WSL gate (the real
    DPAPI variant above stays Windows-only)."""
    from canonical_key_provider import CanonicalKeyProvider
    runtime_path = tmp_path / "run" / "mem0" / "canonical-key"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text("runtime-injected-key")
    dpapi_path = tmp_path / "canonical-key.dpapi"
    dpapi_path.write_bytes(b"\x01fake-blob")
    provider = CanonicalKeyProvider(runtime_key_path=runtime_path, dpapi_path=dpapi_path,
                                    plaintext_path=tmp_path / "canonical-key")
    with patch("canonical_key_provider._is_windows", return_value=True), \
         patch("canonical_key_provider.dpapi_decrypt", return_value=b"dpapi-should-lose"):
        assert provider.get_key() == "runtime-injected-key"


def test_dpapi_branch_mocked_windows(tmp_path):
    """L13: the dpapi-decrypt branch (blob read → decrypt → strip), runnable on
    the WSL gate via mocked _is_windows + dpapi_decrypt."""
    from canonical_key_provider import CanonicalKeyProvider
    dpapi_path = tmp_path / "canonical-key.dpapi"
    dpapi_path.write_bytes(b"\x01fake-blob")
    provider = CanonicalKeyProvider(dpapi_path=dpapi_path,
                                    plaintext_path=tmp_path / "canonical-key",
                                    runtime_key_path=tmp_path / "absent-runtime-key")
    with patch("canonical_key_provider._is_windows", return_value=True), \
         patch("canonical_key_provider.dpapi_decrypt", return_value=b"dpapi-key\n") as dec:
        assert provider.get_key() == "dpapi-key"
        dec.assert_called_once_with(b"\x01fake-blob")
    assert provider.key_source == "dpapi"


def test_runtime_key_unreadable_falls_back_with_warning(tmp_path, caplog):
    """L13: unreadable runtime key (directory-as-keypath: exists() True,
    read_text() raises OSError subclass, cross-platform) → WARN + plaintext."""
    import logging
    from canonical_key_provider import CanonicalKeyProvider
    runtime_path = tmp_path / "run" / "mem0" / "canonical-key"
    runtime_path.mkdir(parents=True)  # exists() True, read_text() raises OSError
    plaintext_path = tmp_path / "canonical-key"
    plaintext_path.write_text("plaintext-fallback-key")
    provider = CanonicalKeyProvider(runtime_key_path=runtime_path,
                                    dpapi_path=tmp_path / "canonical-key.dpapi",
                                    plaintext_path=plaintext_path)
    with caplog.at_level(logging.WARNING, logger="canonical_key_provider"):
        assert provider.get_key() == "plaintext-fallback-key"
    assert any("unreadable" in r.getMessage() for r in caplog.records)


# --- v0.20 Phase D (M6): key_source tracking + canonical_key health fragment ---


def test_key_source_tracks_serving_branch(tmp_path):
    """M6: provider exposes WHICH source served the key (key_source), so
    /health/deep can report canonical_key.source without re-deriving it."""
    from canonical_key_provider import CanonicalKeyProvider
    # runtime
    runtime_path = tmp_path / "run" / "mem0" / "canonical-key"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text("rk")
    p1 = CanonicalKeyProvider(runtime_key_path=runtime_path,
                              dpapi_path=tmp_path / "canonical-key.dpapi",
                              plaintext_path=tmp_path / "canonical-key")
    assert p1.key_source == "runtime"  # property triggers the first read
    # plaintext
    plaintext_path = tmp_path / "canonical-key"
    plaintext_path.write_text("pk")
    p2 = CanonicalKeyProvider(runtime_key_path=tmp_path / "absent-rk",
                              dpapi_path=tmp_path / "canonical-key.dpapi",
                              plaintext_path=plaintext_path)
    assert p2.get_key() == "pk"
    assert p2.key_source == "plaintext"
    # none
    p3 = CanonicalKeyProvider(runtime_key_path=tmp_path / "absent-rk",
                              dpapi_path=tmp_path / "absent.dpapi",
                              plaintext_path=tmp_path / "absent-pk")
    assert p3.get_key() is None
    assert p3.key_source == "none"


def test_canonical_key_health_keyless_with_blob_flips_not_ok(tmp_path):
    """M6: keyless-degraded (dpapi blob present, no key loadable — the
    ExecStartPre=- swallowed-failure state) must report ok=False so
    /health/deep flips ok=false and Test-MemoryStack FAILs INVARIANTS."""
    from canonical_key_provider import CanonicalKeyProvider, canonical_key_health
    dpapi_path = tmp_path / "canonical-key.dpapi"
    dpapi_path.write_bytes(b"\x01fake-blob")
    provider = CanonicalKeyProvider(runtime_key_path=tmp_path / "absent-rk",
                                    dpapi_path=dpapi_path,
                                    plaintext_path=tmp_path / "canonical-key")
    with patch("canonical_key_provider._is_windows", return_value=False):
        h = canonical_key_health(provider)
    assert h == {"ok": False, "present": False, "source": "none", "dpapi_blob": True}


def test_canonical_key_health_devbox_without_blob_stays_ok(tmp_path):
    """M6: a dev box with NO key configured at all (no blob either) stays
    green — keylessness is only a failure when the key was provisioned."""
    from canonical_key_provider import CanonicalKeyProvider, canonical_key_health
    provider = CanonicalKeyProvider(runtime_key_path=tmp_path / "absent-rk",
                                    dpapi_path=tmp_path / "absent.dpapi",
                                    plaintext_path=tmp_path / "absent-pk")
    h = canonical_key_health(provider)
    assert h == {"ok": True, "present": False, "source": "none", "dpapi_blob": False}


def test_canonical_key_health_runtime_key_ok(tmp_path):
    """M6: healthy production shape — runtime-injected key present."""
    from canonical_key_provider import CanonicalKeyProvider, canonical_key_health
    runtime_path = tmp_path / "run" / "mem0" / "canonical-key"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text("runtime-injected-key")
    dpapi_path = tmp_path / "canonical-key.dpapi"
    dpapi_path.write_bytes(b"\x01fake-blob")
    provider = CanonicalKeyProvider(runtime_key_path=runtime_path,
                                    dpapi_path=dpapi_path,
                                    plaintext_path=tmp_path / "canonical-key")
    h = canonical_key_health(provider)
    assert h == {"ok": True, "present": True, "source": "runtime", "dpapi_blob": True}


def test_dpapi_orphan_error_mentions_runtime_path(tmp_path, caplog):
    """v0.19 Phase H: the dpapi-present+no-plaintext diagnostic must point at the
    runtime-injection path (ExecStartPre dpapi-fetch-key.sh) as the primary fix."""
    import logging
    from canonical_key_provider import CanonicalKeyProvider

    dpapi_path = tmp_path / "canonical-key.dpapi"
    dpapi_path.write_bytes(b"\x01\x02\x03not-a-real-dpapi-blob")
    provider = CanonicalKeyProvider(
        runtime_key_path=tmp_path / "run" / "mem0" / "canonical-key",  # missing
        dpapi_path=dpapi_path,
        plaintext_path=tmp_path / "canonical-key",  # missing
    )
    with patch("canonical_key_provider._is_windows", return_value=False):
        with caplog.at_level(logging.ERROR, logger="canonical_key_provider"):
            assert provider.get_key() is None
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(
        "dpapi-fetch-key" in r.getMessage() and "runtime" in r.getMessage()
        for r in error_records
    ), f"expected runtime-path guidance in error, got: {[r.getMessage() for r in caplog.records]}"
