"""An unfinished episode's running summary records what a person typed, and only that.

``upsert_in_progress_episode`` appends a preview of every UserPromptSubmit prompt to the in-progress
episode's ``summary_text``. Claude Code raises that event for background task notifications and for
messages another agent session relays, so the summary the recent-sessions view shows filled with
``<task-notification>`` XML and ``<cross-session-message>`` wrappers (live: an unfinished session's
summary opened with a notification and a relayed message).

Write side: a non-human turn (``hook_contract.is_non_human_turn``) appends nothing, but the checkpoint
still lands: ``ended_at`` moves and ``sessions.message_count`` counts it (C10: "the episode checkpoint
still lands"; the stale-episode clock reads ``ended_at``). The append itself is a pure helper that no
longer eats pipes and spaces of real text and no longer freezes on the first prompts.

Read side: ``scrub_running_summary`` drops machine segments from the text of every row that is not
``complete`` (``recent()`` and ``search_fts()``), which also cleans up the rows written before this fix
without touching the database. ``get_episode`` stays raw: it is the drill-down.

Everything here is headless: a temp SQLite file with the real schema, no app import, no live service.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "mem0-server"))

import episodic  # noqa: E402
from episodic import _connect_to, init_schema, upsert_in_progress_episode  # noqa: E402

_CORPUS_PATH = REPO_ROOT / "scripts" / "windows" / "tests" / "fixtures" / "machine-turn-prompts.json"
CORPUS = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))["prompts"]
NON_HUMAN = [c for c in CORPUS if c["machine_turn"] or c["relayed_agent_message"]]

GAP = episodic.RUNNING_SUMMARY_GAP
SEP = episodic.RUNNING_SUMMARY_SEP


@pytest.fixture()
def db(tmp_path):
    conn = _connect_to(tmp_path / "episodic.db")
    init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture()
def clock(monkeypatch):
    """A strictly increasing clock, so 'ended_at moved' is a comparison, not a race."""
    ticks = iter(range(1, 10_000))
    monkeypatch.setattr(episodic, "_iso_now", lambda: f"2026-10-01T00:00:00.{next(ticks):06d}+00:00")


def _episode(db, session="s1"):
    return dict(db.execute(
        "SELECT * FROM episodes WHERE session_id = ? AND state = 'in_progress'", (session,)).fetchone())


def _count(db, session="s1"):
    return db.execute("SELECT message_count FROM sessions WHERE session_id = ?", (session,)).fetchone()[0]


def _prompt(db, text, session="s1"):
    return upsert_in_progress_episode(db, session, prompt_text=text)


# ---------------------------------------------------------------------------
# The corpus is the source of the non-human prompts
# ---------------------------------------------------------------------------

def test_non_human_corpus_has_both_kinds():
    assert any(c["machine_turn"] for c in NON_HUMAN), "a task notification is needed"
    assert any(c["relayed_agent_message"] for c in NON_HUMAN), "a relayed agent message is needed"


# ---------------------------------------------------------------------------
# Write side: the turn appends nothing, the checkpoint still lands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", NON_HUMAN, ids=[c["name"] for c in NON_HUMAN])
def test_a_non_human_turn_leaves_the_summary_but_moves_the_clock_and_the_count(db, clock, case):
    ep_id, _ = _prompt(db, "fix the login bug")
    before, count_before = _episode(db), _count(db)

    ep2, action = _prompt(db, case["prompt"][:300])  # the hook sends the first 300 characters

    after = _episode(db)
    assert (ep2, action) == (ep_id, "updated")
    assert after["summary_text"] == before["summary_text"] == "fix the login bug"
    assert after["ended_at"] > before["ended_at"], "the checkpoint still lands: the stale clock reads ended_at"
    assert _count(db) == count_before + 1, "the turn still counts toward sessions.message_count"


@pytest.mark.parametrize("case", NON_HUMAN, ids=[c["name"] for c in NON_HUMAN])
def test_a_session_that_opens_with_a_non_human_turn_inserts_an_empty_summary(db, clock, case):
    ep_id, action = _prompt(db, case["prompt"][:300])
    row = _episode(db)
    assert (ep_id, action) == (row["id"], "created")
    assert row["summary_text"] == ""
    assert row["goal_text"] == "" and row["state"] == "in_progress"
    assert _count(db) == 1

    # the first thing a person types then becomes the opening ask, with no stray separator
    _prompt(db, "what is the state of the deploy")
    assert _episode(db)["summary_text"] == "what is the state of the deploy"


def test_non_human_turns_between_human_ones_leave_no_trace(db, clock):
    notification = next(c for c in NON_HUMAN if c["machine_turn"])["prompt"]
    relayed = next(c for c in NON_HUMAN if c["relayed_agent_message"])["prompt"]
    for text in ("first ask", notification, "second ask", relayed, notification, "third ask"):
        _prompt(db, text[:300])
    assert _episode(db)["summary_text"] == SEP.join(["first ask", "second ask", "third ask"])
    assert _count(db) == 6


def test_human_prompts_append_in_order(db, clock):
    for text in ("alpha", "beta", "gamma"):
        _prompt(db, text)
    assert _episode(db)["summary_text"] == "alpha | beta | gamma"


def test_a_prompt_that_only_quotes_the_wrapper_is_still_a_human_prompt(db, clock):
    quoted = next(c for c in CORPUS if "quotes a notification mid-text" in c["name"])["prompt"]
    _prompt(db, quoted)
    assert _episode(db)["summary_text"] == quoted


def test_snippet_sizes_are_unchanged(db, clock):
    """The opening prompt is kept to 300 characters, each later one to 200."""
    _prompt(db, "a" * 400)
    assert _episode(db)["summary_text"] == "a" * 300
    _prompt(db, "b" * 400)
    assert _episode(db)["summary_text"] == "a" * 300 + SEP + "b" * 200


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_prompt_still_checkpoints_without_touching_the_summary(db, clock, empty):
    _prompt(db, "real ask")
    before = _episode(db)
    _prompt(db, empty)
    after = _episode(db)
    assert after["summary_text"] == "real ask"
    assert after["ended_at"] > before["ended_at"] and _count(db) == 2


def test_one_in_progress_row_per_session_still_holds(db, clock):
    for text in ("a", "b", "c"):
        _prompt(db, text)
    assert db.execute("SELECT COUNT(*) FROM episodes WHERE session_id = 's1'").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# _append_running_summary: the pure append
# ---------------------------------------------------------------------------

def test_append_joins_with_the_separator():
    assert episodic._append_running_summary("", "first") == "first"
    assert episodic._append_running_summary("first", "second") == "first | second"


def test_append_does_not_strip_pipes_or_spaces_of_real_text():
    """The old ``.strip(" | ")`` took any of space and pipe off both ends of the whole summary."""
    append = episodic._append_running_summary
    assert append("", "| leading pipe") == "| leading pipe"
    assert append("| a", "b") == "| a | b"
    assert append("a", "b |") == "a | b |"
    assert append("a", "b | ") == "a | b | "
    assert append("a", " b") == "a |  b"
    assert append("a | b |", "c") == "a | b | | c"


def test_append_without_a_snippet_returns_the_summary_unchanged():
    assert episodic._append_running_summary("a | b", "") == "a | b"
    assert episodic._append_running_summary("a | b", None) == "a | b"
    assert episodic._append_running_summary("", None) == ""


def _grow(n, size=200, cap=800):
    """n prompts of `size` characters appended one by one; returns (summary, [prompts])."""
    prompts = [f"p{i:02d}-" + "x" * (size - 4) for i in range(n)]
    summary = ""
    for p in prompts:
        summary = episodic._append_running_summary(summary, p, cap)
    return summary, prompts


def test_cap_keeps_the_opening_ask_and_the_newest_prompts_with_one_gap_marker():
    summary, prompts = _grow(12)
    segments = summary.split(SEP)
    assert len(summary) <= 800
    assert segments[0] == prompts[0], "the session's opening ask is always kept"
    assert segments[-1] == prompts[-1], "the newest prompt is always kept (the log no longer freezes)"
    assert segments.count(GAP) == 1 and segments[1] == GAP, "one marker, right after the opening ask"
    kept_tail = segments[2:]
    assert kept_tail == prompts[-len(kept_tail):], "the kept tail is the newest prompts, contiguous"
    assert len(kept_tail) >= 2, "as many of the newest prompts as fit, not just the last one"
    # and it is the most that fit: one more would break the cap
    assert len(SEP.join([prompts[0], GAP, prompts[-len(kept_tail) - 1], *kept_tail])) > 800


def test_repeated_overflow_never_duplicates_the_gap_marker_or_loses_the_first_prompt():
    summary, prompts = _grow(60)
    segments = summary.split(SEP)
    assert segments.count(GAP) == 1
    assert segments[0] == prompts[0] and segments[-1] == prompts[-1]
    assert len(summary) <= 800


def test_no_gap_marker_while_everything_fits():
    summary, prompts = _grow(3)
    assert summary == SEP.join(prompts) and GAP not in summary


def test_the_cap_is_a_parameter():
    summary, prompts = _grow(6, size=60, cap=200)
    assert len(summary) <= 200
    assert summary.split(SEP)[0] == prompts[0] and summary.split(SEP)[-1] == prompts[-1]


def test_a_legacy_oversized_summary_is_brought_back_under_the_cap():
    legacy = SEP.join(f"old{i}-" + "y" * 196 for i in range(9))  # 1,800+ characters, no marker
    out = episodic._append_running_summary(legacy, "newest ask")
    assert len(out) <= 800
    assert out.startswith("old0-") and out.endswith("newest ask") and GAP in out.split(SEP)


def test_through_upsert_the_summary_keeps_first_and_newest(db, clock):
    prompts = [f"ask{i:02d}-" + "z" * 190 for i in range(14)]
    for p in prompts:
        _prompt(db, p)
    summary = _episode(db)["summary_text"]
    assert len(summary) <= 800
    assert summary.startswith(prompts[0][:300]) and summary.endswith(prompts[-1][:200])
    assert summary.split(SEP).count(GAP) == 1


# ---------------------------------------------------------------------------
# Wiring: both checkpoint routes reach the filter through one function
# ---------------------------------------------------------------------------

def test_both_checkpoint_routes_go_through_the_one_upsert():
    """app.py cannot be imported headless: pin the wiring in its source. The filter lives in
    episodic.upsert_in_progress_episode, so it only covers a route that reaches it. Both the
    checkpoint-only POST and the context bundle (the path a substantive prompt takes) call
    _checkpoint_core, which is the sole caller of the upsert, and neither pre-filters in app.py."""
    src = (REPO_ROOT / "mem0-server" / "app.py").read_text(encoding="utf-8")
    core = src[src.index("def _checkpoint_core("):src.index('@app.post("/v1/episodes/checkpoint")')]
    assert "_episodic_upsert_checkpoint(" in core
    assert src.count("_episodic_upsert_checkpoint(") == 1, "_checkpoint_core is the only caller of the upsert"
    ckpt = src[src.index('@app.post("/v1/episodes/checkpoint")'):src.index("def context_bundle(")]
    assert "_checkpoint_core(b)" in ckpt
    bundle = src[src.index("def context_bundle("):src.index('@app.post("/v1/episodes")')]
    assert "_checkpoint_core(EpisodeCheckpointIn(" in bundle
    assert "is_non_human_turn" not in src, "the filter belongs in episodic.py, where it is headless-testable"
