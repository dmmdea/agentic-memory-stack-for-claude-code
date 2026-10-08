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

from _home_isolation import apply_home
from test_dream_consolidate import EV, INS, SIG, FakeMem0, _judge, _mod, _run

INS3 =json.dumps({"insights": [
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


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME under tmp_path so nothing touches a real ~/.mem0; the step's outcome file lives beside it.
    apply_home redirects every variable a platform reads (HOME, USERPROFILE, HOMEDRIVE/HOMEPATH) and Path.home()."""
    apply_home(monkeypatch, tmp_path)
    (tmp_path / ".mem0").mkdir()
    for v in ("MEM0_PROMOTION_GATE_MODE", "MEM0_EVAL_ROOT", "MEM0_URL", "MEM0_KEY", "MEM0_API_KEY_FILE",
              "AMS_STORE_CHECKOUT", "AMS_STORE_BIN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AMS_OUTCOME_FILE", str(tmp_path / "outcome"))
    return tmp_path


@pytest.fixture
def m(home, monkeypatch):
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
    assert work == {"signals": 1, "consolidated": 3, "posted": 0, "spooled": 3, "replayed": 0,
                    "replay_failed": 0, "spool_depth": 3, "nominated": 0, "structural_rejected": 0, "promoted": 0, "promote_failed": 0, "gate_blocked": 0}
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
    assert head == "ok" and work == {"signals": 1, "consolidated": 1, "posted": 1, "spooled": 0, "replayed": 3,
                                                   "replay_failed": 0, "spool_depth": 0, "nominated": 0, "structural_rejected": 0, "promoted": 0, "promote_failed": 0, "gate_blocked": 0}
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


def test_failed_replay_keeps_the_line_and_reads_degraded(home, m):
    """A dead embedder tonight with nothing new to say still means last night's insight is NOT stored:
    that is unfinished work, so the receipt must not read ok (WP-1 fix round 1)."""
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS, PROMO))
    _run(m, ["--force"], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, NONE, PROMO))
    assert len(_spool(home)) == 1
    head, work = _outcome(home)
    assert head == "degraded:replay-failed-1", "the queued insight was not stored; that is not ok"
    assert work["replayed"] == 0 and work["spooled"] == 0
    assert work["replay_failed"] == 1 and work["spool_depth"] == 1


def test_partial_replay_counts_only_the_lines_that_failed(home, m):
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS3, PROMO))
    fm = Mem0(EV, fail_adds=2)   # the replay's first two POSTs 500, the third lands
    _run(m, ["--force"], mem0=fm, judge=_judge(SIG, NONE, PROMO))
    head, work = _outcome(home)
    assert head == "degraded:replay-failed-2"
    assert work["replayed"] == 1 and work["replay_failed"] == 2 and work["spool_depth"] == 2
    assert len(_spool(home)) == 2


def test_failed_replay_and_a_failed_new_post_name_both(home, m):
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS, PROMO))
    other = '{"insights":[{"text":"A brand new insight","source_memory_ids":["e1"]}]}'
    _run(m, ["--force"], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, other, PROMO))
    head, work = _outcome(home)
    assert head == "degraded:posted-0-of-1,replay-failed-1"
    assert work["spool_depth"] == 2 and work["spooled"] == 1 and work["replay_failed"] == 1


def test_a_standing_backlog_on_a_night_that_never_replayed_reads_degraded(home, m):
    """A no-signal night returns before phase 3: the spool is untouched, and it must not read as a clean night."""
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS3, PROMO))
    _run(m, ["--force"], mem0=Mem0(EV), judge=_judge('{"signals":[]}'))
    head, work = _outcome(home)
    assert head == "degraded:spool-backlog-3"
    assert work["spool_depth"] == 3 and work["replay_failed"] == 0 and work["replayed"] == 0


def test_a_drained_backlog_reads_ok_again(home, m):
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS, PROMO))
    _run(m, ["--force"], mem0=Mem0(EV), judge=_judge(SIG, NONE, PROMO))
    assert _outcome(home) == ("ok", {"signals": 1, "consolidated": 0, "posted": 0, "spooled": 0, "replayed": 1,
                                     "replay_failed": 0, "spool_depth": 0, "nominated": 0, "structural_rejected": 0, "promoted": 0, "promote_failed": 0, "gate_blocked": 0})


def test_dry_run_with_a_standing_spool_does_not_read_as_a_backlog(home, m):
    """A dry run skips the replay by design, so the queue it left alone is not a degraded night."""
    _run(m, [], mem0=Mem0(EV, fail_adds=99), judge=_judge(SIG, INS3, PROMO))
    assert len(_spool(home)) == 3
    (home / "outcome").unlink()
    _run(m, ["--dry-run"], mem0=Mem0(EV), judge=_judge(SIG, INS3, PROMO))
    head, work = _outcome(home)
    assert head == "ok", head
    assert work["spool_depth"] == 3 and work["replayed"] == 0


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


def test_a_full_canonical_page_reads_degraded(home, m):
    """The server caps one search at 500: a full page may hide canonicals from the dedup guard, so it is not ok."""
    fm = Mem0(EV)
    fm.search_canonical = lambda: [{"id": f"c{i}", "memory": f"canonical {i}"} for i in range(m.CANONICAL_FETCH_LIMIT)]
    _run(m, [], mem0=fm, judge=_judge(SIG, INS, PROMO))
    assert _outcome(home)[0] == "degraded:canonical-fetch-truncated"


def test_a_canonical_page_short_of_the_cap_is_ok(home, m):
    fm = Mem0(EV)
    fm.search_canonical = lambda: [{"id": f"c{i}", "memory": f"canonical {i}"} for i in range(m.CANONICAL_FETCH_LIMIT - 1)]
    _run(m, [], mem0=fm, judge=_judge(SIG, INS, PROMO))
    assert _outcome(home)[0] == "ok"


def test_the_whole_09_24_night_names_every_loss(home, m, monkeypatch):
    ev = _drift_env(home, monkeypatch, snapshot_rc=1)
    fm = Mem0(EV, fail_adds=99)
    fm.search_canonical = lambda: (_ for _ in ()).throw(RuntimeError("Server error 500"))
    _run(m, [], mem0=fm, judge=_judge(SIG, INS3, PROMO), eval_runner=ev)
    head, work = _outcome(home)
    assert head == "degraded:posted-0-of-3,drift-snapshot-failed,canonical-fetch-failed"
    assert work["spooled"] == 3 and work["spool_depth"] == 3


# ---- outcomes that are not degraded ----------------------------------------------------------
def test_a_clean_night_writes_ok_with_counts(home, m):
    _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, INS, PROMO))
    assert _outcome(home) == ("ok", {"signals": 1, "consolidated": 1, "posted": 1, "spooled": 0, "replayed": 0,
                                     "replay_failed": 0, "spool_depth": 0, "nominated": 0, "structural_rejected": 0, "promoted": 0, "promote_failed": 0, "gate_blocked": 0})


def test_a_no_signal_night_is_ok_and_carries_its_counts(home, m):
    _run(m, [], mem0=Mem0(EV), judge=_judge('{"signals":[]}'))
    assert _outcome(home) == ("ok", {"signals": 0, "consolidated": 0, "posted": 0, "spooled": 0, "replayed": 0,
                                     "replay_failed": 0, "spool_depth": 0, "nominated": 0, "structural_rejected": 0, "promoted": 0, "promote_failed": 0, "gate_blocked": 0})


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


# ---- 1.8: the usage ledger says "not measured", never "0 tokens" ---------------------------------
def _ledger(home):
    p = home / ".mem0" / "maintenance" / "codex-usage.jsonl"
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_ledger_row_keeps_measured_usage_and_null_for_a_miss(home, m):
    def judge(prompt, effort="low", timeout_s=60, model="", **kw):
        if "signals" in prompt and judge.n == 0:
            judge.n += 1
            return {"ok": True, "response": SIG, "tokens_used": 4037, "duration_ms": 3,
                    "model_resolved": "gpt-6-astra", "effort_resolved": "high"}
        judge.n += 1
        # a CLI whose footer could not be read: tokens_used is None, not 0
        return {"ok": True, "response": NONE if judge.n == 2 else PROMO, "tokens_used": None, "duration_ms": 3}
    judge.n = 0
    _run(m, [], mem0=Mem0(EV), judge=judge)
    rows = {r["component"]: r for r in _ledger(home)}
    assert rows["dream-gather"]["tokens_used"] == 4037
    assert rows["dream-gather"]["model_resolved"] == "gpt-6-astra" and rows["dream-gather"]["effort_resolved"] == "high"
    assert rows["dream-consolidate"]["tokens_used"] is None, "unmeasured is null in the ledger"
    assert rows["dream-consolidate"]["model_resolved"] is None


# ---- WG-01 / CM-04: the autopromote phase is part of the receipt ---------------------------------------------------
# 2026-10-07: the dream nominated a memory, the 4C gate said PROMOTE, PATCH /tier answered 422 (the server's imperative
# canary read "... Do not audit, fix or document them" as a standing order) and the receipt was `ok` with no counters.
NOM = '[{"memory_id":"e1","reason":"evergreen invariant","confidence":0.9}]'
BANNER = ("Promoting memory e1e1e1e1-aaaa-bbbb-cccc-dddddddddddd to canonical (action=promote)...\n"
          "  ts=2026-10-07T08:01:18Z\n  nonce=11111111-2222-3333-4444-555555555555\n  token=AbCdEfGhIjKlMnOpQrSt...\n")
REFUSED = BANNER + "curl: (22) The requested URL returned error: 422\nExpecting value: line 1 column 1 (char 0)"


def test_a_refused_promotion_reads_degraded_and_is_counted(home, m, monkeypatch):
    monkeypatch.setenv("MEM0_PROMOTION_GATE_MODE", "off")
    monkeypatch.setattr(m, "_canonize", lambda mid, reason: (1, REFUSED))
    out = _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, INS, NOM))
    head, work = _outcome(home)
    assert head == "degraded:autopromote-failed-1"
    assert (work["nominated"], work["promoted"], work["promote_failed"], work["gate_blocked"]) == (1, 0, 1, 0)
    assert out["outcome"] == head and out["promoted"] == 0


def test_the_promotion_failure_log_keeps_the_cause_not_the_banner(home, m, monkeypatch, capsys):
    """The banner mem0-canonize.sh prints is ~190 characters; the log kept the first 200, so the cause was clipped
    to `curl: (22`. The refusal is at the END of the output."""
    monkeypatch.setenv("MEM0_PROMOTION_GATE_MODE", "off")
    monkeypatch.setattr(m, "_canonize", lambda mid, reason: (1, REFUSED))
    _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, INS, NOM))
    assert "returned error: 422" in capsys.readouterr().out


def test_a_good_promotion_is_counted_and_stays_ok(home, m, monkeypatch):
    monkeypatch.setenv("MEM0_PROMOTION_GATE_MODE", "off")
    monkeypatch.setattr(m, "_canonize", lambda mid, reason: (0, '{"ok": true, "tier": "canonical"}'))
    _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, INS, NOM))
    head, work = _outcome(home)
    assert head == "ok" and (work["nominated"], work["promoted"], work["promote_failed"]) == (1, 1, 0)


def test_a_gate_block_is_counted_but_is_not_a_degraded_night(home, m, monkeypatch):
    monkeypatch.setenv("MEM0_PROMOTION_GATE_MODE", "enforce")
    monkeypatch.setattr(m.ap, "promotion_gate_verdict", lambda mid, text, rec, **kw: {
        "memoryId": mid, "candidatePreview": text[:140], "source": "l1a", "sourceClass": "untrusted", "siblingCount": 0,
        "siblingThreshold": 0.6, "wasReObserved": False, "corroborationCount": 1, "nearCanonicalCount": 0,
        "contradicts": False, "contradictionParsed": True, "contradictionCanonical": None, "codexMs": None,
        "codexTokens": 0, "gate": {"promote": False, "reason": "insufficient corroboration", "gate_class": "uncorroborated"}})
    monkeypatch.setattr(m, "_canonize", lambda mid, reason: (_ for _ in ()).throw(AssertionError("a blocked nominee is never sent")))
    _run(m, [], mem0=Mem0(EV), judge=_judge(SIG, INS, NOM))
    head, work = _outcome(home)
    assert head == "ok" and (work["nominated"], work["promoted"], work["promote_failed"], work["gate_blocked"]) == (1, 0, 0, 1)


def test_a_nominee_the_server_canary_would_refuse_is_never_gated_or_sent(home, m, monkeypatch):
    """The 10-07 shape: the second sentence opens with `Do not`. The dream's own filter looked only at the start of the
    whole text, case-sensitively; the server's canary is per sentence. Now the nominee is a structural reject, so it
    spends no gate call, makes no PATCH, and tomorrow's run meets the same verdict instead of the same 422."""
    calls = []
    monkeypatch.setattr(m, "_canonize", lambda mid, reason: (calls.append(("canonize", mid)), (1, REFUSED))[1])
    monkeypatch.setattr(m.ap, "promotion_gate_verdict", lambda *a, **k: (calls.append(("gate",)), (_ for _ in ()).throw(RuntimeError("gated")))[1])
    ev = [dict(EV[0], memory="The old exporter and the Alpha Drive project are RETIRED. Do not audit, fix or document them; treat any reference to them as stale.")]
    _run(m, [], mem0=Mem0(ev), judge=_judge(SIG, INS, NOM))
    assert calls == []
    head, work = _outcome(home)
    assert head == "ok" and (work["nominated"], work["promote_failed"]) == (0, 0)
    assert work["structural_rejected"] == 1, "the drop is no longer silent: it is a count in the receipt"
