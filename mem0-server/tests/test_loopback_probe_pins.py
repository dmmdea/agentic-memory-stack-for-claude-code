# mem0-server/tests/test_loopback_probe_pins.py
"""v1.23.1/v1.23.2: no maintenance script probes the server on a hard-coded loopback address.

The native authority binds its tailnet address only (spec §4), so a loopback probe there reads
"down" against a healthy server: stack-promote.sh's rehearsal read "inconclusive" every time,
deploy.sh's health gate would abort a good deploy, a hand-run mem0-canonize.sh on the authority
posted to nothing. Each script keeps exactly one loopback literal — its documented DEFAULT for a
box with no bind / no authority file — and every probe goes through the resolved variable.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOPBACK = "http://127.0.0.1:18791"


def _code(path: Path) -> str:
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))


def test_deploy_sh_probes_the_bind_address():
    code = _code(REPO_ROOT / "scripts" / "wsl" / "deploy.sh")
    assert code.count(LOOPBACK) == 1, "one literal: the wildcard/unset-bind default of MEM0_HEALTH_URL"
    assert re.search(r'""\|0\.0\.0\.0\)\s+MEM0_HEALTH_URL="http://127\.0\.0\.1:18791"', code)
    assert 'MEM0_HEALTH_URL="http://${MEM0_BIND}:18791"' in code
    for probe in re.findall(r"curl [^\n]*18791[^\n]*", code):
        assert "$MEM0_HEALTH_URL" in probe, probe
    assert 'MEM0_URL="$MEM0_HEALTH_URL"' in code, "the retrieval gate targets the same address"


def test_deploy_sh_never_starts_a_dormant_replica_mem0():
    """v1.23.2: the v1.23.1 deploy on the first demoted box restarted (= started) the replica's
    dormant mem0 and health-gated a store nobody reads. The role gate must sit BEFORE the restart,
    exit without restarting when the replica's mem0 is inactive, and skip the retrieval-families
    gate (which judges the authority's store) when a live travel-mode replica is restarted."""
    code = _code(REPO_ROOT / "scripts" / "wsl" / "deploy.sh")
    gate = code.index('if [ "${MEM0_ROLE:-brain}" = "replica" ]; then')
    smoke = code.index('python -c "import app"')
    restart = code.index("systemctl --user restart mem0.service")
    # v1.23.3: `import app` opens the Qdrant connection at import time, so the smoke can never pass
    # on a dormant replica — the gate must come BEFORE it (v1.23.2 had it after, and both replicas
    # stopped at the smoke with their files already synced).
    assert gate < smoke < restart, "the role gate must run before the import smoke and the restart"
    block = code[gate:smoke]
    assert "systemctl --user is-active --quiet mem0.service" in block
    assert "py_compile" in block, "a dormant replica still gets a byte-compile of the synced modules"
    assert "exit 0" in block, "a dormant replica stops after the file sync"
    assert "MEM0_SKIP_RETRIEVAL_GATE=1" in block
    assert code.index(". \"$HOME/.mem0/stack.env\"") < gate, "MEM0_ROLE comes from the sourced stack.env"


def test_authority_installer_restarts_a_running_server_on_a_rerun():
    """v1.23.3: `enable --now` does not restart an active unit; a re-run that stamps a new VERSION
    and syncs new modules must restart mem0 or the old code keeps serving under the new stamp."""
    sh = (REPO_ROOT / "install" / "linux-authority.sh").read_text(encoding="utf-8")
    stamp = sh.index('cp "$REPO_ROOT/VERSION" "$MEM0_APP/VERSION"')
    restart = sh.index("systemctl --user restart mem0.service")
    assert stamp < restart, "the restart must follow the VERSION stamp and module sync"
    assert "enable --now qdrant.service mem0.service" not in sh, "mem0 must not be started via enable --now (no restart on re-run)"


def test_stack_promote_follows_mem0_bind():
    code = _code(REPO_ROOT / "scripts" / "wsl" / "stack-promote.sh")
    assert LOOPBACK not in code
    assert "s/^MEM0_BIND=//p" in code
    assert 'MEM0_URL="${MEM0_URL:-http://${_bind:-127.0.0.1}:18791}"' in code


def test_canonize_reads_the_authority_file_before_loopback():
    code = _code(REPO_ROOT / "scripts" / "wsl" / "mem0-canonize.sh")
    assert code.count(LOOPBACK) == 1, "one literal: the last-resort default"
    assert 'MEM0="${MEM0_URL:-}"' in code
    assert "$HOME/.mem0/authority-url" in code
    assert 'MEM0="${MEM0:-http://127.0.0.1:18791}"' in code
