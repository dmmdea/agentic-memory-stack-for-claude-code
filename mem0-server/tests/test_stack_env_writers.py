"""1.31.1: ~/.mem0/stack.env must be a file every reader agrees on.

The native authority's installer wrote `MEM0_WIKI_SOURCES=<a> <b>` (space-separated, unquoted).
deploy.sh and storage-cap-check.sh SOURCE the file as bash, so the second word ran as a command
and `deploy.sh --dry-run` died on the first line it reached. Quoting alone is wrong: the other
readers do not unquote. They are the sed `stack_val` readers (wiki-index-nightly.sh,
stack-promote.sh, the installers' own inherit), `ams_env.stack_env()` and
`job_liveness.read_stack_env()`, which split on the first '=' and keep the rest verbatim.

The contract pinned here:
  1. list values are stored comma-separated (no whitespace), and the installer accepts either
     separator on input and on inherit (so an old space-separated line is rewritten, not kept);
  2. every writer refuses a value that is not a plain token: whitespace or any shell
     metacharacter aborts the install before anything is written;
  3. the rendered file is sourceable by `bash -c 'set -e; . stack.env'` and yields the SAME
     value for every key through bash, the sed reader, ams_env and job_liveness;
  4. every stack.env writer goes through the one writer (install/stack-env.sh).
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "install" / "linux-authority.sh"
LIB = REPO_ROOT / "install" / "stack-env.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(args, tmp_path, stack_env=None):
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True, exist_ok=True)
    if stack_env is not None:
        (home / ".mem0" / "stack.env").write_text(stack_env, encoding="utf-8")
    sec = tmp_path / "secrets"
    sec.mkdir(exist_ok=True)
    (sec / "ams-api-key.cred").write_bytes(b"x" * 64)
    (sec / "ams-canonical-key.cred").write_bytes(b"y" * 64)
    env = dict(os.environ)
    env["HOME"] = str(home)
    r = subprocess.run([BASH, str(SCRIPT), *args, "--secrets-dir", str(sec)],
                       capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120)
    return r, home


def _render(tmp_path, *flags, stack_env=None, name="render"):
    out = tmp_path / name
    r, _ = _run(["--bind-ip", "192.0.2.9", *flags, "--render-only", str(out)], tmp_path, stack_env=stack_env)
    return r, out / "stack.env"


def _lines(path):
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]


# --- (1) list values are comma-separated -----------------------------------------------------

@pytest.mark.parametrize("given", ["op@pc-a op@pc-b", "op@pc-a,op@pc-b", "op@pc-a, op@pc-b", " op@pc-a ,, op@pc-b "])
def test_wiki_sources_are_rendered_comma_separated(tmp_path, given):
    r, se = _render(tmp_path, "--wiki-sources", given, "--wiki-pull-key", "/k/wiki")
    assert r.returncode == 0, r.stderr
    assert "MEM0_WIKI_SOURCES=op@pc-a,op@pc-b" in _lines(se)


@pytest.mark.parametrize("old", ["op@pc-a op@pc-b", "op@pc-a,op@pc-b"], ids=["legacy-spaces", "commas"])
def test_inherit_round_trips_both_forms_to_commas(tmp_path, old):
    r, se = _render(tmp_path, stack_env=f"MEM0_WSL_USER=tenant\nMEM0_WIKI_SOURCES={old}\n")
    assert r.returncode == 0, r.stderr
    assert "--wiki-sources inherited from ~/.mem0/stack.env" in r.stdout
    assert "MEM0_WIKI_SOURCES=op@pc-a,op@pc-b" in _lines(se)
    # and a second re-run from the file just rendered is a fixed point
    (tmp_path / "home" / ".mem0" / "stack.env").write_text(se.read_text(encoding="utf-8"), encoding="utf-8")
    r2, se2 = _render(tmp_path, name="render2")
    assert r2.returncode == 0, r2.stderr
    assert se2.read_text(encoding="utf-8") == se.read_text(encoding="utf-8")


# --- (2) the generic guard ---------------------------------------------------------------------

@pytest.mark.parametrize("flag,value", [
    ("--pcloud-dir", "/mnt/My Drive/backups"),
    ("--zfs-dataset", "pool/ams;reboot"),
    ("--zfs-dataset", "pool/$(id)"),
    ("--pcloud-dir", "/x/`id`"),
    ("--wiki-pull-key", "/k/wiki'x"),
    ("--pcloud-dir", '/x/"y"'),
    ("--zfs-dataset", "pool/a|b"),
    ("--pcloud-dir", "~/backups"),
    ("--wiki-sources", "op@pc-a&op@pc-b"),
])
def test_installer_refuses_a_value_that_is_not_a_plain_token(tmp_path, flag, value):
    r, se = _render(tmp_path, flag, value)
    assert r.returncode != 0
    assert "whitespace or a shell metacharacter" in r.stderr, r.stderr
    assert not se.exists(), "nothing may be written when a value is refused"


def test_a_bad_value_is_refused_before_any_side_effect(tmp_path):
    """The check runs when the values are resolved, not when the file is written: a dry run
    (which writes nothing and would otherwise pass) must already refuse, so a real install never
    gets as far as installing Qdrant or the venv with a receipt it cannot write."""
    r, home = _run(["--bind-ip", "192.0.2.9", "--pcloud-dir", "/mnt/My Drive/x", "--dry-run"], tmp_path)
    assert r.returncode != 0
    assert "whitespace or a shell metacharacter" in r.stderr
    assert "[dry-run]" not in r.stdout


def test_an_inherited_bad_value_is_refused_too(tmp_path):
    r, se = _render(tmp_path, stack_env="MEM0_WSL_USER=tenant\nMEM0_PCLOUD_DIR=/mnt/My Drive/x\n")
    assert r.returncode != 0
    assert "whitespace or a shell metacharacter" in r.stderr


# --- (3) every reader sees the same file -------------------------------------------------------

def test_rendered_stack_env_is_sourceable_and_every_reader_agrees(tmp_path):
    ev = tmp_path / "eval-root"
    (ev / "eval" / "retrieval-drift").mkdir(parents=True)
    (ev / "eval" / "retrieval-drift" / "retrieval_drift.py").write_text("# stub\n", encoding="utf-8")
    r, se = _render(tmp_path, "--user-id", "tenantx", "--wiki-sources", "op@pc-a op@pc-b",
                    "--wiki-pull-key", "/k/id_wiki", "--eval-root", str(ev), "--pcloud-dir", "/srv/mirror",
                    "--zfs-dataset", "pool/apps/ams", "--embed-model", "embeddinggemma-ams")
    assert r.returncode == 0, r.stderr
    keys = [ln.split("=", 1)[0] for ln in _lines(se)]
    assert "MEM0_WIKI_SOURCES" in keys and "MEM0_PCLOUD_DIR" in keys

    # bash: set -e + source must succeed, and print each key back
    dump = "; ".join(f'printf "%s=%s\\n" {k} "${{{k}}}"' for k in keys)
    b = subprocess.run([BASH, "-c", f'set -e; . "{se.as_posix()}"; {dump}'], capture_output=True, text=True, timeout=30)
    assert b.returncode == 0, b.stderr
    via_bash = dict(ln.split("=", 1) for ln in b.stdout.splitlines())

    # the sed reader every shell consumer uses (stack_val / inherit_from_stack_env)
    via_sed = {}
    for k in keys:
        s = subprocess.run(["sed", "-n", f"s/^{k}=//p", se.as_posix()], capture_output=True, text=True, timeout=30)
        via_sed[k] = s.stdout.splitlines()[0] if s.stdout else ""

    ams_env = _load("ams_env_under_test", REPO_ROOT / "scripts" / "wsl" / "ams_env.py")
    ams_env._mem0_dir = lambda: se.parent  # read the rendered file, not the operator's
    real = se.parent / "stack.env"
    via_ams = ams_env.stack_env()
    assert real == se

    import sys
    sys.path.insert(0, str(REPO_ROOT / "mem0-server"))
    import job_liveness
    via_jl = job_liveness.read_stack_env(se)

    for k in keys:
        assert via_bash[k] == via_sed[k] == via_ams[k] == via_jl[k], (k, via_bash[k], via_sed[k], via_ams[k], via_jl[k])
    assert via_bash["MEM0_WIKI_SOURCES"] == "op@pc-a,op@pc-b"


# --- (4) one writer --------------------------------------------------------------------------

def test_stack_env_lib_parses_and_refuses_before_writing(tmp_path):
    assert subprocess.run([BASH, "-n", str(LIB)], capture_output=True, timeout=30).returncode == 0
    f = tmp_path / "stack.env"
    ok = subprocess.run([BASH, "-c", f'. "{LIB.as_posix()}"; stack_env_write "{f.as_posix()}" MEM0_A=x MEM0_B= MEM0_C=a,b'],
                        capture_output=True, text=True, timeout=30)
    assert ok.returncode == 0, ok.stderr
    assert f.read_text(encoding="utf-8") == "MEM0_A=x\nMEM0_B=\nMEM0_C=a,b\n"
    bad = subprocess.run([BASH, "-c", f'. "{LIB.as_posix()}"; stack_env_write "{f.as_posix()}" MEM0_A=new "MEM0_B=a b"'],
                         capture_output=True, text=True, timeout=30)
    assert bad.returncode != 0
    assert "whitespace or a shell metacharacter" in bad.stderr
    assert f.read_text(encoding="utf-8") == "MEM0_A=x\nMEM0_B=\nMEM0_C=a,b\n", "a refused write must leave the old file intact"
    lst = subprocess.run([BASH, "-c", f'. "{LIB.as_posix()}"; stack_env_list " a@x ,, b@y  c@z,"'],
                         capture_output=True, text=True, timeout=30)
    assert lst.stdout == "a@x,b@y,c@z"


@pytest.mark.parametrize("writer", ["install/1-wsl-services.sh", "install/linux-authority.sh", "install/linux-replica.sh"])
def test_every_stack_env_writer_goes_through_the_one_writer(writer):
    text = (REPO_ROOT / writer).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "stack-env.sh" in code, f"{writer} must source install/stack-env.sh"
    assert "stack_env_write " in code, f"{writer} must write stack.env through stack_env_write"
    assert not re.search(r"(cat|printf|echo)[^\n]*>>?\s*\"?\$[A-Z_]*[^\n]*stack\.env", code), \
        f"{writer} still writes stack.env directly"


def test_no_other_file_writes_stack_env():
    """The inventory behind test_every_stack_env_writer_goes_through_the_one_writer: a new
    writer must be added there (and route through stack_env_write), not appear silently."""
    writers = set()
    for p in list(REPO_ROOT.glob("install/*.sh")) + list(REPO_ROOT.glob("scripts/**/*.sh")) + \
             list(REPO_ROOT.glob("claude-config/*.sh")):
        code = "\n".join(ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                         if not ln.lstrip().startswith("#"))
        if re.search(r">>?\s*\"?[^\s\"]*stack\.env", code) or "stack_env_write " in code:
            writers.add(p.relative_to(REPO_ROOT).as_posix())
    writers.discard("install/stack-env.sh")
    assert writers == {"install/1-wsl-services.sh", "install/linux-authority.sh", "install/linux-replica.sh"}, writers


# --- the sourcing consumers, and the legacy line as a negative control -----------------------

SOURCERS = ["scripts/wsl/deploy.sh", "claude-config/storage-cap-check.sh",
            "scripts/wsl/stack-backup-manifest.sh", "scripts/wsl/ensure-codex-shim.sh"]


def test_the_sourcing_consumers_are_the_ones_listed():
    """Every script that sources stack.env; a new one must be added to SOURCERS."""
    found = set()
    for p in list(REPO_ROOT.glob("scripts/**/*.sh")) + list(REPO_ROOT.glob("claude-config/*.sh")) + \
             list(REPO_ROOT.glob("install/*.sh")):
        if re.search(r'(^|[\s;&|])\.\s+"\$HOME/\.mem0/stack\.env"', p.read_text(encoding="utf-8", errors="replace"), re.M):
            found.add(p.relative_to(REPO_ROOT).as_posix())
    assert found == set(SOURCERS), found


def _source_like(script, stack_env_path):
    """Run the consumer's own sourcing statement (deploy.sh runs under set -euo pipefail)."""
    home = stack_env_path.parent.parent
    return subprocess.run([BASH, "-c", 'set -euo pipefail; . "$HOME/.mem0/stack.env"; printf "%s" "${MEM0_WIKI_SOURCES:-}"'],
                          capture_output=True, text=True, timeout=30, env={**os.environ, "HOME": str(home)})


def test_every_sourcing_consumer_accepts_the_rendered_receipt_and_rejected_the_legacy_line(tmp_path):
    r, se = _render(tmp_path, "--wiki-sources", "op@pc-a op@pc-b", "--wiki-pull-key", "/k/wiki")
    assert r.returncode == 0, r.stderr
    home = tmp_path / "srchome"
    (home / ".mem0").mkdir(parents=True)
    target = home / ".mem0" / "stack.env"
    target.write_text(se.read_text(encoding="utf-8"), encoding="utf-8")
    ok = _source_like("deploy.sh", target)
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout == "op@pc-a,op@pc-b"
    # negative control: the pre-1.31.1 line fails exactly the way the brain did
    target.write_text("MEM0_ROLE=brain\nMEM0_WIKI_SOURCES=op@pc-a op@pc-b\n", encoding="utf-8")
    bad = _source_like("deploy.sh", target)
    assert bad.returncode != 0
    assert "op@pc-b: command not found" in bad.stderr
