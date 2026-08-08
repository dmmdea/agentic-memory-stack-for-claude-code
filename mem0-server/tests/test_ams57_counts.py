"""AMS-57 — a LIST page is not a total.

The goals and open_questions health rows derived their "total" by counting the
rows a `limit=200` LIST call returned. Once either table passed 200 rows the
probe reported exactly 200, every run, stamped OK — and the saturation is what
made it look healthy: 200 goals reads as a manageable backlog, 2955 does not.

These are headless: they build a real schema in a tmp SQLite file and drive
episodic.py directly. No live stack.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EPISODIC = REPO_ROOT / "mem0-server" / "episodic.py"

_spec = importlib.util.spec_from_file_location("episodic_ams57", EPISODIC)
episodic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(episodic)

# The exact page size the old probe used, and the number of rows that exposes it.
PROBE_LIMIT = 200
OVER_LIMIT = 250


@pytest.fixture()
def conn(tmp_path):
    c = episodic._connect_to(tmp_path / "episodic.db")
    episodic.init_schema(c)
    yield c
    c.close()


def _seed_goals(c, n_open, n_blocked=0, n_completed=0):
    rows = (
        [("open",)] * n_open + [("blocked",)] * n_blocked + [("completed",)] * n_completed
    )
    for i, (status,) in enumerate(rows):
        c.execute(
            "INSERT INTO goals (title, status, priority) VALUES (?, ?, ?)",
            (f"goal {i}", status, 3),
        )
    c.commit()


def _seed_oq(c, n_open, n_resolved=0):
    rows = [("open",)] * n_open + [("resolved",)] * n_resolved
    for i, (status,) in enumerate(rows):
        c.execute(
            "INSERT INTO open_questions (question_text, status, priority) VALUES (?, ?, ?)",
            (f"question {i}", status, 3),
        )
    c.commit()


def test_goal_count_is_not_capped_by_the_probe_page_size(conn):
    """The regression itself: more rows than the page, counted correctly."""
    _seed_goals(conn, n_open=OVER_LIMIT, n_blocked=40, n_completed=10)
    total_rows = OVER_LIMIT + 40 + 10

    # What the OLD probe did — and why it lied.
    page = episodic.list_goals(conn, limit=PROBE_LIMIT)
    assert len(page) == PROBE_LIMIT, "fixture must exceed the page for this to mean anything"

    c = episodic.count_goals(conn)
    assert c["total"] == total_rows
    assert c["by_status"]["open"] == OVER_LIMIT
    assert c["by_status"]["blocked"] == 40
    assert c["by_status"]["completed"] == 10
    # The whole point: the count must disagree with the saturated page.
    assert c["total"] > len(page)


def test_open_question_count_is_not_capped_by_the_probe_page_size(conn):
    _seed_oq(conn, n_open=OVER_LIMIT, n_resolved=15)
    page = episodic.list_open_questions(conn, status="open", limit=PROBE_LIMIT)
    assert len(page) == PROBE_LIMIT

    c = episodic.count_open_questions(conn)
    assert c["total"] == OVER_LIMIT + 15
    assert c["by_status"]["open"] == OVER_LIMIT
    assert c["by_status"]["resolved"] == 15
    assert c["by_status"]["open"] > len(page)


def test_counts_are_zero_safe_on_an_empty_table(conn):
    """The 0-goals branch drives a real WARN about extraction failing, so an
    empty table must count as 0 rather than raise."""
    assert episodic.count_goals(conn) == {"total": 0, "by_status": {}}
    assert episodic.count_open_questions(conn) == {"total": 0, "by_status": {}}


def test_count_helper_refuses_an_unknown_table(conn):
    """The table name is interpolated into SQL, so the allowlist is load-bearing."""
    with pytest.raises(ValueError):
        episodic._count_by_status(conn, "goals; DROP TABLE goals--")
    with pytest.raises(ValueError):
        episodic._count_by_status(conn, "sessions")


def test_count_routes_are_declared_before_the_id_routes():
    """/v1/goals/{goal_id} types the id as int, so a literal "count" arriving
    there first would 422. Ordering is the only thing keeping the new routes
    reachable, and nothing else would catch it headlessly."""
    src = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    for literal, param in (
        ('@app.get("/v1/goals/count")', '@app.get("/v1/goals/{goal_id}")'),
        ('@app.get("/v1/open_questions/count")', '@app.get("/v1/open_questions/{oq_id}")'),
    ):
        i_lit, i_par = src.find(literal), src.find(param)
        assert i_lit != -1, f"missing route {literal}"
        assert i_par != -1, f"missing route {param}"
        assert i_lit < i_par, f"{literal} must be declared before {param} or it is unreachable"


def test_tms_no_longer_reports_a_list_page_as_a_total():
    """Source-shape pin on the probe. The fallback may still LIST, but it must
    mark a saturated page as a floor (>=) instead of printing a bare total."""
    tms = (REPO_ROOT / "scripts" / "windows" / "Test-MemoryStack.ps1").read_text(encoding="utf-8")

    for row, count_route in (
        ("goals :v0.16", "/v1/goals/count"),
        ("open_questions :v0.17", "/v1/open_questions/count"),
    ):
        assert count_route in tms, f"{row} must read the true count from {count_route}"

    # Every remaining limit=200 LIST of these two tables is a fallback, and each
    # must be guarded by a saturation check.
    assert tms.count("-ge $lim") == 2, \
        "each LIST fallback must compare the returned page against its own limit"
    assert tms.count('">=$($arr.Count)') == 2, \
        "a saturated fallback page must be reported as a floor, not a total"


def test_tms_fallback_does_not_inline_wrap_the_rest_response():
    """@(Invoke-RestMethod ...) inline does NOT unroll a returned JSON array —
    it wraps it as ONE element, so .Count reads 1 regardless of how many rows
    came back. Caught live: the fallback reported "1 total (1 open)" against a
    200-row page, with $arr[0] itself being Object[].

    An undercounting fallback is the same defect class this finding is about,
    so the two-step form is pinned rather than left to convention.
    """
    tms = (REPO_ROOT / "scripts" / "windows" / "Test-MemoryStack.ps1").read_text(encoding="utf-8")
    # Comment lines are excluded on purpose — the note explaining this rule
    # necessarily quotes the banned form.
    code = [ln for ln in tms.splitlines() if not ln.lstrip().startswith("#")]
    offenders = [ln.strip() for ln in code if "@(Invoke-RestMethod" in ln]
    assert not offenders, (
        "assign the response first, then @($resp) — an inline @(Invoke-RestMethod ...) "
        f"wraps the array as a single element and silently undercounts to 1: {offenders}"
    )
