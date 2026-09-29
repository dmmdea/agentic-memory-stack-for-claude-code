"""The dream's honest outcome (C1): what it posted, what it spooled, what it lost.

2026-09-24: the embedder answered 500 for an hour around the 03:00 chain. The dream logged three
"insight post failed (non-fatal)" lines, posted 0 of 3, skipped its drift compare and its canonical
fetch, exited 0 and was receipted ok. Now: it waits for the embedder, spools an insight it could not
post and replays it first on the next run, and writes a degraded outcome that says which of those
happened. Collaborators are injected exactly as in test_dream_consolidate.py."""
import json
import time
from pathlib import Path

import httpx
import pytest

from test_dream_consolidate import EV, INS, SIG, FakeMem0, _judge, _mod, _run, home  # noqa: F401

INS3 = json.dumps({"insights": [
    {"text": f"Insight number {i} about the authority", "source_memory_ids": ["e1"], "confidence": 0.7} for i in (1, 2, 3)]})
NONE = '{"insights":[]}'
PROMO = "[]"   # the promote call's reply: no nominees


class Mem0(FakeMem0):
    """A fake authority whose insight POST can fail (embedder 500) and whose embedder readiness is scripted."""

    def __init__(self, *a, fail_adds=0, embedder=None, **kw):
        super().__init__(*a, **kw)
        self.fail_adds = fail_adds
        self.attempts = 0
        if embedder is not None:
            self._embedder = list(embedder)
            self.embedder_polls = 0

            def health_embedder():
                self.embedder_polls += 1
                return self._embedder.pop(0) if len(self._embedder) > 1 else self._embedder[0]
            self.health_embedder = health_embedder

    def add(self, text, metadata):
        self.attempts += 1
        if self.attempts <= self.fail_adds:
            raise httpx.HTTPStatusError("Server error '500 Internal Server Error'", request=None, response=None)
        return super().add(text, metadata)


@pytest.fixture(autouse=True)
def _env(home, monkeypatch):  # noqa: F811
    monkeypatch.delenv("AMS_OUTCOME_FILE", raising=False)
    monkeypatch.setenv("AMS_OUTCOME_FILE", str(home / "outcome"))


@pytest.fixture
def m(monkeypatch):
    mod = _mod()
    monkeypatch.setattr(mod, "_run_deployed", lambda script, env=None: (0, "ok"))
    mod.sleeps = []
    monkeypatch.setattr(mod, "_sleep", lambda s: mod.sleeps.append(s))
    return mod


def _outcome(home):
    line = (home / "outcome").read_text(encoding="utf-8")
    assert line.endswith("\n") and line.count("\n") == 1, "exactly one line"
    head, _, work = line.strip().partition(" {")
    return head, json.loads("{" + work)


def _spool(home):
    p = home / ".mem0" / "maintenance" / "dream" / "insight-spool.jsonl"
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()] if p.exists() else []


# ---- (b)+(c) the failed POST is spooled, the outcome says so ------------------------------
def test_failed_insight_post_is_spooled_and_reads_degraded(home, m):
    fm = Mem0(EV, fail_adds=99)
    out = _run(m, [], mem0=fm, judge=_judge(SIG, INS3, PROMO))
    assert out["phase"] == "done" and out["posted"] == 0
    spooled = _spool(home)
    assert [s["text"] for s in spooled] == [f"Insight number {i} about the authority" for i in (1, 2, 3)]
    assert spooled[0]["metadata"]["tier"] == "insight" and spooled[0]["metadata"]["source"] == "dream-consolidator"
    head, work = _outcome(home)
    assert head == "degraded:posted-0-of-3"
    assert work == {"signals": 1, "consolidated": 3, "posted": 0, "spooled": 3, "replayed": 0}
    assert out["outcome"] == "degraded:posted-0-of-3" and out["work"] == work


def test_partial_post_counts_and_only_the_failed_ones_spool(home, m):
    fm = Mem0(EV, fail_adds=1)   # the FIRST POST 500s, the other two land
    _run(m, [], mem0=fm, judge=_judge(SIG, INS3, PROMO))
    head, work = _outcome(home)
    assert head == "degraded:posted-2-of-3" and work["posted"] == 2 and work["spooled"] == 1
    assert [s["text"] for s in _spool(home)] == ["Insight number 1 about the authority"]
    assert fm.patched == ["e1", "e1"], "lineage is touched only for what actually landed"


def test_spooled_insight_is_replayed_first_on_the_next_run(home, m):
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS3, PROMO))
    assert len(_spool(home)) == 3
    fm = Mem0(EV)
    out = _run(m, ["--force"], mem0=fm, judge=_judge(SIG, '{"insights":[{"text":"A brand new insight","source_memory_ids":["e1"]}]}', PROMO))
    posted = [t for t, _ in fm.added]
    assert posted[:3] == [f"Insight number {i} about the authority" for i in (1, 2, 3)], "the backlog goes first"
    assert posted[3] == "A brand new insight" and len(posted) == 4
    assert _spool(home) == [], "a replayed line leaves the spool"
    head, work = _outcome(home)
    assert head == "ok" and work == {"signals": 1, "consolidated": 1, "posted": 1, "spooled": 0, "replayed": 3}
    assert out["posted"] == 1


def test_spool_dedups_by_content_hash_across_runs(home, m):
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS, PROMO))
    _run(m, ["--force"], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS, PROMO))
    assert len(_spool(home)) == 1, "the same insight failing on two nights is one spool line"


def test_replayed_insight_is_not_posted_twice_when_the_dream_regenerates_it(home, m):
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS, PROMO))
    fm = Mem0(EV)
    _run(m, ["--force"], mem0=fm, judge=_judge(SIG, INS, PROMO))   # the same insight comes back
    assert len(fm.added) == 1
    head, work = _outcome(home)
    assert head == "ok" and work["replayed"] == 1 and work["posted"] == 1 and work["consolidated"] == 1


def test_failed_replay_keeps_the_line(home, m):
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS, PROMO))
    _run(m, ["--force"], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, NONE, PROMO))
    assert len(_spool(home)) == 1
    head, work = _outcome(home)
    assert head == "ok", "nothing new was lost tonight; the backlog is still queued, not dropped"
    assert work["replayed"] == 0 and work["spooled"] == 0


def test_dry_run_never_touches_the_spool(home, m):
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS, PROMO))
    before = _spool(home)
    fm = Mem0(EV)
    _run(m, ["--dry-run"], mem0=fm, judge=_judge(SIG, INS, PROMO))
    assert fm.added == [] and _spool(home) == before


# ---- (a) the embedder preflight --------------------------------------------------------------
def test_waits_for_the_embedder_in_30s_steps_then_proceeds(home, m):
    fm = Mem0(EV, embedder=[False, False, True])
    out = _run(m, [], mem0=fm, judge=_judge(SIG, INS, PROMO))
    assert m.sleeps == [30, 30] and fm.embedder_polls == 3
    assert out["posted"] == 1 and _outcome(home)[0] == "ok"


def test_gives_up_waiting_after_ten_minutes_and_still_runs(home, m):
    fm = Mem0(EV, embedder=[False], fail_adds=99)
    _run(m, [], mem0=fm, judge=_judge(SIG, INS, PROMO))
    assert m.sleeps == [30] * 20 and fm.embedder_polls == 21, "10 minutes in 30 s steps, then proceed"
    assert _outcome(home)[0] == "degraded:posted-0-of-1", "the wait does not hide what the dead embedder cost"


def test_no_wait_when_the_embedder_is_already_up_or_unprobeable(home, m):
    fm = Mem0(EV, embedder=[True])
    _run(m, [], mem0=fm, judge=_judge(SIG, INS, PROMO))
    assert m.sleeps == [] and fm.embedder_polls == 1
    fm = FakeMem0(EV)   # a client with no embedder probe (older fake): nothing to wait on
    _run(m, ["--force"], mem0=fm, judge=_judge(SIG, INS, PROMO))
    assert m.sleeps == []


def test_dry_run_does_not_wait_for_the_embedder(home, m):
    fm = Mem0(EV, embedder=[False])
    _run(m, ["--dry-run"], mem0=fm, judge=_judge(SIG, INS, PROMO))
    assert m.sleeps == [] and fm.embedder_polls == 0


# ---- (c) the other degraded reasons ---------------------------------------------------------
def _drift_env(home, monkeypatch, snapshot_rc):
    monkeypatch.setenv("MEM0_EVAL_ROOT", str(home / "eval"))
    d = home / "eval" / "eval" / "retrieval-drift"
    d.mkdir(parents=True)
    (d / "retrieval_drift.py").write_text("")

    def ev(cmd):
        if "-h" in cmd:
            return (0, "--state")
        if "snapshot" in cmd:
            if snapshot_rc == 0:
                Path(cmd[cmd.index("--out") + 1]).write_text("{}")
            return (snapshot_rc, "snapshot")
        return (0, "")
    return ev


def test_drift_snapshot_failure_reads_degraded(home, m, monkeypatch):
    ev = _drift_env(home, monkeypatch, snapshot_rc=1)
    _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, INS, PROMO), eval_runner=ev)
    head, work = _outcome(home)
    assert head == "degraded:drift-snapshot-failed" and work["posted"] == 1


def test_a_healthy_drift_snapshot_is_ok(home, m, monkeypatch):
    ev = _drift_env(home, monkeypatch, snapshot_rc=0)
    _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, INS, PROMO), eval_runner=ev)
    assert _outcome(home)[0] == "ok"


def test_no_drift_guard_installed_is_not_a_failure(home, m):
    _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, INS, PROMO))   # no MEM0_EVAL_ROOT: the guard is simply not deployed
    assert _outcome(home)[0] == "ok"


def test_canonical_fetch_failure_reads_degraded(home, m):
    fm = Mem0(EV)
    fm.search_canonical = lambda: (_ for _ in ()).throw(RuntimeError("Server error 500"))
    _run(m, [], mem0=fm, judge=_judge(SIG, INS, PROMO))
    assert _outcome(home)[0] == "degraded:canonical-fetch-failed"


def test_the_whole_09_24_night_names_every_loss(home, m, monkeypatch):
    ev = _drift_env(home, monkeypatch, snapshot_rc=1)
    fm = Mem0(EV, fail_adds=99)
    fm.search_canonical = lambda: (_ for _ in ()).throw(RuntimeError("Server error 500"))
    _run(m, [], mem0=fm, judge=_judge(SIG, INS3, PROMO), eval_runner=ev)
    head, work = _outcome(home)
    assert head == "degraded:posted-0-of-3,drift-snapshot-failed,canonical-fetch-failed"
    assert work["spooled"] == 3


# ---- outcomes that are not degraded ----------------------------------------------------------
def test_a_clean_night_writes_ok_with_counts(home, m):
    _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, INS, PROMO))
    assert _outcome(home) == ("ok", {"signals": 1, "consolidated": 1, "posted": 1, "spooled": 0, "replayed": 0})


def test_a_no_signal_night_is_ok_and_carries_its_counts(home, m):
    _run(m, [], mem0=Mem0(EV), judge=_judge('{"signals":[]}'))
    assert _outcome(home) == ("ok", {"signals": 0, "consolidated": 0, "posted": 0, "spooled": 0, "replayed": 0})


def test_a_failed_phase_writes_failed_and_a_skipped_night_writes_nothing(home, m):
    out = _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, {"ok": False, "error_type": "boom"}))
    assert out.get("failed") and _outcome(home)[0] == "failed:consolidate"
    (home / "outcome").unlink()
    (home / ".mem0" / "maintenance" / "last-dream").write_text(str(int(time.time())))
    out = _run(m, [], mem0=Mem0(EV), judge=_judge())
    assert out["phase"] == "throttle" and not (home / "outcome").exists(), "a skipped night has no outcome line: ok"


def test_without_a_step_outcome_file_nothing_is_written_and_nothing_raises(home, m, monkeypatch):
    monkeypatch.delenv("AMS_OUTCOME_FILE")
    out = _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, INS, PROMO))
    assert out["outcome"] == "ok" and not (home / "outcome").exists()


# ---- 1.7: the dream's search stamps the hook contract -------------------------------------------
def test_canonical_search_body_carries_the_hook_contract_version(home):
    mod = _mod()
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"results": []})
    client = mod.Mem0Client("http://authority.invalid", "k", "u", http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.search_canonical()
    assert seen[0]["hook_contract_version"] == "17.0"
    assert seen[0]["filters"] == {"tier": "canonical", "user_id": "u"}, "the rest of the body is unchanged"


def test_stamped_contract_version_is_one_the_server_knows():
    import hook_contract
    assert _mod().SEARCH_HOOK_CONTRACT_VERSION in hook_contract.KNOWN_HOOK_CONTRACT_VERSIONS


def test_embedder_probe_is_the_health_embedder_route(home):
    mod = _mod()
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"ok": True, "loaded": True, "warm_ms": 3})
    client = mod.Mem0Client("http://authority.invalid", "k", "u", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.health_embedder() is True and urls == ["http://authority.invalid/health/embedder"]
    client = mod.Mem0Client("http://authority.invalid", "k", "u", http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(503, json={"reason": "cold-embedder"}))))
    assert client.health_embedder() is False, "a cold embedder answers 503: not ready, not an error"
