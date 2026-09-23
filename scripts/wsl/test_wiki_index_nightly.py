"""wiki-index-nightly.sh — the chain step's pull/skip/fail contract, with ssh and the
builder replaced by fakes on PATH and a scratch HOME (docs/systems/wiki-index.md).

Run: python3 -m pytest scripts/wsl/test_wiki_index_nightly.py -q  (bash + tar only)
"""
import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "wiki-index-nightly.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or shutil.which("tar") is None, reason="bash/tar not available")


def _wiki_tar(tmp_path: Path, pages: int) -> Path:
    src = tmp_path / "vault" / "wiki" / "entities"
    src.mkdir(parents=True, exist_ok=True)
    for i in range(pages):
        (src / f"Page {i}.md").write_text(f"---\ntype: entity\n---\n\n# Page {i}\n\nA page.\n", encoding="utf-8")
    out = tmp_path / "wiki.tar"
    with tarfile.open(out, "w") as tf:
        tf.add(tmp_path / "vault" / "wiki", arcname="wiki")
    return out


def _fake_bin(tmp_path: Path, ssh_body: str) -> Path:
    """A PATH dir with a fake `ssh` (behaviour given) and a fake python that records its call."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    ssh = b / "ssh"
    ssh.write_text("#!/usr/bin/env bash\n" + ssh_body, encoding="utf-8")
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    py = b / "fakepy"
    py.write_text('#!/usr/bin/env bash\necho "builder root=$WIKI_ROOT embed=${MEM0_EMBED_MODEL:-unset} args=$*" >> "$FAKE_LOG"\n',
                  encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return b


def _run(tmp_path: Path, sources: str, ssh_body: str, extra_env=None, stack_env: str | None = None):
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True, exist_ok=True)
    if stack_env is not None:
        (home / ".mem0" / "stack.env").write_text(stack_env, encoding="utf-8")
    b = _fake_bin(tmp_path, ssh_body)
    log = tmp_path / "builder.log"
    env = dict(os.environ)
    env.update({"HOME": str(home), "PATH": f"{b}{os.pathsep}{env['PATH']}", "FAKE_LOG": str(log),
                "WIKI_PY": str(b / "fakepy"), "WIKI_PULL_KEY": str(tmp_path / "nokey")})
    if sources is not None:
        env["WIKI_SOURCES"] = sources
    env.update(extra_env or {})
    r = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60, check=False)
    return r, home, (log.read_text(encoding="utf-8") if log.exists() else "")


def test_pulls_from_the_first_reachable_source_and_builds(tmp_path):
    tar = _wiki_tar(tmp_path, 3)
    # host "down" fails; host "up" streams the tar; the remote word is ignored (forced command).
    body = f'case "$*" in *down*) exit 255;; esac; cat "{tar}"\n'
    r, home, log = _run(tmp_path, "op@down op@up", body)
    assert r.returncode == 0, r.stderr
    assert "op@down unreachable" in r.stderr
    assert "pulled 3 pages from op@up" in r.stdout
    snap = home / "wiki-index" / "wiki"
    assert sorted(p.name for p in snap.rglob("*.md")) == ["Page 0.md", "Page 1.md", "Page 2.md"]
    assert f"root={snap}" in log and "wiki-index-build.py" in log
    assert (home / "wiki-index" / "last-pull").read_text().strip().isdigit()


def test_embed_model_comes_from_stack_env(tmp_path):
    tar = _wiki_tar(tmp_path, 1)
    r, _, log = _run(tmp_path, "op@up", f'cat "{tar}"\n', stack_env="MEM0_EMBED_MODEL=embeddinggemma-custom\n")
    assert r.returncode == 0, r.stderr
    assert "embed=embeddinggemma-custom" in log


def test_sources_come_from_stack_env_when_no_env_override(tmp_path):
    tar = _wiki_tar(tmp_path, 1)
    r, _, log = _run(tmp_path, None, f'cat "{tar}"\n',
                     stack_env="MEM0_WIKI_SOURCES=op@fromenv\n")
    assert r.returncode == 0, r.stderr
    assert "pulled 1 pages from op@fromenv" in r.stdout
    assert "wiki-index-build.py" in log


def test_unconfigured_is_a_loud_failure(tmp_path):
    r, _, log = _run(tmp_path, None, "exit 255\n", stack_env="")
    assert r.returncode == 1
    assert "MEM0_WIKI_SOURCES is not set" in r.stderr
    assert log == ""


def test_no_source_with_a_fresh_stamp_keeps_the_index_and_exits_zero(tmp_path):
    home = tmp_path / "home"
    (home / "wiki-index").mkdir(parents=True)
    (home / "wiki-index" / "last-pull").write_text(str(int(time.time()) - 3600))
    r, _, log = _run(tmp_path, "op@down", "exit 255\n")
    assert r.returncode == 0, r.stderr
    assert "index kept as-is (last pull 1 h ago" in r.stdout
    assert log == ""  # nothing rebuilt


def test_no_source_with_a_stale_stamp_fails(tmp_path):
    home = tmp_path / "home"
    (home / "wiki-index").mkdir(parents=True)
    (home / "wiki-index" / "last-pull").write_text(str(int(time.time()) - 100 * 3600))
    r, _, log = _run(tmp_path, "op@down", "exit 255\n")
    assert r.returncode == 1
    assert "last pull is 100 h old" in r.stderr
    assert log == ""


def test_an_empty_tar_does_not_replace_the_snapshot(tmp_path):
    home = tmp_path / "home"
    keep = home / "wiki-index" / "wiki" / "entities"
    keep.mkdir(parents=True)
    (keep / "Keep.md").write_text("# Keep\n\nkept\n")
    (home / "wiki-index" / "last-pull").write_text(str(int(time.time()) - 3600))
    empty = tmp_path / "empty.tar"
    (tmp_path / "vault" / "wiki").mkdir(parents=True)
    with tarfile.open(empty, "w") as tf:
        tf.add(tmp_path / "vault" / "wiki", arcname="wiki")
    r, _, log = _run(tmp_path, "op@up", f'cat "{empty}"\n')
    assert r.returncode == 0, r.stderr
    assert "op@up unreachable or empty" in r.stderr
    assert (keep / "Keep.md").exists()
    assert log == ""


# 1.31.1: stack.env is SOURCED by bash (deploy.sh, storage-cap-check.sh), so the installer now
# stores the list comma-separated; a space-separated value made `. stack.env` run the second
# host as a command. The step reads both forms, so a box still carrying the old line keeps
# working until its stack.env is rewritten.
@pytest.mark.parametrize("value", ["op@down,op@up", "op@down op@up", "op@down, op@up", " op@down ,,op@up "],
                         ids=["commas", "legacy-spaces", "comma-space", "stray-separators"])
def test_stack_env_sources_split_on_commas_and_whitespace(tmp_path, value):
    tar = _wiki_tar(tmp_path, 2)
    body = f'case "$*" in *down*) exit 255;; esac; cat "{tar}"\n'
    r, _, log = _run(tmp_path, None, body, stack_env=f"MEM0_WIKI_SOURCES={value}\n")
    assert r.returncode == 0, r.stderr
    assert "op@down unreachable" in r.stderr, "the first source must be tried on its own"
    assert "pulled 2 pages from op@up" in r.stdout
    assert "," not in r.stdout.split("pulled 2 pages from ", 1)[1].split()[0]


def test_env_override_accepts_commas_too(tmp_path):
    tar = _wiki_tar(tmp_path, 1)
    body = f'case "$*" in *down*) exit 255;; esac; cat "{tar}"\n'
    r, _, _ = _run(tmp_path, "op@down,op@up", body)
    assert r.returncode == 0, r.stderr
    assert "pulled 1 pages from op@up" in r.stdout


@pytest.mark.parametrize("value", ["op@first,op@second", "op@first op@second"], ids=["commas", "legacy-spaces"])
def test_every_stack_env_source_is_tried_in_order(tmp_path, value):
    """Through the real stack_val path (no WIKI_SOURCES override): both hosts are attempted, in
    the order written, each as its own ssh target."""
    calls = tmp_path / "ssh-calls"
    body = f'for a in "$@"; do case "$a" in op@*) echo "$a" >> "{calls}";; esac; done; exit 255\n'
    r, _, _ = _run(tmp_path, None, body, stack_env=f"MEM0_WIKI_SOURCES={value}\n", extra_env={"WIKI_MAX_STALE_H": "72"})
    assert calls.read_text(encoding="utf-8").split() == ["op@first", "op@second"]
    assert r.stderr.index("op@first unreachable") < r.stderr.index("op@second unreachable")
