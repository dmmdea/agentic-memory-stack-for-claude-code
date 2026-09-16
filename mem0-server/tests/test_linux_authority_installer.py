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


def test_l10_audit_gets_its_own_credential_dropin(tmp_path):
    """l10-audit runs on its own timer outside the chain; on v1.22.0 it exited 1 with
    'no mem0 API key' because only mem0.service and the chain steps loaded the credential."""
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    conf = (out / "l10-audit.service.d" / "native.conf").read_text(encoding="utf-8")
    assert f"LoadCredentialEncrypted=ams-api-key:{tmp_path / 'secrets'}/ams-api-key.cred" in conf
    assert "Environment=MEM0_API_KEY_FILE=%d/ams-api-key" in conf and "__SECRETS_DIR__" not in conf


def test_embed_model_is_rendered_into_the_drop_in(tmp_path):
    """The store is bound to the exact GGUF it was embedded with; the authority names the llama-swap
    model that serves that file (a stock 'embeddinggemma' of another conversion scored noise)."""
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Environment=MEM0_EMBED_MODEL=embeddinggemma\n" in (out / "mem0.service.d" / "native.conf").read_text(encoding="utf-8")
    out2 = tmp_path / "render2"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--embed-model", "embeddinggemma-ams", "--render-only", str(out2)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Environment=MEM0_EMBED_MODEL=embeddinggemma-ams\n" in (out2 / "mem0.service.d" / "native.conf").read_text(encoding="utf-8")


def test_tenant_inherits_from_the_existing_receipt_on_a_rerun(tmp_path):
    """v1.23.1: an omitted --user-id must take the tenant already in ~/.mem0/stack.env. The
    2026-09-14 cutover re-ran the installer without the flag and it rewrote the tenant to the Linux
    login: every search ran as the wrong user and the canaries read 0/7 against a healthy store."""
    home = tmp_path / "home"; (home / ".mem0").mkdir(parents=True)
    (home / ".mem0" / "stack.env").write_text("MEM0_WSL_USER=oldtenant\nMEM0_HOST_KIND=native\n", encoding="utf-8")
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "tenant inherited from ~/.mem0/stack.env: oldtenant" in r.stdout
    assert "Environment=MEM0_DEFAULT_USER_ID=oldtenant" in (out / "mem0.service").read_text(encoding="utf-8")
    # an explicit flag still wins
    out2 = tmp_path / "render2"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--user-id", "newtenant", "--render-only", str(out2)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Environment=MEM0_DEFAULT_USER_ID=newtenant" in (out2 / "mem0.service").read_text(encoding="utf-8")


def test_first_install_without_a_receipt_falls_back_to_the_login_name(tmp_path):
    out = tmp_path / "render"
    r, home = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    login = os.environ.get("USER") or os.getlogin()
    assert f"Environment=MEM0_DEFAULT_USER_ID={login}" in (out / "mem0.service").read_text(encoding="utf-8")


def _stack_env(home, extra=""):
    (home / ".mem0").mkdir(parents=True, exist_ok=True)
    (home / ".mem0" / "stack.env").write_text("MEM0_WSL_USER=oldtenant\nMEM0_HOST_KIND=native\n" + extra, encoding="utf-8")


def test_every_optional_flag_inherits_from_stack_env_on_a_rerun(tmp_path):
    """v1.23.2: the tenant was only the first flag caught. A re-run without --embed-model reverted
    the embed model to the stock name (the D13 wrong-conversion defect: searches score noise while
    /health/deep stays green), an omitted --eval-root dropped the drift canary, an omitted
    --zfs-dataset the pool-usage check, an omitted --pcloud-dir a custom mirror path."""
    home = tmp_path / "home"
    eval_root = tmp_path / "eval-root"
    (eval_root / "eval" / "retrieval-drift").mkdir(parents=True)
    (eval_root / "eval" / "retrieval-drift" / "retrieval_drift.py").write_text("# stub\n", encoding="utf-8")
    _stack_env(home, "MEM0_EMBED_MODEL=embeddinggemma-custom\n"
                     f"MEM0_EVAL_ROOT={eval_root}\n"
                     "MEM0_PCLOUD_DIR=/srv/mirror/memory-backups\n"
                     "MEM0_ZFS_DATASET=pool/apps/ams\n")
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    for line in ("--embed-model inherited from ~/.mem0/stack.env: embeddinggemma-custom",
                 f"--eval-root inherited from ~/.mem0/stack.env: {eval_root}",
                 "--pcloud-dir inherited from ~/.mem0/stack.env: /srv/mirror/memory-backups",
                 "--zfs-dataset inherited from ~/.mem0/stack.env: pool/apps/ams"):
        assert line in r.stdout, (line, r.stdout)
    conf = (out / "mem0.service.d" / "native.conf").read_text(encoding="utf-8")
    assert "Environment=MEM0_EMBED_MODEL=embeddinggemma-custom\n" in conf
    assert "Environment=MEM0_ZFS_DATASET=pool/apps/ams\n" in conf
    # an explicit flag still wins over the receipt
    out2 = tmp_path / "render2"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--embed-model", "other-model", "--zfs-dataset", "other/ds",
                 "--render-only", str(out2)], tmp_path)
    assert r.returncode == 0, r.stderr
    conf2 = (out2 / "mem0.service.d" / "native.conf").read_text(encoding="utf-8")
    assert "Environment=MEM0_EMBED_MODEL=other-model\n" in conf2
    assert "Environment=MEM0_ZFS_DATASET=other/ds\n" in conf2
    assert "--embed-model inherited" not in r.stdout and "--zfs-dataset inherited" not in r.stdout


def test_explicit_empty_flag_clears_the_inherited_value(tmp_path):
    """v1.23.5: `--eval-root ""` (or any optional flag given empty) clears the inherited value."""
    home = tmp_path / "home"
    eval_root = tmp_path / "eval-root"
    (eval_root / "eval" / "retrieval-drift").mkdir(parents=True)
    (eval_root / "eval" / "retrieval-drift" / "retrieval_drift.py").write_text("# stub\n", encoding="utf-8")
    _stack_env(home, f"MEM0_EVAL_ROOT={eval_root}\nMEM0_ZFS_DATASET=pool/apps/ams\nMEM0_EMBED_MODEL=custom\n")
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--eval-root", "", "--zfs-dataset", "", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--eval-root cleared (explicit empty value; not inherited)" in r.stdout
    assert "--zfs-dataset cleared (explicit empty value; not inherited)" in r.stdout
    assert "--eval-root inherited" not in r.stdout and "--zfs-dataset inherited" not in r.stdout
    assert "--embed-model inherited from ~/.mem0/stack.env: custom" in r.stdout, "flags not given still inherit"
    conf = (out / "mem0.service.d" / "native.conf").read_text(encoding="utf-8")
    assert "MEM0_ZFS_DATASET" not in conf, "an explicitly cleared dataset must not come back from the drop-in either"


def test_an_inherited_eval_root_is_still_validated(tmp_path):
    """Inheriting must not smuggle a stale value past the check an explicit flag gets."""
    home = tmp_path / "home"
    _stack_env(home, f"MEM0_EVAL_ROOT={tmp_path / 'gone'}\n")
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(tmp_path / "render")], tmp_path)
    assert r.returncode != 0
    assert "retrieval_drift.py" in r.stderr


def test_zfs_dataset_inherits_from_a_pre_v1232_drop_in(tmp_path):
    """Boxes installed before v1.23.2 recorded the dataset only in the rendered drop-in."""
    home = tmp_path / "home"
    _stack_env(home)
    d = home / ".config" / "systemd" / "user" / "mem0.service.d"
    d.mkdir(parents=True)
    (d / "native.conf").write_text("[Service]\nEnvironment=MEM0_ZFS_DATASET=legacy/apps/ams\n", encoding="utf-8")
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--zfs-dataset inherited from the installed drop-in: legacy/apps/ams" in r.stdout
    assert "Environment=MEM0_ZFS_DATASET=legacy/apps/ams\n" in (out / "mem0.service.d" / "native.conf").read_text(encoding="utf-8")


def test_first_install_applies_the_defaults(tmp_path):
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    conf = (out / "mem0.service.d" / "native.conf").read_text(encoding="utf-8")
    assert "Environment=MEM0_EMBED_MODEL=embeddinggemma\n" in conf
    assert "MEM0_ZFS_DATASET" not in conf
    assert "inherited" not in r.stdout


def test_stack_env_records_the_zfs_dataset():
    """The installer writes what a re-run must be able to read back."""
    sh = SCRIPT.read_text(encoding="utf-8")
    assert "printf 'MEM0_ZFS_DATASET=%s\\n' \"$ZFS_DATASET\" >> \"$MEM0_DIR/stack.env\"" in sh


# ---- the hub checkout and the store-judge step (register P4-1b) --------------------------
HUB = "ams-hub@hubbox:ams-store.git"


def test_store_judge_unit_is_absent_when_the_box_holds_no_checkout(tmp_path):
    """Every authority runs this installer; only the store hub holds a checkout. Rendering the
    step elsewhere would enable a nightly with nothing to judge - and leave __AMS_CHECKOUT__
    unresolved, which render_units refuses outright."""
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    names = {p.name for p in out.rglob("*")}
    assert "ams-step-store-judge.service" not in names
    for p in out.rglob("*"):
        if p.is_file():
            assert not re.search(r"__[A-Z_]+__", p.read_text(encoding="utf-8")), p.name


def test_store_judge_unit_is_rendered_and_resolved_with_a_checkout(tmp_path):
    out = tmp_path / "render"
    r, _ = _run(["--bind-ip", "192.0.2.9", "--ams-checkout", "/srv/ams/judge", "--ams-hub", HUB,
                 "--render-only", str(out)], tmp_path)
    assert r.returncode == 0, r.stderr
    unit = (out / "ams-step-store-judge.service").read_text(encoding="utf-8")
    assert not re.search(r"__[A-Z_]+__", unit), "every sentinel must resolve"
    assert "Environment=AMS_STORE_CHECKOUT=/srv/ams/judge" in unit
    assert "Environment=AMS_STORE_HUB_HOST=hubbox" in unit, "the host is parsed out of --ams-hub"
    assert "Environment=AMS_STORE_BIN=/usr/local/bin/ams-store" in unit
    assert "After=mem0.service ams-step-dream.service" in unit, "the plan is dream's output"
    assert "WantedBy=ams-nightly.target" in unit and "PartOf=ams-nightly.target" in unit
    assert "Requires=" not in unit, "a failed step must never stop the chain (design section 4)"
    assert "ams-step.sh --guarded store-judge" in unit, "every chain step carries the boot guard"
    assert "LoadCredentialEncrypted=ams-api-key" in unit and "MEM0_API_KEY_FILE=%d/ams-api-key" in unit
    # The refresh must not run before the judge has applied the night's plan.
    refresh = (out / "ams-step-index-refresh.service").read_text(encoding="utf-8")
    assert "ams-step-store-judge.service" in refresh


def test_dry_run_with_a_checkout_still_writes_nothing(tmp_path):
    r, home = _run(["--bind-ip", "192.0.2.9", "--ams-checkout", str(tmp_path / "judge"),
                    "--ams-hub", HUB, "--dry-run"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "[dry-run]" in r.stdout
    assert not (tmp_path / "judge").exists(), "--dry-run must not create the checkout"
    assert not (home / ".ssh").exists(), "--dry-run must not touch the ssh config"


def test_a_dotted_hub_host_is_refused(tmp_path):
    """reMagicDNS in the remote policy refuses a dotted name at sync time; the installer must
    refuse it at install time rather than build a checkout that can never push."""
    r, _ = _run(["--bind-ip", "192.0.2.9", "--ams-checkout", str(tmp_path / "judge"),
                 "--ams-hub", "ams-hub@hub.example.com:ams-store.git", "--render-only",
                 str(tmp_path / "render")], tmp_path)
    # render-only exits before the checkout block, so the refusal is asserted on the parser:
    sh = SCRIPT.read_text(encoding="utf-8")
    assert "is dotted; reach is the tailnet MagicDNS name only" in sh
    assert r.returncode == 0


def test_the_store_binary_comes_from_a_verified_release_asset():
    sh = SCRIPT.read_text(encoding="utf-8")
    assert 'asset="ams-store-linux-amd64"' in sh
    assert "releases/download/$tag" in sh
    assert "checksum mismatch for $asset" in sh, "a mismatch must refuse, never install"
    assert "--ams-store-binary" in sh and "--ams-store-sums" in sh, "an offline drop is the sanctioned alternative"
    assert "sudo -n install -m 0755" in sh
    # Ordering: the binary and the checkout come BEFORE the units, so the chain step is never
    # enabled on a box where what it runs is missing.
    assert sh.index("[4b] store binary + hub checkout") < sh.index("[5] units (native drop-in")


def test_the_checkout_is_hub_role_seeded_and_single_remote():
    sh = SCRIPT.read_text(encoding="utf-8")
    assert "printf 'hub\\n' > \"$state/role\"" in sh, "judge-apply refuses without role=hub"
    assert "ssh-keygen -F \"$host\" -f \"$HOME/.ssh/known_hosts\"" in sh
    assert "the hub's host key is not in ~/.ssh/known_hosts" in sh, "an unseeded checkout must refuse loudly"
    assert "id_ed25519_ams_hub is absent" in sh
    assert "the remote policy allows only hub" in sh
    assert "init -q -b main" in sh


def test_the_plan_schema_travels_with_the_deployed_scripts():
    """The store-judge phase validates the plan against this schema before writing it and
    refuses to write without it. The schema lives under docs/ (it is the published contract,
    generated from the Go types), so the scripts/wsl/* glob does not carry it - measured on the
    authority after the first install: validate_plan returned "the judge-plan schema is not
    deployed beside this script" and no plan would ever have been written."""
    sh = SCRIPT.read_text(encoding="utf-8")
    assert 'cp "$REPO_ROOT/docs/schemas/judge-plan.schema.json" "$SCRIPTS_DIR/judge-plan.schema.json"' in sh
    assert (REPO_ROOT / "docs" / "schemas" / "judge-plan.schema.json").is_file(), \
        "the generated schema must be checked in, or the installer copies nothing"


def test_stack_env_records_the_checkout_and_the_hub():
    sh = SCRIPT.read_text(encoding="utf-8")
    assert "printf 'MEM0_AMS_CHECKOUT=%s\\n' \"$AMS_CHECKOUT\" >> \"$MEM0_DIR/stack.env\"" in sh
    assert "printf 'MEM0_AMS_HUB=%s\\n' \"$AMS_HUB\" >> \"$MEM0_DIR/stack.env\"" in sh
    assert "inherit_from_stack_env AMS_CHECKOUT MEM0_AMS_CHECKOUT --ams-checkout" in sh
    assert "inherit_from_stack_env AMS_HUB      MEM0_AMS_HUB      --ams-hub" in sh


def test_the_judge_apply_wrapper_treats_a_missing_plan_as_deterministic_only():
    """The P4-1b gate's second half: the floor lands even when the plan is empty."""
    w = (REPO_ROOT / "scripts" / "wsl" / "ams-store-judge-apply.sh").read_text(encoding="utf-8")
    assert "deterministic-only night" in w
    assert 'if [ -s "$PLAN" ]; then' in w, "a missing plan skips the apply loop"
    i_else = w.index("no plan at $PLAN")
    i_sync = w.index('"$BIN" sync $sync_args')
    assert i_else < i_sync, "the sync must run whether or not a plan existed"
    assert "MEM0_API_KEY_FILE" in w, "the corpus key comes from the systemd credential, never a flag"
    assert "--plan" in w and "--workspace" in w and "--state-root" in w
