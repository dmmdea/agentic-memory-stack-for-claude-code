"""v0.19 fix-pass: parity tests for the versioned systemd unit + installer.

Adversarial-review HIGH closure: the Phase H key-injection chain lived only in
the hand-edited live unit (~/.config/systemd/user/mem0.service) — any redeploy
from systemd/mem0.service or installer re-run silently stripped it, leaving the
server keyless (all canonical/insight mutations 503). These tests pin the
repo-shipped unit and installer so the chain can never drop out of version
control again.
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import embedder_profile as EP

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT = REPO_ROOT / "systemd" / "mem0.service"
INSTALLER = REPO_ROOT / "install" / "1-wsl-services.sh"
APP = REPO_ROOT / "mem0-server" / "app.py"
BASH = shutil.which("bash")


def test_mem0_unit_carries_phase_h_key_chain():
    """The three Phase H [Service] lines must ship in the versioned unit."""
    text = UNIT.read_text(encoding="utf-8")
    service_section = text.split("[Service]", 1)[1].split("[Install]", 1)[0]
    assert "RuntimeDirectory=mem0" in service_section
    assert "RuntimeDirectoryMode=0700" in service_section
    # '-' prefix is load-bearing: fail-soft on fresh/plaintext boxes
    assert "ExecStartPre=-%h/apps/mem0-server/dpapi-fetch-key.sh" in service_section


def test_mem0_unit_disables_runtime_phone_home():
    """MEM-18 (2026-07-03): the versioned unit must pin BOTH opt-outs.
    MEM0_TELEMETRY=False is what mem0 2.0.4's telemetry.py actually reads
    (anything outside true/1/yes disables PostHog — verified against the
    installed lib source); HF_HUB_OFFLINE=1 stops any transitive
    huggingface-hub phone-home from the server process."""
    text = UNIT.read_text(encoding="utf-8")
    service_section = text.split("[Service]", 1)[1].split("[Install]", 1)[0]
    assert "Environment=MEM0_TELEMETRY=False" in service_section
    assert "Environment=HF_HUB_OFFLINE=1" in service_section


def test_mem0_unit_pins_durable_fastembed_cache():
    """AMS-09b (2026-08-07): the sparse leg died a second time because the
    fastembed model cache lived in /tmp (reboot-wiped) while HF_HUB_OFFLINE=1
    forbids the server from re-downloading. The versioned unit must pin the
    cache to a reboot-surviving dir — dropping this line resurrects the
    boot-window death."""
    text = UNIT.read_text(encoding="utf-8")
    service_section = text.split("[Service]", 1)[1].split("[Install]", 1)[0]
    assert "Environment=FASTEMBED_CACHE_PATH=%h/.cache/fastembed" in service_section


def test_installer_warms_fastembed_cache_into_durable_dir():
    """The installer's encoder warm must populate the SAME dir the unit reads:
    the export must exist AND precede the SparseTextEmbedding warm, else the
    warm lands in /tmp and the server (offline) starts cold after every
    reboot."""
    text = INSTALLER.read_text(encoding="utf-8")
    export_ix = text.find(
        'export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$HOME/.cache/fastembed}"')
    warm_ix = text.find('SparseTextEmbedding(model_name="Qdrant/bm25")')
    assert export_ix != -1, "installer no longer exports FASTEMBED_CACHE_PATH"
    assert warm_ix != -1, "installer no longer warms the BM25 encoder"
    assert export_ix < warm_ix, "cache-dir export must precede the encoder warm"


def test_deploy_seeds_durable_fastembed_cache_before_restart():
    """AMS-09b (W5 review F2): deploy.sh must seed the durable cache and must
    do it BEFORE the service restart, so the fresh process loads from the
    reboot-surviving dir on first encode. Dropping the step re-arms the
    reboot time-bomb the cache_note check exists to catch."""
    deploy = (REPO_ROOT / "scripts" / "wsl" / "deploy.sh").read_text(
        encoding="utf-8")
    seed_ix = deploy.find(
        "FASTEMBED_CACHE_PATH=\"$FASTEMBED_CACHE\" \"$APP_DIR/.venv/bin/python\"")
    restart_ix = deploy.find("systemctl --user restart mem0.service")
    assert seed_ix != -1, "deploy.sh no longer seeds the fastembed cache"
    assert restart_ix != -1
    assert seed_ix < restart_ix, "seed must precede the restart"
    assert 'SparseTextEmbedding(model_name=' in deploy


def test_jobs_queue_suite_is_ci_gated():
    """W6 F7e: a test file absent from ci.yml's explicit allowlist gates
    nothing (the W1 silent-not-gating class) — pin the queue suite's entry."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8")
    assert "mem0-server/tests/test_jobs_queue.py" in ci


def test_deploy_retrieval_gate_rejects_all_skip_runs():
    """W5 Trains-2+3 review fix 3: pytest exits 0 on an all-skip suite, so a
    seeding regression that skipped every family would green-light the deploy.
    The gate must require at least one PASSED family on a zero-exit run."""
    deploy = (REPO_ROOT / "scripts" / "wsl" / "deploy.sh").read_text(
        encoding="utf-8")
    assert "GATE VACUOUS" in deploy
    assert "grep -qE '[1-9][0-9]* passed'" in deploy
    assert "test_retrieval_families.py" in deploy


def test_mem0_unit_execstartpre_ordered_before_execstart():
    """systemd runs ExecStartPre before ExecStart regardless of file order, but
    keep the unit readable: the key fetch appears before the uvicorn line."""
    text = UNIT.read_text(encoding="utf-8")
    assert text.index("ExecStartPre=") < text.index("ExecStart=%h")


def test_installer_deploys_dpapi_fetch_script():
    """install/1-wsl-services.sh must deploy dpapi-fetch-key.sh next to the app
    modules (CRLF-stripped + executable) — the unit's ExecStartPre depends on it."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert re.search(
        r'tr -d "\\r" < "\$REPO_ROOT/scripts/wsl/dpapi-fetch-key\.sh" > '
        r'"\$MEM0_DIR/dpapi-fetch-key\.sh" && chmod \+x', text)


def test_installer_guards_dpapi_backed_canonical_key():
    """The canonical-key generator must not rotate a DPAPI-backed key on
    re-run: it runs only when NEITHER plaintext nor .dpapi blob exists."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert ('if [ ! -f "$CANON_KEY_FILE" ] && [ ! -f "$CANON_KEY_FILE.dpapi" ]'
            in text)


def test_installer_copies_every_module_app_imports():
    """Fresh-install parity: every local module app.py imports (top-level or
    function-level) must be in the installer's MEM0_MODULES copy list —
    a missing one crash-loops a fresh server on ModuleNotFoundError."""
    local_modules = {p.stem for p in (REPO_ROOT / "mem0-server").glob("*.py")}
    app_text = APP.read_text(encoding="utf-8")
    # Match BOTH `from X import ...` AND bare `import X` (v0.27.2: the R5 modules
    # codex_shim_client + nli_write_gate are imported bare — the from-only regex missed
    # them, the exact blind spot this crash-loop guard exists to prevent).
    _from = re.findall(r"^\s*from\s+(\w+)\s+import", app_text, re.M)
    _bare = re.findall(r"^\s*import\s+(\w+)", app_text, re.M)
    imported = {m for m in (_from + _bare) if m in local_modules} | {"config"}  # build_config is the entry point either way
    installer_text = INSTALLER.read_text(encoding="utf-8")
    m = re.search(r'^MEM0_MODULES="([^"]+)"', installer_text, re.M)
    assert m, "MEM0_MODULES list missing from install/1-wsl-services.sh"
    copied = {Path(f).stem for f in m.group(1).split()}
    missing = imported - copied
    assert not missing, f"installer does not copy modules app.py imports: {missing}"


# --- v0.20 Phase D (M9): post-Phase-H remediation text must not advise key ---
# --- regeneration on a DPAPI box (generate-fresh = key split-brain)        ---

SECURITY_INVARIANTS = REPO_ROOT / "mem0-server" / "security_invariants.py"
GENERATE_KEY_SH = REPO_ROOT / "scripts" / "wsl" / "generate-canonical-key.sh"


def test_keyless_503_remediation_is_dpapi_aware():
    """The 503 strings in app.py and security_invariants.py must point at the
    Phase H recovery path (dpapi-fetch-key / runbook Recovery / restart), not
    a bare 'run generate-canonical-key.sh' — following that on a DPAPI box
    silently rotates the key out from under the blob."""
    for src in (APP, SECURITY_INVARIANTS):
        text = src.read_text(encoding="utf-8")
        assert "(run scripts/wsl/generate-canonical-key.sh)" not in text, src.name
        assert "Run generate-canonical-key.sh first" not in text, src.name
    app_text = APP.read_text(encoding="utf-8")
    si_text = SECURITY_INVARIANTS.read_text(encoding="utf-8")
    for text, name in ((app_text, "app.py"), (si_text, "security_invariants.py")):
        assert "dpapi-canonical-key.md" in text, f"{name}: 503 text must cite the runbook Recovery"
        assert "dpapi-fetch-key" in text, f"{name}: 503 text must point at the runtime injection"


def test_generate_canonical_key_guards_existing_dpapi_blob():
    """generate-canonical-key.sh refuses (exit 1) when canonical-key.dpapi
    exists unless --force — kills the split-brain chain at the root no matter
    which stale doc an operator follows."""
    text = GENERATE_KEY_SH.read_text(encoding="utf-8")
    assert "canonical-key.dpapi" in text
    assert "--force" in text
    assert "REFUSING" in text


# --- v0.22 Phase G: installer auto-enables the weekly hygiene sweep timers ---
# --- (goals-stale-sweep + contradiction-sweep) in report-safe defaults.    ---

GOALS_SWEEP_SERVICE = REPO_ROOT / "systemd" / "goals-stale-sweep.service"


def test_installer_deploys_both_sweep_units():
    """1-wsl-services.sh must copy both sweep .service AND .timer units into
    ~/.config/systemd/user (otherwise enable --now fails on a fresh box)."""
    text = INSTALLER.read_text(encoding="utf-8")
    for unit in ("goals-stale-sweep.service", "goals-stale-sweep.timer",
                 "contradiction-sweep.service", "contradiction-sweep.timer"):
        assert unit in text, f"installer does not deploy {unit}"


def test_installer_enables_both_sweep_timers():
    """The installer must `systemctl --user enable --now` BOTH sweep timers so
    a fresh install gets the weekly hygiene runs without a manual step."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert re.search(r"enable --now[^\n]*goals-stale-sweep\.timer", text), \
        "installer does not enable goals-stale-sweep.timer"
    assert re.search(r"enable --now[^\n]*contradiction-sweep\.timer", text), \
        "installer does not enable contradiction-sweep.timer"


def test_goals_stale_sweep_service_scoped_auto_abandon():
    """Goal redesign (operator-approved 2026-08-09): --auto-abandon is now
    STANDING — this supersedes the old report-only pin. The safety that pin
    protected moved into the script and is pinned there (test_goal_redesign:
    abandon_exempt never touches manual goals; --abandon-days defaults to the
    operator-set 90). What the unit must NOT do is quietly shrink that window:
    passing an explicit --abandon-days here would override the 90d default
    outside the reviewed script contract."""
    text = GOALS_SWEEP_SERVICE.read_text(encoding="utf-8")
    exec_line = next((ln for ln in text.splitlines()
                      if ln.strip().startswith("ExecStart=")), "")
    assert exec_line, "goals-stale-sweep.service has no ExecStart"
    assert "--auto-abandon" in exec_line, \
        "the standing scoped auto-abandon (operator-approved 2026-08-09) is missing"
    assert "--abandon-days" not in exec_line, \
        "the unit must not override the script's operator-set 90d window"


# --- v0.22 M5: the destructive egemma-rollback-prune one-shot must be ---
# --- version-controlled (unit + script in repo, deployed by installer) ---

PRUNE_SERVICE = REPO_ROOT / "systemd" / "egemma-rollback-prune.service"
PRUNE_TIMER = REPO_ROOT / "systemd" / "egemma-rollback-prune.timer"
PRUNE_SCRIPT = REPO_ROOT / "scripts" / "wsl" / "egemma-rollback-prune.sh"


def test_rollback_prune_units_are_version_controlled():
    """Both the .service and .timer for the destructive rollback-prune one-shot
    must ship in repo systemd/ (they used to live only as hand-placed live units,
    outside version control + the parity audit — the exact anti-pattern the v0.19
    Phase-H HIGH established this test to prevent)."""
    assert PRUNE_SERVICE.exists(), "egemma-rollback-prune.service missing from systemd/"
    assert PRUNE_TIMER.exists(), "egemma-rollback-prune.timer missing from systemd/"
    svc = PRUNE_SERVICE.read_text(encoding="utf-8")
    assert "Type=oneshot" in svc
    assert "egemma-rollback-prune.sh" in svc, "service ExecStart must run the prune script"
    timer = PRUNE_TIMER.read_text(encoding="utf-8")
    assert "OnCalendar=" in timer
    assert "WantedBy=timers.target" in timer


def test_installer_deploys_rollback_prune_units_and_script():
    """1-wsl-services.sh must copy both units into ~/.config/systemd/user AND
    deploy the script the unit ExecStart points at."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert "egemma-rollback-prune.service" in text
    assert "egemma-rollback-prune.timer" in text
    # the script is deployed (CRLF-stripped) to ~/.mem0/
    assert "egemma-rollback-prune.sh" in text


def test_installer_does_not_arm_the_rollback_prune_timer():
    """The destructive one-shot is migration-specific (fires 2026-06-21) — a fresh
    install starts on mem0_egemma_768 with no `memories` anchor to prune, so the
    installer must NOT `enable` the timer (deploy != arm)."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert not re.search(r"enable[^\n]*egemma-rollback-prune\.timer", text), \
        "installer must not auto-enable the destructive rollback-prune one-shot"


def test_rollback_prune_gate_is_binding_based():
    """v0.22 H2: the gate must check the LIVE bound collection (from /health/deep),
    not just the egemma collection's existence — otherwise it can't detect a
    rollback and would delete the live `memories` anchor."""
    text = PRUNE_SCRIPT.read_text(encoding="utf-8")
    # reads the bound collection from health/deep and compares to the expected one
    assert "collection" in text
    assert "health/deep" in text
    assert "ROLLBACK DETECTED" in text, "the gate must distinctly flag a detected rollback"


def test_health_deep_reports_bound_collection():
    """app.py /health/deep must expose the live bound collection_name so the gate
    can read it (the H2 binding signal)."""
    app_text = APP.read_text(encoding="utf-8")
    assert 'out["collection"]' in app_text
    assert "mem.vector_store.collection_name" in app_text


def test_deploy_skips_the_native_chain_units_on_a_wsl_host():
    """deploy.sh installs systemd/*.service|*.timer with the WSL sentinels only; the ams-* units of the
    native authority carry __SECRETS_DIR__ / LoadCredentialEncrypted lines and belong to
    install/linux-authority.sh. A WSL brain or replica must never receive them. Since 1.31.2 the
    native host is refused before the loop (test_deploy_host_kind.py), so the skip is unconditional."""
    text = (REPO_ROOT / "scripts" / "wsl" / "deploy.sh").read_text(encoding="utf-8")
    assert 'case "$unit" in ams-*) continue ;; esac' in text
    assert text.index('MEM0_HOST_KIND:-}') < text.index('for src in "$REPO_ROOT"/systemd/*.service')


# --- the embedding space (mem0-server/embedder_profile.py) in the units and the installer ---

NATIVE_CONF = REPO_ROOT / "systemd" / "mem0-native.conf"
EMBED_LIB = REPO_ROOT / "scripts" / "wsl" / "embed-profile.sh"


def test_native_drop_in_takes_the_profile_and_its_alias_variable_from_the_installer():
    """The drop-in names no profile, alias or collection: the installer renders all three, and the
    alias goes under the variable embedder_profile reads for the profile (a literal MEM0_EMBED_MODEL=
    would be ignored for any profile but the default space)."""
    text = NATIVE_CONF.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("Environment=")]
    assert "Environment=MEM0_EMBED_PROFILE=__EMBED_PROFILE__" in lines
    assert "Environment=__EMBED_MODEL_VAR__=__EMBED_MODEL__" in lines
    assert not [ln for ln in lines if ln.startswith("Environment=MEM0_EMBED_MODEL")], lines
    for p in EP.PROFILES.values():
        assert p.model not in text and p.memories not in text, p.name


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("name", sorted(EP.PROFILES))
def test_the_shell_names_the_scoped_alias_variable_as_embedder_profile_reads_it(name, tmp_path, monkeypatch):
    """embed-profile.sh's ep_env_key and the installer's drop-in rest on the shell and the module
    agreeing on one variable name; ask the module whether a variable of that name selects the alias."""
    r = subprocess.run([BASH, "-c", f'. "{EMBED_LIB.as_posix()}"; ep_env_key "{name}"'], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    key = r.stdout
    assert key == EP._model_key(EP.PROFILES[name]), (key, EP._model_key(EP.PROFILES[name]))
    from _home_isolation import apply_home
    with monkeypatch.context() as m:
        apply_home(m, tmp_path / "empty")
        for k in [k for k in os.environ if k.startswith("MEM0_EMBED")]:
            m.delenv(k)
        m.setenv(key, "served-as-this")
        assert EP.embed_model(EP.PROFILES[name]) == "served-as-this"


def test_the_legacy_alias_variable_is_the_legacy_profiles_alone():
    """linux-authority.sh writes the unscoped MEM0_EMBED_MODEL for the legacy profile and the scoped
    variable for every other: that is embedder_profile.embed_model's own rule, asked rather than assumed."""
    import tempfile
    for name, p in EP.PROFILES.items():
        with tempfile.TemporaryDirectory() as home:
            env = {**{k: v for k, v in os.environ.items() if not k.startswith("MEM0_")}, "HOME": home, "USERPROFILE": home,
                   "MEM0_EMBED_MODEL": "legacy-alias"}
            out = subprocess.run(["python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import embedder_profile as ep; "
                                  "print(ep.embed_model(ep.get(sys.argv[2])))", str(REPO_ROOT / "mem0-server"), name],
                                 capture_output=True, text=True, env=env, timeout=30)
        assert (out.stdout.strip() == "legacy-alias") == (name == EP.LEGACY_PROFILE), (name, out.stdout, out.stderr)


def test_rollback_prune_timer_ships_unarmed_and_the_authority_installer_turns_it_off():
    """Un-armed is a property of the shipped file, not of nobody having enabled it: with Persistent=true a
    PAST OnCalendar fires the destructive prune at once as a missed run, so the shipped date is a placeholder
    in the far future. The operator sets the real one when arming. The native installer also disables it."""
    timer = PRUNE_TIMER.read_text(encoding="utf-8")
    m = re.search(r"^OnCalendar=(\d{4})-", timer, re.M)
    assert m, "the timer needs an OnCalendar for the parity audit"
    assert int(m.group(1)) >= datetime.date.today().year + 50, "the shipped date must be a far-future placeholder"
    authority = (REPO_ROOT / "install" / "linux-authority.sh").read_text(encoding="utf-8")
    assert "egemma-rollback-prune" in authority.split("disable --now", 1)[0].rsplit("for t in", 1)[1]
    for installer in (INSTALLER, REPO_ROOT / "install" / "linux-authority.sh", REPO_ROOT / "install" / "linux-replica.sh"):
        code = installer.read_text(encoding="utf-8")
        assert not re.search(r"(enable|start)[^\n]*egemma-rollback-prune", code), installer.name


def test_rollback_prune_units_describe_a_profile_gated_cleanup_not_the_v022_one_shot():
    svc = PRUNE_SERVICE.read_text(encoding="utf-8")
    assert "embed_profile.profile" in svc and "NEW profile" in svc
    assert "nomic" not in svc and "2026-06-21" not in PRUNE_TIMER.read_text(encoding="utf-8")


# The WSL installer stages two GGUFs and prints the llama-swap entries. Neither can run as a whole here
# (it installs Qdrant and a venv), so its functions are lifted out of the file and run for real.

def _installer_function(name: str) -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _fake_tools(tmp_path: Path, payload: bytes) -> Path:
    """A PATH dir with a curl that writes `payload` to its -o target and no huggingface-cli."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    payload_file = tmp_path / "payload.bin"
    payload_file.write_bytes(payload)
    curl = b / "curl"
    curl.write_text(
        '#!/usr/bin/env bash\nout=""\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; *) shift;; esac; done\n'
        f'echo called >> "{tmp_path / "curl.calls"}"\ncp "{payload_file}" "$out"\n', encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return b


def _stage(tmp_path: Path, payload: bytes, want: str, existing: bytes | None = None):
    home = tmp_path / "home"
    (home / "models").mkdir(parents=True, exist_ok=True)
    dest = home / "models" / "x.gguf"
    if existing is not None:
        dest.write_bytes(existing)
    b = _fake_tools(tmp_path, payload)
    script = f'USER_HOME="{home.as_posix()}"\nset -eo pipefail\n{_installer_function("stage_gguf")}\n' \
             f'stage_gguf "{dest.as_posix()}" some/repo x.gguf "Test model" "{want}" "1MB"\n'
    env = {**os.environ, "PATH": f"{b}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    return r, dest


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_staged_gguf_is_checksum_verified_and_a_mismatch_is_removed(tmp_path):
    import hashlib
    good = b"the real gguf"
    r, dest = _stage(tmp_path, good, hashlib.sha256(good).hexdigest())
    assert r.returncode == 0, r.stderr
    assert dest.read_bytes() == good and "sha256 verified" in r.stdout
    # a download that does not hash to the expected digest is removed, so a re-run fetches it again
    r, dest = _stage(tmp_path / "bad", b"corrupt bytes", hashlib.sha256(good).hexdigest())
    assert r.returncode == 0, r.stderr
    assert not dest.exists(), "a wrong-checksum file must not stay behind to be served"
    assert "has sha256" in r.stdout and "removed" in r.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_present_gguf_is_not_fetched_again_and_a_foreign_one_is_reported_not_deleted(tmp_path):
    import hashlib
    good = b"the real gguf"
    r, dest = _stage(tmp_path, b"never fetched", hashlib.sha256(good).hexdigest(), existing=good)
    assert r.returncode == 0, r.stderr
    assert "already present" in r.stdout and not (tmp_path / "curl.calls").exists()
    r, dest = _stage(tmp_path / "other", b"never fetched", hashlib.sha256(good).hexdigest(), existing=b"an operator's own file")
    assert dest.read_bytes() == b"an operator's own file", "an operator-supplied file is never deleted"
    assert "WARN" in r.stdout and "not the expected" in r.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_failed_download_warns_and_leaves_no_file(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    curl = b / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 22\n", encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    (home / "models").mkdir(parents=True)
    dest = home / "models" / "x.gguf"
    script = f'USER_HOME="{home.as_posix()}"\nset -eo pipefail\n{_installer_function("stage_gguf")}\nstage_gguf "{dest.as_posix()}" r/r x.gguf L "" ""\n'
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env={**os.environ, "PATH": f"{b}{os.pathsep}/usr/bin{os.pathsep}/bin"}, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "could not fetch" in r.stdout and not dest.exists()


def test_the_wsl_installer_stages_embeddinggemma2_beside_the_300m_file():
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'EG2_HF_REPO="ggml-org/embeddinggemma-2-GGUF"' in text
    assert 'EG2_HF_FILE="embeddinggemma-2-Q8_0.gguf"' in text
    assert 'EG2_SHA256="2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135"' in text
    assert 'EG2_GGUF="$USER_HOME/models/embeddinggemma-2-Q8_0.gguf"' in text
    # the 300m fetch stays: a rollback box and the migration both need the old space served
    assert 'EGEMMA_HF_REPO="ggml-org/embeddinggemma-300M-GGUF"' in text
    assert text.index("stage_gguf \"$EGEMMA_GGUF\"") < text.index("stage_gguf \"$EG2_GGUF\"")
    assert "embedder_profile.py" in re.search(r'^MEM0_MODULES="([^"]*)"', text, re.M).group(1).split()


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_printed_llama_swap_entries_serve_the_trained_windows_and_name_the_llama_cpp_floor():
    """The stanza is what an operator pastes into llama-swap: one EmbeddingGemma-2 entry with its
    projector (--mmproj: images, audio and video), mean-pooled with flash attention, its ubatch the
    profile's ctx_tokens (the largest input one embed takes; 2048 measured 1,196 MiB loaded with the
    projector, 4096 would cost 1,870 MiB), in the group that never evicts the memory stack, and the
    llama.cpp build that has the gemma-embedding2 architecture. No long entry by default."""
    fn = _installer_function("print_stanza_eg2") + "\n" + _installer_function("print_stanza_300m")
    r = subprocess.run([BASH, "-c", f'EG2_GGUF=/m/eg2.gguf; EG2_MMPROJ=/m/mmproj.gguf; EGEMMA_GGUF=/m/300m.gguf\n{fn}\nprint_stanza_eg2; echo ----; print_stanza_300m'],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    eg2, old = r.stdout.split("----")
    assert "b11452" in eg2 and "embeddinggemma2:" in eg2 and "embeddinggemma2-long:" not in eg2
    assert "members: [embeddinggemma2]" in eg2 and "swap: false" in eg2
    p = EP.PROFILES["egemma2"]
    ctx = p.ctx_tokens
    for flag in ("--ctx-size 4096", "--batch-size 4096", f"--ubatch-size {ctx}", "--pooling mean", "--embeddings",
                 "-ngl 99", "--flash-attn on", "--model /m/eg2.gguf", "--mmproj /m/mmproj.gguf", "ttl: 300"):
        assert flag in eg2, (flag, eg2)
    assert "MEM0_EMBED_LONG_MODEL_EGEMMA2" in eg2, "the optional long entry names the knob that declares it"
    assert "--ctx-size 2048" in old and "--model /m/300m.gguf" in old, "the 300m entry is unchanged"
    assert "262144" in eg2, "the stanza warns off the GGUF header's window"
