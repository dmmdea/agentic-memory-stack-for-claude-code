"""WP-2 (session-12 audit): a replica's SessionStart banner reads the AUTHORITY, not its own frozen files.

Before this change `storage-cap-check.sh` gated only the drift block on `~/.mem0/role`. On a replica
every other brain artifact (MEMORY.md, the job-queue mirror, brand-scope status, the l10 flag counts,
the contradiction review queue and sweep log, the local episodic.db) froze at the authority cutover and
was presented as current, and the weekly Codex re-judge was spawned against the dormant local store.

Contract under test:
  * role != brain: none of the brain-only lines print and the re-judge is never spawned.
  * role != brain, authority up: "recent sessions" come from GET /v1/episodes?recent=20, labelled
    "(authority)"; authority reachable but the read fails -> one "recent sessions unavailable" line.
  * any role, authority up: ONE loud "[AMS] brain NOT OK" line when /health/maintenance says so, nothing
    when it is healthy, nothing (and no crash) on malformed JSON.
  * replica, authority down: exactly one plain "[AMS] authority unreachable" line and nothing stale.

Harness: the REAL script under bash, HOME at a fixture dir, `curl` and `nohup` replaced by recording
shims first on PATH, so no test ever touches a network or a real store.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "storage-cap-check.sh"

FAKE_CURL = r"""#!/bin/bash
# Recording curl shim. Behaviour is chosen by which fixture files exist in $FAKE_DIR.
echo "curl $*" >> "$FAKE_DIR/calls.log"
url=""
for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done
case "$url" in
  *:18792/health) exit 0 ;;
  */health/maintenance) [ -f "$FAKE_DIR/maintenance.json" ] && { cat "$FAKE_DIR/maintenance.json"; exit 0; }; exit 7 ;;
  */v1/episodes*) [ -f "$FAKE_DIR/episodes.json" ] && { cat "$FAKE_DIR/episodes.json"; exit 0; }; exit 7 ;;
  */health) [ -f "$FAKE_DIR/up" ] && exit 0; exit 7 ;;
  *) exit 7 ;;
esac
"""

FAKE_NOHUP = r"""#!/bin/bash
echo "nohup $*" >> "$FAKE_DIR/calls.log"
exit 0
"""

ENSURE_SHIM = r"""#!/bin/bash
echo "ensure-codex-shim $*" >> "$FAKE_DIR/calls.log"
"""

UNREACHABLE = "[AMS] authority unreachable"


class Box:
    """One fixture HOME plus the recording shims."""

    def __init__(self, tmp: Path, role: str | None):
        self.home = tmp / "home"
        self.fake = tmp / "fake"
        self.bin = tmp / "bin"
        self.winprofile = tmp / "winprofile"
        for d in (self.home / ".mem0", self.fake, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        (self.bin / "curl").write_text(FAKE_CURL)
        (self.bin / "nohup").write_text(FAKE_NOHUP)
        for n in ("curl", "nohup"):
            (self.bin / n).chmod(0o755)
        (self.home / ".mem0" / "api-key").write_text("test-key\n")
        (self.home / ".mem0" / "authority-url").write_text("http://authority.invalid:1\n")
        if role is not None:
            (self.home / ".mem0" / "role").write_text(role + "\n")

    def authority(self, *, up: bool = True, maintenance=None, episodes=None, raw_maintenance: str | None = None):
        (self.fake / "up").unlink(missing_ok=True)
        if up:
            (self.fake / "up").write_text("1")
        if raw_maintenance is not None:
            (self.fake / "maintenance.json").write_text(raw_maintenance)
        elif maintenance is not None:
            (self.fake / "maintenance.json").write_text(json.dumps(maintenance))
        if episodes is not None:
            (self.fake / "episodes.json").write_text(json.dumps(episodes))

    def run(self) -> str:
        env = {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "HOMEDRIVE": os.path.splitdrive(str(self.home))[0],
            "HOMEPATH": os.path.splitdrive(str(self.home))[1],
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "CLAUDE_CWD": "/tmp",
            "FAKE_DIR": str(self.fake),
            # The morning-summary counter only runs when the script lives under /mnt/c/Users/*, which
            # no repo-path test run does; this override (honoured by the script only when set) points
            # it at the fixture profile so the role gate on that block is actually exercised.
            "AMS_WINPROFILE_OVERRIDE": str(self.winprofile),
        }
        res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, f"script must always exit 0: {res.stderr}"
        return res.stdout

    def calls(self) -> str:
        p = self.fake / "calls.log"
        return p.read_text() if p.exists() else ""


def _ok_maintenance() -> dict:
    return {
        "ok": True, "steps": {}, "stale_steps": [], "failed_steps": [], "degraded_steps": [],
        "pool": {"used_pct": 41.2, "alarm": False, "threshold_pct": 85, "health": "ONLINE", "health_alarm": False},
        "drift": {"alarm": False, "before": 7, "n_total": 7},
    }


def _ep(goal: str, ended: str, brand: str = "") -> dict:
    return {"id": 1, "goal_text": goal, "ended_at": ended, "brand": brand}


def _plant_brain_artifacts(home: Path) -> None:
    """Every brain-only input, present and stale/alarming, as a replica carries them after cutover."""
    m = home / ".mem0"
    md = m / "MEMORY.md"
    md.write_text("# stale index\n")
    old = time.time() - 30 * 86400
    os.utime(md, (old, old))
    (m / "jobs-heartbeat.json").write_text(json.dumps({"ts": "2026-01-01T00:00:00Z", "failed_24h": 3}))
    (m / "brand-scope-status.json").write_text(json.dumps({"n_misscoped": 4}))
    (m / "audit-flags.jsonl").write_text(
        "".join(json.dumps({"memory_id": f"m{i}", "flag_type": "x"}) + "\n" for i in range(25)))
    (m / "l10-state.json").write_text(json.dumps({"reviewed_keys": []}))
    (m / "contradiction-promote-review.jsonl").write_text('{"id":"a"}\n{"id":"b"}\n')
    (m / "contradiction-sweep.jsonl").write_text(
        "".join('{"outcome": "no-op:codex-shim-unreachable"}\n' for _ in range(3)))
    (m / "stack.env").write_text("MEM0_REPO_ROOT_WSL=/nonexistent-repo\n")
    # The Windows-profile morning-summary the counter reads: a section stamped "now", so a
    # brain counts it and a replica must not.
    ms = home.parent / "winprofile" / ".claude" / "state" / "dream" / "morning-summary.md"
    ms.parent.mkdir(parents=True, exist_ok=True)
    ms.write_text(f"## {time.strftime('%Y-%m-%d %H:%M')} dream\n- something to review\n")
    apps = home / "apps" / "mem0-scripts"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "ensure-codex-shim.sh").write_text(ENSURE_SHIM)
    # The local episodic store, frozen at cutover.
    con = sqlite3.connect(m / "episodic.db")
    con.executescript(
        "CREATE TABLE sessions(session_id TEXT, brand TEXT);"
        "CREATE TABLE episodes(session_id TEXT, goal_text TEXT, ended_at TEXT);"
        "INSERT INTO sessions VALUES('s1','');"
        "INSERT INTO episodes VALUES('s1','FROZEN-LOCAL-GOAL','2026-09-10T08:16:07Z');")
    con.commit()
    con.close()


BRAIN_ONLY_MARKERS = [
    "MEMORY.md stale",
    "L10 audit-flags",
    "brand-scope:",
    "job-queue mirror STALE",
    "FAILED in 24h",
    "contradiction verdict(s) await review",
    "contradiction sweep: 3+ consecutive no-op",
    "FROZEN-LOCAL-GOAL",
    "morning-summary section(s)",
    "[heartbeat]",
    "[storage-cap]",
]


def _assert_no_brain_lines(out: str) -> None:
    for marker in BRAIN_ONLY_MARKERS:
        assert marker not in out, f"replica banner printed a brain-only line ({marker!r}):\n{out}"


def _wait_for(box: Box, needle: str, seconds: float = 8.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if needle in box.calls():
            return True
        time.sleep(0.1)
    return False


# --- Task 2.1: brain-only blocks are gated, the re-judge is never spawned ------------------------


def test_replica_prints_no_brain_artifacts_and_spawns_nothing(tmp_path):
    box = Box(tmp_path, "replica")
    _plant_brain_artifacts(box.home)
    box.authority(up=True, maintenance=_ok_maintenance(), episodes=[])
    out = box.run()
    _assert_no_brain_lines(out)
    time.sleep(1.5)  # the re-judge chain is a detached subshell; give it every chance to fire
    calls = box.calls()
    assert "nohup" not in calls, calls
    assert "ensure-codex-shim" not in calls, calls
    assert not (box.home / ".mem0" / "last-contradiction-rejudge").exists()


def test_client_role_is_gated_like_a_replica(tmp_path):
    box = Box(tmp_path, "client")
    _plant_brain_artifacts(box.home)
    box.authority(up=True, maintenance=_ok_maintenance(), episodes=[])
    _assert_no_brain_lines(box.run())


def test_brain_still_prints_its_artifacts_and_spawns_the_rejudge(tmp_path):
    """Positive control: the gate must not have silenced the brain (and proves the shims observe a spawn)."""
    box = Box(tmp_path, "brain")
    _plant_brain_artifacts(box.home)
    box.authority(up=True, maintenance=_ok_maintenance())
    out = box.run()
    for marker in ("MEMORY.md stale", "L10 audit-flags", "brand-scope:", "job-queue mirror STALE",
                   "contradiction verdict(s) await review", "contradiction sweep: 3+ consecutive no-op",
                   "morning-summary section(s) in last 48h", "FROZEN-LOCAL-GOAL"):
        assert marker in out, f"brain lost {marker!r}:\n{out}"
    assert _wait_for(box, "nohup"), box.calls()


# --- Task 2.2: recent sessions from the authority on a replica -------------------------------------


def test_replica_recent_sessions_come_from_the_authority(tmp_path):
    box = Box(tmp_path, "replica")
    _plant_brain_artifacts(box.home)
    eps = [_ep("", "2026-09-29T20:00:00Z")] + [
        _ep(f"live goal {i}", f"2026-09-29T1{9 - i}:00:00Z", "brand-a" if i == 0 else "") for i in range(7)]
    box.authority(up=True, maintenance=_ok_maintenance(), episodes=eps)
    out = box.run()
    assert "[agentic-memory-stack] recent sessions (last 5) (authority):" in out
    assert "live goal 0" in out and "[brand-a]" in out and "2026-09-29 19:00" in out
    assert "live goal 4" in out
    assert "live goal 5" not in out, "only the 5 newest goal-bearing rows"
    assert "FROZEN-LOCAL-GOAL" not in out
    assert "/v1/episodes?recent=20" in box.calls()


def test_replica_recent_sessions_read_failure_prints_one_unavailable_line(tmp_path):
    box = Box(tmp_path, "replica")
    box.authority(up=True, maintenance=_ok_maintenance())  # /health answers, /v1/episodes does not
    out = box.run()
    assert out.count("recent sessions unavailable: authority unreachable") == 1, out
    assert "[agentic-memory-stack] recent sessions unavailable: authority unreachable" in out


def test_replica_recent_sessions_empty_list_prints_nothing(tmp_path):
    box = Box(tmp_path, "replica")
    box.authority(up=True, maintenance=_ok_maintenance(), episodes=[_ep("", "2026-09-29T20:00:00Z")])
    out = box.run()
    assert "recent sessions" not in out, out


def test_brain_keeps_the_local_recent_sessions_read(tmp_path):
    box = Box(tmp_path, "brain")
    _plant_brain_artifacts(box.home)
    box.authority(up=True, maintenance=_ok_maintenance(), episodes=[_ep("authority goal", "2026-09-29T20:00:00Z")])
    out = box.run()
    assert "recent sessions (last 5):" in out and "FROZEN-LOCAL-GOAL" in out
    assert "(authority)" not in out
    assert "/v1/episodes" not in box.calls()


# --- Task 2.3: one loud authority health line ------------------------------------------------------


def test_health_line_silent_when_ok(tmp_path):
    for role in ("brain", "replica"):
        box = Box(tmp_path / role, role)
        box.authority(up=True, maintenance=_ok_maintenance(), episodes=[])
        assert "[AMS]" not in box.run()


def test_health_line_names_failed_degraded_pool_stale_and_drift(tmp_path):
    box = Box(tmp_path, "replica")
    m = _ok_maintenance()
    m.update({
        "ok": False,
        "failed_steps": [{"step": "wiki-index", "ts": "t", "note": "n"}, {"step": "backup", "ts": "t", "note": "n"}],
        "degraded_steps": [{"step": "dream", "ts": "t", "note": "posted-0-of-3"}],
        "stale_steps": ["prune"],
        "pool": {"used_pct": 84.7, "alarm": False, "threshold_pct": 85, "health": "DEGRADED", "health_alarm": True},
        "drift": {"alarm": True, "before": 5, "n_total": 7},
    })
    box.authority(up=True, maintenance=m, episodes=[])
    out = box.run()
    lines = [ln for ln in out.splitlines() if ln.startswith("[AMS]")]
    assert lines == [
        "[AMS] brain NOT OK — failed: wiki-index, backup; degraded: dream; pool 84.7% DEGRADED; "
        "stale: prune; drift alarm"], out


def test_health_line_omits_empty_parts_and_fires_on_drift_alone(tmp_path):
    box = Box(tmp_path, "brain")
    m = _ok_maintenance()
    m["drift"] = {"alarm": True, "before": 5, "n_total": 7}  # ok stays True: drift is reported, not folded
    box.authority(up=True, maintenance=m)
    lines = [ln for ln in box.run().splitlines() if ln.startswith("[AMS]")]
    assert lines == ["[AMS] brain NOT OK — drift alarm"]


def test_health_line_only_the_alarming_pool_part(tmp_path):
    box = Box(tmp_path, "brain")
    m = _ok_maintenance()
    m["ok"] = False
    m["pool"] = {"used_pct": 91.0, "alarm": True, "threshold_pct": 85, "health": "ONLINE", "health_alarm": False}
    box.authority(up=True, maintenance=m)
    lines = [ln for ln in box.run().splitlines() if ln.startswith("[AMS]")]
    assert lines == ["[AMS] brain NOT OK — pool 91.0% ONLINE"]


@pytest.mark.parametrize("raw", ["{not json", "", "[]", '"a string"', "null", "7"])
def test_health_line_malformed_json_prints_nothing_and_never_crashes(tmp_path, raw):
    box = Box(tmp_path, "replica")
    box.authority(up=True, raw_maintenance=raw, episodes=[])
    assert "[AMS]" not in box.run()  # run() asserts exit 0


def test_health_line_with_garbage_parts_degrades_to_the_bare_line(tmp_path):
    box = Box(tmp_path, "replica")
    box.authority(up=True, raw_maintenance='{"ok": false, "failed_steps": 7, "pool": "x", "stale_steps": [1, null]}',
                  episodes=[])
    lines = [ln for ln in box.run().splitlines() if ln.startswith("[AMS]")]
    assert lines == ["[AMS] brain NOT OK"]


def test_health_call_is_bounded(tmp_path):
    box = Box(tmp_path, "brain")
    box.authority(up=True, maintenance=_ok_maintenance())
    box.run()
    lines = [ln for ln in box.calls().splitlines() if "/health/maintenance" in ln]
    assert len(lines) == 1 and "--max-time 1.5" in lines[0], lines


def test_brain_authority_down_does_not_print_the_health_line(tmp_path):
    box = Box(tmp_path, "brain")
    box.authority(up=False)
    out = box.run()
    assert "/health/maintenance" not in box.calls()
    assert UNREACHABLE not in out  # a brain that is starting keeps its own message
    assert "memory server still starting" in out


def test_banner_replica_authority_down(tmp_path):
    box = Box(tmp_path, "replica")
    _plant_brain_artifacts(box.home)
    box.authority(up=False)
    out = box.run()
    _assert_no_brain_lines(out)
    assert out.count("authority unreachable") == 1, out
    assert out.splitlines().count(UNREACHABLE) == 1, out
    assert "memory server still starting" not in out
    assert "recent sessions" not in out
