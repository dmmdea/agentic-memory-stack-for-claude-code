"""v0.20 Phase D (L7+L8): first automated tests for scripts/wsl/dpapi-fetch-key.sh.

The script had zero automated coverage and an unchecked `base64 -d` pipeline
that could install a truncated/empty key (which the provider then served as
'' pre-L1 — a 403 storm instead of the documented 503-degraded state).

Strategy: run the real script in a subprocess with a STUBBED powershell.exe
(the script's only Windows dependency) pointed at by the DPAPI_FETCH_PS env
seam, exercising success / garbage-stdout / empty-stdout end-to-end:
  - success    → key installed atomically (mode 600), exit 0, 'provisioned' log
  - garbage    → decode fails, falls into the retry loop, exit 1, NO key, no tmp debris
  - empty      → retry loop, exit 1, NO key
  - no blob    → immediate FATAL exit 1

Env knobs used (all pre-existing except DPAPI_FETCH_PS, added as the minimal
test seam): MEM0_DPAPI_BLOB, RUNTIME_DIRECTORY, DPAPI_FETCH_RETRIES,
DPAPI_FETCH_SLEEP, DPAPI_FETCH_PS.

Also covers the M9 generate-canonical-key.sh blob guard behaviorally (HOME=tmp).
"""
from __future__ import annotations

import base64
import os
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="bash subprocess tests run on the WSL gate only"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FETCH_SCRIPT = REPO_ROOT / "scripts" / "wsl" / "dpapi-fetch-key.sh"
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "wsl" / "generate-canonical-key.sh"

KEY_PLAINTEXT = "phase-d-test-canonical-key-0123456789"


def _install_script(src: Path, tmp_path: Path) -> Path:
    """Copy the script CRLF-stripped + executable (mirrors the installer's
    `tr -d '\\r' ... && chmod +x` deploy step)."""
    dest = tmp_path / src.name
    dest.write_bytes(src.read_bytes().replace(b"\r", b""))
    dest.chmod(0o755)
    return dest


def _write_ps_stub(tmp_path: Path, decrypt_stdout: str) -> Path:
    """Fake powershell.exe: the interop probe (`-Command 'exit 0'`) succeeds;
    the decrypt invocation consumes stdin and prints the canned output."""
    stub = tmp_path / "powershell.exe"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"exit 0"*) exit 0 ;;\n'
        "esac\n"
        "cat >/dev/null\n"
        f"printf '%s' '{decrypt_stdout}'\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _run_fetch(tmp_path: Path, decrypt_stdout: str, *, blob: bool = True,
               retries: int = 2) -> tuple[subprocess.CompletedProcess, Path, Path]:
    script = _install_script(FETCH_SCRIPT, tmp_path)
    stub = _write_ps_stub(tmp_path, decrypt_stdout)
    blob_path = tmp_path / "canonical-key.dpapi"
    if blob:
        blob_path.write_bytes(b"\x01fake-dpapi-blob")
    out_dir = tmp_path / "runtime-mem0"
    env = {
        **os.environ,
        "MEM0_DPAPI_BLOB": str(blob_path),
        "RUNTIME_DIRECTORY": str(out_dir),
        "DPAPI_FETCH_RETRIES": str(retries),
        "DPAPI_FETCH_SLEEP": "0",
        "DPAPI_FETCH_PS": str(stub),
    }
    proc = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    return proc, out_dir, out_dir / "canonical-key"


def test_fetch_success_installs_key_mode_600(tmp_path):
    """Happy path: valid base64 on stub stdout → key installed, 600, exit 0."""
    proc, out_dir, out_file = _run_fetch(
        tmp_path, base64.b64encode(KEY_PLAINTEXT.encode()).decode()
    )
    assert proc.returncode == 0, proc.stderr
    assert out_file.read_text(encoding="utf-8") == KEY_PLAINTEXT
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    assert "provisioned" in proc.stderr


def test_fetch_garbage_stdout_retries_and_exits_1(tmp_path):
    """L8: invalid base64 from interop must NOT be installed — the checked
    decode falls into the retry loop (no success log, no exit 0) and the run
    ends FATAL exit 1 with no key file and no tmp debris."""
    proc, out_dir, out_file = _run_fetch(tmp_path, "@@@not-base64@@@", retries=2)
    assert proc.returncode == 1, proc.stderr
    assert not out_file.exists(), "garbage decode must never install a key"
    assert "canonical key provisioned to" not in proc.stderr, "success log on a failed decode"
    assert "not installing" in proc.stderr
    assert "attempt 2/2" in proc.stderr, f"expected bounded retries, got: {proc.stderr}"
    assert "FATAL" in proc.stderr
    # atomic-install hygiene: failed attempts leave no .canonical-key.XXXXXX tmp files
    assert list(out_dir.glob(".canonical-key.*")) == []


def test_fetch_empty_stdout_retries_and_exits_1(tmp_path):
    """Empty interop output (decrypt failed) → retry loop → FATAL exit 1."""
    proc, out_dir, out_file = _run_fetch(tmp_path, "", retries=2)
    assert proc.returncode == 1, proc.stderr
    assert not out_file.exists()
    assert "decrypt failed or empty" in proc.stderr
    assert "FATAL" in proc.stderr


def test_fetch_missing_blob_is_fatal(tmp_path):
    """No DPAPI blob → immediate FATAL exit 1 (nothing to decrypt)."""
    proc, out_dir, out_file = _run_fetch(tmp_path, "", blob=False)
    assert proc.returncode == 1
    assert "FATAL: DPAPI blob not found" in proc.stderr
    assert not out_file.exists()


# --- M9: generate-canonical-key.sh blob guard (behavioral) ---


def test_generate_key_refuses_when_dpapi_blob_exists(tmp_path):
    """A fresh plaintext key next to an existing DPAPI blob = key split-brain.
    The generator must refuse (exit 1) and point at the runbook Recovery."""
    script = _install_script(GENERATE_SCRIPT, tmp_path)
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    (home / ".mem0" / "canonical-key.dpapi").write_bytes(b"\x01fake-blob")
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(["bash", str(script)], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1
    assert "REFUSING" in (proc.stdout + proc.stderr)
    assert not (home / ".mem0" / "canonical-key").exists()


def test_generate_key_still_works_on_fresh_box(tmp_path):
    """No blob, no key → generator works exactly as before (fresh install)."""
    script = _install_script(GENERATE_SCRIPT, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(["bash", str(script)], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    key_file = home / ".mem0" / "canonical-key"
    assert key_file.exists()
    assert len(key_file.read_text().strip()) >= 32
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
