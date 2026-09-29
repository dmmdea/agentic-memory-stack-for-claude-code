"""install/deploy-stamp.sh: the release sha the installers record beside each deployed runtime.

The deployed tree has no .git, so anything that must say which commit it runs (the backup
manifest's git_sha was "unknown" on every set since the cutover) reads a stamp the installer
wrote. The stamp is one line: a 40-hex sha, or the word "unknown" - never empty, never a guess.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "install" / "deploy-stamp.sh"
BASH = shutil.which("bash")
GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="bash/git not available")

SHA = "0123456789abcdef0123456789abcdef01234567"


def _sh(snippet):
    return subprocess.run([BASH, "-c", f'. "{LIB.as_posix()}"; {snippet}'], capture_output=True, text=True, timeout=30)


def _git_repo(path):
    path.mkdir(parents=True)
    cfg = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([GIT, "init", "-q", str(path)], check=True, timeout=30)
    subprocess.run([GIT, *cfg, "-C", str(path), "commit", "-q", "--allow-empty", "-m", "x"], check=True, timeout=30)
    return subprocess.run([GIT, "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True, timeout=30).stdout.strip()


def test_the_sha_is_the_checkouts_head(tmp_path):
    head = _git_repo(tmp_path / "repo")
    r = _sh(f'deploy_stamp_sha "{(tmp_path / "repo").as_posix()}"')
    assert r.returncode == 0, r.stderr
    assert r.stdout == head and re.fullmatch(r"[0-9a-f]{40}", head)


def test_a_tree_without_git_uses_the_stamp_it_was_shipped_with(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "DEPLOYED_SHA").write_text(SHA + "\n", encoding="utf-8")
    assert _sh(f'deploy_stamp_sha "{tree.as_posix()}"').stdout == SHA


def test_no_evidence_is_the_word_unknown_not_an_empty_stamp(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    assert _sh(f'deploy_stamp_sha "{tree.as_posix()}"').stdout == "unknown"
    (tree / "DEPLOYED_SHA").write_text("not a sha\n", encoding="utf-8")
    assert _sh(f'deploy_stamp_sha "{tree.as_posix()}"').stdout == "unknown", "a malformed stamp is not passed on"


def test_write_puts_one_line_named_DEPLOYED_SHA_in_each_directory(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "DEPLOYED_SHA").write_text(SHA + "\n", encoding="utf-8")
    r = _sh(f'deploy_stamp_write "{tree.as_posix()}" "{(tmp_path / "a").as_posix()}" "{(tmp_path / "b").as_posix()}"')
    assert r.returncode == 0, r.stderr
    for d in ("a", "b"):
        assert (tmp_path / d / "DEPLOYED_SHA").read_text(encoding="utf-8") == SHA + "\n"
    assert SHA[:12] in r.stdout


@pytest.mark.parametrize("installer", ["linux-authority.sh", "linux-replica.sh"])
def test_the_installers_that_stamp_VERSION_stamp_the_sha_beside_it(installer):
    """Neither installer runs hermetically past --render-only, so the wiring is pinned by text:
    the library is sourced and the stamp is written into the app dir (and, on the authority,
    beside the deployed scripts where the manifest writer lives)."""
    sh = (REPO_ROOT / "install" / installer).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in sh.splitlines() if not ln.lstrip().startswith("#"))
    assert '. "$SCRIPT_DIR/deploy-stamp.sh"' in code
    call = re.search(r'deploy_stamp_write "\$REPO_ROOT"([^\n]*)', code)
    assert call and '"$MEM0_APP"' in call.group(1), f"{installer} must stamp the mem0 app dir"
    if installer == "linux-authority.sh":
        assert '"$SCRIPTS_DIR"' in call.group(1), "the manifest writer runs from the scripts dir"
