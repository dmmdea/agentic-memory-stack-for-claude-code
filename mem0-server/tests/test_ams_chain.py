# mem0-server/tests/test_ams_chain.py
"""One nightly chain on the authority (spec §4): timer → target, steps hang off it with
WantedBy=/After= (never Requires=), each step runs through ams-step.sh which writes a receipt."""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = REPO_ROOT / "systemd"
SCRIPTS = REPO_ROOT / "scripts" / "wsl"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

STEPS = ["stack-backup", "health-stamp", "rtcwake"]
ORDER = ["dream", "semantic-dedup", "index-refresh", "goal-recurrence-promote", "decay-scan", "goals-stale-sweep",
         "contradiction-sweep", "episodic-reconcile", "retrieval-pairs", "stack-backup", "syncoid", "pcloud-copy",
         "morning-summary", "health-stamp", "rtcwake"]
WEEKLY = {"decay-scan", "goals-stale-sweep", "contradiction-sweep", "episodic-reconcile", "retrieval-pairs"}
PYTHON = {"dream", "semantic-dedup", "index-refresh", "goal-recurrence-promote", "decay-scan", "goals-stale-sweep",
          "contradiction-sweep", "episodic-reconcile", "retrieval-pairs"}
CODEX = {"dream", "contradiction-sweep", "retrieval-pairs"}
# Every step AFTER the stamping stack-backup step runs unguarded: the first v1.22.1 chain no-op'd
# syncoid, pcloud-copy and morning-summary because their predecessor had just stamped the night.
UNGUARDED = {"syncoid", "pcloud-copy", "morning-summary", "health-stamp", "rtcwake"}


def test_every_step_is_a_unit_in_chain_order():
    """spec §4: dream -> dedup -> ... -> backup -> off-box copies -> summary -> health -> rtcwake, each a
    WantedBy=/After= step (never Requires=), weekly ones gated, Python ones carrying their own key."""
    prev = None
    for s in ORDER:
        t = (SYSTEMD / f"ams-step-{s}.service").read_text(encoding="utf-8")
        assert "Requires=" not in t and "WantedBy=ams-nightly.target" in t and "PartOf=ams-nightly.target" in t
        if prev:
            assert f"ams-step-{prev}.service" in t.split("After=", 1)[1].splitlines()[0], f"{s} must run after {prev}"
        assert f" {s} " in t and "/tmp/" not in t
        if s in WEEKLY:
            assert "--weekly Sun" in t
        else:
            assert "--weekly" not in t
        if s == "stack-backup":
            assert "--guard stack-backup" in t, "the backup is the step that stamps last-chain-success"
        elif s in UNGUARDED:
            assert "--guard" not in t
            assert ORDER.index(s) > ORDER.index("stack-backup"), "only steps after the stamping step may run unguarded"
        else:
            assert f"--guarded {s}" in t or f"--guarded --weekly Sun {s}" in t, f"{s} must be check-only guarded"
        if s in PYTHON:
            assert "LoadCredentialEncrypted=ams-api-key:__SECRETS_DIR__/ams-api-key.cred" in t
            assert "Environment=MEM0_API_KEY_FILE=%d/ams-api-key" in t and "Environment=MEM0_HOST_KIND=native" in t
            assert "/home/" not in t and "%h/apps/mem0-server/.venv/bin/python" in t
        if s in CODEX:
            assert "CODEX_HOME=__SECRETS_DIR__/codex" in t and "codex-shim-spawn" not in t and "18792" not in t
        else:
            assert "CODEX_HOME" not in t
        prev = s
    dream = (SYSTEMD / "ams-step-dream.service").read_text(encoding="utf-8")
    assert "ams-canonical-key:__SECRETS_DIR__/ams-canonical-key.cred" in dream, "autopromote signs with the canonical credential"
    assert "codex-usage-report.py --probe" in dream, "the window probe runs before the quota gate"
    assert "RequiresMountsFor=%h/pCloudDrive" in (SYSTEMD / "ams-step-pcloud-copy.service").read_text(encoding="utf-8")
    assert "ConditionPathExists=%h/.mem0/scripts/syncoid.sh" in (SYSTEMD / "ams-step-syncoid.service").read_text(encoding="utf-8")
    for f in ("ams-pcloud-copy.sh", "ams-morning-summary.sh"):
        r = subprocess.run([BASH, "-n", str(SCRIPTS / f)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr


def test_guarded_checks_but_never_stamps(tmp_path):
    r, rows, home = _step(tmp_path, ["--guarded", "demo", "true"])
    assert r.returncode == 0 and not (home / ".mem0" / "maintenance" / "last-chain-success").exists()
    r, rows, home = _step(tmp_path, ["--guard", "demo", "true"])
    stamp = home / ".mem0" / "maintenance" / "last-chain-success"
    before = stamp.read_text()
    r, rows, _ = _step(tmp_path, ["--guarded", "demo", "bash", "-c", "exit 9"], env_extra={"AMS_GUARD_NOW": str(int(before) + 60)})
    assert r.returncode == 0 and rows[-1]["note"].startswith("guard: chain succeeded since")
    stamp.write_text(str(int(before) - 2 * 86400))
    r, rows, _ = _step(tmp_path, ["--guarded", "demo", "true"])
    assert r.returncode == 0 and rows[-1]["note"] == "" and stamp.read_text() == str(int(before) - 2 * 86400), "check-only: the stamp is untouched"


def test_pcloud_copy_copies_the_newest_set_only(tmp_path):
    home = tmp_path / "home"
    b = home / ".mem0" / "backups"
    b.mkdir(parents=True)
    for ts in ("20260910-033116", "20260911-031903"):
        for f in (f"manifest-{ts}.json", f"qdrant-{ts}.snapshot", f"history-{ts}.db"):
            (b / f).write_text("x")
    dst = tmp_path / "pcloud"
    dst.mkdir()
    env = dict(os.environ, HOME=str(home), MEM0_PCLOUD_DIR=str(dst))
    r = subprocess.run([BASH, str(SCRIPTS / "ams-pcloud-copy.sh")], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in dst.iterdir()) == ["history-20260911-031903.db", "manifest-20260911-031903.json", "qdrant-20260911-031903.snapshot"]
    r = subprocess.run([BASH, str(SCRIPTS / "ams-pcloud-copy.sh")], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "3 file(s)" in r.stdout, "idempotent re-run"
    env["MEM0_PCLOUD_DIR"] = str(tmp_path / "absent" / "host")
    r = subprocess.run([BASH, str(SCRIPTS / "ams-pcloud-copy.sh")], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 3 and "not a directory" in r.stderr, "an unmounted destination is a FAILED step"
    (tmp_path / "mounted").mkdir()
    env["MEM0_PCLOUD_DIR"] = str(tmp_path / "mounted" / "host")
    r = subprocess.run([BASH, str(SCRIPTS / "ams-pcloud-copy.sh")], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and (tmp_path / "mounted" / "host" / "manifest-20260911-031903.json").exists(), "the leaf under a mounted parent is created"


def test_morning_summary_section_from_receipts(tmp_path):
    home = tmp_path / "home"
    d = home / ".mem0" / "maintenance"
    (d / "dream").mkdir(parents=True)
    (d / "receipts.jsonl").write_text(
        '{"ts":"2099-01-01T08:00:00Z","step":"dream","ok":true,"exit":0,"duration_ms":40000,"receipt_id":"x","note":""}\n'
        '{"ts":"2098-12-01T08:00:00Z","step":"old","ok":true,"exit":0,"duration_ms":1,"receipt_id":"y","note":""}\n'
        '{"ts":"2099-01-01T08:01:00Z","step":"pcloud-copy","ok":false,"exit":3,"duration_ms":12,"receipt_id":"z","note":"not a directory"}\n')
    (d / "health-maintenance.json").write_text('{"ok":true,"stale_steps":[],"pool":{"used_pct":78.0},"usage":{"used_percent":2}}')
    (d / "dream" / "gather.json").write_text('{"signals":[{"kind":"decision"}],"tokens":123,"dry_run":false}')
    env = dict(os.environ, HOME=str(home), AMS_SUMMARY_NOW="2099-01-01T09:00:00Z")
    r = subprocess.run([BASH, str(SCRIPTS / "ams-morning-summary.sh")], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    t = (d / "morning-summary.md").read_text(encoding="utf-8")
    assert t.startswith("\n## Chain -- 2099-01-01 09:00") and "- dream ok 40000ms" in t and "pcloud-copy FAILED 12ms -- not a directory" in t
    assert "- old ok" not in t and "pool 78.0% usage 2%" in t and "dream: 1 signal(s), 123 tokens" in t


def test_chain_units_exist_and_never_require():
    timer = (SYSTEMD / "ams-nightly.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03:00:00" in timer and "Persistent=true" in timer and "OnBootSec=15min" in timer
    assert "Unit=ams-nightly.target" in timer
    for s in STEPS:
        text = (SYSTEMD / f"ams-step-{s}.service").read_text(encoding="utf-8")
        assert "Requires=" not in text
        assert "WantedBy=ams-nightly.target" in text
        assert "PartOf=ams-nightly.target" in text
        assert "ams-step.sh " in text and (" " + s + " ") in text
    assert "ams-step-morning-summary.service" in (SYSTEMD / "ams-step-health-stamp.service").read_text(encoding="utf-8")
    assert "ams-step-health-stamp.service" in (SYSTEMD / "ams-step-rtcwake.service").read_text(encoding="utf-8")
    for f in ("ams-step.sh", "ams-rtcwake-arm.sh", "ams-health-stamp.sh"):
        r = subprocess.run([BASH, "-n", str(SCRIPTS / f)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr


def _step(tmp_path, args, env_extra=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home))
    env.pop("MEM0_URL", None)
    env.update(env_extra or {})
    r = subprocess.run([BASH, str(SCRIPTS / "ams-step.sh"), *args], capture_output=True, text=True, env=env, timeout=60)
    rp = home / ".mem0" / "maintenance" / "receipts.jsonl"
    rows = [json.loads(ln) for ln in rp.read_text(encoding="utf-8").splitlines() if ln.strip()] if rp.exists() else []
    return r, rows, home


def test_step_writes_a_receipt_with_duration_and_exit(tmp_path):
    r, rows, _ = _step(tmp_path, ["demo", "bash", "-c", "sleep 0.2; echo hi"])
    assert r.returncode == 0, r.stderr
    assert rows[-1]["step"] == "demo" and rows[-1]["ok"] is True
    assert 150 <= rows[-1]["duration_ms"] < 5000, "milliseconds, on GNU and uutils date alike (uutils prints ns for %3N)"
    assert re.fullmatch(r"demo-\d{8}T\d{6}Z-[0-9a-f]{6}", rows[-1]["receipt_id"])
    r, rows, _ = _step(tmp_path, ["demo", "bash", "-c", "echo bad >&2; exit 23"])
    assert r.returncode == 23
    assert rows[-1]["ok"] is False and "bad" in rows[-1]["note"] and rows[-1]["exit"] == 23


def test_receipt_note_keeps_the_tail_of_a_long_stderr(tmp_path):
    """The receipt note is the only diagnostic of an unattended 3 am failure; a stderr of
    thousands of lines must still leave its last 400 bytes in the note (no substitution race)."""
    r, rows, _ = _step(tmp_path, ["demo", "bash", "-c", "for i in $(seq 1 5000); do echo line-$i >&2; done; exit 7"])
    assert r.returncode == 7
    assert "line-5000" in rows[-1]["note"] and rows[-1]["ok"] is False
    assert "line-5000" in r.stderr, "stderr is still echoed to the caller (the journal)"


def _local(s):
    return str(int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))))


def test_guard_is_calendar_aware(tmp_path):
    """The first rule ('< 20 h') let a 22:19 hand run void the 03:00 night (2026-09-11: the
    scheduled stack-backup was a guarded no-op). The guard now asks: did the chain succeed since
    the most recent scheduled boundary? A boot re-run after a completed night still no-ops."""
    r, rows, home = _step(tmp_path, ["--guard", "demo", "true"])
    assert r.returncode == 0 and rows[-1]["ok"] is True
    stamp = home / ".mem0" / "maintenance" / "last-chain-success"
    assert abs(int(stamp.read_text()) - int(time.time())) < 60, "a guarded success stamps now"
    # evening hand run (22:19) must not suppress the 03:05 scheduled run
    stamp.write_text(_local("2026-09-10 22:19:00"))
    r, rows, _ = _step(tmp_path, ["--guard", "demo", "bash", "-c", "exit 9"], env_extra={"AMS_GUARD_NOW": _local("2026-09-11 03:05:00")})
    assert r.returncode == 9
    # boot re-run at 03:20 after a 03:02 success -> no-op
    stamp.write_text(_local("2026-09-11 03:02:00"))
    r, rows, _ = _step(tmp_path, ["--guard", "demo", "bash", "-c", "exit 9"], env_extra={"AMS_GUARD_NOW": _local("2026-09-11 03:20:00")})
    assert r.returncode == 0 and rows[-1]["note"].startswith("guard: chain succeeded since the last 03:00 boundary")
    # a daytime boot at 14:00 the same day -> still no-op; the next night (03:05 +1d) -> runs
    r, rows, _ = _step(tmp_path, ["--guard", "demo", "bash", "-c", "exit 9"], env_extra={"AMS_GUARD_NOW": _local("2026-09-11 14:00:00")})
    assert r.returncode == 0
    r, rows, _ = _step(tmp_path, ["--guard", "demo", "bash", "-c", "exit 9"], env_extra={"AMS_GUARD_NOW": _local("2026-09-12 03:05:00")})
    assert r.returncode == 9


def test_weekly_gate_no_ops_on_other_days(tmp_path):
    r, rows, _ = _step(tmp_path, ["--weekly", "Sun", "demo", "bash", "-c", "exit 9"], env_extra={"AMS_STEP_TODAY": "Mon"})
    assert r.returncode == 0 and rows[-1]["note"] == "weekly: not Sun; no-op" and rows[-1]["ok"] is True
    r, rows, _ = _step(tmp_path, ["--weekly", "Sun", "demo", "bash", "-c", "exit 9"], env_extra={"AMS_STEP_TODAY": "Sun"})
    assert r.returncode == 9


def test_step_exports_mem0_url_from_authority_file(tmp_path):
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    (home / ".mem0" / "authority-url").write_text("http://192.0.2.9:18791\n", encoding="utf-8")
    r, rows, _ = _step(tmp_path, ["demo", "bash", "-c", "echo url=$MEM0_URL"])
    assert "url=http://192.0.2.9:18791" in r.stdout
    r, rows, _ = _step(tmp_path, ["demo", "bash", "-c", "echo url=$MEM0_URL"], env_extra={"MEM0_URL": "http://x:1"})
    assert "url=http://x:1" in r.stdout, "an explicit MEM0_URL wins"


def test_rtcwake_arm_computes_next_0245():
    r = subprocess.run([BASH, str(SCRIPTS / "ams-rtcwake-arm.sh"), "--dry-run", "02:45"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    epoch = int(r.stdout.strip().split()[-1])
    assert 0 < epoch - int(time.time()) <= 24 * 3600


def test_step_is_locale_proof(tmp_path):
    """The authority's user manager exports a Spanish LC_NUMERIC: $EPOCHREALTIME then carries a comma
    and the first live chain died with 'value too great for base' and NO receipt; `date +%a` printed
    'dom', so --weekly Sun could never fire. The step pins LC_ALL=C and strips non-digits."""
    text = (SCRIPTS / "ams-step.sh").read_text(encoding="utf-8")
    assert "export LC_ALL=C" in text and "${EPOCHREALTIME//[!0-9]/}" in text and "${EPOCHREALTIME/./}" not in text
    comma_locale = None
    try:
        avail = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=30).stdout.split()
        comma_locale = next((l for l in avail if l.split(".")[0] in ("es_CO", "es_ES", "de_DE", "fr_FR")), None)
    except (OSError, subprocess.TimeoutExpired):
        pass
    env_extra = {"LC_ALL": "", "LANG": "en_US.UTF-8", "LC_NUMERIC": comma_locale, "LC_TIME": comma_locale} if comma_locale else {}
    r, rows, _ = _step(tmp_path, ["--weekly", time.strftime("%a"), "demo", "bash", "-c", "sleep 0.2; exit 0"], env_extra=env_extra)
    assert r.returncode == 0 and rows[-1]["ok"] is True and 150 <= rows[-1]["duration_ms"] < 5000
