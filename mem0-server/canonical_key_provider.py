"""v0.18 Phase A + v0.19 Phase H: canonical-key provider.

Single point of canonical-key access for security_invariants.py and any future
consumer. Three sources, strict precedence:

1. Runtime-injected key (v0.19 Phase H) — `$XDG_RUNTIME_DIR/mem0/canonical-key`,
   a tmpfs file written by `scripts/wsl/dpapi-fetch-key.sh` (ExecStartPre on
   mem0.service) which DPAPI-decrypts ~/.mem0/canonical-key.dpapi via WSL
   interop at service start. This is how the Linux server reads the key while
   the at-rest artifact stays encrypted. Branch disabled (path None) when
   XDG_RUNTIME_DIR is unset and no explicit path given (e.g. plain Windows).
2. ~/.mem0/canonical-key.dpapi — DPAPI blob, decryptable on Windows only.
3. ~/.mem0/canonical-key — plaintext mode-600 file (dev/recovery fallback;
   removed from the production WSL box at v0.19 Phase H cutover).

DPAPI threat model (per docs/modular/dpapi-canonical-key.md):
- Same-user processes on the same machine CAN decrypt — this is intentional;
  the v0.17 F.1.3 threat model documents single-user trust scope.
- DIFFERENT users on same machine cannot decrypt (user-scope flag).
- Offline disk reads (image acquisition, backup leak) cannot decrypt without
  the user's Windows credentials cache.
- The win vs plaintext mode-600 file: a backup that copies ~/.mem0/ to another
  machine, or a stolen unlocked disk, no longer leaks the canonical-key.
- The runtime tmpfs copy is RAM-backed (/run/user/<uid>), mode 600 inside a
  mode-700 RuntimeDirectory, and is removed by systemd when the service stops.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Optional


def _is_windows() -> bool:
    return platform.system() == "Windows" or os.name == "nt"


def dpapi_encrypt(plaintext: bytes, description: str = "agentic-memory-stack canonical-key v0.18") -> bytes:
    """DPAPI user-scope encrypt. Windows only. Raises on non-Windows."""
    if not _is_windows():
        raise RuntimeError("DPAPI is Windows-only")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    in_blob = DATA_BLOB(len(plaintext), ctypes.cast(ctypes.create_string_buffer(plaintext), ctypes.POINTER(ctypes.c_byte)))
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    desc_buf = ctypes.c_wchar_p(description)
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not crypt32.CryptProtectData(ctypes.byref(in_blob), desc_buf, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)):
        raise OSError(f"CryptProtectData failed: GetLastError={kernel32.GetLastError()}")
    try:
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return result
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_decrypt(blob: bytes) -> bytes:
    """DPAPI user-scope decrypt. Windows only. Raises on non-Windows or wrong-user."""
    if not _is_windows():
        raise RuntimeError("DPAPI is Windows-only")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    in_blob = DATA_BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_byte)))
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)):
        raise OSError(f"CryptUnprotectData failed: GetLastError={kernel32.GetLastError()}")
    try:
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return result
    finally:
        kernel32.LocalFree(out_blob.pbData)


class CanonicalKeyProvider:
    """Single point of canonical-key access.

    Order of preference:
    1. $XDG_RUNTIME_DIR/mem0/canonical-key (tmpfs, injected by ExecStartPre
       dpapi-fetch-key.sh — v0.19 Phase H; skipped when path unresolvable)
    2. ~/.mem0/canonical-key.dpapi (DPAPI-encrypted blob, Windows only)
    3. ~/.mem0/canonical-key (plaintext, mode 600 — dev/recovery fallback)

    None if none present. Key cached after first read."""

    def __init__(
        self,
        dpapi_path: Optional[Path] = None,
        plaintext_path: Optional[Path] = None,
        runtime_key_path: Optional[Path] = None,
    ):
        default_dir = Path.home() / ".mem0"
        self.dpapi_path = dpapi_path if dpapi_path is not None else default_dir / "canonical-key.dpapi"
        self.plaintext_path = plaintext_path if plaintext_path is not None else default_dir / "canonical-key"
        if runtime_key_path is not None:
            self.runtime_key_path: Optional[Path] = runtime_key_path
        else:
            xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
            # systemd sets XDG_RUNTIME_DIR for user services; without it (plain
            # Windows process, bare cron) the runtime branch is simply disabled.
            self.runtime_key_path = Path(xdg) / "mem0" / "canonical-key" if xdg else None
        # Path-traversal guard: paths must resolve under user home OR be under tmp (for tests)
        guarded = [self.dpapi_path, self.plaintext_path]
        if self.runtime_key_path is not None:
            guarded.append(self.runtime_key_path)
        for p in guarded:
            try:
                resolved = p.resolve(strict=False)
            except (OSError, RuntimeError) as e:
                raise ValueError(f"path resolve failed for {p}: {e}")
            home = Path.home().resolve()
            tmp_prefixes = [
                Path(os.environ.get("TEMP", "/tmp")).resolve(),
                Path(os.environ.get("TMP", "/tmp")).resolve(),
            ]
            # Also accept /tmp on Linux/WSL
            try:
                tmp_prefixes.append(Path("/tmp").resolve())
            except (OSError, RuntimeError):
                pass
            # Also accept C:\Users on Windows (covers all user-scoped test paths)
            try:
                users_dir = Path("C:/Users").resolve()
                tmp_prefixes.append(users_dir)
            except (OSError, RuntimeError):
                pass
            # v0.19 Phase H: accept the user runtime dir (tmpfs) — systemd's
            # XDG_RUNTIME_DIR and the conventional /run/user/<uid> location.
            xdg_guard = os.environ.get("XDG_RUNTIME_DIR", "").strip()
            if xdg_guard:
                try:
                    tmp_prefixes.append(Path(xdg_guard).resolve())
                except (OSError, RuntimeError):
                    pass
            try:
                tmp_prefixes.append(Path("/run/user").resolve())
            except (OSError, RuntimeError):
                pass
            tmp_prefixes = [t for t in tmp_prefixes if t is not None]
            under_home = str(resolved).startswith(str(home))
            under_tmp = any(str(resolved).startswith(str(t)) for t in tmp_prefixes)
            if not (under_home or under_tmp):
                raise ValueError(f"canonical-key path {resolved} outside user home, tmp, or runtime dir")
        self._cached_key: Optional[str] = None
        self._cache_loaded = False
        # v0.20 Phase D (M6): which source served the cached key —
        # 'runtime' | 'dpapi' | 'plaintext' | 'none'. Exposed via key_source
        # so /health/deep can report canonical_key.source.
        self._cached_source: str = "none"

    @property
    def key_source(self) -> str:
        """Source that served the key ('runtime'|'dpapi'|'plaintext'|'none').
        Triggers the first read if the cache is cold."""
        if not self._cache_loaded:
            self.get_key()
        return self._cached_source

    def get_key(self) -> Optional[str]:
        if self._cache_loaded:
            return self._cached_key
        # v0.19 Phase H: runtime-injected key (tmpfs, ExecStartPre DPAPI fetch)
        # wins over everything — it IS the decrypted DPAPI blob on the WSL box.
        if self.runtime_key_path is not None and self.runtime_key_path.exists():
            try:
                _rk = self.runtime_key_path.read_text(encoding="utf-8").strip()
                # v0.20 Phase D (L1): ''-is-absent — an empty/whitespace runtime
                # file (e.g. a truncated dpapi-fetch-key decode) must NOT be
                # served as a valid key ('' would mask the fallback chain AND
                # feed hmac.new(b'') downstream); treat as absent + WARN.
                if _rk:
                    self._cached_key = _rk
                    self._cached_source = "runtime"
                    self._cache_loaded = True
                    return self._cached_key
                import logging
                logging.getLogger(__name__).warning(
                    f"runtime canonical-key at {self.runtime_key_path} is empty/whitespace, "
                    "falling back to dpapi/plaintext"
                )
            except OSError as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"runtime canonical-key at {self.runtime_key_path} unreadable, "
                    f"falling back to dpapi/plaintext: {e}"
                )
        # Prefer DPAPI on Windows
        if _is_windows() and self.dpapi_path.exists():
            try:
                blob = self.dpapi_path.read_bytes()
                self._cached_key = dpapi_decrypt(blob).decode("utf-8").strip()
                self._cached_source = "dpapi"
                self._cache_loaded = True
                return self._cached_key
            except (OSError, RuntimeError) as e:
                import logging
                logging.getLogger(__name__).warning(f"DPAPI decrypt failed, falling back to plaintext: {e}")
        # v0.18 fix-pass HIGH (updated v0.19 Phase H): loud diagnostic for the
        # broken state — a DPAPI blob without a runtime-injected key and without
        # plaintext is unreadable on WSL/Linux (DPAPI is Windows-only), so
        # canonical/insight mutations will 503 until a key source is restored AND
        # the server restarted (get_key() caches the None below). Primary fix:
        # the runtime path should have been provisioned by dpapi-fetch-key.sh
        # (ExecStartPre on mem0.service) — check its journal output.
        if not _is_windows() and self.dpapi_path.exists() and not self.plaintext_path.exists():
            import logging
            logging.getLogger(__name__).error(
                "canonical-key.dpapi present but DPAPI is unavailable on this platform, "
                f"no runtime-injected key at {self.runtime_key_path} (dpapi-fetch-key.sh "
                "ExecStartPre failed? check `journalctl --user -u mem0`), and no "
                "plaintext key exists; recover by restarting mem0 (re-runs the fetch) "
                "or restore plaintext via [ProtectedData]::Unprotect from Windows"
            )
        # Plaintext fallback
        if self.plaintext_path.exists():
            _pk = self.plaintext_path.read_text(encoding="utf-8").strip()
            # v0.20 Phase D (L1): same ''-is-absent rule as the runtime branch.
            if _pk:
                self._cached_key = _pk
                self._cached_source = "plaintext"
                self._cache_loaded = True
                return self._cached_key
            import logging
            logging.getLogger(__name__).warning(
                f"plaintext canonical-key at {self.plaintext_path} is empty/whitespace, "
                "treating as absent"
            )
        self._cached_key = None
        self._cached_source = "none"
        self._cache_loaded = True
        return None


def canonical_key_health(provider: "CanonicalKeyProvider") -> dict:
    """v0.20 Phase D (M6): the canonical_key fragment for /health/deep.

    ok flips False ONLY in the keyless-degraded state — the key was provisioned
    (DPAPI blob on disk) but nothing loadable served it (dpapi-fetch-key.sh
    ExecStartPre failed and ExecStartPre=- swallowed it). A dev box with no key
    configured at all stays green: canonical promotions are simply disabled
    there by design, not broken."""
    key = provider.get_key()
    blob_present = provider.dpapi_path.exists()
    present = bool(key)
    return {
        "ok": present or not blob_present,
        "present": present,
        "source": provider.key_source,
        "dpapi_blob": blob_present,
    }
