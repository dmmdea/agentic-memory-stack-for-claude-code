"""Headless pins for the W6 durable job queue (scripts/wsl/jobs.py).

Every roast binding gets a discriminating test: F1 (keyed per-line-ts receipt
observation; reap receipts can't satisfy), F2 (fresh/live claims NOT reaped —
the dangerous direction; stale+dead reaped; foreign-host on age alone), F7
(missing role file ⇒ brain; exit-code propagation; no-op preserved distinct;
receipt-missing ⇒ failed). Isolated via tmp Path.home()."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "wsl" / "jobs.py"

_spec = importlib.util.spec_from_file_location("jobs_queue", SCRIPT)
jobs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jobs)


@pytest.fixture(autouse=True)
def _tmp_home(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "REAP_LOG", tmp_path / "jobs-reap.jsonl")
    monkeypatch.setattr(jobs, "HEARTBEAT", tmp_path / "jobs-heartbeat.json")
    monkeypatch.setattr(jobs, "ROLE_FILE", tmp_path / "role")
    yield tmp_path


def _receipt_line(path: Path, key: str, outcome: str = "ok",
                  ts: float | None = None) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": jobs._iso(ts), "outcome": outcome,
                            "jobs_key": key}) + "\n")


def _args(name: str, receipt: Path, argv, stale_after: float = 21600):
    import argparse
    return argparse.Namespace(name=name, receipt=str(receipt), argv=argv,
                              stale_after=stale_after)


def _writer_argv(receipt: Path, outcome: str = "ok", rc: int = 0):
    """A real child that writes a keyed receipt line — the honest happy path."""
    code = (
        "import json, os, sys, datetime as dt;"
        f"open(r'{receipt}', 'a').write(json.dumps({{"
        "'ts': dt.datetime.now(dt.timezone.utc).isoformat(),"
        f"'outcome': '{outcome}',"
        "'jobs_key': os.environ['JOBS_IDEMPOTENCY_KEY']}) + '\\n');"
        f"sys.exit({rc})"
    )
    return [sys.executable, "-c", code]


def _rows(tmp, name):
    import sqlite3
    if not (tmp / "jobs.db").exists():
        return []          # role-gated runs never even create the db
    conn = sqlite3.connect(str(tmp / "jobs.db"))
    try:
        return conn.execute(
            "SELECT state, outcome, reap_count, idempotency_key FROM jobs"
            " WHERE name=? ORDER BY id", (name,)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def test_happy_path_claim_run_observe_done(_tmp_home):
    receipt = _tmp_home / "job.jsonl"
    rc = jobs.cmd_run(_args("j1", receipt, _writer_argv(receipt)))
    assert rc == 0
    rows = _rows(_tmp_home, "j1")
    assert len(rows) == 1 and rows[0][0] == "done" and rows[0][1] == "ok"
    hb = json.loads((_tmp_home / "jobs-heartbeat.json").read_text())
    assert hb["running"] == 0 and hb["failed_24h"] == 0


def test_noop_outcome_preserved_distinct(_tmp_home):
    receipt = _tmp_home / "job.jsonl"
    rc = jobs.cmd_run(_args("j1", receipt,
                            _writer_argv(receipt, outcome="no-op:zero-canonicals")))
    assert rc == 0
    rows = _rows(_tmp_home, "j1")
    assert rows[0][0] == "done" and rows[0][1] == "no-op:zero-canonicals"


def test_receipt_missing_marks_failed(_tmp_home):
    receipt = _tmp_home / "job.jsonl"
    rc = jobs.cmd_run(_args("j1", receipt,
                            [sys.executable, "-c", "pass"]))  # writes nothing
    assert rc != 0
    rows = _rows(_tmp_home, "j1")
    assert rows[0][0] == "failed" and rows[0][1] == "failed:receipt-missing"


def test_child_exit_code_propagates(_tmp_home):
    receipt = _tmp_home / "job.jsonl"
    rc = jobs.cmd_run(_args("j1", receipt,
                            _writer_argv(receipt, outcome="degraded:aborted", rc=1)))
    assert rc == 1  # F7b: degraded stays loud through the wrapper
    rows = _rows(_tmp_home, "j1")
    assert rows[0][0] == "failed" and rows[0][1] == "degraded:aborted"


def test_unkeyed_or_old_receipt_lines_do_not_satisfy(_tmp_home):
    # F1: a pre-existing line (older ts, no key) must not mark done.
    receipt = _tmp_home / "job.jsonl"
    _receipt_line(receipt, key="someone-elses-key", ts=time.time() - 999)
    with receipt.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": jobs._iso(), "outcome": "ok"}) + "\n")  # no key
    rc = jobs.cmd_run(_args("j1", receipt, [sys.executable, "-c", "pass"]))
    assert rc != 0
    assert _rows(_tmp_home, "j1")[0][1] == "failed:receipt-missing"


def test_reap_receipts_cannot_satisfy_observation(_tmp_home):
    # F1d: the queue-owned reap log is a DIFFERENT file; even a copied line
    # shape without the run's key can never satisfy.
    out = jobs.observe_receipt(_tmp_home / "jobs-reap.jsonl", "k", time.time())
    assert out is None


def test_fresh_live_claim_is_not_reaped(_tmp_home):
    # F2's DANGEROUS direction: a running row owned by a LIVE same-host pid
    # with a fresh claim must survive reap — a reap-everything implementation
    # passes the stale test and fails here.
    conn = jobs._connect()
    with conn:
        conn.execute(
            "INSERT INTO jobs (name, state, idempotency_key, owner_host,"
            " owner_pid, claimed_at, stale_after, created_at)"
            " VALUES ('j1','running','k1',?,?,?,21600,?)",
            (jobs._host(), os.getpid(), time.time(), time.time()))
    assert jobs.reap(conn, "j1") == 0
    conn.close()
    assert _rows(_tmp_home, "j1")[0][0] == "running"


def test_stale_dead_same_host_claim_is_reaped_with_same_key(_tmp_home):
    conn = jobs._connect()
    dead_pid = 999999
    with conn:
        conn.execute(
            "INSERT INTO jobs (name, state, idempotency_key, owner_host,"
            " owner_pid, claimed_at, stale_after, created_at)"
            " VALUES ('j1','running','crash-key',?,?,?,60,?)",
            (jobs._host(), dead_pid, time.time() - 3600, time.time() - 3600))
    assert jobs.reap(conn, "j1") == 1
    conn.close()
    rows = _rows(_tmp_home, "j1")
    assert rows[0][0] == "queued" and rows[0][2] == 1
    assert rows[0][3] == "crash-key"  # QUEUE-3: re-run reuses the SAME key
    reap_lines = (_tmp_home / "jobs-reap.jsonl").read_text().splitlines()
    assert json.loads(reap_lines[-1])["event"] == "reap"


def test_stale_but_alive_same_host_claim_survives(_tmp_home):
    # F2: age alone must not requeue a same-host job whose pid is ALIVE
    # (long-running destructive work). Own pid + THIS boot = alive.
    conn = jobs._connect()
    with conn:
        conn.execute(
            "INSERT INTO jobs (name, state, idempotency_key, owner_host,"
            " owner_pid, owner_boot_id, claimed_at, stale_after, created_at)"
            " VALUES ('j1','running','k1',?,?,?,?,60,?)",
            (jobs._host(), os.getpid(), jobs._boot_id(),
             time.time() - 3600, time.time() - 3600))
    assert jobs.reap(conn, "j1") == 0
    conn.close()
    assert _rows(_tmp_home, "j1")[0][0] == "running"


def test_stale_alive_pid_from_a_DIFFERENT_boot_is_reaped(_tmp_home):
    """Review fix 3 (the wedge): after `wsl --shutdown` the pid counter
    restarts and an unrelated process can occupy a crashed job's pid, so
    _pid_alive answers True forever and the row is never reaped — the job
    silently never runs again. A boot-id mismatch means the owner cannot be
    alive whatever the pid table says. Uses OUR OWN (definitely alive) pid so
    the test isolates the boot check, not pid liveness."""
    conn = jobs._connect()
    with conn:
        conn.execute(
            "INSERT INTO jobs (name, state, idempotency_key, owner_host,"
            " owner_pid, owner_boot_id, claimed_at, stale_after, created_at)"
            " VALUES ('j1','running','k1',?,?,'boot-from-a-previous-life',?,60,?)",
            (jobs._host(), os.getpid(), time.time() - 3600, time.time() - 3600))
    assert jobs.reap(conn, "j1") == 1
    conn.close()
    assert _rows(_tmp_home, "j1")[0][0] == "queued"


def test_foreign_host_stale_claim_reaps_on_age_alone(_tmp_home):
    # Review fix 4c: owner_pid is OUR OWN pid — locally alive, so the row is
    # only reapable because the HOST differs. With owner_pid=12345 (dead on
    # most runners) deleting the host check entirely stayed green.
    conn = jobs._connect()
    with conn:
        conn.execute(
            "INSERT INTO jobs (name, state, idempotency_key, owner_host,"
            " owner_pid, owner_boot_id, claimed_at, stale_after, created_at)"
            " VALUES ('j1','running','k1','other-box',?,?,?,60,?)",
            (os.getpid(), jobs._boot_id(),
             time.time() - 3600, time.time() - 3600))
    assert jobs.reap(conn, "j1") == 1
    conn.close()
    assert _rows(_tmp_home, "j1")[0][0] == "queued"


def test_reap_does_not_requeue_a_row_reclaimed_since_the_snapshot(_tmp_home):
    """Review fix 2 (TOCTOU): between reap's SELECT (autocommit) and its
    UPDATE another reaper can requeue the row and a third claimant re-claim
    it FRESH. A `state='running'`-only guard would requeue that live claim
    and allow two concurrent executions. Simulated by mutating the row
    between snapshot and update via a patched connection."""
    conn = jobs._connect()
    stale = time.time() - 3600
    with conn:
        conn.execute(
            "INSERT INTO jobs (name, state, idempotency_key, owner_host,"
            " owner_pid, owner_boot_id, claimed_at, stale_after, created_at)"
            " VALUES ('j1','running','k1',?,999999,'old-boot',?,60,?)",
            (jobs._host(), stale, stale))
    fired = {"done": False}

    class _RacingConn:
        """sqlite3.Connection attributes are read-only, so intercept via a
        thin proxy (delegates everything else verbatim)."""
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if (sql.strip().startswith("UPDATE jobs SET state='queued'")
                    and not fired["done"]):
                fired["done"] = True
                # A NEW owner claims the row between snapshot and update.
                with self._inner:
                    self._inner.execute(
                        "UPDATE jobs SET owner_pid=?, owner_boot_id=?,"
                        " claimed_at=? WHERE name='j1'",
                        (os.getpid(), jobs._boot_id(), time.time()))
            return self._inner.execute(sql, *a, **kw)

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def __getattr__(self, item):
            return getattr(self._inner, item)

    assert jobs.reap(_RacingConn(conn), "j1") == 0, \
        "reap clobbered a freshly re-claimed row"
    assert fired["done"], "the race was never simulated — test is vacuous"
    conn.close()
    assert _rows(_tmp_home, "j1")[0][0] == "running"


def test_foreign_host_fresh_claim_survives(_tmp_home):
    conn = jobs._connect()
    with conn:
        conn.execute(
            "INSERT INTO jobs (name, state, idempotency_key, owner_host,"
            " owner_pid, claimed_at, stale_after, created_at)"
            " VALUES ('j1','running','k1','other-box',12345,?,21600,?)",
            (time.time(), time.time()))
    assert jobs.reap(conn, "j1") == 0
    conn.close()


def test_reaped_row_reclaimed_and_rerun_once(_tmp_home):
    # The crash-sim end to end: dead stale claim -> reap -> run re-claims the
    # SAME row (same key) -> receipt -> done. Exactly one completion.
    conn = jobs._connect()
    with conn:
        conn.execute(
            "INSERT INTO jobs (name, state, idempotency_key, owner_host,"
            " owner_pid, claimed_at, stale_after, created_at)"
            " VALUES ('j1','running','crash-key',?,999999,?,60,?)",
            (jobs._host(), time.time() - 3600, time.time() - 3600))
    conn.close()
    receipt = _tmp_home / "job.jsonl"
    rc = jobs.cmd_run(_args("j1", receipt, _writer_argv(receipt)))
    assert rc == 0
    rows = _rows(_tmp_home, "j1")
    assert len(rows) == 1                      # no duplicate row leaked
    assert rows[0][0] == "done" and rows[0][3] == "crash-key"


def test_second_claim_loses_quietly(_tmp_home):
    conn = jobs._connect()
    with conn:
        conn.execute(
            "INSERT INTO jobs (name, state, idempotency_key, owner_host,"
            " owner_pid, claimed_at, stale_after, created_at)"
            " VALUES ('j1','running','k1',?,?,?,21600,?)",
            (jobs._host(), os.getpid(), time.time(), time.time()))
    conn.close()
    receipt = _tmp_home / "job.jsonl"
    rc = jobs.cmd_run(_args("j1", receipt, _writer_argv(receipt)))
    assert rc == 0                              # quiet loss, exit 0
    rows = _rows(_tmp_home, "j1")
    assert len(rows) == 1 and rows[0][0] == "running"   # untouched


def test_role_gate_missing_file_means_brain(_tmp_home):
    # F7a: MISSING role file => brain => the work RUNS.
    assert not (_tmp_home / "role").exists()
    receipt = _tmp_home / "job.jsonl"
    assert jobs.cmd_run(_args("j1", receipt, _writer_argv(receipt))) == 0
    assert _rows(_tmp_home, "j1")[0][0] == "done"


def test_role_gate_replica_refuses(_tmp_home):
    (_tmp_home / "role").write_text("replica", encoding="utf-8")
    receipt = _tmp_home / "job.jsonl"
    assert jobs.cmd_run(_args("j1", receipt, _writer_argv(receipt))) == 0
    assert _rows(_tmp_home, "j1") == []         # nothing claimed, nothing ran
    assert not receipt.exists()


def test_cli_parses_the_shipped_call_site_token_order(_tmp_home, monkeypatch):
    """THE blocker regression pin (review finding 1): every test above calls
    cmd_run(Namespace(...)) and therefore BYPASSES the parser — while both
    SHIPPED call sites (the systemd ExecStart and the installer's task
    action) failed at argparse with exit 2 before any code ran, because
    REMAINDER after a positional swallowed --receipt. This drives the real
    CLI, as a subprocess, in the byte-exact token order the unit uses."""
    import subprocess as sp

    receipt = _tmp_home / "job.jsonl"
    child = _writer_argv(receipt)
    argv = [sys.executable, str(SCRIPT), "run", "cli-probe",
            "--receipt", str(receipt), "--stale-after", "10800", "--"] + child
    env = dict(os.environ)
    env["HOME"] = str(_tmp_home)          # isolate: never touch the real ~/.mem0
    env["USERPROFILE"] = str(_tmp_home)
    proc = sp.run(argv, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"CLI failed on the shipped token order: rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
    assert "-> done outcome=ok" in proc.stdout
    import sqlite3
    conn = sqlite3.connect(str(_tmp_home / ".mem0" / "jobs.db"))
    try:
        row = conn.execute(
            "SELECT state, outcome FROM jobs WHERE name='cli-probe'").fetchone()
    finally:
        conn.close()
    assert row == ("done", "ok")


def test_cli_call_site_order_matches_the_unit_and_installer():
    """Pins the token ORDER itself: if a future edit reorders the shipped
    call sites, the CLI test above stops proving what production runs."""
    unit = (REPO_ROOT / "systemd" / "contradiction-sweep.service").read_text(
        encoding="utf-8")
    exec_line = next(ln for ln in unit.splitlines()
                     if ln.strip().startswith("ExecStart="))
    assert "jobs.py run contradiction-sweep --receipt " in exec_line
    assert " -- " in exec_line
    installer = (REPO_ROOT / "install" / "2-windows-config.ps1").read_text(
        encoding="utf-8")
    assert "jobs.py run semantic-dedup --receipt " in installer


def test_concurrent_claim_race_one_winner(_tmp_home):
    # Two claimants, one row: the conditional-insert guard admits exactly one.
    c1 = jobs._connect()
    c2 = jobs._connect()
    r1 = jobs._claim(c1, "j1", 21600)
    r2 = jobs._claim(c2, "j1", 21600)
    assert (r1 is None) != (r2 is None)
    c1.close(); c2.close()
    rows = _rows(_tmp_home, "j1")
    assert len(rows) == 1 and rows[0][0] == "running"
