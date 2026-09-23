"""1.31.2: scripts/wsl/deploy.sh refuses a native Linux authority before it writes anything.

deploy.sh is the WSL deploy path. On a box whose ~/.mem0/stack.env says MEM0_HOST_KIND=native it
used to render the ams-* units with only the five WSL sentinels (a literal __SECRETS_DIR__ in every
step unit's LoadCredentialEncrypted= and CODEX_HOME=), re-render mem0.service with the WSL DPAPI
ExecStartPre the native installer drops, and write the WSL per-job timers onto a box that runs one
chain. Its --dry-run passed and listed the damage as "unit CHANGED" lines. The native path is a
re-run of install/linux-authority.sh, which inherits everything else from stack.env.

The contract pinned here:
  1. a native stack.env makes deploy.sh exit non-zero, with and without --dry-run;
  2. the refusal comes before ANY write: every path and byte under the temp HOME is unchanged;
  3. the message names install/linux-authority.sh with --bind-ip / --secrets-dir taken from
     stack.env's MEM0_BIND / MEM0_SECRETS_DIR;
  4. the value is compared like the other readers (.strip().lower()): a CRLF receipt or a value
     with surrounding whitespace is still native;
  5. a WSL stack.env (and a missing MEM0_HOST_KIND) still gets past the gate;
  6. the unit loop never installs an ams-* unit (no host kind reaches it that owns them).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "wsl" / "deploy.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

NATIVE_ENV = (
    "MEM0_WSL_USER=tenant\nMEM0_WIN_USER=\nMEM0_DISTRO=native\nMEM0_HOST_KIND=native\n"
    "MEM0_REPO_ROOT_WSL=/srv/checkout\nMEM0_BIND=192.0.2.9\nMEM0_ROLE=brain\n"
    "MEM0_SECRETS_DIR=/srv/secrets\nMEM0_EMBED_MODEL=embeddinggemma\n"
)
WSL_ENV = (
    "MEM0_WSL_USER=tenant\nMEM0_WIN_USER=winuser\nMEM0_DISTRO=Ubuntu\nMEM0_HOST_KIND=wsl\n"
    "MEM0_REPO_ROOT_WSL=/srv/checkout\nMEM0_BIND=127.0.0.1\nMEM0_ROLE=brain\n"
)


def _stubs(tmp_path):
    """Commands a deploy that got past the gate would reach for; each one is a recorder that fails,
    so a regressed gate can never touch a real user manager, network or Windows side."""
    d = tmp_path / "stubs"
    d.mkdir(exist_ok=True)
    log = tmp_path / "stub-calls.log"
    for name in ("systemctl", "curl", "cmd.exe", "wslpath"):
        p = d / name
        p.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{log.as_posix()}"\nexit 97\n',
                     encoding="utf-8", newline="\n")
        p.chmod(0o755)
    return d, log


def _files(root):
    """Every path under root with its bytes (None for a directory): a write, a new file or a new
    directory all change it."""
    return sorted((p.relative_to(root).as_posix(), p.read_bytes() if p.is_file() else None)
                  for p in root.rglob("*"))


def _run(tmp_path, stack_env, *args):
    home = tmp_path / "home"
    # an installed box: the runtime roots deploy.sh syncs into already exist (rsync creates only
    # the last path component, so a bare HOME would fail the WSL dry run for the wrong reason)
    for d in (".mem0", "apps/mem0-server", "apps/mem0-scripts", ".config/systemd/user"):
        (home / d).mkdir(parents=True, exist_ok=True)
    if stack_env is not None:
        (home / ".mem0" / "stack.env").write_text(stack_env, encoding="utf-8", newline="\n")
    before = _files(home)
    stubs, log = _stubs(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEM0_")}
    env["HOME"] = str(home)
    env["USER"] = env.get("USER") or "tenant"  # deploy.sh reads $USER under set -u; Git Bash has none
    env["PATH"] = str(stubs) + os.pathsep + env.get("PATH", "")
    r = subprocess.run([BASH, str(DEPLOY), *args], capture_output=True, text=True, env=env,
                       cwd=str(REPO_ROOT), timeout=120)
    return r, home, before, log


@pytest.mark.parametrize("args", [[], ["--dry-run"]], ids=["deploy", "dry-run"])
def test_native_stack_env_is_refused_before_any_write(tmp_path, args):
    r, home, before, log = _run(tmp_path, NATIVE_ENV, *args)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert _files(home) == before, f"deploy.sh wrote under HOME on a native box: {_files(home)}"
    assert not log.exists(), log.read_text(encoding="utf-8")
    # the refusal comes first: none of the pipeline's own output was produced
    assert "==> deploy:" not in out
    assert "unit CHANGED" not in out
    assert "install/linux-authority.sh --bind-ip 192.0.2.9 --secrets-dir /srv/secrets" in r.stderr, out


# The other MEM0_HOST_KIND readers (job_liveness, codex_shim_client) compare .strip().lower(); a
# receipt that one of them calls native must be native here too. A CRLF file (hand-edited, copied
# over SMB/SCP) sources as "native\r", and a quoted value can carry spaces or a tab; each one fell
# through to the WSL pipeline (--dry-run exit 0 with the unit CHANGED list) before the strip.
NATIVE_VARIANTS = {
    "crlf-file": NATIVE_ENV.replace("\n", "\r\n"),
    "crlf-host-kind-line": NATIVE_ENV.replace("MEM0_HOST_KIND=native\n", "MEM0_HOST_KIND=native\r\n"),
    "spaces": NATIVE_ENV.replace("MEM0_HOST_KIND=native", 'MEM0_HOST_KIND="  native  "'),
    "tab-and-cr": NATIVE_ENV.replace("MEM0_HOST_KIND=native", "MEM0_HOST_KIND=$'\\tNative \\r'"),
}


@pytest.mark.parametrize("args", [[], ["--dry-run"]], ids=["deploy", "dry-run"])
@pytest.mark.parametrize("variant", sorted(NATIVE_VARIANTS))
def test_native_with_cr_or_surrounding_whitespace_is_still_refused(tmp_path, variant, args):
    r, home, before, log = _run(tmp_path, NATIVE_VARIANTS[variant], *args)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert _files(home) == before, f"deploy.sh wrote under HOME on a native box: {_files(home)}"
    assert not log.exists(), log.read_text(encoding="utf-8")
    assert "==> deploy:" not in out and "unit CHANGED" not in out, out
    # the printed command is runnable as shown: no \r carried over from a CRLF receipt
    assert "install/linux-authority.sh --bind-ip 192.0.2.9 --secrets-dir /srv/secrets\n" in r.stderr, repr(r.stderr)


def test_native_host_kind_is_matched_case_insensitively(tmp_path):
    """job_liveness and the codex transport lower-case the value; the gate must agree with them."""
    r, home, before, _ = _run(tmp_path, NATIVE_ENV.replace("MEM0_HOST_KIND=native", "MEM0_HOST_KIND=Native"),
                              "--dry-run")
    assert r.returncode != 0
    assert _files(home) == before
    assert "linux-authority.sh" in r.stderr


def test_refusal_without_bind_or_secrets_still_names_the_flags(tmp_path):
    env = "MEM0_HOST_KIND=native\nMEM0_ROLE=brain\n"
    r, home, before, _ = _run(tmp_path, env, "--dry-run")
    assert r.returncode != 0
    assert _files(home) == before
    assert "--bind-ip <" in r.stderr and "--secrets-dir <" in r.stderr, r.stderr


@pytest.mark.parametrize("stack_env", [WSL_ENV, WSL_ENV.replace("MEM0_HOST_KIND=wsl\n", ""), None],
                         ids=["wsl", "no-host-kind", "no-stack-env"])
def test_wsl_host_gets_past_the_gate(tmp_path, stack_env):
    r, _, _, _ = _run(tmp_path, stack_env, "--dry-run")
    out = r.stdout + r.stderr
    assert "linux-authority.sh" not in out, out
    assert "==> deploy:" in r.stdout, out
    if shutil.which("rsync"):
        # the full dry run completes; without rsync (a Git Bash dev box) it stops at step 1,
        # which is past the gate this file pins
        assert r.returncode == 0, out
        assert "dry-run complete" in r.stdout, out
        assert "unit CHANGED: ams-" not in r.stdout, out


def test_the_unit_loop_never_installs_an_ams_unit():
    """The ams-* units belong to install/linux-authority.sh (they need __SECRETS_DIR__ and the
    credential lines only it renders). With native hosts refused up front, the loop skips them
    unconditionally; a host-kind condition there would be dead code claiming a path that is gone."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert 'case "$unit" in ams-*) continue ;; esac' in text
    assert "HOST_KIND\" = \"native\" ] || continue" not in text
