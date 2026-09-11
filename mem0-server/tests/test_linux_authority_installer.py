# mem0-server/tests/test_linux_authority_installer.py
"""install/linux-authority.sh — the native Linux AUTHORITY installer (spec §4).

Runs the script itself (bash) in a scratch HOME. The native install must render a unit set
that carries none of the WSL-only lines (the DPAPI ExecStartPre, /mnt/c, cmd.exe,
powershell.exe), must load both secrets through systemd-creds, and must never bind 0.0.0.0.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "install" / "linux-authority.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

WSL_ONLY = re.compile(r"/mnt/c|cmd\.exe|powershell\.exe|dpapi-fetch-key\.sh|/run/WSL")


def _run(args, tmp_path, secrets=True):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    sec = tmp_path / "secrets"
    sec.mkdir(exist_ok=True)
    if secrets:
        (sec / "ams-api-key.cred").write_bytes(b"x" * 64)
        (sec / "ams-canonical-key.cred").write_bytes(b"y" * 64)
    env = dict(os.environ)
    env["HOME"] = str(home)
    r = subprocess.run([BASH, str(SCRIPT), *args, "--secrets-dir", str(sec)],
                       capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120)
    return r, home


def test_script_parses():
    r = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("ip", ["0.0.0.0", "::", "127.0.0.1", "localhost", "not-an-ip"])
def test_refuses_a_wildcard_loopback_or_malformed_bind(tmp_path, ip):
    r, home = _run(["--bind-ip", ip, "--dry-run"], tmp_path)
    assert r.returncode != 0
    assert "bind" in r.stderr.lower()
    assert not (home / ".mem0").exists()


def test_refuses_when_a_cred_file_is_missing(tmp_path):
    r, home = _run(["--bind-ip", "192.0.2.9", "--dry-run"], tmp_path, secrets=False)
    assert r.returncode != 0
    assert "ams-api-key.cred" in r.stderr
    assert not (home / ".mem0").exists()


def test_dry_run_writes_nothing(tmp_path):
    r, home = _run(["--bind-ip", "192.0.2.9", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "[dry-run]" in r.stdout
    assert not (home / ".mem0").exists()
    assert not (home / ".config").exists()


def test_render_only_unit_set_is_native(tmp_path):
    out = tmp_path / "render"
    r, home = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (home / ".mem0").exists()
    files = {p.relative_to(out).as_posix(): p.read_text(encoding="utf-8")
             for p in out.rglob("*") if p.is_file()}
    assert "mem0.service" in files and "qdrant.service" in files and "l10-audit.timer" in files
    assert "mem0.service.d/native.conf" in files
    for name, text in files.items():
        assert not WSL_ONLY.search(text), f"{name} carries a WSL-only line"
        assert not re.search(r"__[A-Z_]+__", text), f"{name} has an unresolved sentinel"
    conf = files["mem0.service.d/native.conf"]
    assert "ExecStartPre=\n" in conf, "the drop-in must CLEAR the WSL ExecStartPre before adding its own"
    assert "wait-for-bind.sh 192.0.2.9" in conf
    assert conf.count("LoadCredentialEncrypted=") == 2, "both keys come through systemd-creds"
    assert "ams-canonical-key.cred" in conf and "ams-api-key.cred" in conf
    assert "Environment=MEM0_API_KEY_FILE=%d/ams-api-key" in conf
    assert "Environment=MEM0_HOST_KIND=native" in conf
    assert "--host 192.0.2.9 --port 18791" in files["mem0.service"]
    assert "--host 0.0.0.0" not in files["mem0.service"]


def test_render_only_includes_the_chain_and_no_per_job_timers(tmp_path):
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    names = {p.name for p in out.rglob("*.timer")}
    assert names == {"l10-audit.timer", "ams-nightly.timer"}, names


def test_installer_enables_every_chain_step():
    """WantedBy=ams-nightly.target only binds a step once it is enabled; the first live run
    started the target and pulled in nothing because only the timer was enabled."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'systemctl --user enable "\$\(basename "\$u"\)"', text)
    assert "ams-step-*.service" in text


def test_wait_for_bind_parses_and_refuses_wildcard():
    script = REPO_ROOT / "scripts" / "wsl" / "wait-for-bind.sh"
    r = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([BASH, str(script), "0.0.0.0", "1"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 78


def test_rendered_units_never_hardcode_the_tenant_home(tmp_path):
    """The Linux user and the mem0 tenant differ on a native box: the first live l10-audit run died
    203/EXEC on /home/<tenant>/apps/... . Every home-relative path must render as %h."""
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--user-id", "tenantx", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    for f in out.rglob("*"):
        if f.is_file():
            t = f.read_text(encoding="utf-8")
            assert "/home/tenantx" not in t, f"{f.name} resolves a path through the tenant name"
            assert "/home/__WSL_USER__" not in t
    assert "Environment=MEM0_DEFAULT_USER_ID=tenantx" in (out / "mem0.service").read_text(encoding="utf-8")
    assert "%h/apps/mem0-server/.venv/bin/python" in (out / "l10-audit.service").read_text(encoding="utf-8")


def test_native_conf_pins_codex_home_to_the_secrets_dir(tmp_path):
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    conf = (out / "mem0.service.d" / "native.conf").read_text(encoding="utf-8")
    assert f"Environment=CODEX_HOME={tmp_path / 'secrets'}/codex" in conf


def test_eval_root_is_validated_and_pcloud_dir_accepted(tmp_path):
    r, _ = _run(["--bind-ip", "192.0.2.9", "--eval-root", str(tmp_path / "nope"), "--dry-run"], tmp_path)
    assert r.returncode != 0 and "retrieval_drift.py" in r.stderr
    ev = tmp_path / "eval" / "eval" / "retrieval-drift"
    ev.mkdir(parents=True)
    (ev / "retrieval_drift.py").write_text("", encoding="utf-8")
    r, _ = _run(["--bind-ip", "192.0.2.9", "--eval-root", str(tmp_path / "eval"), "--pcloud-dir", "/x/y", "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    text = SCRIPT.read_text(encoding="utf-8")
    assert "MEM0_EVAL_ROOT=%s" in text and "MEM0_PCLOUD_DIR=%s" in text


def test_nft_persistence_unit_is_a_root_oneshot():
    t = (REPO_ROOT / "systemd" / "ams-nft.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in t and "ExecStart=/usr/sbin/nft -f /etc/nftables.d/ams.nft" in t
    assert "After=network-pre.target" in t and "WantedBy=multi-user.target" in t
    sh = SCRIPT.read_text(encoding="utf-8")
    assert "ams-nft.service" in sh and "sudo -n" in sh
    assert "enable --now nftables.service" not in sh, "nftables.service would flush the iptables-nft tables"
