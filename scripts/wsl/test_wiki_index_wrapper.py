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
    drive, tail = os.path.splitdrive(str(home))
    # HOME plus the Windows variables, so no platform resolves ~ to the real profile.
    env.update({"HOME": str(home), "USERPROFILE": str(home), "HOMEDRIVE": drive, "HOMEPATH": tail,
                "PATH": f"{b}{os.pathsep}{env['PATH']}", "FAKE_LOG": str(log),
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


# ---- WP-8: a session build stamps last-build on the brain (freshness for either refresh path) ----
def test_build_stamps_last_build_on_the_brain_through_the_open_tunnel(tmp_path):
    env, home, log = _env(tmp_path, {"WIKI_BRAIN_SSH": "brainhost"})
    _snapshot(home)
    r = _run(env, "build")
    assert r.returncode == 0, r.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    opens = [c for c in calls if c.startswith("ssh -f -N -M")]
    builds = [c for c in calls if c.startswith("py ")]
    stamps = [c for c in calls if "last-build" in c]
    closes = [c for c in calls if "-O exit brainhost" in c]
    assert len(stamps) == 1, calls
    assert "brainhost" in stamps[0] and "-S " in stamps[0], "reuses the control socket to the brain"
    assert "wiki-index/last-build" in stamps[0]
    assert calls.index(opens[0]) < calls.index(builds[0]) < calls.index(stamps[0]) < calls.index(closes[0])


def test_a_failed_build_is_not_stamped(tmp_path):
    env, home, log = _env(tmp_path, {"WIKI_BRAIN_SSH": "brainhost"})
    _snapshot(home)
    failing = tmp_path / "bin" / "fakepy"
    failing.write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
    r = _run(env, "build")
    assert r.returncode == 3
    assert not any("last-build" in c for c in log.read_text(encoding="utf-8").splitlines())


def test_a_stamp_failure_warns_but_does_not_fail_the_build(tmp_path):
    env, home, log = _env(tmp_path, {"WIKI_BRAIN_SSH": "brainhost"})
    _snapshot(home)
    (tmp_path / "bin" / "ssh").write_text(
        '#!/usr/bin/env bash\necho "ssh $*" >> "$FAKE_LOG"\ncase "$*" in *last-build*) exit 255;; esac\nexit 0\n',
        encoding="utf-8")
    r = _run(env, "build")
    assert r.returncode == 0, r.stderr
    assert "could not stamp last-build" in r.stderr


# ---------------------------------------------------------------------------- the wiki's own space
# The authority reports its wiki space on /health/deep (embed_profile.wiki.profile). A replica that
# serves that space's model builds and searches here, pinned to it; one that does not hands the
# snapshot or the query to the brain, which embeds with its own model.

FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
for a in "$@"; do
  case "$a" in
    */health/deep) printf '%s' "${FAKE_DEEP:-{\}}"; exit 0 ;;
    */models) printf '{"data":[{"id":"%s"}]}' "${FAKE_SERVED:-}"; exit 0 ;;
  esac
done
exit 0
"""
SMART_PY = """#!/usr/bin/env bash
if [ "${1:-}" = "-c" ]; then
  case "$2" in
    *embed_model*) echo "${FAKE_ALIAS:-}"; exit 0 ;;
    *embed_profile*) cat >/dev/null; echo "${FAKE_WIKI_PROFILE:-}"; exit 0 ;;
  esac
fi
echo "py root=${WIKI_ROOT:-} host=${WIKI_QDRANT_HOST:-} port=${WIKI_QDRANT_PORT:-} wiki_profile=${MEM0_WIKI_EMBED_PROFILE:-} args=$*" >> "$FAKE_LOG"
"""
SINK_SSH = """#!/usr/bin/env bash
echo "ssh $*" >> "$FAKE_LOG"
case "$*" in *build-here*) cat >/dev/null ;; esac
exit 0
"""


def _space_env(tmp_path, served, wiki_profile="egemma2", alias="embeddinggemma2"):
    env, home, log = _env(tmp_path, {"FAKE_DEEP": '{"embed_profile":{"wiki":{"profile":"%s"}}}' % wiki_profile,
                                     "FAKE_WIKI_PROFILE": wiki_profile, "FAKE_ALIAS": alias,
                                     "FAKE_SERVED": served, "WIKI_BRAIN_SSH": "brain"})
    b = tmp_path / "bin"
    for name, body in (("curl", FAKE_CURL), ("fakepy", SMART_PY), ("ssh", SINK_SSH)):
        f = b / name
        f.write_text(body, encoding="utf-8")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    (home / ".mem0" / "authority-url").write_text("http://brain-host:18791\n", encoding="utf-8")
    return env, home, log


def test_build_goes_to_the_brain_when_this_box_lacks_the_wiki_space(tmp_path):
    env, home, log = _space_env(tmp_path, served="embeddinggemma")
    _snapshot(home)
    r = _run(env, "build")
    calls = log.read_text(encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "build-here" in calls and "brain" in calls
    assert "-f -N -M" not in calls                       # no tunnel: nothing embeds here
    assert "py root=" not in calls                       # and the local builder never ran
    assert "the brain embeds" in r.stderr


def test_build_runs_here_in_the_authority_space_when_this_box_serves_it(tmp_path):
    env, home, log = _space_env(tmp_path, served="embeddinggemma2")
    _snapshot(home)
    r = _run(env, "build")
    calls = log.read_text(encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "-f -N -M" in calls and "build-here" not in calls
    assert "wiki_profile=egemma2" in calls               # pinned to the authority's wiki space


def test_search_goes_to_the_brain_with_the_query_quoted(tmp_path):
    env, _, log = _space_env(tmp_path, served="")
    r = _run(env, "search", "where's the wiki; really?", "--k", "3")
    calls = log.read_text(encoding="utf-8")
    assert r.returncode == 0, r.stderr
    line = [l for l in calls.splitlines() if "search-here" in l][0]
    assert r"where\'s\ the\ wiki\;\ really\?" in line and "--k 3" in line


def test_build_here_builds_into_the_local_qdrant_and_stamps(tmp_path):
    env, home, log = _env(tmp_path)
    r = subprocess.run([BASH, str(SCRIPT), "build-here"], capture_output=True, env=env,
                       input=_wiki_tar(tmp_path, 3), timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    calls = log.read_text(encoding="utf-8")
    assert "py root=" in calls and "host= port=" in calls     # the brain's own Qdrant (defaults)
    assert (home / "wiki-index" / "last-build").read_text().strip().isdigit()
    assert not list((home / "wiki-index").glob("wiki.push.*"))  # the pushed copy is removed
