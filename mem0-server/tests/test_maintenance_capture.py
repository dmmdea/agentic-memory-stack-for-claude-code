# mem0-server/tests/test_maintenance_capture.py
"""GET /health/maintenance `capture`: authority-side liveness of the PC-side L1a extractor (audit CRIT-01).

Nothing on the authority noticed a dead extractor. job_liveness fills l1a_attempt_age_h / l1a_success_age_h only from
the Windows profile, which a MEM0_HOST_KIND=native brain has not got, so the `l1a-extraction` capability row is
`unknown` there for ever; and Gatus polls /health/maintenance only (never /health/deep, which loads the embedder).

The two signals are in episodic.db, written by two different PC scripts:
  activity  every UserPromptSubmit upserts the session's in_progress episode (ended_at moves), L1a or not;
  success   L1a's POST /v1/episodes finalizes it to state='complete' (ended_at = the run's end).
A dead extractor on live PCs is activity without success; PCs switched off for a week is neither.

Headless (no `import app`): the pure build() with an injected reader, and the real episodic functions on a temp DB."""
import datetime as dt
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import maintenance_health as mh  # noqa: E402

NOW = dt.datetime(2026, 10, 8, 15, 0, tzinfo=dt.timezone.utc)


def _ago(hours):
    return (NOW - dt.timedelta(hours=hours)).isoformat()


def _build(tmp_path, **kw):
    p = tmp_path / "receipts.jsonl"
    p.write_text("", encoding="utf-8")
    kw.setdefault("pool_reader", lambda: (10, 90))
    kw.setdefault("boots_reader", lambda: [])
    kw.setdefault("judge_transport", lambda: "native")
    return mh.build(p, NOW, **kw)


def _sig(success_h, activity_h):
    return {"success_at": None if success_h is None else _ago(success_h),
            "activity_at": None if activity_h is None else _ago(activity_h)}


# ---- the pure verdict ---------------------------------------------------------------------------
@pytest.mark.parametrize("success,activity,state", [
    (0.1, 0.1, "ok"),            # healthy
    (48.0, None, "ok"),          # the boundary is inclusive
    (47.9, 100, "ok"),
    (60, 0.5, "quiet"),          # inside the grace window: sessions are running, one stale stamp does not convict
    (96.0, 0.5, "quiet"),        # the conviction boundary is exclusive
    (96.1, 0.5, "stalled"),      # attempts arriving, no success for days: the dead extractor
    (500, 48.0, "stalled"),
    (500, 48.1, "quiet"),        # the PCs went quiet too (a trip, a long weekend): cannot convict
    (500, None, "quiet"),        # no activity on record at all
    (None, 0.5, "unknown"),      # never succeeded here: a broken extractor cannot be told from a new install (F12)
    (None, None, "unknown"),
])
def test_capture_state_truth_table(success, activity, state):
    assert mh.capture_state(success, activity) == state


@pytest.mark.parametrize("first,state", [
    (0.1, "quiet"),              # the first prompt after a long break: L1a has not had its first chance yet
    (0.99, "quiet"),
    (1.0, "stalled"),            # the grace boundary is inclusive
    (30.0, "stalled"),           # days of sessions and no finished run: the dead extractor
    (None, "stalled"),           # a reader without the third signal keeps the two-signal verdict
])
def test_a_stale_success_convicts_only_after_the_sessions_have_lasted_the_grace(first, state):
    assert mh.capture_state(120.0, 0.05, first) == state


def test_the_thresholds_are_the_capability_manifests_own():
    """One definition of 'quiet' and 'convict' for l1a. Read as text: importing capabilities pulls the admission
    gate into a headless test."""
    src = open(os.path.join(HERE, "capabilities.py"), encoding="utf-8").read()
    assert f"FRESH_H = {mh.CAPTURE_QUIET_H}" in src and f"L1A_CONVICT_H = {mh.CAPTURE_STALLED_H}" in src


# ---- build(): the block rides in the payload and never reddens it --------------------------------------
def test_without_a_reader_there_is_no_capture_key_and_ok_is_unchanged(tmp_path):
    for kw in ({}, {"capture_reader": None}):
        out = _build(tmp_path, **kw)
        assert "capture" not in out and out["ok"] is True


def test_a_healthy_capture_rides_in_the_payload(tmp_path):
    out = _build(tmp_path, capture_reader=lambda: _sig(0.2, 0.1))
    assert out["capture"] == {"state": "ok", "stalled": False, "success_at": _ago(0.2), "success_age_h": 0.2,
                              "activity_at": _ago(0.1), "activity_age_h": 0.1, "quiet_after_h": 48.0, "stalled_after_h": 96.0}
    assert out["ok"] is True


def test_a_stalled_capture_is_reported_but_never_reddens_ok_or_the_step_lists(tmp_path):
    """Gatus pages on failed_steps / stale_steps / write_path.ok. A capture stall is a heads-up of its own, so it must
    not leak into any of them (and `ok` stays the nightly chain's verdict)."""
    out = _build(tmp_path, capture_reader=lambda: _sig(120, 0.5))
    assert out["capture"]["state"] == "stalled" and out["capture"]["stalled"] is True
    assert out["capture"]["success_age_h"] == 120.0 and out["capture"]["activity_age_h"] == 0.5
    assert out["ok"] is True
    assert out["failed_steps"] == [] and out["degraded_steps"] == [] and out["stale_steps"] == []


def test_pcs_switched_off_for_a_week_is_quiet_not_stalled(tmp_path):
    out = _build(tmp_path, capture_reader=lambda: _sig(190, 185))
    assert out["capture"]["state"] == "quiet" and out["capture"]["stalled"] is False


def test_an_empty_store_reads_unknown_never_stalled(tmp_path):
    out = _build(tmp_path, capture_reader=lambda: {"success_at": None, "activity_at": None})
    assert out["capture"]["state"] == "unknown" and out["capture"]["stalled"] is False
    assert out["capture"]["success_at"] is None and "note" not in out["capture"]


def test_a_reader_that_raises_reads_unknown_and_the_rest_of_the_payload_is_intact(tmp_path):
    def boom():
        raise RuntimeError("database is locked")
    out = _build(tmp_path, capture_reader=boom)
    assert out["capture"]["state"] == "unknown" and out["capture"]["stalled"] is False
    assert out["capture"]["note"] == "capture reader failed"
    assert out["ok"] is True and "pool" in out and out["steps"] == {}


@pytest.mark.parametrize("bad", [None, "ok", 12345, ["x"]])
def test_a_reader_that_answers_with_something_else_reads_unknown(tmp_path, bad):
    out = _build(tmp_path, capture_reader=lambda: bad)
    assert out["capture"]["state"] == "unknown" and out["capture"]["stalled"] is False


@pytest.mark.parametrize("junk", ["", "not a time", 12345, ["x"]])
def test_an_unreadable_timestamp_is_no_reading(tmp_path, junk):
    out = _build(tmp_path, capture_reader=lambda: {"success_at": junk, "activity_at": _ago(1)})
    assert out["capture"]["state"] == "unknown" and out["capture"]["success_at"] is None


def test_powershell_and_server_timestamp_formats_both_parse(tmp_path):
    """episodes.ended_at holds '...+00:00' (this server) and '...Z' with 7 fractional digits (PowerShell 'o')."""
    out = _build(tmp_path, capture_reader=lambda: {"success_at": "2026-10-08T14:17:07.3485017Z",
                                                   "activity_at": "2026-10-08T14:48:16.332085+00:00"})
    assert out["capture"]["state"] == "ok" and out["capture"]["success_age_h"] == 0.7 and out["capture"]["activity_age_h"] == 0.2


def test_a_timestamp_from_the_future_is_age_zero(tmp_path):
    out = _build(tmp_path, capture_reader=lambda: {"success_at": (NOW + dt.timedelta(minutes=5)).isoformat(), "activity_at": None})
    assert out["capture"]["success_age_h"] == 0.0 and out["capture"]["state"] == "ok"


def test_a_stamp_more_than_an_hour_ahead_is_ignored_and_named(tmp_path):
    """A PC clock far ahead would otherwise hold the verdict at ok until real time caught up."""
    far = (NOW + dt.timedelta(hours=30)).isoformat()
    out = _build(tmp_path, capture_reader=lambda: {"success_at": far, "activity_at": _ago(0.2)})
    assert out["capture"]["success_at"] is None and out["capture"]["state"] == "unknown"
    assert "success_at" in out["capture"]["note"]


def test_capture_and_write_path_are_independent_blocks(tmp_path):
    wp = {"ok": False, "last_ok_at": None, "last_error_at": "x", "last_error": "503 upstream", "errors_1h": 1, "writes_1h": 1}
    out = _build(tmp_path, capture_reader=lambda: _sig(1, 1), write_path_reader=lambda: dict(wp))
    assert out["write_path"] == wp and out["capture"]["state"] == "ok" and out["ok"] is False


def test_build_asks_the_reader_exactly_once(tmp_path):
    calls = []
    _build(tmp_path, capture_reader=lambda: calls.append(1) or _sig(1, 1))
    assert calls == [1]


# ---- the real episodic functions, on a temp DB: the two signals move independently ----------------------
@pytest.fixture()
def store(tmp_path, monkeypatch):
    import episodic as ep
    conn = ep._connect_to(tmp_path / "episodic.db")
    ep.init_schema(conn)
    clock = {"t": dt.datetime(2026, 9, 20, 9, 0, tzinfo=dt.timezone.utc)}
    monkeypatch.setattr(ep, "_iso_now", lambda: clock["t"].isoformat())

    class S:
        episodic = ep
        c = conn

        @staticmethod
        def at(t):
            clock["t"] = t

        @staticmethod
        def prompt(session, text="hi"):
            ep.upsert_in_progress_episode(conn, session, prompt_text=text)

        @staticmethod
        def l1a_finishes(session, ended_at):
            ep.finalize_episode(conn, session, goal_text="g", summary_text="s", ended_at=ended_at, message_count=3)
    yield S
    conn.close()


def _t(day, hour=9, minute=0):
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=dt.timezone.utc)


def test_an_empty_store_has_neither_signal(store):
    assert store.episodic.capture_signals(store.c) == {"activity_at": None, "success_at": None,
                                                       "first_activity_at": None}


def test_a_prompt_moves_activity_and_leaves_success_alone(store):
    store.at(_t(20)); store.prompt("s1")
    assert store.episodic.capture_signals(store.c) == {"activity_at": _t(20).isoformat(), "success_at": None,
                                                       "first_activity_at": None}
    store.at(_t(20, 10)); store.prompt("s1")                       # the same session, a later prompt
    assert store.episodic.capture_signals(store.c)["activity_at"] == _t(20, 10).isoformat()


def test_l1a_finishing_a_run_is_the_success_signal(store):
    store.at(_t(20)); store.prompt("s1")
    done = _t(20, 10).isoformat().replace("+00:00", "Z")        # PowerShell sends 'Z'
    store.l1a_finishes("s1", done)
    sig = store.episodic.capture_signals(store.c)
    assert sig["success_at"] == done


def test_a_dead_extractor_is_activity_without_success(store):
    """Day 20 L1a works; days 21-26 the PCs keep prompting and nothing finalizes. At day 26 the block says stalled."""
    store.at(_t(20)); store.prompt("s1"); store.l1a_finishes("s1", _t(20, 9, 30).isoformat())
    for day in range(21, 27):
        store.at(_t(day)); store.prompt(f"s{day}")
    out = mh.build(_receipts(store), _t(26, 12), pool_reader=lambda: (10, 90), boots_reader=lambda: [],
                   judge_transport=lambda: "native",
                   capture_reader=lambda: store.episodic.capture_signals(store.c))
    assert out["capture"]["state"] == "stalled" and out["capture"]["stalled"] is True
    assert out["capture"]["success_age_h"] > mh.CAPTURE_STALLED_H and out["capture"]["activity_age_h"] == 3.0


def test_the_same_silence_with_the_pcs_off_is_quiet(store):
    """Day 20 works, then nothing at all for eight days (an absence like the longest in the episode history): not an
    alarm."""
    store.at(_t(20)); store.prompt("s1"); store.l1a_finishes("s1", _t(20, 9, 30).isoformat())
    out = mh.build(_receipts(store), _t(28, 12), pool_reader=lambda: (10, 90), boots_reader=lambda: [],
                   judge_transport=lambda: "native",
                   capture_reader=lambda: store.episodic.capture_signals(store.c))
    assert out["capture"]["state"] == "quiet" and out["capture"]["stalled"] is False


def test_the_first_prompts_after_a_long_trip_are_quiet_until_l1a_has_had_its_chance(store):
    """Day 20 works, the PCs are off for eight days, then a session starts on day 28 at 12:00. Five minutes in, L1a has
    not had a chance: quiet. Ninety minutes of prompting later with still no finished run: stalled."""
    store.at(_t(20)); store.prompt("s1"); store.l1a_finishes("s1", _t(20, 9, 30).isoformat())
    store.at(_t(28, 12)); store.prompt("s28")

    def verdict(at):
        return mh.build(_receipts(store), at, pool_reader=lambda: (10, 90), boots_reader=lambda: [],
                        judge_transport=lambda: "native",
                        capture_reader=lambda: store.episodic.capture_signals(store.c))["capture"]

    assert verdict(_t(28, 12, 5))["state"] == "quiet"
    store.at(_t(28, 13, 30)); store.prompt("s28")
    assert verdict(_t(28, 13, 31))["state"] == "stalled"


def test_the_capture_reader_connection_is_read_only(store, tmp_path):
    import sqlite3
    ro = store.episodic.connect_readonly(tmp_path / "episodic.db")
    try:
        assert store.episodic.capture_signals(ro)["success_at"] is None
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM episodes")
    finally:
        ro.close()


def test_a_reconcile_that_abandons_an_episode_does_not_fake_a_success(store):
    store.at(_t(20)); store.prompt("s1")
    store.c.execute("UPDATE episodes SET state = 'abandoned' WHERE session_id = 's1'"); store.c.commit()
    assert store.episodic.capture_signals(store.c)["success_at"] is None


def test_both_queries_stay_on_the_ended_at_index(store):
    """/health/maintenance sits behind a 1.5 s session-start budget (claude-config/storage-cap-check.sh): the success
    query must not fall back to the state index plus a sort of every complete episode (31 ms on the live 5,079 rows,
    against 0.02 ms on idx_episodes_ended)."""
    ep = store.episodic
    for sql in (ep.CAPTURE_ACTIVITY_SQL, ep.CAPTURE_SUCCESS_SQL):     # exactly what capture_signals executes
        plan = " | ".join(r[3] for r in store.c.execute("EXPLAIN QUERY PLAN " + sql))
        assert "idx_episodes_ended" in plan and "TEMP B-TREE" not in plan, plan
    plan = " | ".join(r[3] for r in store.c.execute("EXPLAIN QUERY PLAN " + ep.CAPTURE_FIRST_ACTIVITY_SQL,
                                                     ("2026-09-20T09:00:00+00:00",)))
    assert "idx_episodes_ended" in plan, plan                         # a range read, not a full scan


def _receipts(store):
    import pathlib
    p = pathlib.Path(store.c.execute("PRAGMA database_list").fetchone()[2]).parent / "receipts.jsonl"
    p.write_text("", encoding="utf-8")
    return p


# ---- the route wiring (reads app.py as text, like test_maintenance_health_steps' ack pin) -------------------
def test_the_route_wires_the_capture_reader_through_the_episodic_store():
    app_py = os.environ.get("AMS_APP_PY") or os.path.join(HERE, "app.py")
    if not os.path.exists(app_py):
        pytest.skip("app.py not in this checkout")
    src = open(app_py, encoding="utf-8").read()
    route = src[src.index("def health_maintenance"):]
    route = route[:route.index("@app.get", 1)]
    assert "capture_reader=" in route, "the route must hand build() a capture_reader"
    assert "capture_signals as _episodic_capture_signals" in src, "app.py imports the episodic reader"
    helper = src[src.index("def _capture_signals"):]
    helper = helper[:helper.index("@app.get", 1)]
    assert "conn.close()" in helper, "the polled endpoint must not leak a SQLite connection per request"
    assert "_episodic_connect_readonly()" in helper, "the probe reads over the read-only, short-timeout connection"
    assert "scroll(" not in helper and "vector_store" not in helper, "the capture check must never walk Qdrant on the request path"
