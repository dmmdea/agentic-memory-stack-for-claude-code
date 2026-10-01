"""The curation jobs' outcome lines, proved against the REAL scripts/wsl/ams-step.sh in a sandbox home.

The step outcome contract (C1) lets a job that exits 0 say it did nothing: it writes ONE line,
`<status>[:<reason>] <json counts>`, to $AMS_OUTCOME_FILE and ams-step.sh puts it into the receipt as
`status`, `note` and `work`. Two branches each taught a job to write that line (the sweep's no-op
reads degraded; the rotation marker, the review queue and a refused delete degrade), and the unit
tests on each side call the writers directly. These tests run each job's real main() as the child of the real
ams-step.sh, with only its network faked, and read the receipt the nightly chain would have written:
a run that did nothing, or did less than it planned, must not read ok, and the reason must be in the receipt.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
DRIVER = Path(__file__).with_name("curation_step_driver.py")
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _home_env(home, **extra):
    """The child env with the home redirected on every platform: HOME alone leaves a child whose `~` is
    read from USERPROFILE (or HOMEDRIVE+HOMEPATH) writing into the real profile."""
    h = str(home)
    drive, tail = os.path.splitdrive(h)
    return dict(os.environ, HOME=h, USERPROFILE=h, HOMEDRIVE=drive, HOMEPATH=tail, **extra)


def _run(tmp_path, step, scenario):
    """Run the job the way its shipped unit does and return (process, sandbox home).

    semantic-dedup and episodic-reconcile run straight under `ams-step.sh <step> python <job>`; the
    contradiction sweep runs `ams-step.sh --weekly Sun <step> python jobs.py run <step> --receipt <its log>
    --stale-after N -- python <job>`, i.e. one process deeper, under the durable job queue. The queue passes
    the environment (AMS_OUTCOME_FILE) and both output streams through, and it is the piece that could have
    swallowed the outcome line or the reason on stderr, so the sweep is driven through it here too."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = _home_env(home)
    for k in ("MEM0_URL", "AMS_OUTCOME_FILE", "MEM0_KEY", "MEM0_API_KEY", "MEM0_API_KEY_FILE",
              "MEM0_DEFAULT_USER_ID", "JOBS_IDEMPOTENCY_KEY"):
        env.pop(k, None)
    job = [sys.executable, str(DRIVER), scenario]
    wrapper = []
    if step == "contradiction-sweep":
        env["AMS_STEP_TODAY"] = "Sun"
        wrapper = ["--weekly", "Sun"]
        job = [sys.executable, str(SCRIPTS / "jobs.py"), "run", step,
               "--receipt", str(home / ".mem0" / "contradiction-sweep.jsonl"), "--stale-after", "10800",
               "--", *job]
    r = subprocess.run([BASH, str(SCRIPTS / "ams-step.sh"), *wrapper, step, *job],
                       capture_output=True, text=True, env=env, timeout=180)
    return r, home


def _rows(home):
    rp = home / ".mem0" / "maintenance" / "receipts.jsonl"
    return [json.loads(ln) for ln in rp.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _receipt(tmp_path, step, scenario):
    """One run in a fresh sandbox: (process, the single receipt it wrote)."""
    r, home = _run(tmp_path, step, scenario)
    rows = _rows(home)
    assert len(rows) == 1, (rows, r.stdout[-600:], r.stderr[-600:])
    return r, rows[0]


# ------------------------------------------------------------------ the contradiction sweep

def test_healthy_sweep_receipt_says_how_much_of_the_canonical_set_it_covered(tmp_path):
    r, row = _receipt(tmp_path, "contradiction-sweep", "sweep-ok")
    assert r.returncode == 0, r.stderr
    assert row["ok"] is True and row["status"] == "ok" and row["exit"] == 0 and row["note"] == ""
    w = row["work"]
    # the four coverage keys of the step contract, with this package's rotation counts beside them
    assert (w["canonicals_checked"], w["canonicals_total"], w["pairs"], w["yes"]) == (2, 3, 0, 0)
    assert w["weeks_for_full_pass"] == 2 and w["marker_written"] == 2 and w["marker_failed"] == 0


def test_sweep_no_op_is_a_degraded_receipt_with_its_reason(tmp_path):
    """Codex shim down: exit 0 by design (the weekly unit is not noisy), but nothing was judged."""
    r, row = _receipt(tmp_path, "contradiction-sweep", "sweep-no-op")
    assert r.returncode == 0, r.stderr
    assert row["ok"] is True and row["exit"] == 0, "a no-op still stamps the chain"
    assert row["status"] == "degraded" and row["note"] == "no-op-codex-shim-unreachable"


def test_failed_rotation_marker_never_reads_ok_and_the_reason_is_in_the_note(tmp_path):
    r, row = _receipt(tmp_path, "contradiction-sweep", "sweep-marker-failed")
    assert r.returncode == 1
    assert row["ok"] is False and row["status"] == "failed" and row["exit"] == 1
    assert "degraded:marker-failed:1" in row["note"], row["note"]
    w = row["work"]
    assert w["marker_failed"] == 1 and w["canonicals_checked"] == 2 and w["canonicals_total"] == 3


def test_ok_re_judge_pass_cannot_launder_a_no_op_sweep_pass(tmp_path):
    """The Sunday unit chains the stamped re-judge after the sweep under ONE receipt."""
    r, row = _receipt(tmp_path, "contradiction-sweep", "sweep-chain-no-op-then-ok")
    assert r.returncode == 0, r.stderr
    assert row["status"] == "degraded" and row["note"] == "no-op-zero-canonicals"
    w = row["work"]
    assert w["canonicals_total"] == 0, "the sweep pass's counts survive"
    assert w["rejudge_stamped_found"] == 0, "and the re-judge pass's are added beside them, prefixed"


def test_failed_sweep_pass_keeps_its_reason_when_the_re_judge_runs_after_it(tmp_path):
    r, row = _receipt(tmp_path, "contradiction-sweep", "sweep-chain-failed-then-ok")
    assert r.returncode == 1
    assert row["ok"] is False and row["status"] == "failed"
    assert "degraded:marker-failed:1" in row["note"], (
        "the re-judge prints after the failed pass; the note must still be the reason: " + row["note"])
    assert row["work"]["marker_failed"] == 1 and "rejudge_stamped_found" in row["work"]


# ------------------------------------------------------------------ semantic dedup

def test_dedup_that_deletes_what_it_planned_is_ok_with_its_counts(tmp_path):
    r, row = _receipt(tmp_path, "semantic-dedup", "dedup-ok")
    assert r.returncode == 0, r.stderr
    assert row["ok"] is True and row["status"] == "ok" and row["note"] == ""
    w = row["work"]
    assert (w["scanned"], w["compared_pairs"], w["candidates"], w["deleted"]) == (6, 6, 2, 2)


def test_dedup_whose_deletes_were_all_refused_reads_degraded_not_ok(tmp_path):
    """Planned two deletions, the API refused both: the run did none of its work."""
    r, row = _receipt(tmp_path, "semantic-dedup", "dedup-refused")
    assert r.returncode == 0, r.stderr
    assert row["ok"] is True and row["exit"] == 0
    assert row["status"] == "degraded" and row["note"] == "deletes-refused"
    w = row["work"]
    assert (w["planned"], w["deleted"], w["delete_failed"]) == (2, 0, 2)


def test_dedup_that_compared_nothing_on_a_large_corpus_reads_degraded(tmp_path):
    """The audited failure: 16k points scanned, zero pairs compared, receipt ok for weeks."""
    r, row = _receipt(tmp_path, "semantic-dedup", "dedup-compared-0")
    assert r.returncode == 0, r.stderr
    assert row["status"] == "degraded" and row["note"] == "compared-0"
    assert row["work"]["scanned"] == 1001 and row["work"]["compared_pairs"] == 0


# ------------------------------------------------------------------ episodic reconcile

def test_episodic_reconcile_with_a_coverage_gap_is_a_degraded_receipt_that_still_exits_zero(tmp_path):
    r, row = _receipt(tmp_path, "episodic-reconcile", "episodic-coverage")
    assert r.returncode == 0, r.stderr
    assert row["ok"] is True and row["status"] == "degraded" and row["note"] == "embedding-coverage-40"
    w = row["work"]
    assert w["coverage_pct"] == 40 and w["abandoned"] == 1 and w["embedded"] == 7


# ------------------------------------------------------------------ the daily episode upkeep

def test_episode_upkeep_ok_run_is_an_ok_receipt_with_the_counts(tmp_path):
    r, row = _receipt(tmp_path, "episode-upkeep", "episodic-upkeep-ok")
    assert r.returncode == 0, r.stderr
    assert row["step"] == "episode-upkeep"
    assert row["ok"] is True and row["status"] == "ok" and row["exit"] == 0 and row["note"] == ""
    w = row["work"]
    assert w["abandoned"] == 1 and w["embedded"] == 3 and w["missing"] == 3 and w["remaining"] == 0
    assert w["in_progress_remaining"] == 0


def test_episode_upkeep_with_the_embedder_down_is_a_degraded_receipt_that_still_exits_zero(tmp_path):
    """The embedder never came up: the vectors are still missing, which must read degraded (not ok), while
    the SQLite sweep still ran and the chain's next steps still run (exit 0)."""
    r, row = _receipt(tmp_path, "episode-upkeep", "episodic-upkeep-embedder-down")
    assert r.returncode == 0, r.stderr
    assert row["ok"] is True and row["status"] == "degraded" and row["note"] == "embedder-down"
    w = row["work"]
    assert w["abandoned"] == 1 and w["embedded"] == 0 and w["missing"] == 4 and w["remaining"] == 4


def test_a_missed_vector_turns_the_maintenance_verdict_red_until_the_next_clean_upkeep(tmp_path):
    """The point of the daily step: a gap of 4 is no longer '99 % ok'. /health/maintenance reads the step's
    latest receipt; a degraded one turns `ok` false and a later clean run clears it."""
    import datetime as dt

    sys.path.insert(0, str(SCRIPTS.parents[1] / "mem0-server"))
    import maintenance_health as mh

    home = _run(tmp_path, "episode-upkeep", "episodic-upkeep-embedder-down")[1]
    receipts = home / ".mem0" / "maintenance" / "receipts.jsonl"

    def verdict():
        return mh.build(receipts, dt.datetime.now(dt.timezone.utc), pool_reader=lambda: (10, 90),
                        boots_reader=lambda: [], judge_transport=lambda: "native")

    out = verdict()
    assert out["ok"] is False
    assert {d["step"]: d["note"] for d in out["degraded_steps"]} == {"episode-upkeep": "embedder-down"}
    _run(tmp_path, "episode-upkeep", "episodic-upkeep-ok")
    assert verdict()["degraded_steps"] == []


# ------------------------------------------------------------------ the consumer of the receipts

def test_curation_receipts_turn_the_maintenance_verdict_red_until_a_later_ok_run(tmp_path):
    """The receipts exist so /health/maintenance can tell a run that did nothing from one that worked: it
    reads each step's LATEST receipt, a degraded or failed one turns `ok` false, a later ok run clears it."""
    import datetime as dt

    sys.path.insert(0, str(SCRIPTS.parents[1] / "mem0-server"))
    import maintenance_health as mh

    home = _run(tmp_path, "semantic-dedup", "dedup-refused")[1]
    _run(tmp_path, "episodic-reconcile", "episodic-coverage")
    _run(tmp_path, "contradiction-sweep", "sweep-marker-failed")
    receipts = home / ".mem0" / "maintenance" / "receipts.jsonl"

    def verdict():
        return mh.build(receipts, dt.datetime.now(dt.timezone.utc), pool_reader=lambda: (10, 90),
                        boots_reader=lambda: [], judge_transport=lambda: "native")

    out = verdict()
    assert out["ok"] is False
    assert {d["step"]: d["note"] for d in out["degraded_steps"]} == {
        "semantic-dedup": "deletes-refused", "episodic-reconcile": "embedding-coverage-40"}
    assert [f["step"] for f in out["failed_steps"]] == ["contradiction-sweep"]
    assert "degraded:marker-failed:1" in out["failed_steps"][0]["note"]

    _run(tmp_path, "semantic-dedup", "dedup-ok")                   # a later ok run of a step clears that step
    assert [d["step"] for d in verdict()["degraded_steps"]] == ["episodic-reconcile"]
