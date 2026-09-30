"""ams-dream-now.sh: one dream run started by hand, on the authority, outside the chain guard.

The nightly dream is `ams-step-dream.service`, a chain step. Run from a shell it does nothing
useful: it needs two systemd credentials and three Environment= lines the unit supplies, and its
`--guarded` turns it into a receipted no-op once the night has succeeded. The helper starts a
transient user unit that reproduces the credentials and the environment and runs the unit's own
command without `--guarded`. `systemd-run` is a fake on PATH that records its argv, so these tests
pin the exact call: nothing here needs systemd, a credential, Codex or a network.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parents[1]
HELPER = SCRIPTS / "ams-dream-now.sh"
UNIT = REPO_ROOT / "systemd" / "ams-step-dream.service"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or os.name == "nt", reason="needs a POSIX bash")

FAKE_SYSTEMD_RUN = """#!/bin/bash
printf '%s\\0' "$@" > "$FAKE_ARGV"
printf '%s' "${XDG_RUNTIME_DIR:-}" > "$FAKE_ARGV.xdg"
exit "${FAKE_RC:-0}"
"""


def _home_env(home, **extra):
    """The child env with the home redirected on every platform: HOME alone leaves a child whose `~` is
    read from USERPROFILE (or HOMEDRIVE+HOMEPATH) writing into the real profile."""
    h = str(home)
    drive, tail = os.path.splitdrive(h)
    return dict(os.environ, HOME=h, USERPROFILE=h, HOMEDRIVE=drive, HOMEPATH=tail, **extra)


class Box:
    """A sandboxed authority: HOME with a stack.env receipt, the two .cred files, the deployed
    scripts, and a fake systemd-run first on PATH."""

    def __init__(self, tmp: Path, *, stack_env="default", role_file="brain"):
        self.home = tmp / "home"
        self.sec = tmp / "secrets"
        self.bin = tmp / "bin"
        self.argv_file = tmp / "argv"
        for d in (self.home / ".mem0", self.sec, self.bin, self.home / "apps" / "mem0-scripts",
                  self.home / "apps" / "mem0-server" / ".venv" / "bin"):
            d.mkdir(parents=True)
        for c in ("ams-api-key", "ams-canonical-key"):
            (self.sec / f"{c}.cred").write_text("not a real credential\n", encoding="utf-8")
        for f in ("ams-step.sh", "dream-consolidate.py", "codex-usage-report.py"):
            (self.home / "apps" / "mem0-scripts" / f).write_text("", encoding="utf-8")
        py = self.home / "apps" / "mem0-server" / ".venv" / "bin" / "python"
        py.write_text("", encoding="utf-8")
        py.chmod(0o755)
        if stack_env == "default":
            stack_env = (f"MEM0_HOST_KIND=native\nMEM0_BIND=192.0.2.10\nMEM0_ROLE=brain\n"
                         f"MEM0_SECRETS_DIR={self.sec}\n")
        if stack_env is not None:
            (self.home / ".mem0" / "stack.env").write_text(stack_env, encoding="utf-8")
        if role_file is not None:
            (self.home / ".mem0" / "role").write_text(role_file + "\n", encoding="utf-8")
        fake = self.bin / "systemd-run"
        fake.write_text(FAKE_SYSTEMD_RUN, encoding="utf-8")
        fake.chmod(0o755)

    def run(self, *args, rc=0, xdg=None):
        env = _home_env(self.home, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_ARGV=str(self.argv_file),
                        FAKE_RC=str(rc))
        for k in [k for k in env if k.startswith("MEM0_")] + ["XDG_RUNTIME_DIR"]:
            env.pop(k, None)
        if xdg is not None:
            env["XDG_RUNTIME_DIR"] = xdg
        return subprocess.run([BASH, str(HELPER), *args], capture_output=True, text=True, env=env, timeout=60)

    def argv(self):
        return self.argv_file.read_bytes().decode("utf-8").split("\0")[:-1] if self.argv_file.exists() else None


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def _properties(argv):
    """The values of every `-p <property>` pair, in order."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]


def test_the_exact_systemd_run_call(box):
    r = box.run()
    assert r.returncode == 0, r.stderr
    argv = box.argv()
    home, sec = box.home, box.sec
    py = f"{home}/apps/mem0-server/.venv/bin/python"
    scripts = f"{home}/apps/mem0-scripts"

    unit = argv[4]
    assert re.fullmatch(r"--unit=ams-dream-now-\d{8}T\d{6}Z", unit), unit
    assert argv[:4] == ["--user", "--pipe", "--wait", "--collect"]
    assert argv[5:] == [
        "-p", f"LoadCredentialEncrypted=ams-api-key:{sec}/ams-api-key.cred",
        "-p", f"LoadCredentialEncrypted=ams-canonical-key:{sec}/ams-canonical-key.cred",
        "-p", "Environment=MEM0_HOST_KIND=native",
        "-p", "Environment=MEM0_CODEX_TRANSPORT=native",
        "-p", f"Environment=CODEX_HOME={sec}/codex",
        "-p", f"ExecStartPre=-{py} {scripts}/codex-usage-report.py --probe",
        "/bin/bash", "-c", 'export MEM0_API_KEY_FILE="$CREDENTIALS_DIRECTORY/ams-api-key"; exec "$@"', "_",
        "/bin/bash", f"{scripts}/ams-step.sh", "dream", py, f"{scripts}/dream-consolidate.py", "--force",
    ]
    assert "--guarded" not in argv and "--guard" not in argv, "the chain guard would turn the run into a no-op"


def test_the_call_is_the_units_own_credentials_environment_and_command(box):
    """Drift guard: the helper must carry exactly what ams-step-dream.service carries. A credential or
    Environment= line added to the unit and not to the helper makes the hand run fail or behave
    differently from the night, and only this test says so."""
    box.run()
    argv = box.argv()
    props = _properties(argv)
    unit = UNIT.read_text(encoding="utf-8")
    sub = {"__SECRETS_DIR__": str(box.sec), "%h": str(box.home)}

    def render(s):
        for k, v in sub.items():
            s = s.replace(k, v)
        return s

    creds = [render(m) for m in re.findall(r"^LoadCredentialEncrypted=(.+)$", unit, re.M)]
    assert len(creds) == 2
    assert [p.split("=", 1)[1] for p in props if p.startswith("LoadCredentialEncrypted=")] == creds

    wrapper = argv[argv.index("-c") + 1]
    envs = [render(m) for m in re.findall(r"^Environment=(.+)$", unit, re.M)]
    assert envs, "the unit sets its environment with Environment= lines"
    for line in envs:
        name, value = line.split("=", 1)
        if value.startswith("%d/"):
            # systemd-run does not expand %d in -p (measured: the process sees the literal "%d/..."), so a
            # credential path is exported inside the command from $CREDENTIALS_DIRECTORY
            assert f'export {name}="$CREDENTIALS_DIRECTORY/{value[3:]}"' in wrapper, line
        else:
            assert f"Environment={line}" in props, line

    pre = [render(m) for m in re.findall(r"^ExecStartPre=(.+)$", unit, re.M)]
    assert [p.split("=", 1)[1] for p in props if p.startswith("ExecStartPre=")] == pre

    start = render(re.search(r"^ExecStart=(.+)$", unit, re.M).group(1))
    assert " --guarded " in start
    tail = " ".join(argv[argv.index("_") + 1:])
    assert tail == start.replace(" --guarded ", " ") + " --force", (tail, start)


def test_a_dream_throttled_by_its_own_23_hour_rule_is_not_a_hand_run(box):
    """dream-consolidate.py skips ("nightly throttle (23h) not yet elapsed") when it ran less than
    23 h ago, and after the nightly run it always did. The helper's whole job is to run now, so it
    passes --force, which bypasses that throttle only (the judge lock and the quota gate stay)."""
    box.run()
    assert box.argv()[-1] == "--force"


def test_extra_arguments_are_refused_before_anything_starts(box):
    """A --dry-run would still be receipted as a `dream` step and refresh the dream's freshness."""
    r = box.run("--dry-run")
    assert r.returncode == 64
    assert box.argv() is None
    assert "usage" in r.stderr


def test_help_prints_usage_and_starts_nothing(box):
    r = box.run("--help")
    assert r.returncode == 0
    assert box.argv() is None
    assert "ams-dream-now.sh" in r.stdout + r.stderr


@pytest.mark.parametrize("stack_env,role_file", [
    ("MEM0_HOST_KIND=native\nMEM0_ROLE=replica\nMEM0_SECRETS_DIR=/x\n", "replica"),
    ("MEM0_HOST_KIND=native\nMEM0_ROLE=replica\nMEM0_SECRETS_DIR=/x\n", "brain"),   # stack.env is the receipt
    (None, "replica"),
    (None, None),                                                                    # no evidence of being the authority
])
def test_it_refuses_to_run_anywhere_but_the_authority(tmp_path, stack_env, role_file):
    b = Box(tmp_path, stack_env=stack_env, role_file=role_file)
    r = b.run()
    assert r.returncode == 3
    assert b.argv() is None, "systemd-run must not be reached on a box that is not the authority"
    assert "ssh <brain-alias> 'bash ~/apps/mem0-scripts/ams-dream-now.sh'" in r.stderr


def test_the_role_file_stands_in_when_stack_env_has_no_role(tmp_path):
    b = Box(tmp_path, stack_env=f"MEM0_HOST_KIND=native\nMEM0_SECRETS_DIR={tmp_path / 'secrets'}\n", role_file="brain")
    assert b.run().returncode == 0
    assert b.argv() is not None


def test_a_wsl_brain_is_refused_with_its_own_way_to_force_a_dream(tmp_path):
    b = Box(tmp_path, stack_env="MEM0_ROLE=brain\nMEM0_DISTRO=Ubuntu\n")
    r = b.run()
    assert r.returncode == 3
    assert b.argv() is None
    assert "MEM0_HOST_KIND" in r.stderr and "dream-consolidate.ps1" in r.stderr


def test_an_authority_without_a_recorded_secrets_dir_says_how_to_record_it(tmp_path):
    b = Box(tmp_path, stack_env="MEM0_HOST_KIND=native\nMEM0_ROLE=brain\n")
    r = b.run()
    assert r.returncode == 2
    assert b.argv() is None
    assert "MEM0_SECRETS_DIR" in r.stderr and "linux-authority.sh" in r.stderr


@pytest.mark.parametrize("missing", ["ams-api-key.cred", "ams-canonical-key.cred"])
def test_a_missing_credential_file_stops_before_the_unit_starts(box, missing):
    (box.sec / missing).unlink()
    r = box.run()
    assert r.returncode == 2
    assert box.argv() is None
    assert missing in r.stderr


def test_a_missing_deployed_script_stops_before_the_unit_starts(box):
    (box.home / "apps" / "mem0-scripts" / "dream-consolidate.py").unlink()
    r = box.run()
    assert r.returncode == 2
    assert box.argv() is None
    assert "dream-consolidate.py" in r.stderr


def test_the_units_exit_status_is_the_helpers(box):
    """systemd-run --wait returns the service's status; a failed dream must not read as success."""
    assert box.run(rc=5).returncode == 5


def test_the_user_bus_is_found_over_a_plain_ssh_command(box):
    """`ssh <alias> 'bash ...'` may reach a shell without XDG_RUNTIME_DIR; systemd-run --user needs it."""
    box.run()
    assert (box.argv_file.parent / "argv.xdg").read_text(encoding="utf-8") == f"/run/user/{os.getuid()}"
    box.run(xdg="/run/user/4242")
    assert (box.argv_file.parent / "argv.xdg").read_text(encoding="utf-8") == "/run/user/4242"


def test_the_helper_is_deployed_with_the_chain_scripts():
    """linux-authority.sh copies scripts/wsl/*.sh (CR stripped, chmod +x) into ~/apps/mem0-scripts, which
    is where the refusal message tells a replica's operator to find it."""
    inst = (REPO_ROOT / "install" / "linux-authority.sh").read_text(encoding="utf-8")
    assert '"$REPO_ROOT"/scripts/wsl/*.sh' in inst and '"$SCRIPTS_DIR"' in inst
    assert HELPER.name in (REPO_ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
