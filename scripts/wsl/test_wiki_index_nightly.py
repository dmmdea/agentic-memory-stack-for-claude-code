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
    r = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60, check=False)
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
    assert "index kept as-is (index is 1 h old, limit 72 h)" in r.stdout
    assert log == ""  # nothing rebuilt


def test_no_source_with_a_stale_stamp_fails(tmp_path):
    home = tmp_path / "home"
    (home / "wiki-index").mkdir(parents=True)
    (home / "wiki-index" / "last-pull").write_text(str(int(time.time()) - 100 * 3600))
    r, _, log = _run(tmp_path, "op@down", "exit 255\n")
    assert r.returncode == 1
    assert "the index is 100 h old" in r.stderr
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


# ---- WP-8: freshness is the newer of the pull and the build stamp (either refresh path) --------
import json  # noqa: E402


def _stamp(home: Path, name: str, age_h: float):
    d = home / "wiki-index"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(str(int(time.time() - age_h * 3600)))


def _outcome(tmp_path: Path):
    """The single C1 line the job left in AMS_OUTCOME_FILE: (status[:reason], json object)."""
    f = tmp_path / "outcome"
    lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
    assert len(lines) <= 1, lines
    if not lines:
        return None, None
    head, _, payload = lines[0].partition(" ")
    return head, json.loads(payload)


def _down_env(tmp_path: Path):
    return {"AMS_OUTCOME_FILE": str(tmp_path / "outcome")}


DOWN = 'echo "ssh: connect to host op port 22: Connection timed out" >&2; exit 255\n'


def test_a_successful_build_stamps_last_build_and_reports_ok(tmp_path):
    tar = _wiki_tar(tmp_path, 2)
    r, home, _ = _run(tmp_path, "op@up", f'cat "{tar}"\n', extra_env=_down_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (home / "wiki-index" / "last-build").read_text().strip().isdigit()
    status, work = _outcome(tmp_path)
    assert status == "ok" and work["pulled"] == 2 and work["source"] == "op@up"


def test_a_failed_build_leaves_no_last_build_and_fails_the_step(tmp_path):
    tar = _wiki_tar(tmp_path, 1)
    r, home, _ = _run(tmp_path, "op@up", f'cat "{tar}"\n', extra_env=_down_env(tmp_path))
    assert r.returncode == 0
    (home / "wiki-index" / "last-build").unlink()
    # the same run again with a builder that exits 1
    (tmp_path / "bin" / "fakepy").write_text('#!/usr/bin/env bash\nexit 1\n', encoding="utf-8")
    env = dict(os.environ, HOME=str(home), PATH=f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
               WIKI_PY=str(tmp_path / "bin" / "fakepy"), WIKI_PULL_KEY=str(tmp_path / "nokey"), WIKI_SOURCES="op@up")
    r = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60, check=False)
    assert r.returncode == 1
    assert not (home / "wiki-index" / "last-build").exists()


def test_skip_night_with_a_fresh_index_is_ok_and_carries_per_source_detail(tmp_path):
    home = tmp_path / "home"
    _stamp(home, "last-pull", 10)
    r, _, _ = _run(tmp_path, "op@down", DOWN, extra_env=_down_env(tmp_path))
    assert r.returncode == 0, r.stderr
    status, work = _outcome(tmp_path)
    assert status == "ok"
    assert work["fresh_age_h"] == 10
    assert work["sources"] == [{"source": "op@down", "exit": 255,
                                "stderr": "ssh: connect to host op port 22: Connection timed out"}]
    assert "op@down unreachable or empty (ssh exit 255): ssh: connect to host op port 22" in r.stderr


def test_skip_night_past_24h_is_degraded_naming_the_hours(tmp_path):
    home = tmp_path / "home"
    _stamp(home, "last-pull", 50)
    _stamp(home, "last-build", 30)
    r, _, _ = _run(tmp_path, "op@down", DOWN, extra_env=_down_env(tmp_path))
    assert r.returncode == 0, r.stderr
    status, work = _outcome(tmp_path)
    assert status == "degraded:no-source-fresh-30h", "freshness is the NEWER stamp, not the pull"
    assert work["fresh_age_h"] == 30 and work["sources"][0]["exit"] == 255


def test_the_session_build_alone_keeps_a_skip_night_ok(tmp_path):
    home = tmp_path / "home"
    _stamp(home, "last-pull", 100)   # no pull for four days ...
    _stamp(home, "last-build", 3)    # ... but a session rebuilt the index this morning
    r, _, _ = _run(tmp_path, "op@down", DOWN, extra_env=_down_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert _outcome(tmp_path)[0] == "ok"


def test_the_72h_limit_measures_freshness_not_the_pull(tmp_path):
    home = tmp_path / "home"
    _stamp(home, "last-pull", 100)
    _stamp(home, "last-build", 40)   # fresh_age 40 h < 72 h: no failure although the pull is 100 h old
    r, _, _ = _run(tmp_path, "op@down", DOWN, extra_env=_down_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert _outcome(tmp_path)[0] == "degraded:no-source-fresh-40h"


def test_both_stamps_past_72h_fail(tmp_path):
    home = tmp_path / "home"
    _stamp(home, "last-pull", 100)
    _stamp(home, "last-build", 80)
    r, _, _ = _run(tmp_path, "op@down", DOWN, extra_env=_down_env(tmp_path))
    assert r.returncode == 1
    assert "the index is 80 h old (limit 72 h)" in r.stderr


def test_no_stamp_at_all_fails(tmp_path):
    r, _, _ = _run(tmp_path, "op@down", DOWN, extra_env=_down_env(tmp_path))
    assert r.returncode == 1
    assert "no pull or build has ever been recorded" in r.stderr


def test_a_hand_run_without_an_outcome_file_still_works(tmp_path):
    home = tmp_path / "home"
    _stamp(home, "last-pull", 50)
    env = {"AMS_OUTCOME_FILE": ""}
    r, _, _ = _run(tmp_path, "op@down", DOWN, extra_env=env)
    assert r.returncode == 0, r.stderr


# ---- WP-8 fix round 1: the outcome JSON survives hostile ssh stderr ---------------------------
_HOSTILE = {
    # a cut at 200 characters lands between the two characters of an escaped backslash
    "backslash_at_the_cap": b"a" * 199 + b"\\" + b"b" * 50,
    "quote_at_the_cap": b"a" * 199 + b'"' + b"b" * 50,
    "windows_path_and_quotes": rb'D:\tools\wiki\wiki-tar.cmd: "vault not found" \\host\share ' * 6,
    # a 200-byte cap or the 300-byte tail lands inside a two-byte character
    "multibyte_at_the_head_cap": b"a" * 199 + "\u00e9".encode() + b"b" * 50,
    "multibyte_at_the_tail_cut": b"x" + "\u00e9".encode() * 200,
    "lone_invalid_byte": b"denied \xff\xfe by host " + b"c" * 250,
    "mixed_long": (rb'ssh: "no\such" ' + "\u00e9\u00fc".encode() + b"\\") * 40,
}


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_hostile_ssh_stderr_still_yields_parsable_utf8_json(tmp_path, name):
    home = tmp_path / "home"
    _stamp(home, "last-pull", 50)   # degraded night: the per-source detail is the whole point
    errfile = tmp_path / "stderr.bin"
    errfile.write_bytes(_HOSTILE[name])
    body = f'cat "{errfile}" >&2; exit 255\n'
    r, _, _ = _run(tmp_path, "op@down", body, extra_env=_down_env(tmp_path))
    assert r.returncode == 0, r.stderr
    raw = (tmp_path / "outcome").read_bytes()
    raw.decode("utf-8")                       # strict: an invalid sequence raises here
    status, work = _outcome(tmp_path)
    assert status == "degraded:no-source-fresh-50h", "the night must not read outcome-unparsable"
    assert work["sources"][0]["source"] == "op@down" and work["sources"][0]["exit"] == 255
    assert 0 < len(work["sources"][0]["stderr"].encode("utf-8")) <= 200


def test_escapes_are_kept_whole_when_the_text_fits(tmp_path):
    home = tmp_path / "home"
    _stamp(home, "last-pull", 10)
    errfile = tmp_path / "stderr.bin"
    errfile.write_bytes(('C:\\bin "x" caf\u00e9').encode())
    r, _, _ = _run(tmp_path, "op@down", f'cat "{errfile}" >&2; exit 255\n', extra_env=_down_env(tmp_path))
    assert r.returncode == 0, r.stderr
    _, work = _outcome(tmp_path)
    assert work["sources"][0]["stderr"] == 'C:\\bin "x" caf\u00e9'
