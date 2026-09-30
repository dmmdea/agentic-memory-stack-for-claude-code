"""scripts/upgrade-check.sh must report what it actually saw.

Two legs of the inventory used to fail silently: a missing pip-audit printed a bare "REVIEW:"
with no rows (which reads like "nothing found"), and the llama-swap version regex could not
match `version: v256 (...)`, so the installed version was always unknown. These tests run the
real script against stubbed tools (pip, pip-audit, curl, npm, llama-swap) on a private PATH;
nothing touches the network, a real venv or a running service.
"""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "upgrade-check.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None or os.name == "nt", reason="needs a POSIX bash")

OSV_VULN = {"results": [{"vulns": [{"id": "GHSA-aaaa-bbbb-cccc"}, {"id": "PYSEC-2026-1"}]}, {}]}
OSV_CLEAN = {"results": [{}, {}]}
PIP_LIST = [{"name": "cryptography", "version": "48.0.1"}, {"name": "starlette", "version": "1.6.0"}]


def _stub(path: Path, body: str):
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class Env:
    """A sandbox: fake venv, fake curl/npm on PATH, log directory."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.venv = tmp / "venv"
        self.log = tmp / "log"
        for d in (self.bin, self.venv / "bin", self.log):
            d.mkdir(parents=True)
        _stub(self.venv / "bin" / "pip", (
            'if [ "$1 $2" = "list --outdated" ]; then echo "[]"; exit 0; fi\n'
            f"echo '{json.dumps(PIP_LIST)}'\n"
        ))
        self.set_osv(OSV_CLEAN)
        (self.tmp / "github-tag").write_text("v256", encoding="utf-8")
        _stub(self.bin / "curl", f'''
args="$*"
case "$args" in
  *api.osv.dev*)
    cat > "{self.log}/osv-payload.json"
    echo osv >> "{self.log}/curl-calls"
    [ -f "{self.tmp}/osv-down" ] && exit 7
    cat "{self.tmp}/osv-response.json" ;;
  *api.github.com/repos/mostlygeek/llama-swap*)
    echo "{{\\"tag_name\\":\\"$(cat "{self.tmp}/github-tag")\\"}}" ;;
  *api.github.com/repos/qdrant/qdrant*) echo '{{"tag_name":"v1.19.1"}}' ;;
  *localhost:6333*) echo '{{"version":"1.19.1"}}' ;;
  *) exit 7 ;;
esac
''')
        _stub(self.bin / "npm", 'echo 0.155.1\n')

    def set_osv(self, doc):
        (self.tmp / "osv-response.json").write_text(json.dumps(doc), encoding="utf-8")

    def osv_down(self):
        (self.tmp / "osv-down").write_text("1", encoding="utf-8")

    def pip_audit(self, body):
        _stub(self.venv / "bin" / "pip-audit", body)

    def llama_swap(self, version_line):
        _stub(self.bin / "llama-swap", f"echo '{version_line}'\n")

    def run(self):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "MEM0_VENV": str(self.venv),
            "LLAMA_SWAP_BIN": str(self.bin / "llama-swap"),
            "LC_ALL": "C",
        }
        return subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)

    def curl_calls(self):
        f = self.log / "curl-calls"
        return f.read_text().split() if f.exists() else []


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.llama_swap("version: v256 (6701d0d), built at 2026-09-17T07:09:27Z")
    return e


# ---- security leg -------------------------------------------------------------------------

def test_missing_pip_audit_is_unavailable_and_exits_nonzero(env):
    env.osv_down()
    r = env.run()
    assert "security scan UNAVAILABLE" in r.stdout
    assert r.returncode != 0
    assert "REVIEW:" not in r.stdout, "a scan that did not run must never read as a (possibly empty) review"
    assert "## llama-swap" in r.stdout, "the rest of the inventory must still print"


def test_missing_pip_audit_falls_back_to_osv_querybatch(env):
    env.set_osv(OSV_VULN)
    r = env.run()
    assert "security scan UNAVAILABLE" in r.stdout, "a missing scanner is loud even when the fallback works"
    assert r.returncode != 0
    assert "OSV" in r.stdout
    assert "cryptography==48.0.1" in r.stdout
    assert "GHSA-aaaa-bbbb-cccc" in r.stdout and "PYSEC-2026-1" in r.stdout
    payload = json.loads((env.log / "osv-payload.json").read_text())
    assert payload["queries"][0] == {"package": {"name": "cryptography", "ecosystem": "PyPI"}, "version": "48.0.1"}
    assert len(payload["queries"]) == len(PIP_LIST)


def test_osv_fallback_reports_clean_when_no_advisories(env):
    env.set_osv(OSV_CLEAN)
    r = env.run()
    assert "no known advisories" in r.stdout
    assert "GHSA" not in r.stdout


def test_pip_audit_clean_is_clean_and_skips_the_fallback(env):
    env.pip_audit('echo "No known vulnerabilities found" >&2\nexit 0\n')
    r = env.run()
    assert r.returncode == 0, r.stdout
    assert "clean (no known CVEs)" in r.stdout
    assert "UNAVAILABLE" not in r.stdout
    assert env.curl_calls() == []


def test_pip_audit_findings_are_listed(env):
    env.pip_audit(
        'echo "Found 1 known vulnerability in 1 package" >&2\n'
        'echo "Name Version ID Fix Versions"\n'
        'echo "cryptography 48.0.1 GHSA-g6cj-pr64-35w5 50.0.0"\n'
        "exit 1\n"
    )
    r = env.run()
    assert "REVIEW:" in r.stdout
    assert "GHSA-g6cj-pr64-35w5" in r.stdout
    assert "UNAVAILABLE" not in r.stdout


def test_pip_audit_that_dies_without_a_verdict_is_unavailable(env):
    env.pip_audit('echo "Traceback: boom" >&2\nexit 2\n')
    r = env.run()
    assert "security scan UNAVAILABLE" in r.stdout
    assert r.returncode != 0
    assert "REVIEW:" not in r.stdout


def test_missing_venv_is_unavailable_and_exits_nonzero(env):
    shutil.rmtree(env.venv)
    r = env.run()
    assert "security scan UNAVAILABLE" in r.stdout
    assert r.returncode != 0


# ---- llama-swap leg -----------------------------------------------------------------------

def test_llama_swap_version_with_v_prefix_is_read(env):
    env.pip_audit('echo "No known vulnerabilities found" >&2\n')
    r = env.run()
    assert "installed 256 | latest 256 | CURRENT" in r.stdout, r.stdout


def test_llama_swap_old_format_without_v_is_read(env):
    env.pip_audit('echo "No known vulnerabilities found" >&2\n')
    env.llama_swap("version: 230 (abc1234), built at 2026-05-01T00:00:00Z")
    r = env.run()
    assert "installed 230 | latest 256 | UPDATE" in r.stdout, r.stdout


def test_llama_swap_unreadable_version_is_unknown_not_update(env):
    env.pip_audit('echo "No known vulnerabilities found" >&2\n')
    env.llama_swap("something unexpected")
    r = env.run()
    assert "installed ? |" in r.stdout
    assert "UNKNOWN" in r.stdout
    assert "| UPDATE" not in r.stdout


# ---- policy text --------------------------------------------------------------------------

def test_script_documents_floors_not_caps_and_points_at_nothing_private():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "<49" not in text
    assert "==2.0.4" not in text
    assert "VERSIONS.md" not in text and "UPGRADE.md" not in text
