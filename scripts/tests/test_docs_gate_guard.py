"""The docs gate refuses guidance that puts a model resident or on the CPU.

Every idle model unloads at ttl 300 and runs on the GPU (RAM is overflow only), so a
recommendation of a zero ttl, a persistent always-loaded group or zero GPU layers is a defect
wherever it appears. The gate scans every tracked file; CHANGELOG.md alone is exempt because it
records history. Each test builds a throwaway git repo around a COPY of the gate, so nothing
here depends on the state of the real tree.

The forbidden strings are assembled from parts so this file does not trip the gate it tests.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "ci" / "check-docs.py"

ZERO_TTL = "ttl" + ": 0"
RESIDENT = "always" + "_loaded"
CPU_ONLY = "--n-gpu" + "-layers 0"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _run_gate(tmp_path, files):
    """Track `files` (relpath -> text) in a fresh repo holding a copy of the gate; run it."""
    (tmp_path / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(GATE, tmp_path / "scripts" / "ci" / "check-docs.py")
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    return subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / "ci" / "check-docs.py")],
        capture_output=True, text=True,
    )


@pytest.mark.parametrize(
    "rel,line",
    [
        ("install/setup.sh", f'echo "    {ZERO_TTL}"'),
        ("skill/notes.md", f"    {ZERO_TTL}    # never auto-unload"),
        ("docs/systems/x.md", f"Put both models in the {RESIDENT} group."),
        ("mem0-server/mod.py", f"# served from the {RESIDENT} persistent group"),
        ("install/llama.md", f"--embeddings --pooling mean {CPU_ONLY}"),
        ("install/llama2.md", "--embeddings --n-gpu" + "-layers=0 --ctx-size 2048"),
    ],
)
def test_forbidden_guidance_fails_the_gate_anywhere_in_the_tree(tmp_path, rel, line):
    r = _run_gate(tmp_path, {rel: line + "\n"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"{rel}:1: forbidden guidance" in r.stdout


def test_changelog_is_the_only_exemption(tmp_path):
    hist = f"- removed the {RESIDENT} group and {ZERO_TTL} from the installer hint\n"
    r = _run_gate(tmp_path, {"CHANGELOG.md": hist})
    assert r.returncode == 0, r.stdout + r.stderr
    r2 = _run_gate(tmp_path / "second", {"docs/CHANGELOG.md": hist, "notes/CHANGELOG.md.bak": hist})
    assert r2.returncode == 1
    assert "docs/CHANGELOG.md:1" in r2.stdout and "notes/CHANGELOG.md.bak:1" in r2.stdout


def test_compliant_guidance_passes(tmp_path):
    ok = (
        "ttl: 300\n"
        "--n-gpu-layers 999\n"
        + "ttl" + ": 0.5 is not a zero ttl\n"
        "groups:\n  support:\n    swap: false\n    exclusive: false\n"
    )
    r = _run_gate(tmp_path, {"install/llama-swap-setup.md": ok})
    assert r.returncode == 0, r.stdout + r.stderr


def test_binary_and_untracked_files_are_ignored(tmp_path):
    r = _run_gate(tmp_path, {"docs/README.md": "ok\n"})
    (tmp_path / "scratch.txt").write_text(ZERO_TTL, encoding="utf-8")  # untracked
    (tmp_path / "blob.bin").write_bytes(b"\0" + ZERO_TTL.encode())
    _git(tmp_path, "add", "blob.bin")
    r = subprocess.run([sys.executable, str(tmp_path / "scripts" / "ci" / "check-docs.py")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_gate_and_this_test_do_not_trip_themselves():
    """The patterns are spelled so their own source is clean (no exemption needed)."""
    for path in (GATE, Path(__file__)):
        text = path.read_text(encoding="utf-8")
        for needle in (ZERO_TTL, RESIDENT, CPU_ONLY):
            assert needle not in text, f"{path.name} contains {needle!r}"
