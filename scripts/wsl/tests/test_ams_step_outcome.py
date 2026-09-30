"""ams-step.sh outcome contract (C1): a job that exits 0 may still say it did nothing.

The job writes ONE line to $AMS_OUTCOME_FILE - `<status>[:<reason>] <json-counts>` - and the
receipt carries it as `status` + `work`. Exit 0 with no line, or with `ok`, stays a plain success;
the guard and --weekly no-ops keep `status:"ok"`."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _home_env(home, **extra):
    """The child env with the home redirected on every platform: HOME alone leaves a child whose `~` is
    read from USERPROFILE (or HOMEDRIVE+HOMEPATH) writing into the real profile."""
    h = str(home)
    drive, tail = os.path.splitdrive(h)
    return dict(os.environ, HOME=h, USERPROFILE=h, HOMEDRIVE=drive, HOMEPATH=tail, **extra)


def _step(tmp_path, args):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = _home_env(home)
    for k in ("MEM0_URL", "AMS_OUTCOME_FILE"):
        env.pop(k, None)
    r = subprocess.run([BASH, str(SCRIPTS / "ams-step.sh"), *args], capture_output=True, text=True, env=env, timeout=60)
    rp = home / ".mem0" / "maintenance" / "receipts.jsonl"
    rows = [json.loads(ln) for ln in rp.read_text(encoding="utf-8").splitlines() if ln.strip()] if rp.exists() else []
    return r, rows, home


def _job(script):
    return ["demo", "bash", "-c", script]


def test_degraded_outcome_is_receipted_with_reason_and_work(tmp_path):
    r, rows, _ = _step(tmp_path, _job(
        'echo \'degraded:posted-0-of-3 {"consolidated":3,"posted":0}\' > "$AMS_OUTCOME_FILE"; exit 0'))
    assert r.returncode == 0, r.stderr
    row = rows[-1]
    assert row["ok"] is True and row["status"] == "degraded" and row["exit"] == 0
    assert row["note"] == "posted-0-of-3" and row["work"] == {"consolidated": 3, "posted": 0}
    # the receipt line itself carries the fields in the documented spelling
    line = (tmp_path / "home" / ".mem0" / "maintenance" / "receipts.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    assert '"status":"degraded"' in line and '"work":{"consolidated":3,"posted":0}' in line


def test_nonzero_exit_is_failed_and_keeps_the_stderr_tail(tmp_path):
    r, rows, _ = _step(tmp_path, _job('echo boom >&2; exit 3'))
    assert r.returncode == 3
    row = rows[-1]
    assert row["ok"] is False and row["status"] == "failed" and row["exit"] == 3 and "boom" in row["note"]
    assert row["work"] == {}


def test_exit_zero_with_failed_outcome_is_not_ok(tmp_path):
    r, rows, _ = _step(tmp_path, _job('echo \'failed:stale-set {}\' > "$AMS_OUTCOME_FILE"'))
    assert r.returncode == 0
    row = rows[-1]
    assert row["ok"] is False and row["status"] == "failed" and row["exit"] == 0
    assert row["note"] == "stale-set" and row["work"] == {}


def test_failed_outcome_never_stamps_the_chain(tmp_path):
    """A guarded stamping step that reports failed:* must not write last-chain-success."""
    _, rows, home = _step(tmp_path, ["--guard", "demo", "bash", "-c", 'echo "failed:no-manifest {}" > "$AMS_OUTCOME_FILE"'])
    assert rows[-1]["ok"] is False
    assert not (home / ".mem0" / "maintenance" / "last-chain-success").exists()
    _, rows, home = _step(tmp_path, ["--guard", "demo", "bash", "-c", 'echo "degraded:x {}" > "$AMS_OUTCOME_FILE"'])
    assert rows[-1]["ok"] is True and rows[-1]["status"] == "degraded"
    assert (home / ".mem0" / "maintenance" / "last-chain-success").exists(), "degraded is a success that says so"


def test_no_outcome_or_ok_outcome_is_plain_success(tmp_path):
    _, rows, _ = _step(tmp_path, _job("exit 0"))
    assert rows[-1]["ok"] is True and rows[-1]["status"] == "ok" and rows[-1]["work"] == {} and rows[-1]["note"] == ""
    _, rows, _ = _step(tmp_path, _job('echo \'ok {"scanned":16,"deleted":0}\' > "$AMS_OUTCOME_FILE"'))
    assert rows[-1]["status"] == "ok" and rows[-1]["work"] == {"scanned": 16, "deleted": 0} and rows[-1]["note"] == ""
    _, rows, _ = _step(tmp_path, _job('echo ok > "$AMS_OUTCOME_FILE"; echo not-a-note'))
    assert rows[-1]["status"] == "ok" and rows[-1]["note"] == "", "an ok step never borrows its stdout as a note"


@pytest.mark.parametrize("garbage", ["banana", "degraded:x {not json}", "degraded:x [1,2]", "warn:x {}"])
def test_garbage_outcome_is_degraded_and_unparsable(tmp_path, garbage):
    r, rows, _ = _step(tmp_path, _job(f"printf '%s\\n' '{garbage}' > \"$AMS_OUTCOME_FILE\""))
    assert r.returncode == 0
    row = rows[-1]
    assert row["ok"] is True and row["status"] == "degraded"
    assert row["note"].startswith("outcome-unparsable") and garbage[:20] in row["note"]


def test_unparsable_note_is_clipped_to_120_chars(tmp_path):
    _, rows, _ = _step(tmp_path, _job('python3 -c "print(\'z\'*500)" > "$AMS_OUTCOME_FILE"'))
    note = rows[-1]["note"]
    assert note.startswith("outcome-unparsable: ") and len(note) == len("outcome-unparsable: ") + 120


def test_weekly_skip_and_guard_no_op_keep_status_ok(tmp_path):
    r, rows, home = _step(tmp_path, ["--weekly", "Zzz", "demo", "true"])
    assert rows[-1]["ok"] is True and rows[-1]["status"] == "ok" and rows[-1]["work"] == {}
    assert rows[-1]["note"].startswith("weekly:")
    mp = home / ".mem0" / "maintenance"
    (mp / "last-chain-success").write_text("4102444800\n")   # year 2100: newer than any boundary
    env = _home_env(home, AMS_GUARD_NOW="4102444900")
    env.pop("MEM0_URL", None)
    subprocess.run([BASH, str(SCRIPTS / "ams-step.sh"), "--guard", "demo", "true"], env=env, timeout=60, capture_output=True)
    last = json.loads((mp / "receipts.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["note"].startswith("guard:") and last["status"] == "ok" and last["ok"] is True


def test_last_stdout_line_becomes_the_note_when_status_is_not_ok(tmp_path):
    """journald drops stdout that has no unit cgroup; the receipt must carry the evidence itself."""
    _, rows, _ = _step(tmp_path, _job('echo first; echo "index kept as-is (last pull 48 h ago)"; echo \'degraded {}\' > "$AMS_OUTCOME_FILE"'))
    assert rows[-1]["status"] == "degraded" and rows[-1]["note"] == "index kept as-is (last pull 48 h ago)"
    _, rows, _ = _step(tmp_path, _job('echo "last words"; exit 4'))
    assert rows[-1]["status"] == "failed" and rows[-1]["note"] == "last words", "empty stderr falls back to stdout"
    _, rows, _ = _step(tmp_path, _job('echo out-line; echo err-line >&2; exit 4'))
    assert rows[-1]["note"] == "err-line", "stderr stays the note when there is one"
    _, rows, _ = _step(tmp_path, _job('echo out-line; echo \'degraded:why {}\' > "$AMS_OUTCOME_FILE"'))
    assert rows[-1]["note"] == "why", "a reason beats the stdout fallback"


def test_stdout_still_reaches_the_caller(tmp_path):
    r, _, _ = _step(tmp_path, _job("echo visible-on-journal"))
    assert "visible-on-journal" in r.stdout


def test_outcome_file_is_private_to_the_run_and_removed(tmp_path):
    r, rows, home = _step(tmp_path, _job('echo "$AMS_OUTCOME_FILE"'))
    path = r.stdout.strip()
    assert path and not Path(path).exists(), "the mktemp file does not outlive the step"
    # a stale line left by an earlier run cannot leak into the next one
    _, rows, _ = _step(tmp_path, _job("exit 0"))
    assert rows[-1]["status"] == "ok"
