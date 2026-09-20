"""wiki-index.sh — the replica wrapper's contract (docs/systems/wiki-index.md): the snapshot
from a tar, the tunnel opened and CLOSED around a build/search, the alias resolution order.
ssh and the builder are fakes on PATH under a scratch HOME.

1.30.1: the EXIT trap read a `local` that no longer existed and died "unbound variable",
leaving the tunnel open; this file is the test that was missing.

Run: python3 -m pytest scripts/wsl/test_wiki_index_wrapper.py -q  (bash + tar only)
"""
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "wiki-index.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or shutil.which("tar") is None, reason="bash/tar not available")

FAKE_SSH = """#!/usr/bin/env bash
# records every call; a tunnel open (-f -N -M) and a control close (-O exit) both succeed
echo "ssh $*" >> "$FAKE_LOG"
exit 0
"""
FAKE_PY = """#!/usr/bin/env bash
echo "py root=${WIKI_ROOT:-} host=${WIKI_QDRANT_HOST:-} port=${WIKI_QDRANT_PORT:-} args=$*" >> "$FAKE_LOG"
"""


def _env(tmp_path: Path, extra=None):
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True, exist_ok=True)
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    for name, body in (("ssh", FAKE_SSH), ("fakepy", FAKE_PY)):
        f = b / name
        f.write_text(body, encoding="utf-8")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = dict(os.environ)
    env.update({"HOME": str(home), "PATH": f"{b}{os.pathsep}{env['PATH']}", "FAKE_LOG": str(log),
                "WIKI_PY": str(b / "fakepy")})
    env.pop("WIKI_BRAIN_SSH", None)
    env.update(extra or {})
    return env, home, log


def _run(env, *args, stdin=None):
    return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, env=env,
                          input=stdin, timeout=60, check=False)


def _wiki_tar(tmp_path: Path, pages: int) -> bytes:
    src = tmp_path / "vault" / "wiki" / "entities"
    src.mkdir(parents=True, exist_ok=True)
    for i in range(pages):
        (src / f"Page {i}.md").write_text(f"# Page {i}\n\ntext\n", encoding="utf-8")
    out = tmp_path / "wiki.tar"
    with tarfile.open(out, "w") as tf:
        tf.add(tmp_path / "vault" / "wiki", arcname="wiki")
    return out.read_bytes()


def _snapshot(home: Path):
    d = home / "wiki-index" / "wiki" / "entities"
    d.mkdir(parents=True, exist_ok=True)
    (d / "A.md").write_text("# A\n\na\n", encoding="utf-8")
    return home / "wiki-index" / "wiki"


def test_snapshot_from_a_tar_and_refuses_an_empty_one(tmp_path):
    env, home, _ = _env(tmp_path)
    r = subprocess.run([BASH, str(SCRIPT), "snapshot"], capture_output=True, env=env,
                       input=_wiki_tar(tmp_path, 2), timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    assert b"snapshot 2 pages" in r.stdout
    assert sorted(p.name for p in (home / "wiki-index" / "wiki").rglob("*.md")) == ["Page 0.md", "Page 1.md"]
    empty = tmp_path / "empty.tar"
    (tmp_path / "e" / "wiki").mkdir(parents=True)
    with tarfile.open(empty, "w") as tf:
        tf.add(tmp_path / "e" / "wiki", arcname="wiki")
    r = subprocess.run([BASH, str(SCRIPT), "snapshot"], capture_output=True, env=env,
                       input=empty.read_bytes(), timeout=60, check=False)
    assert r.returncode == 1
    assert (home / "wiki-index" / "wiki" / "entities" / "Page 0.md").exists()  # kept


def test_build_opens_the_tunnel_runs_the_builder_and_closes_the_tunnel(tmp_path):
    env, home, log = _env(tmp_path, {"WIKI_BRAIN_SSH": "brainhost"})
    snap = _snapshot(home)
    r = _run(env, "build")
    assert r.returncode == 0, r.stderr
    assert "unbound" not in r.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    opens = [c for c in calls if c.startswith("ssh -f -N -M") and "-L 16333:127.0.0.1:6333 brainhost" in c]
    closes = [c for c in calls if "-O exit brainhost" in c]
    builds = [c for c in calls if c.startswith("py ")]
    assert len(opens) == 1 and len(closes) == 1 and len(builds) == 1, calls
    assert calls.index(opens[0]) < calls.index(builds[0]) < calls.index(closes[0])
    assert f"root={snap} host=127.0.0.1 port=16333" in builds[0] and "wiki-index-build.py" in builds[0]


def test_search_passes_the_query_and_k_and_closes_the_tunnel(tmp_path):
    env, _, log = _env(tmp_path, {"WIKI_BRAIN_SSH": "brainhost", "WIKI_TUNNEL_PORT": "17000"})
    r = _run(env, "search", "where is the brain", "--k", "3")
    assert r.returncode == 0, r.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert any("-L 17000:127.0.0.1:6333 brainhost" in c for c in calls)
    assert any(c.startswith("py ") and "port=17000" in c and "wiki-search.py where is the brain --k 3" in c for c in calls)
    assert any("-O exit brainhost" in c for c in calls)


def test_alias_resolves_from_stack_env_then_ssh_config_then_the_host(tmp_path):
    env, home, log = _env(tmp_path)
    _snapshot(home)
    (home / ".mem0" / "authority-url").write_text("http://brain-box:18791\n")
    # 1. ssh config Host whose HostName is the authority host
    (home / ".ssh").mkdir()
    (home / ".ssh" / "config").write_text("Host other\n    HostName elsewhere\n\nHost mybrain\n    HostName brain-box\n    User svc\n")
    r = _run(env, "build")
    assert r.returncode == 0, r.stderr
    assert "-L 16333:127.0.0.1:6333 mybrain" in log.read_text(encoding="utf-8")
    # 2. stack.env wins over the ssh config
    log.write_text("")
    (home / ".mem0" / "stack.env").write_text("MEM0_ROLE=replica\nMEM0_BRAIN_SSH=fromenv\n")
    r = _run(env, "build")
    assert r.returncode == 0, r.stderr
    assert "-L 16333:127.0.0.1:6333 fromenv" in log.read_text(encoding="utf-8")
    # 3. nothing configured, no matching Host: the authority host itself
    log.write_text("")
    (home / ".mem0" / "stack.env").write_text("MEM0_ROLE=replica\n")
    (home / ".ssh" / "config").write_text("Host other\n    HostName elsewhere\n")
    r = _run(env, "build")
    assert r.returncode == 0, r.stderr
    assert "-L 16333:127.0.0.1:6333 brain-box" in log.read_text(encoding="utf-8")


def test_build_without_a_snapshot_or_alias_fails_loudly(tmp_path):
    env, home, log = _env(tmp_path, {"WIKI_BRAIN_SSH": "brainhost"})
    r = _run(env, "build")
    assert r.returncode == 1 and "no snapshot" in r.stderr
    env2, home2, log2 = _env(tmp_path / "second")
    _snapshot(home2)
    r = _run(env2, "build")
    assert r.returncode == 1 and "no brain alias" in r.stderr
    assert not log2.exists()
