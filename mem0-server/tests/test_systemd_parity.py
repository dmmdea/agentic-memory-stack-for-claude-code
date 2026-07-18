"""v0.19 fix-pass: parity tests for the versioned systemd unit + installer.

Adversarial-review HIGH closure: the Phase H key-injection chain lived only in
the hand-edited live unit (~/.config/systemd/user/mem0.service) — any redeploy
from systemd/mem0.service or installer re-run silently stripped it, leaving the
server keyless (all canonical/insight mutations 503). These tests pin the
repo-shipped unit and installer so the chain can never drop out of version
control again.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT = REPO_ROOT / "systemd" / "mem0.service"
INSTALLER = REPO_ROOT / "install" / "1-wsl-services.sh"
APP = REPO_ROOT / "mem0-server" / "app.py"


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


def test_goals_stale_sweep_service_stays_report_only():
    """The deployed goals-stale-sweep unit must NOT pass --auto-abandon: the
    installer-enabled timer must run in report-only mode (no destructive goal
    status flips on an unattended schedule)."""
    text = GOALS_SWEEP_SERVICE.read_text(encoding="utf-8")
    exec_line = next((ln for ln in text.splitlines()
                      if ln.strip().startswith("ExecStart=")), "")
    assert exec_line, "goals-stale-sweep.service has no ExecStart"
    assert "--auto-abandon" not in exec_line, \
        "goals-stale-sweep ExecStart must stay report-only (no --auto-abandon)"


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
