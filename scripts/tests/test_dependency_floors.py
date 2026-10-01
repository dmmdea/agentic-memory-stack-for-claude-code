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
    "pyjwt": ">=2.15.0",
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


def _raw_pip_specs(pattern):
    """The pip arguments of the installer line matching `pattern`, exactly as bash will see them."""
    hits = [ln for ln in INSTALLER.splitlines() if re.search(pattern, ln)]
    assert len(hits) == 1, f"expected exactly one installer pip line matching {pattern!r}, got {hits}"
    return hits[0].split(" install ", 1)[1].split(" || ")[0]


def test_every_comparison_spec_on_the_installer_pip_lines_is_quoted_in_the_raw_line():
    """Unquoted `pkg>=1.2` is a shell redirect: bash installs unpinned `pkg` and writes a file named
    `=1.2`. The shlex tokenizing above hides that (it never redirects), so look at the RAW text:
    once the quoted spans are removed, no `<` or `>` may remain in the pip arguments."""
    for label, pattern in (("fresh", r"pip install --quiet 'mem0ai"), ("refresh", r'\.venv/bin/pip" install --quiet ')):
        raw = _raw_pip_specs(pattern)
        unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "", raw)
        assert "<" not in unquoted and ">" not in unquoted, (
            f"installer {label} pip line has a version comparison outside quotes (a shell redirect, "
            f"not a floor): {raw}")
        for floor_name, floor in FLOORS.items():
            assert f"'{floor_name}{floor}'" in raw, f"installer {label} line: {floor_name}{floor} must be single-quoted"


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
    for name, floor in (("starlette", "1.3.1"), ("cryptography", "50.0.1"), ("pyjwt", "2.15.0"), ("mem0ai", "2.0.4")):
        assert f'"{name}": "{floor}"' in block, f"post-condition does not assert {name}>={floor}"
    assert '"pip", "check"' in block, "post-condition must run pip check"
    assert "pip-audit" in block, "post-condition must require pip-audit"
    assert "sys.exit(0 if ok else 1)" in block


def test_the_fatal_text_and_the_success_echo_name_every_floor_the_post_condition_asserts():
    fatal = re.search(r'PYEOF\' \|\| \{ echo "([^"]*)"', INSTALLER).group(1)
    ok = re.search(r'^echo "  post-conditions satisfied \(([^)]*)\)"$', INSTALLER, re.M).group(1)
    for text in (fatal, ok):
        for floor in ("starlette>=1.3.1", "cryptography>=50.0.1", "pyjwt>=2.15.0", "mem0ai>=2.0.4"):
            assert floor in text, f"{floor} missing from: {text}"


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


# ---- the post-condition, executed (not only text-pinned) ----------------------------------

import subprocess
import sys
import tempfile
import textwrap


def _postcondition_source() -> str:
    m = re.search(r"<<'PYEOF'[^\n]*\n(.*?)\nPYEOF\n", INSTALLER.split("Post-conditions for BOTH branches", 1)[1], re.S)
    assert m, "post-condition block not found"
    return m.group(1)


def _run_postcondition(tmp_path, versions, pip_check_rc=0, pip_check_out="", with_audit=True):
    """Run the installer's real post-condition block against a stubbed venv.

    The block reads installed versions (importlib.metadata), runs `python -m pip check`, looks
    for pip-audit beside sys.executable, and imports fastmcp/fastembed. Each is replaced by a
    stub so the LOGIC (which combinations exit non-zero, and what is printed) is what runs.
    """
    tmp_path = Path(tempfile.mkdtemp(dir=tmp_path))  # one private venv dir per call
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").write_text("", encoding="utf-8")
    if with_audit:
        audit = bindir / "pip-audit"
        audit.write_text("#!/bin/sh\n", encoding="utf-8")
        audit.chmod(0o755)
    prelude = textwrap.dedent(f"""
        import importlib.metadata as md, subprocess, sys, types
        _v = {versions!r}
        def _version(name):
            if name not in _v:
                raise md.PackageNotFoundError(name)
            return _v[name]
        md.version = _version
        sys.executable = {str(bindir / "python")!r}
        _real_run = subprocess.run
        def _run(cmd, *a, **k):
            if list(cmd)[1:4] == ["-m", "pip", "check"]:
                return subprocess.CompletedProcess(cmd, {pip_check_rc}, stdout={pip_check_out!r}, stderr="")
            return _real_run(cmd, *a, **k)
        subprocess.run = _run
        fm = types.ModuleType("fastmcp"); fm.FastMCP = object; sys.modules["fastmcp"] = fm
        fe = types.ModuleType("fastembed")
        class SparseTextEmbedding:
            def __init__(self, **kw): pass
        fe.SparseTextEmbedding = SparseTextEmbedding; sys.modules["fastembed"] = fe
    """)
    script = tmp_path / "postcondition.py"
    script.write_text(prelude + _postcondition_source(), encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True)


MET = {"starlette": "1.6.0", "cryptography": "50.0.1", "pyjwt": "2.15.1", "mem0ai": "2.1.0"}


def test_postcondition_passes_when_floors_met_pip_check_clean_and_audit_present(tmp_path):
    r = _run_postcondition(tmp_path, MET)
    assert r.returncode == 0, r.stderr


def test_postcondition_fails_on_each_broken_leg(tmp_path):
    below = _run_postcondition(tmp_path, {**MET, "cryptography": "48.0.1"})
    assert below.returncode == 1 and "cryptography 48.0.1 is below the floor 50.0.1" in below.stderr
    absent = _run_postcondition(tmp_path, {k: v for k, v in MET.items() if k != "mem0ai"})
    assert absent.returncode == 1 and "mem0ai is not installed" in absent.stderr
    # the pyjwt floor is asserted like the others: the vulnerable 2.14.0, and an absent package
    old_jwt = _run_postcondition(tmp_path, {**MET, "pyjwt": "2.14.0"})
    assert old_jwt.returncode == 1 and "pyjwt 2.14.0 is below the floor 2.15.0" in old_jwt.stderr
    no_jwt = _run_postcondition(tmp_path, {k: v for k, v in MET.items() if k != "pyjwt"})
    assert no_jwt.returncode == 1 and "pyjwt is not installed (floor 2.15.0)" in no_jwt.stderr
    dirty = _run_postcondition(tmp_path, MET, pip_check_rc=1, pip_check_out="thinc 8.3 has requirement x")
    assert dirty.returncode == 1 and "pip check is not clean" in dirty.stderr
    noaudit = _run_postcondition(tmp_path, MET, with_audit=False)
    assert noaudit.returncode == 1 and "pip-audit is not installed" in noaudit.stderr


def test_a_conflict_or_missing_scanner_names_its_remedy_not_just_the_network(tmp_path):
    """An unrelated resolver conflict fails every re-run; the message must say how to clear it."""
    dirty = _run_postcondition(tmp_path, MET, pip_check_rc=1, pip_check_out="thinc 8.3 has requirement x")
    assert "not a network problem" in dirty.stderr and "pip install" in dirty.stderr
    noaudit = _run_postcondition(tmp_path, MET, with_audit=False)
    assert "pip install pip-audit" in noaudit.stderr
    fatal = re.search(r'PYEOF\' \|\| \{ echo "([^"]*)"', INSTALLER).group(1)
    assert "pip check" in fatal and "network" in fatal and "conflict" in fatal
