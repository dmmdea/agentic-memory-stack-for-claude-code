"""Dependency floors: one set, no caps, and the installer proves it.

The server's Python dependencies are floors (a minimum that carries the security fixes), never
caps or exact pins: a cap is what kept cryptography below its fixed release for weeks, and
the requirements file and the installer disagreed about mem0ai. mem0-server/requirements.txt
documents the set, install/1-wsl-services.sh enforces it (two pip lines and a post-condition),
and these tests hold the two together so they cannot drift apart again.
"""
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQ = (ROOT / "mem0-server" / "requirements.txt").read_text(encoding="utf-8")
INSTALLER = (ROOT / "install" / "1-wsl-services.sh").read_text(encoding="utf-8")

FLOORS = {
    "cryptography": ">=50.0.1",
    "mem0ai[nlp]": ">=2.0.4",
    "starlette": ">=1.3.1",
}


def _requirement_specs():
    specs = {}
    for line in REQ.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            m = re.match(r"^([A-Za-z0-9_.\-]+(?:\[[^\]]+\])?)(.*)$", line)
            specs[m.group(1)] = m.group(2).strip()
    return specs


def _pip_line(pattern):
    hits = [ln for ln in INSTALLER.splitlines() if re.search(pattern, ln)]
    assert len(hits) == 1, f"expected exactly one installer pip line matching {pattern!r}, got {hits}"
    tokens = shlex.split(hits[0].split(" install ", 1)[1].split(" || ")[0])
    tokens = [t for t in tokens if not t.startswith("--")]
    out = {}
    for t in tokens:
        m = re.match(r"^([A-Za-z0-9_.\-]+(?:\[[^\]]+\])?)(.*)$", t)
        out[m.group(1)] = m.group(2)
    return out


FRESH = _pip_line(r"pip install --quiet 'mem0ai")
REFRESH = _pip_line(r'\.venv/bin/pip" install --quiet ')


def test_requirements_and_both_installer_lines_carry_the_same_floors():
    req = _requirement_specs()
    # requirements.txt lists mem0ai's extra as `mem0ai[nlp]`, exactly as the installer does
    for name, floor in FLOORS.items():
        assert req.get(name) == floor, f"requirements.txt: {name} is {req.get(name)!r}, want {floor!r}"
        assert FRESH.get(name) == floor, f"installer fresh line: {name} is {FRESH.get(name)!r}, want {floor!r}"
        assert REFRESH.get(name) == floor, f"installer refresh line: {name} is {REFRESH.get(name)!r}, want {floor!r}"


def test_pip_audit_is_installed_on_both_installer_branches_and_listed():
    assert "pip-audit" in FRESH and "pip-audit" in REFRESH
    assert "pip-audit" in _requirement_specs()


def test_no_cap_or_exact_pin_survives_in_the_dependency_sources():
    for name, text in (("requirements.txt", REQ), ("1-wsl-services.sh", INSTALLER)):
        assert "<49" not in text, f"{name}: the cryptography cap is back"
        assert not re.search(r"mem0ai(\[[a-z]+\])?==", text), f"{name}: mem0ai is exact-pinned again"
    for spec in list(_requirement_specs().items()) + list(FRESH.items()) + list(REFRESH.items()):
        assert "==" not in spec[1] and "<" not in spec[1].replace("<=", ""), f"capped or pinned: {spec}"


def test_installer_post_condition_asserts_the_floors_pip_check_and_pip_audit():
    """The post-condition runs in a real venv, which a unit test cannot build; pin its text."""
    m = re.search(r"<<'PYEOF'.*?\nPYEOF\n", INSTALLER.split("Post-conditions for BOTH branches", 1)[1], re.S)
    assert m, "post-condition block not found"
    block = m.group(0)
    for name, floor in (("starlette", "1.3.1"), ("cryptography", "50.0.1"), ("mem0ai", "2.0.4")):
        assert f'"{name}": "{floor}"' in block, f"post-condition does not assert {name}>={floor}"
    assert '"pip", "check"' in block, "post-condition must run pip check"
    assert "pip-audit" in block, "post-condition must require pip-audit"
    assert "sys.exit(0 if ok else 1)" in block


def test_no_dangling_pointers_to_private_ledgers_in_dependency_sources():
    for name, text in (("requirements.txt", REQ), ("1-wsl-services.sh", INSTALLER)):
        assert "VERSIONS.md" not in text and "UPGRADE.md" not in text, f"{name} cites a file that is not in this repo"


def test_unit_descriptions_carry_no_version_numbers():
    for unit in ("qdrant.service", "mem0.service"):
        desc = next(
            ln for ln in (ROOT / "systemd" / unit).read_text(encoding="utf-8").splitlines()
            if ln.startswith("Description=")
        )
        assert not re.search(r"\bv?\d+\.\d+", desc), f"{unit}: {desc!r} hard-codes a version that goes stale"
