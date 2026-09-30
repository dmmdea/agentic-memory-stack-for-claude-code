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


DEPLOY = REPO_ROOT / "scripts" / "wsl" / "deploy.sh"


def test_deploy_sh_stamps_only_through_the_contract_and_reads_it_back_as_a_ref(tmp_path):
    """deploy.sh wrote DEPLOYED_SHA with `git rev-parse HEAD > file 2>/dev/null || true`. The redirect
    truncates the file before git runs, so a checkout git cannot read (an invalid .git, a safe.directory
    refusal) left an EMPTY stamp: the backup manifest read no evidence, and the next deploy printed
    `git checkout ` as its rollback ref. The contract (deploy_stamp_write) writes a 40-hex sha or the
    word `unknown`, so deploy.sh now goes through it at both stamp sites, and the rollback reader takes
    only a sha (`unknown` is not a ref)."""
    code = [ln for ln in DEPLOY.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]
    joined = "\n".join(code)
    assert '. "$REPO_ROOT/install/deploy-stamp.sh"' in joined, "deploy.sh must source the contract"
    inline = [ln for ln in code if re.search(r'>\s*"[^"]*DEPLOYED_SHA', ln)]
    assert not inline, f"an inline DEPLOYED_SHA writer bypasses the contract: {inline}"
    calls = [ln.strip() for ln in code if "deploy_stamp_write " in ln]
    assert calls == ['deploy_stamp_write "$REPO_ROOT" "$APP_DIR"'] * 2, \
        "both stamp sites (the dormant-replica exit and the end of a full deploy) go through the contract"

    def stamp(checkout):
        app = tmp_path / f"app-{checkout.name}"
        app.mkdir()
        script = 'set -euo pipefail; REPO_ROOT="$1"; APP_DIR="$2"; . "$3"; ' + calls[0]
        r = subprocess.run([BASH, "-c", script, "stamp", checkout.as_posix(), app.as_posix(), LIB.as_posix()],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return (app / "DEPLOYED_SHA").read_text(encoding="utf-8")

    head = _git_repo(tmp_path / "repo")
    assert stamp(tmp_path / "repo") == head + "\n"
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / ".git").write_text("not a gitfile\n", encoding="utf-8")   # git fails; the old writer left 0 bytes
    assert stamp(unreadable) == "unknown\n"
    bare = tmp_path / "bare"
    bare.mkdir()
    assert stamp(bare) == "unknown\n"

    # the reader that names the rollback ref: a sha is a ref, `unknown` / an empty file / no file are not
    start = next(i for i, ln in enumerate(code) if ln.startswith("PREV_SHA="))
    end = next(i for i in range(start, len(code)) if "<previous-main>" in code[i])
    reader = "\n".join(code[start:end + 1])
    app = tmp_path / "app-reader"
    app.mkdir()

    def ref(content):
        f = app / "DEPLOYED_SHA"
        f.unlink(missing_ok=True)
        if content is not None:
            f.write_text(content, encoding="utf-8")
        script = 'set -euo pipefail; APP_DIR="$1"; ' + reader + '; printf "%s" "$PREV_SHA"'
        r = subprocess.run([BASH, "-c", script, "reader", app.as_posix()], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout

    assert ref(SHA + "\n") == SHA
    for stale in ("unknown\n", "", "\n", None):
        assert ref(stale) == "<previous-main>", repr(stale)


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
