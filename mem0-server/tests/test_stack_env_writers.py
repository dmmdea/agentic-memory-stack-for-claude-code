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


from _home_isolation import home_env  # noqa: E402


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
    env = {k: v for k, v in home_env(home).items() if not k.startswith("MEM0_EMBED")}  # the operator's embedding settings must not reach the installer
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
            "scripts/wsl/ensure-codex-shim.sh"]


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
                          capture_output=True, text=True, timeout=30, env=home_env(home))


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


# --- (5) operator-owned keys survive a re-run ------------------------------------------------
# MEM0_BRAIN_SSH (the brain's SSH alias for scripts/wsl/wiki-index.sh) is written by the operator
# by hand; no installer flag sets it. Every writer rewrites the whole file, so before 1.31.3 any
# re-run silently deleted it and the replica's wiki build/search lost its brain alias.

def test_a_pre_existing_brain_ssh_survives_a_render_only_rerun(tmp_path):
    r, se = _render(tmp_path, stack_env="MEM0_WSL_USER=tenant\nMEM0_BRAIN_SSH=op@brain-alias\n")
    assert r.returncode == 0, r.stderr
    assert "MEM0_BRAIN_SSH=op@brain-alias" in _lines(se)
    assert "MEM0_BRAIN_SSH carried over from ~/.mem0/stack.env" in r.stdout
    # a fixed point: re-running from the rendered file keeps it exactly once
    (tmp_path / "home" / ".mem0" / "stack.env").write_text(se.read_text(encoding="utf-8"), encoding="utf-8")
    r2, se2 = _render(tmp_path, name="render2")
    assert r2.returncode == 0, r2.stderr
    assert [ln for ln in _lines(se2) if ln.startswith("MEM0_BRAIN_SSH=")] == ["MEM0_BRAIN_SSH=op@brain-alias"]


def test_a_pool_health_ack_survives_a_render_only_rerun(tmp_path):
    """The dated pool-health ack is hand-set (no installer flag): a re-run that dropped it would put a
    planned-maintenance DEGRADED pool back on the alarm mid-window. Its value is a plain token."""
    r, se = _render(tmp_path, stack_env="MEM0_WSL_USER=tenant\nMEM0_POOL_HEALTH_ACK=DEGRADED:2026-10-06\n")
    assert r.returncode == 0, r.stderr
    assert "MEM0_POOL_HEALTH_ACK=DEGRADED:2026-10-06" in _lines(se)
    assert "MEM0_POOL_HEALTH_ACK carried over from ~/.mem0/stack.env" in r.stdout


def test_no_brain_ssh_is_invented_when_the_receipt_has_none(tmp_path):
    r, se = _render(tmp_path, stack_env="MEM0_WSL_USER=tenant\n")
    assert r.returncode == 0, r.stderr
    assert not any(ln.startswith("MEM0_BRAIN_SSH") for ln in _lines(se))


def test_stack_env_carry_prints_only_recorded_operator_keys(tmp_path):
    f = tmp_path / "stack.env"
    # a hand edit in a Windows editor leaves a CR; the carried value must be the plain token
    f.write_bytes(b"MEM0_ROLE=replica\r\nMEM0_BRAIN_SSH=op@brain\r\nMEM0_BRAIN_SSH=second\r\n")
    out = subprocess.run([BASH, "-c", f'. "{LIB.as_posix()}"; stack_env_carry "{f.as_posix()}"'],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "MEM0_BRAIN_SSH=op@brain\n"
    none = subprocess.run([BASH, "-c", f'. "{LIB.as_posix()}"; stack_env_carry "{(tmp_path / "absent").as_posix()}"'],
                          capture_output=True, text=True, timeout=30)
    assert none.returncode == 0 and none.stdout == ""


def _carry(tmp_path, body, *skips):
    f = tmp_path / "stack.env"
    f.write_bytes(body)
    return subprocess.run([BASH, "-c", f'. "{LIB.as_posix()}"; stack_env_carry "{f.as_posix()}" ' + " ".join(skips)],
                          capture_output=True, text=True, timeout=30)


def test_the_embedding_space_keys_are_carried_by_pattern(tmp_path):
    """MEM0_EMBED_PROFILE and the alias overrides embedder_profile reads from stack.env survive every
    writer's rewrite. Without them a re-run of the WSL or replica installer rebinds the server to the
    default space's collections while the store sits in another: searches score noise, /health/deep
    stays green. They are carried by pattern, so a profile added later needs no list edit."""
    body = (b"MEM0_ROLE=replica\r\nMEM0_EMBED_PROFILE=egemma2\r\nMEM0_EMBED_MODEL=embeddinggemma-ams\r\n"
            b"MEM0_EMBED_MODEL_EGEMMA2=embeddinggemma2-ams\r\nMEM0_EMBED_LONG_MODEL_EGEMMA2=embeddinggemma2-ams-long\r\n"
            b"MEM0_EMBED_BASE_URL=http://box:11436/v1\r\nMEM0_EMBED_MODEL_FUTURE_2=x\r\nMEM0_EMBED_FOO=not-ours\r\n"
            b"MEM0_EMBED_PROFILE=second-occurrence\r\n")
    out = _carry(tmp_path, body)
    assert out.returncode == 0, out.stderr
    # the first occurrence wins (every sed reader takes it), a CR from a hand edit is dropped, and an
    # unrelated MEM0_EMBED_* name is not ours to carry
    assert out.stdout.splitlines() == [
        "MEM0_EMBED_PROFILE=egemma2", "MEM0_EMBED_MODEL=embeddinggemma-ams",
        "MEM0_EMBED_MODEL_EGEMMA2=embeddinggemma2-ams", "MEM0_EMBED_LONG_MODEL_EGEMMA2=embeddinggemma2-ams-long",
        "MEM0_EMBED_BASE_URL=http://box:11436/v1", "MEM0_EMBED_MODEL_FUTURE_2=x"], out.stdout


def test_a_writer_that_sets_an_embedding_key_itself_skips_it_in_the_carry(tmp_path):
    out = _carry(tmp_path, b"MEM0_EMBED_PROFILE=egemma2\nMEM0_EMBED_MODEL_EGEMMA2=a\nMEM0_EMBED_MODEL=b\n",
                 "MEM0_EMBED_PROFILE", "MEM0_EMBED_MODEL_EGEMMA2")
    assert out.stdout.splitlines() == ["MEM0_EMBED_MODEL=b"]


def test_the_replica_and_wsl_writers_keep_the_profile_through_a_rewrite():
    """Neither installer can run hermetically here; both write stack.env from a fixed list plus the
    carry, and the carry now holds the embedding keys, so the pin is the pattern in the lib plus the
    call that hands the carried keys to the write."""
    for writer in ("install/1-wsl-services.sh", "install/linux-replica.sh"):
        code = "\n".join(ln for ln in (REPO_ROOT / writer).read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
        assert '"${STACK_ENV_CARRY[@]}"' in code, writer
    lib = LIB.read_text(encoding="utf-8")
    assert "STACK_ENV_EMBED_KEY_RE=" in lib and "MEM0_EMBED_(PROFILE|MODEL|MODEL_[A-Z0-9_]+|LONG_MODEL_[A-Z0-9_]+|BASE_URL)" in lib


@pytest.mark.parametrize("writer", ["install/1-wsl-services.sh", "install/linux-authority.sh", "install/linux-replica.sh"])
def test_every_writer_carries_the_operator_keys_into_its_write(writer):
    """1-wsl-services.sh and linux-replica.sh cannot run hermetically here, so their wiring is
    pinned by text: the carried keys must reach the stack_env_write argument list."""
    text = (REPO_ROOT / writer).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert re.search(r'stack_env_carry "\$[A-Z_]*(HOME|MEM0_DIR)[A-Z_]*/(\.mem0/)?stack\.env"', code), writer
    assert '"${STACK_ENV_CARRY[@]}"' in code, f"{writer} must pass the carried keys to its stack.env write"


def test_brand_routing_keys_survive_a_render_only_rerun(tmp_path):
    """C3: MEM0_SHARED_BRANDS (labels visible to every scope) and MEM0_BRAND_MAP (the brain's brand
    map path) are operator-set, no installer flag sets them: a re-run must not delete them."""
    r, se = _render(tmp_path, stack_env="MEM0_WSL_USER=tenant\nMEM0_SHARED_BRANDS=shared-a,shared-b\nMEM0_BRAND_MAP=/srv/ams/brands.json\n")
    assert r.returncode == 0, r.stderr
    assert "MEM0_SHARED_BRANDS=shared-a,shared-b" in _lines(se)
    assert "MEM0_BRAND_MAP=/srv/ams/brands.json" in _lines(se)


# --- (6) the promotion-gate switches survive a re-run (session-12 WP-12) ---------------------
# The 4C promotion gate was set to enforce in the Windows receipt; the brain's stack.env carried
# nothing, so the dream fell back to shadow and three uncorroborated facts became canonical. The
# switches are operator-owned (no installer flag before --promotion-gate-mode), so every writer
# carries them; the brain installer additionally takes the gate mode as a flag.

GATE_KEYS = {
    "MEM0_PROMOTION_GATE_MODE": "enforce",
    "MEM0_SHARED_BRANDS": "shared-a,shared-b",
    "MEM0_BRAND_MAP": "/srv/ams/brands.json",
    "MEM0_NLI_GATE_ENABLED": "1",
}


@pytest.mark.parametrize("key,value", sorted(GATE_KEYS.items()))
def test_the_gate_and_brand_switches_are_operator_owned_and_carried(tmp_path, key, value):
    r, se = _render(tmp_path, stack_env=f"MEM0_WSL_USER=tenant\n{key}={value}\n")
    assert r.returncode == 0, r.stderr
    assert f"{key}={value}" in _lines(se)
    assert f"{key} carried over from ~/.mem0/stack.env: {value}" in r.stdout


def _gate_lines(se):
    return [ln for ln in _lines(se) if ln.startswith("MEM0_PROMOTION_GATE_MODE")]


def test_a_rerun_without_the_flag_keeps_the_promotion_gate_mode(tmp_path):
    r, se = _render(tmp_path, "--promotion-gate-mode", "enforce", stack_env="MEM0_WSL_USER=tenant\n")
    assert r.returncode == 0, r.stderr
    assert _gate_lines(se) == ["MEM0_PROMOTION_GATE_MODE=enforce"]
    # the rendered receipt becomes the next run's input; the flag is now omitted
    (tmp_path / "home" / ".mem0" / "stack.env").write_text(se.read_text(encoding="utf-8"), encoding="utf-8")
    r2, se2 = _render(tmp_path, name="render2")
    assert r2.returncode == 0, r2.stderr
    assert _gate_lines(se2) == ["MEM0_PROMOTION_GATE_MODE=enforce"]


def test_the_flag_overrides_the_recorded_mode_and_is_written_once(tmp_path):
    r, se = _render(tmp_path, "--promotion-gate-mode", "shadow",
                    stack_env="MEM0_WSL_USER=tenant\nMEM0_PROMOTION_GATE_MODE=enforce\n")
    assert r.returncode == 0, r.stderr
    assert _gate_lines(se) == ["MEM0_PROMOTION_GATE_MODE=shadow"]


def test_no_gate_mode_is_invented_on_a_first_install(tmp_path):
    """Enforce makes a gate error fail-safe, so the operator calibrates before choosing it: an
    installer that picked a mode by itself would decide that for him."""
    r, se = _render(tmp_path, stack_env="MEM0_WSL_USER=tenant\n")
    assert r.returncode == 0, r.stderr
    assert _gate_lines(se) == []


@pytest.mark.parametrize("bad", ["strict", "ENFORCE", "on", "enforce,shadow"])
def test_promotion_gate_mode_accepts_only_shadow_or_enforce(tmp_path, bad):
    r, se = _render(tmp_path, "--promotion-gate-mode", bad)
    assert r.returncode != 0
    assert "--promotion-gate-mode must be shadow or enforce" in r.stderr
    assert not se.exists()


def test_an_explicit_empty_gate_mode_clears_the_recorded_one(tmp_path):
    r, se = _render(tmp_path, "--promotion-gate-mode", "",
                    stack_env="MEM0_WSL_USER=tenant\nMEM0_PROMOTION_GATE_MODE=enforce\n")
    assert r.returncode == 0, r.stderr
    assert "--promotion-gate-mode cleared (explicit empty value; not inherited)" in r.stdout
    assert _gate_lines(se) == []


def test_the_shared_operator_key_list_holds_the_gate_and_brand_switches():
    """1-wsl-services.sh and linux-replica.sh cannot run hermetically, so what pins them is the
    shared list in install/stack-env.sh (their calls to stack_env_carry are asserted above)."""
    lib = LIB.read_text(encoding="utf-8")
    m = re.search(r'^STACK_ENV_OPERATOR_KEYS="([^"]*)"', lib, re.M)
    assert m, "STACK_ENV_OPERATOR_KEYS must be one quoted list"
    assert set(GATE_KEYS) <= set(m.group(1).split())


# Carried is not the same as read. The writers keep every operator-owned key across a re-run, but a key
# only takes effect where something reads it, and the mem0 server unit never loads stack.env into its
# environment (no EnvironmentFile=): a reader that wants a key from the file opens it itself. The
# comment beside the key list and the installer doc say who reads each key; these pin the claims.
KEY_READERS = {   # key -> [(file, the text of its stack.env read)]
    "MEM0_PROMOTION_GATE_MODE": [("scripts/wsl/dream-consolidate.py", 'stack_env().get("MEM0_PROMOTION_GATE_MODE")'),
                                 ("mem0-server/capabilities.py", 'read_stack_env(stack_env_path).get("MEM0_PROMOTION_GATE_MODE")')],
    "MEM0_SHARED_BRANDS": [("mem0-server/admission_gate.py", '_stack_env_value("MEM0_SHARED_BRANDS")'),
                           ("scripts/wsl/brand_routing.py", '_stack_env_value("MEM0_SHARED_BRANDS")')],
    "MEM0_BRAND_MAP": [("mem0-server/admission_gate.py", '_stack_env_value("MEM0_BRAND_MAP")'),
                       ("scripts/wsl/brand_routing.py", '_stack_env_value("MEM0_BRAND_MAP")'),
                       ("scripts/wsl/ams-store-judge-apply.sh", "s/^MEM0_BRAND_MAP=//p")],
    "MEM0_POOL_HEALTH_ACK": [("mem0-server/maintenance_health.py", "read_stack_env().get(POOL_ACK_KEY)")],
    "MEM0_BRAIN_SSH": [("scripts/wsl/wiki-index.sh", "s/^MEM0_BRAIN_SSH=//p")],
    "MEM0_WIKI_EMBED_PROFILE": [("mem0-server/embedder_profile.py", '_setting("MEM0_WIKI_EMBED_PROFILE")')],
    "MEM0_QDRANT_COLLECTION": [("mem0-server/embedder_profile.py", '_setting("MEM0_QDRANT_COLLECTION")')],
    "MEM0_COLLECTION": [("mem0-server/embedder_profile.py", '_setting("MEM0_COLLECTION")')],
    "MEM0_EPISODES_COLLECTION": [("mem0-server/embedder_profile.py", '_setting("MEM0_EPISODES_COLLECTION")')],
    "MEM0_WIKI_COLLECTION": [("mem0-server/embedder_profile.py", '_setting("MEM0_WIKI_COLLECTION")')],
    "MEM0_RELEVANCE_THRESHOLD": [("mem0-server/embedder_profile.py", '"relevance_gate": "MEM0_RELEVANCE_THRESHOLD"')],
    "MEM0_RAW_FALLBACK_COSINE_FLOOR": [("mem0-server/embedder_profile.py", '"episode_floor": "MEM0_RAW_FALLBACK_COSINE_FLOOR"')],
    "MEM0_NLI_GATE_COSINE_FLOOR": [("mem0-server/embedder_profile.py", '"nli_floor": "MEM0_NLI_GATE_COSINE_FLOOR"')],
    "MEM0_MEDIA_EMBEDDER": [("mem0-server/embedder_profile.py", '_setting("MEM0_MEDIA_EMBEDDER")')],
}


@pytest.mark.parametrize("key", sorted(KEY_READERS))
def test_every_operator_key_the_docs_call_read_from_stack_env_has_that_reader(key):
    for rel, needle in KEY_READERS[key]:
        assert needle in (REPO_ROOT / rel).read_text(encoding="utf-8"), \
            f"{rel} no longer reads {key} from stack.env: update the comment in install/stack-env.sh and installer-and-deploy.md"


def test_the_carried_nli_flag_is_documented_as_not_read_from_stack_env():
    """MEM0_NLI_GATE_ENABLED is carried, but app.py reads it once, at import, from the process environment
    only, and no unit loads stack.env, so the file's value never reaches it. The comment beside the key list
    and the installer doc must say so; when a unit gains EnvironmentFile= for stack.env, or app.py starts
    reading the file for this key, this fails, and the disclosure is what to update."""
    for unit in (REPO_ROOT / "systemd").iterdir():
        for ln in unit.read_text(encoding="utf-8", errors="replace").splitlines():
            assert not ln.strip().startswith("EnvironmentFile="), (
                f"{unit.name} now loads an environment file; if it is stack.env, update the NLI disclosure in "
                "install/stack-env.sh and docs/systems/installer-and-deploy.md")
    app = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    assert 'return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")' in app, "_env_flag reads os.environ only"
    assert 'NLI_GATE_ENABLED = _env_flag("MEM0_NLI_GATE_ENABLED")' in app
    assert "MEM0_NLI_GATE_ENABLED" not in "\n".join(ln for ln in app.splitlines() if "stack" in ln.lower())
    lib = LIB.read_text(encoding="utf-8")
    doc = (REPO_ROOT / "docs" / "systems" / "installer-and-deploy.md").read_text(encoding="utf-8")
    assert re.search(r"MEM0_NLI_GATE_ENABLED \(the NLI write gate\): NOT read from here", lib)
    assert "systemctl --user edit mem0" in lib and "systemctl --user edit mem0" in doc
    assert "does not turn the NLI write gate on" in doc


def _wsl_record_block() -> str:
    """install/1-wsl-services.sh's own text from the carry to the profile record (1.35.0), verbatim."""
    text = (REPO_ROOT / "install" / "1-wsl-services.sh").read_text(encoding="utf-8")
    start = text.index('mapfile -t STACK_ENV_CARRY < <(stack_env_carry "$USER_HOME/.mem0/stack.env")')
    end = text.index('    echo "  embedding profile recorded: $EP_RECORD ($EP_WHY)"\nfi\n', start)
    return text[start:end] + '    echo "  embedding profile recorded: $EP_RECORD ($EP_WHY)"\nfi\n'


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("case,stack_env,collections,expected", [
    ("fresh box", None, False, "MEM0_EMBED_PROFILE=egemma2"),
    ("receipt from before profiles", "MEM0_WSL_USER=t\nMEM0_EMBED_MODEL=embeddinggemma-ams\n", False,
     "MEM0_EMBED_PROFILE=egemma-300m"),
    ("restored Qdrant data, no receipt yet", None, True, "MEM0_EMBED_PROFILE=egemma-300m"),
    ("a recorded profile is kept", "MEM0_EMBED_PROFILE=egemma-300m\n", False, "MEM0_EMBED_PROFILE=egemma-300m"),
    ("a recorded new profile is kept", "MEM0_EMBED_PROFILE=egemma2\n", True, "MEM0_EMBED_PROFILE=egemma2"),
])
def test_the_wsl_installer_records_the_profile_so_a_default_change_cannot_move_a_store(
        tmp_path, case, stack_env, collections, expected):
    """1.35.0: the WSL installer has no profile flag, and the server reads an unrecorded box as the legacy
    space. So the installer records one on every run: the legacy space for a store already on the box, the
    default for a fresh one, and whatever is recorded otherwise (exactly one MEM0_EMBED_PROFILE line)."""
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    if stack_env is not None:
        (home / ".mem0" / "stack.env").write_text(stack_env, encoding="utf-8")
    if collections:
        (home / "qdrant-server" / "storage" / "collections" / "mem0_egemma_768").mkdir(parents=True)
    script = (f'set -eo pipefail\n. "{LIB.as_posix()}"\n. "{(REPO_ROOT / "scripts/wsl/embed-profile.sh").as_posix()}"\n'
              f'USER_HOME="{home.as_posix()}"; QDRANT_DIR="$USER_HOME/qdrant-server"\n'
              + _wsl_record_block() + 'printf "%s\n" "${STACK_ENV_CARRY[@]}"\n')
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=home_env(home), timeout=60)
    assert r.returncode == 0, (case, r.stderr)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("MEM0_EMBED_PROFILE=")]
    assert lines == [expected], (case, r.stdout)


# --- 1.35.1: an explicit, validated profile switch (MEM0_SET_EMBED_PROFILE) ------------------
# A Windows PC replica had no sanctioned way to change its profile: install.ps1 and
# 1-wsl-services.sh took no profile input, and the only command the restore error named
# (install/linux-replica.sh) is Linux-only. The switch is env-driven because wsl.exe passes no
# environment: install.ps1 -EmbedProfile puts it on the bash line.

def _wsl_stack_env_block() -> str:
    """install/1-wsl-services.sh's own text from the carry through the stack.env write and what follows it
    there (a replica's restore-marker clearing), verbatim: the carry, the profile record, the explicit
    switch, the write that persists them, and the marker."""
    text = (REPO_ROOT / "install" / "1-wsl-services.sh").read_text(encoding="utf-8")
    start = text.index('mapfile -t STACK_ENV_CARRY < <(stack_env_carry "$USER_HOME/.mem0/stack.env")')
    assert text.index('echo "  stack.env written ($USER_HOME/.mem0/stack.env)', start) > start
    return text[start:text.index("# Generate canonical-key if not present", start)]


_CURL_STUB = """#!/bin/sh
# stands in for curl: the block asks for two things, the Qdrant collection and llama-swap's model list
for a in "$@"; do u="$a"; done
printf '%s\\n' "$u" >> "$STUB_CURL_LOG"
case "$u" in
    */v1/models) printf '%s' "$STUB_MODELS_BODY"; exit "${STUB_MODELS_EXIT:-0}" ;;
esac
printf '%s' "$STUB_CURL_BODY"
exit "${STUB_CURL_EXIT:-0}"
"""
_QDRANT_POINTS = '{"result":{"points_count":16946,"status":"green"},"status":"ok","time":0.0002}'
_MODELS_ALL = ('{"object":"list","data":[{"id":"embeddinggemma2"},{"id":"embeddinggemma"},'
               '{"id":"embeddinggemma-ams"},{"id":"bge-reranker-v2-m3"}]}')
_QDRANT_URL = "http://127.0.0.1:6333/collections/mem0_eg2_768"
_MODELS_URL = "http://127.0.0.1:11436/v1/models"
_QDRANT_404 ='{"status":{"error":"Not found: Collection `mem0_eg2_768` does not exist!"},"time":0.0001}'


def _win_marker(tmp_path) -> Path:
    """Where the block looks for the offline watcher's marker: MEM0_WIN_HOME is the fake Windows profile."""
    return tmp_path / "winhome" / ".claude" / "state" / "replica-restored.txt"


def _put_marker(tmp_path) -> Path:
    m = _win_marker(tmp_path)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("2026-10-09T03:00:00Z\n", encoding="utf-8")
    return m


def _run_switch(tmp_path, *, role, stack_env, switch=None, body="", curl_exit=0, models=_MODELS_ALL, models_exit=0,
                media=None):
    """Runs the real stack.env block in bash with a fake HOME and a stub curl (the box's own Qdrant and
    llama-swap must never answer these). `body` is Qdrant's answer, `models` llama-swap's model list. The
    Windows profile is a directory under tmp_path (MEM0_WIN_HOME), so a run never looks at a real one; a
    test that wants the restore marker there puts it with _put_marker first.
    Returns (completed process, stack.env text or None, the URLs curl was asked for, in order)."""
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True, exist_ok=True)
    se = home / ".mem0" / "stack.env"
    if stack_env is not None:
        se.write_text(stack_env, encoding="utf-8", newline="")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "curl"
    stub.write_text(_CURL_STUB, encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    log = tmp_path / "curl.log"
    log.write_text("", encoding="utf-8")
    env = {k: v for k, v in home_env(home).items()
           if not k.startswith(("MEM0_EMBED", "MEM0_SET_EMBED", "MEM0_SET_MEDIA", "MEM0_MEDIA"))}
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env.update(STUB_CURL_LOG=str(log), STUB_CURL_BODY=body, STUB_CURL_EXIT=str(curl_exit),
               STUB_MODELS_BODY=models, STUB_MODELS_EXIT=str(models_exit), MEM0_WIN_HOME=str(tmp_path / "winhome"))
    if switch is not None:
        env["MEM0_SET_EMBED_PROFILE"] = switch
    if media is not None:
        env["MEM0_SET_MEDIA_EMBEDDER"] = media
    script = (f'set -eo pipefail\n. "{LIB.as_posix()}"\n. "{(REPO_ROOT / "scripts/wsl/embed-profile.sh").as_posix()}"\n'
              f'USER_HOME="{home.as_posix()}"; QDRANT_DIR="$USER_HOME/qdrant-server"\n'
              f'WSL_USER=tenant; WIN_USER=winuser; DISTRO=Ubuntu; REPO_ROOT_WSL=/opt/repo; MEM0_BIND=127.0.0.1; MEM0_ROLE={role}\n'
              + _wsl_stack_env_block())
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    return r, (se.read_text(encoding="utf-8") if se.exists() else None), log.read_text(encoding="utf-8").split()


def _profile_lines(stack_env_text):
    return [ln for ln in (stack_env_text or "").splitlines() if ln.startswith("MEM0_EMBED_PROFILE=")]


REPLICA_300M = "MEM0_ROLE=replica\nMEM0_EMBED_PROFILE=egemma-300m\nMEM0_EMBED_MODEL=embeddinggemma-ams\nMEM0_BRAIN_SSH=op@brain-alias\n"


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_replica_switch_replaces_the_recorded_profile_with_exactly_one_line(tmp_path):
    r, se, asked = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M, switch="egemma2")
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"], se
    # every other carried key survives the rewrite
    assert "MEM0_EMBED_MODEL=embeddinggemma-ams" in se and "MEM0_BRAIN_SSH=op@brain-alias" in se
    assert ("embedding profile switched: egemma-300m -> egemma2 (a replica: restore a set made in egemma2 next; "
            "its local llama-swap must serve embeddinggemma2)") in r.stdout, r.stdout
    assert asked == [], "a replica has no store to check: Qdrant is not asked"


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_replica_switch_names_the_alias_this_box_resolves_for_the_new_profile(tmp_path):
    """Going back to EmbeddingGemma-300m: the box's own alias override for that space is the one named."""
    r, se, _ = _run_switch(tmp_path, role="replica",
                           stack_env="MEM0_ROLE=replica\nMEM0_EMBED_PROFILE=egemma2\nMEM0_EMBED_MODEL=embeddinggemma-ams\n",
                           switch="egemma-300m")
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma-300m"], se
    assert ("switched: egemma2 -> egemma-300m (a replica: restore a set made in egemma-300m next; "
            "its local llama-swap must serve embeddinggemma-ams)") in r.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("name", ["egemma-3", "EGEMMA2", "egemma2 ", "../egemma2", "x;y"])
@pytest.mark.parametrize("role", ["replica", "brain"])
def test_an_unknown_profile_is_fatal_and_writes_nothing(tmp_path, role, name):
    r, se, asked = _run_switch(tmp_path, role=role, stack_env=REPLICA_300M, switch=name, body="{}")
    assert r.returncode != 0
    assert "is not an embedding profile embedder_profile.py knows" in r.stderr, r.stderr
    assert se == REPLICA_300M, "stack.env must be byte-for-byte what it was"
    assert asked == [] and not list((tmp_path / "home" / ".mem0").glob("stack.env.tmp.*"))


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_an_unknown_profile_on_a_box_with_no_stack_env_creates_none(tmp_path):
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env=None, switch="nope")
    assert r.returncode != 0 and se is None


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("case,body,curl_exit,why", [
    ("the collection does not exist", _QDRANT_404, 0, "holds no points (or does not exist)"),
    ("the collection exists and is empty", '{"result":{"points_count":0,"status":"green"},"status":"ok"}', 0,
     "holds no points (or does not exist)"),
    ("Qdrant is down", "", 7, "cannot be read"),
    ("Qdrant answers something that is not JSON", "<html>502</html>", 0, "cannot be read"),
])
def test_a_brain_is_refused_a_switch_to_a_space_with_no_points(tmp_path, case, body, curl_exit, why):
    """mem0 creates an absent collection EMPTY: a brain bound to one starts a second, empty store while
    the real one sits in the old space. The check is linux-authority.sh's, made here too."""
    stack_env = "MEM0_ROLE=brain\nMEM0_EMBED_PROFILE=egemma-300m\n"
    r, se, asked = _run_switch(tmp_path, role="brain", stack_env=stack_env, switch="egemma2", body=body, curl_exit=curl_exit)
    assert r.returncode != 0, (case, r.stdout)
    assert "refusing to switch this brain from egemma-300m to egemma2" in r.stderr and why in r.stderr, (case, r.stderr)
    if "holds no points" in why:
        assert "scripts/wsl/embedder-migrate.py" in r.stderr
    assert se == stack_env, (case, "stack.env must not be touched")
    assert asked == ["http://127.0.0.1:6333/collections/mem0_eg2_768"], (case, asked)


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_brain_may_switch_to_a_space_that_holds_points(tmp_path):
    r, se, asked = _run_switch(tmp_path, role="brain",
                               stack_env="MEM0_ROLE=brain\nMEM0_EMBED_PROFILE=egemma-300m\nMEM0_BRAIN_SSH=op@brain-alias\n",
                               switch="egemma2", body=_QDRANT_POINTS)
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"], se
    assert "MEM0_BRAIN_SSH=op@brain-alias" in se
    assert asked == [_QDRANT_URL, _MODELS_URL], "the space is checked first, then the alias"
    assert ("embedding profile switched: egemma-300m -> egemma2 (a brain: 'mem0_eg2_768' holds 16946 points "
            "and llama-swap serves embeddinggemma2; mem0 is restarted below to bind it)") in r.stdout, r.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("case,models,models_exit", [
    ("the alias is not in the list", '{"data":[{"id":"embeddinggemma"},{"id":"bge-reranker-v2-m3"}]}', 0),
    ("only a longer alias is served", '{"data":[{"id":"embeddinggemma2-long"}]}', 0),
    ("llama-swap is down", "", 7),
    ("llama-swap answers something that is not a model list", "<html>502</html>", 0),
])
def test_a_brain_is_refused_a_switch_to_an_alias_llama_swap_does_not_serve(tmp_path, case, models, models_exit):
    """The restart that follows binds mem0 to the new space at once, and every embed call fails until the
    alias is served: the refusal comes first, with stack.env as it was (install/linux-authority.sh's rule)."""
    stack_env = "MEM0_ROLE=brain\nMEM0_EMBED_PROFILE=egemma-300m\n"
    r, se, asked = _run_switch(tmp_path, role="brain", stack_env=stack_env, switch="egemma2", body=_QDRANT_POINTS,
                               models=models, models_exit=models_exit)
    assert r.returncode != 0, (case, r.stdout)
    assert ("refusing to switch this brain from egemma-300m to egemma2: llama-swap on 127.0.0.1:11436 does not "
            "list the alias 'embeddinggemma2'") in r.stderr, (case, r.stderr)
    assert "MEM0_STAGE_EG2=1" in r.stderr and "llama-swap-setup.md" in r.stderr
    assert se == stack_env, (case, "stack.env must not be touched")
    assert asked == [_QDRANT_URL, _MODELS_URL], (case, asked)


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_alias_a_brain_must_find_is_the_one_this_box_resolves_for_the_new_profile(tmp_path):
    """A box that serves the file under another name records it (MEM0_EMBED_MODEL_EGEMMA2); that is the
    alias the server will ask for, so it is the one checked."""
    stack_env = "MEM0_ROLE=brain\nMEM0_EMBED_PROFILE=egemma-300m\nMEM0_EMBED_MODEL_EGEMMA2=eg2-local\n"
    r, se, asked = _run_switch(tmp_path, role="brain", stack_env=stack_env, switch="egemma2", body=_QDRANT_POINTS,
                               models='{"data":[{"id":"eg2-local"}]}')
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"] and "MEM0_EMBED_MODEL_EGEMMA2=eg2-local" in se
    assert "llama-swap serves eg2-local" in r.stdout
    r, se, _ = _run_switch(tmp_path / "other", role="brain", stack_env=stack_env, switch="egemma2", body=_QDRANT_POINTS)
    assert r.returncode != 0 and "does not list the alias 'eg2-local'" in r.stderr, "the stock name does not satisfy it"
    assert se == stack_env


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("pin", [
    "MEM0_QDRANT_COLLECTION=mem0_custom",
    "MEM0_QDRANT_COLLECTION=mem0_egemma_768",   # the old space's own name, pinned
    "MEM0_COLLECTION=memories",                 # the pre-profile key
    "MEM0_QDRANT_COLLECTION=mem0_custom\nMEM0_COLLECTION=mem0_eg2_768",  # the first key wins, as in embedder_profile
])
def test_a_brain_is_refused_when_stack_env_pins_another_collection_over_the_new_space(tmp_path, pin):
    """embedder_profile.collection() lets a pin in stack.env override the profile's collection. A check
    that asked for that bound collection would read the OLD space (it holds points), pass, and leave mem0
    searching it with the new model: both spaces are 768-dim, so nothing errors and /health/deep stays green.
    The new profile's own collection is what must hold the points, and nothing may pin another over it."""
    stack_env = f"MEM0_ROLE=brain\nMEM0_EMBED_PROFILE=egemma-300m\n{pin}\n"
    r, se, asked = _run_switch(tmp_path, role="brain", stack_env=stack_env, switch="egemma2", body=_QDRANT_POINTS)
    assert r.returncode != 0, (pin, r.stdout)
    assert "refusing to switch this brain from egemma-300m to egemma2" in r.stderr and "pins the memories collection to" in r.stderr, r.stderr
    assert "instead of egemma2's 'mem0_eg2_768'" in r.stderr and "Remove that line" in r.stderr
    assert se == stack_env, "stack.env must not be touched"
    assert asked == [], "refused before Qdrant or llama-swap is asked"


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("pin", [
    "MEM0_QDRANT_COLLECTION=mem0_eg2_768",
    "MEM0_COLLECTION=mem0_eg2_768",
    "MEM0_QDRANT_COLLECTION=mem0_eg2_768\nMEM0_COLLECTION=memories",   # the stale legacy key loses to the first
])
def test_a_pin_that_names_the_new_profiles_own_collection_does_not_stop_a_brain_switch(tmp_path, pin):
    r, se, asked = _run_switch(tmp_path, role="brain",
                               stack_env=f"MEM0_ROLE=brain\nMEM0_EMBED_PROFILE=egemma-300m\n{pin}\n",
                               switch="egemma2", body=_QDRANT_POINTS)
    assert r.returncode == 0, (pin, r.stderr)
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"], se
    assert all(ln in se for ln in pin.splitlines()), "the pin is the operator's and is carried"
    assert asked == [_QDRANT_URL, _MODELS_URL]


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("pin", [
    "MEM0_QDRANT_COLLECTION=mem0_custom",
    "MEM0_QDRANT_COLLECTION=mem0_egemma_768",   # the old space's own name, pinned
    "MEM0_COLLECTION=memories",                 # the pre-profile key
    "MEM0_QDRANT_COLLECTION=mem0_custom\nMEM0_COLLECTION=mem0_eg2_768",  # the first key wins, as in embedder_profile
])
def test_a_replica_is_refused_when_stack_env_pins_another_memories_collection_over_the_new_space(tmp_path, pin):
    """Changed in 1.35.1 (this test was test_a_collection_pin_does_not_stop_a_replica_switch, which pinned the
    defect): a replica needs no Qdrant for the check, and a pin it carried into the new space would make
    restore-replica refuse every set (the set holds the new profile's own collection; the server binds the pin).
    So the replica is held to the same rule as a brain, before anything is written."""
    stack_env = REPLICA_300M + pin + "\n"
    marker = _put_marker(tmp_path)
    r, se, asked = _run_switch(tmp_path, role="replica", stack_env=stack_env, switch="egemma2")
    assert r.returncode != 0, (pin, r.stdout)
    assert "refusing to switch this replica from egemma-300m to egemma2" in r.stderr and "pins the memories collection to" in r.stderr, r.stderr
    assert "instead of egemma2's 'mem0_eg2_768'" in r.stderr and "Remove that line" in r.stderr
    assert se == stack_env, "stack.env must not be touched"
    assert asked == [], "a replica is refused without Qdrant or llama-swap being asked"
    assert marker.exists(), "a refused switch leaves the restore marker alone"


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("pin", [
    "MEM0_QDRANT_COLLECTION=mem0_eg2_768",
    "MEM0_COLLECTION=mem0_eg2_768",
    "MEM0_QDRANT_COLLECTION=mem0_eg2_768\nMEM0_COLLECTION=memories",   # the stale legacy key loses to the first
    "MEM0_EPISODES_COLLECTION=episodes_eg2_768",
])
def test_a_pin_that_names_the_new_profiles_own_collection_does_not_stop_a_replica_switch(tmp_path, pin):
    r, se, asked = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M + pin + "\n", switch="egemma2")
    assert r.returncode == 0, (pin, r.stderr)
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"], se
    assert all(ln in se for ln in pin.splitlines()), "the pin is the operator's and is carried"
    assert asked == []


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("role", ["replica", "brain"])
@pytest.mark.parametrize("pin", [
    "MEM0_EPISODES_COLLECTION=episodes_egemma_768",   # the old space's own name, pinned
    "MEM0_EPISODES_COLLECTION=episodes_custom",
])
def test_either_role_is_refused_when_stack_env_pins_another_episodes_collection_over_the_new_space(tmp_path, role, pin):
    """The episodes collection is bound like the memories one (MEM0_EPISODES_COLLECTION over the profile's own), so
    a pin on the old space's keeps the episode fallback on the old vectors. The check names the episodes key and
    runs before Qdrant or llama-swap is asked."""
    stack_env = f"MEM0_ROLE={role}\nMEM0_EMBED_PROFILE=egemma-300m\n{pin}\n"
    r, se, asked = _run_switch(tmp_path, role=role, stack_env=stack_env, switch="egemma2", body=_QDRANT_POINTS)
    assert r.returncode != 0, (role, pin, r.stdout)
    assert f"refusing to switch this {role} from egemma-300m to egemma2" in r.stderr, r.stderr
    assert "pins the episodes collection to" in r.stderr and "MEM0_EPISODES_COLLECTION" in r.stderr, r.stderr
    assert "instead of egemma2's 'episodes_eg2_768'" in r.stderr and "Remove that line" in r.stderr
    assert se == stack_env, "stack.env must not be touched"
    assert asked == []


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_an_episodes_pin_on_the_new_profiles_own_collection_does_not_stop_a_brain_switch(tmp_path):
    r, se, asked = _run_switch(tmp_path, role="brain",
                               stack_env="MEM0_ROLE=brain\nMEM0_EMBED_PROFILE=egemma-300m\nMEM0_EPISODES_COLLECTION=episodes_eg2_768\n",
                               switch="egemma2", body=_QDRANT_POINTS)
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"] and "MEM0_EPISODES_COLLECTION=episodes_eg2_768" in se
    assert asked == [_QDRANT_URL, _MODELS_URL]


# --- 1.35.1: a switch that was made clears the offline watcher's restore marker (a replica's) -------------------
# install.ps1 used to clear it before the WSL phase whenever -EmbedProfile was given on a replica, even when the
# phase then made no switch (profile already recorded, name refused): a stale marker at go_offline forces a restore
# that can throw. The WSL script knows whether a switch happened, and the in-WSL form of the command needs it too.

@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_real_replica_switch_removes_the_restore_marker_and_says_so(tmp_path):
    marker = _put_marker(tmp_path)
    r, se, asked = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M, switch="egemma2")
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"], se
    assert not marker.exists(), "the next go_offline must restore, not trust the old-space restore"
    assert f"cleared {marker} (the replica's last restore is not in the egemma2 space)" in r.stdout, r.stdout
    assert marker.parent.is_dir(), "only the marker goes"
    assert r.stdout.index("stack.env written") < r.stdout.index("cleared "), "after the write that made the switch real"


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_real_replica_switch_with_no_marker_says_none_was_present(tmp_path):
    state = _win_marker(tmp_path).parent
    state.mkdir(parents=True)
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M, switch="egemma2")
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"]
    assert f"no replica-restored.txt marker in {state} (nothing to clear)" in r.stdout, r.stdout
    assert "cleared" not in r.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_real_replica_switch_where_the_windows_profile_is_not_visible_says_so_and_still_succeeds(tmp_path):
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M, switch="egemma2")
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"]
    assert "no Windows state directory at" in r.stdout and "delete %USERPROFILE%" in r.stdout, r.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_marker_that_cannot_be_removed_is_a_warning_not_a_failed_install(tmp_path):
    """The switch is already written when the marker is handled, so an unremovable one (a directory stands in:
    rm -f refuses it) must not abort the installer; it says what to do."""
    marker = _win_marker(tmp_path)
    marker.mkdir(parents=True)
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M, switch="egemma2")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"]
    assert f"WARN: could not clear {marker}" in r.stderr and "up to 24 hours" in r.stderr, r.stderr
    assert marker.is_dir()


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_restore_marker_stays_when_the_profile_is_already_recorded(tmp_path):
    """A switch to the profile already recorded changes nothing, so the last restore is still in the right space."""
    marker = _put_marker(tmp_path)
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env="MEM0_ROLE=replica\nMEM0_EMBED_PROFILE=egemma2\n", switch="egemma2")
    assert r.returncode == 0, r.stderr
    assert "embedding profile is already egemma2" in r.stdout
    assert marker.exists() and "cleared" not in r.stdout and "replica-restored" not in r.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("name", ["egemma-3", "EGEMMA2"])
def test_the_restore_marker_stays_when_the_name_is_refused(tmp_path, name):
    marker = _put_marker(tmp_path)
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M, switch=name)
    assert r.returncode != 0 and se == REPLICA_300M
    assert marker.exists()


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_restore_marker_stays_when_a_pin_refuses_the_switch(tmp_path):
    marker = _put_marker(tmp_path)
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M + "MEM0_EPISODES_COLLECTION=episodes_custom\n",
                           switch="egemma2")
    assert r.returncode != 0
    assert marker.exists()


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_restore_marker_stays_without_a_switch(tmp_path):
    marker = _put_marker(tmp_path)
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M)
    assert r.returncode == 0, r.stderr
    assert marker.exists() and "replica-restored" not in r.stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_brain_switch_never_touches_the_restore_marker(tmp_path):
    """A brain has no offline-watcher marker of its own to clear; a marker that is there is not this installer's."""
    marker = _put_marker(tmp_path)
    r, se, asked = _run_switch(tmp_path, role="brain", stack_env="MEM0_ROLE=brain\nMEM0_EMBED_PROFILE=egemma-300m\n",
                               switch="egemma2", body=_QDRANT_POINTS)
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"] and asked == [_QDRANT_URL, _MODELS_URL]
    assert marker.exists() and "replica-restored" not in r.stdout


def test_the_marker_clearing_reaches_the_windows_profile_the_way_the_other_wsl_scripts_do():
    """/mnt/c/Users/<winuser> (stack-backup.sh, job_liveness.py) unless MEM0_WIN_HOME says otherwise, after the
    stack.env write, and only for a replica whose profile was switched."""
    text = (REPO_ROOT / "install" / "1-wsl-services.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    m = re.search(r'if \[ -n "\$EP_SWITCHED" \] && \[ "\$MEM0_ROLE" = "replica" \]; then\s*'
                  r'RESTORED_DIR="\$\{MEM0_WIN_HOME:-/mnt/c/Users/\$WIN_USER\}/\.claude/state"', code)
    assert m, "the marker is cleared for a switched replica, from the Windows profile"
    assert code.index("stack_env_write") < code.index('echo "  stack.env written') < m.start()


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("role", ["replica", "brain"])
def test_switching_to_the_profile_already_recorded_changes_nothing_and_needs_no_qdrant(tmp_path, role):
    stack_env = f"MEM0_ROLE={role}\nMEM0_EMBED_PROFILE=egemma2\n"
    r, se, asked = _run_switch(tmp_path, role=role, stack_env=stack_env, switch="egemma2", curl_exit=7)
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"]
    assert "embedding profile is already egemma2" in r.stdout and "switched" not in r.stdout
    assert asked == []


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("role,stack_env,expected,note", [
    ("replica", REPLICA_300M, "egemma-300m", "a recorded profile is carried"),
    ("brain", "MEM0_ROLE=brain\nMEM0_EMBED_PROFILE=egemma2\n", "egemma2", "a recorded new profile is carried"),
    ("brain", "MEM0_ROLE=brain\nMEM0_WSL_USER=t\n", "egemma-300m", "an unrecorded receipt is the legacy space"),
    ("replica", None, "egemma2", "a fresh box starts on the default"),
])
def test_without_the_switch_the_installer_behaves_as_it_did_in_1_35_0(tmp_path, role, stack_env, expected, note):
    """No MEM0_SET_EMBED_PROFILE: the profile is carried or recorded and nothing else is said or asked."""
    r, se, asked = _run_switch(tmp_path, role=role, stack_env=stack_env, body='{"result":{"points_count":9}}')
    assert r.returncode == 0, (note, r.stderr)
    assert _profile_lines(se) == [f"MEM0_EMBED_PROFILE={expected}"], (note, se)
    assert "switched" not in r.stdout and "MEM0_SET_EMBED_PROFILE" not in r.stdout + r.stderr, note
    assert asked == [], note


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_an_empty_switch_variable_is_the_same_as_none(tmp_path):
    r, se, asked = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M, switch="")
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma-300m"] and asked == []


def test_the_gguf_staging_and_the_probe_read_the_profile_after_the_switch_is_written():
    """The EmbeddingGemma-2 GGUF + projector are staged when the ACTIVE profile (stack.env, as just
    written) is egemma2, so a switch to it fetches them on the same run: the write comes first, and the
    staging asks embedder_profile, never the env variable."""
    text = (REPO_ROOT / "install" / "1-wsl-services.sh").read_text(encoding="utf-8")
    write = text.index('if ! stack_env_write "$USER_HOME/.mem0/stack.env"')
    staging = text.index("EG2_WANTED=")
    probe = text.index("EMBED_PROFILE=\"$( export HOME=\"$USER_HOME\"; ep_py 'print(ep.active().name)' )\"")
    assert text.index("MEM0_SET_EMBED_PROFILE:-") < write < staging < probe
    wanted = text[staging:text.index("\n", staging)]
    assert "ep.active().name" in wanted and "MEM0_SET_EMBED_PROFILE" not in wanted
    assert 'stage_gguf "$EG2_GGUF"' in text and 'stage_gguf "$EG2_MMPROJ"' in text


def test_a_switched_brain_restarts_mem0_to_bind_the_new_space_and_a_replica_does_not():
    code = "\n".join(ln for ln in (REPO_ROOT / "install" / "1-wsl-services.sh").read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))
    m = re.search(r'if \[ -n "\$EP_SWITCHED" \] && \[ "\$MEM0_ROLE" != "replica" \]; then\s*systemctl --user restart mem0\.service', code)
    assert m, "a switched brain must restart mem0 (enable --now leaves a running server on its old collections)"
    assert code.index("enable_brain_unit mem0.service") < m.start() < code.index("enable_brain_unit l10-audit.timer")


def test_the_installer_documents_the_switch_where_an_operator_meets_it():
    inst = (REPO_ROOT / "install" / "1-wsl-services.sh").read_text(encoding="utf-8")
    assert "MEM0_SET_EMBED_PROFILE=<profile>" in inst and "install.ps1 -EmbedProfile" in inst


# --- 1.35.1: the per-box media switch (MEM0_SET_MEDIA_EMBEDDER, install.ps1 -MediaEmbedder) -------------------

def _media_lines(stack_env_text):
    return [ln for ln in (stack_env_text or "").splitlines() if ln.startswith("MEM0_MEDIA_EMBEDDER=")]


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_media_switch_records_one_line_and_later_runs_carry_it(tmp_path):
    """A text-only replica records MEM0_MEDIA_EMBEDDER=off once; a plain re-run keeps it (it is an operator key),
    and switching it back replaces the line rather than adding a second one."""
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M, media="off")
    assert r.returncode == 0, r.stderr
    assert _media_lines(se) == ["MEM0_MEDIA_EMBEDDER=off"], se
    assert "media embedder recorded: off" in r.stdout
    assert "MEM0_BRAIN_SSH=op@brain-alias" in se and _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma-300m"]
    r, se2, _ = _run_switch(tmp_path, role="replica", stack_env=se)
    assert r.returncode == 0, r.stderr
    assert _media_lines(se2) == ["MEM0_MEDIA_EMBEDDER=off"], "a re-run without the switch keeps the recorded value"
    r, se3, _ = _run_switch(tmp_path, role="replica", stack_env=se2, media="on")
    assert r.returncode == 0, r.stderr
    assert _media_lines(se3) == ["MEM0_MEDIA_EMBEDDER=on"], se3


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_media_switch_together_with_a_profile_switch(tmp_path):
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M, switch="egemma2", media="off")
    assert r.returncode == 0, r.stderr
    assert _profile_lines(se) == ["MEM0_EMBED_PROFILE=egemma2"] and _media_lines(se) == ["MEM0_MEDIA_EMBEDDER=off"], se


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("bad", ["Off", "yes", "1", "of f"])
def test_a_bad_media_switch_value_is_refused_before_stack_env_is_written(tmp_path, bad):
    r, se, _ = _run_switch(tmp_path, role="replica", stack_env=REPLICA_300M, media=bad)
    assert r.returncode != 0 and "must be on or off" in r.stderr, (r.stdout, r.stderr)
    assert se == REPLICA_300M, "nothing written"
