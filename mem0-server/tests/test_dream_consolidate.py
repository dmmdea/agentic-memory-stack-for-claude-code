"""dream-consolidate.py on the authority: store-fed gather, one judge lock, quota gate, receipts
(register P1-3). Collaborators are injected: a fake mem0 client, a scripted judge, a recording
eval runner; HOME lives under tmp_path so nothing touches the operator's ~/.mem0."""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "wsl"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / "mem0-server"))


def _mod():
    spec = importlib.util.spec_from_file_location("dream_consolidate", SCRIPTS / "dream-consolidate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeMem0:
    def __init__(self, evidence=None, canon=None, healthy=True):
        self.ev = evidence or []
        self.canon = canon or []
        self.healthy = healthy
        self.added = []
        self.patched = []

    def health(self):
        return self.healthy

    def evidence(self, limit=100):
        return self.ev[:limit]

    def all_points(self, **kw):
        return sorted(self.ev, key=lambda e: e.get("created_at", ""), reverse=True)

    def search_canonical(self):
        return self.canon

    def goals(self, status, limit):
        return [{"title": f"{status} goal", "brand": "b", "priority": 2}]

    def open_questions(self, status, limit):
        return [{"question_text": "q?", "brand": "b"}]

    def episodes(self, recent):
        return [{"ended_at": "2026-09-10T08:18:00", "brand": "b", "goal_text": "g", "summary_text": "s"}]

    def add(self, text, metadata):
        self.added.append((text, metadata))
        return f"new{len(self.added)}"

    def patch_metadata(self, mid, metadata, actor, reason):
        self.patched.append(mid)
        return True

    def read_memory_md(self):
        return "# index"

    def health_deep(self):
        return {"ok": True, "checks": {"capabilities": {"dead_required": [], "unknown": []}}}


def _judge(*replies):
    it = iter(replies)
    calls = []

    def j(prompt, effort="low", timeout_s=60, model="", **kw):
        calls.append((model, effort))
        r = next(it)
        if isinstance(r, str):
            return {"ok": True, "response": r, "tokens_used": 7, "duration_ms": 3, "transport": "native"}
        return r
    j.calls = calls
    return j


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".mem0").mkdir()
    for v in ("MEM0_PROMOTION_GATE_MODE", "MEM0_EVAL_ROOT", "MEM0_URL", "MEM0_KEY", "MEM0_API_KEY_FILE"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


NOW = "2026-09-11T08:00:00+00:00"
EV = [{"id": "e1", "memory": "The authority binds the tailnet address only", "created_at": "2026-09-11T06:00:00+00:00",
       "metadata": {"tier": "evidence", "source": "l1a-extractor"}},
      {"id": "e2", "memory": "old fact", "created_at": "2026-09-01T06:00:00+00:00", "metadata": {"tier": "evidence"}}]
SIG = '{"signals":[{"kind":"decision","text":"bind tailnet","source_transcript":"s","priority":4}]}'
INS = '{"insights":[{"text":"The authority is reachable only over the tailnet","source_signal_indexes":[0],"source_memory_ids":["e1"],"confidence":0.8}]}'


def _run(m, args, **kw):
    kw.setdefault("qdrant_http", None)
    kw.setdefault("eval_runner", lambda c: (0, ""))
    kw.setdefault("now", NOW)
    return m.run(m.parse_args(args), **kw)


def test_gather_is_store_fed_and_36h_windowed(home):
    m = _mod()
    seen = {}

    def j(prompt, **kw):
        seen["p"] = prompt
        return {"ok": True, "response": '{"signals":[]}', "tokens_used": 1, "duration_ms": 1}
    out = _run(m, [], mem0=FakeMem0(EV), judge=j)
    assert "e1" in seen["p"] and "old fact" not in seen["p"] and "open goal" in seen["p"] and "q?" in seen["p"]
    assert "l1a-extractor" in seen["p"], "evidence carries its source tag into the gather corpus"
    assert out["phase"] == "gather" and out["note"] == "no signals; nothing to consolidate"
    assert (home / ".mem0" / "maintenance" / "last-dream").exists(), "a real no-signal night marks the throttle"
    assert json.loads((home / ".mem0" / "maintenance" / "dream" / "gather.json").read_text())["dry_run"] is False


def test_dry_run_never_marks_or_writes(home):
    m = _mod()
    _run(m, ["--dry-run"], mem0=FakeMem0(EV), judge=_judge('{"signals":[]}'))
    assert not (home / ".mem0" / "maintenance" / "last-dream").exists()
    assert not (home / ".mem0" / "maintenance" / "morning-summary.md").exists()


def test_throttle_and_force(home):
    m = _mod()
    (home / ".mem0" / "maintenance").mkdir()
    (home / ".mem0" / "maintenance" / "last-dream").write_text(str(int(time.time())))
    j = _judge()
    out = _run(m, [], mem0=FakeMem0(EV), judge=j)
    assert out["note"].startswith("skipping: nightly throttle") and j.calls == []
    out = _run(m, ["--force"], mem0=FakeMem0(EV), judge=_judge('{"signals":[]}'))
    assert out["phase"] == "gather"


def test_quota_gate_skips_without_marking(home, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m.codex_usage, "last_window", lambda max_age_h=12: {"used_percent": 90, "resets_in_days": 1.0, "note": ""})
    j = _judge()
    out = _run(m, [], mem0=FakeMem0(EV), judge=j)
    assert "quota" in out["note"] and j.calls == [] and not (home / ".mem0" / "maintenance" / "last-dream").exists()
    rows = [json.loads(ln) for ln in (home / ".mem0" / "maintenance" / "codex-usage.jsonl").read_text().splitlines()]
    assert rows[-1]["outcome"] == "skipped_quota"


def test_unknown_window_allows_and_probe_is_used_once(home, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m.codex_usage, "last_window", lambda max_age_h=12: None)
    probes = []
    out = _run(m, [], mem0=FakeMem0(EV), judge=_judge('{"signals":[]}'),
               probe=lambda ch: (probes.append(ch), {"used_percent": None, "note": "window unavailable (x)"})[1])
    assert probes and out["phase"] == "gather"


def test_full_cycle_posts_insights_with_lineage_and_runs_index(home, monkeypatch):
    m = _mod()
    fm = FakeMem0(EV)
    ran = []
    monkeypatch.setattr(m, "_run_deployed", lambda script, env=None: (ran.append(script), (0, "ok"))[1])
    j = _judge(SIG, INS, "[]")
    out = _run(m, [], mem0=fm, judge=j)
    assert out["phase"] == "done" and out["posted"] == 1
    assert fm.added[0][1]["tier"] == "insight" and fm.added[0][1]["source_memory_ids"] == ["e1"] and fm.patched == ["e1"]
    assert [c[0] for c in j.calls] == ["gpt-5.6-terra", "gpt-6-astra", "gpt-6-astra"]
    assert [c[1] for c in j.calls] == ["medium", "medium", "medium"]
    assert ran == ["memory-index-build.py", "brand-scope-audit.py"]
    st = home / ".mem0" / "maintenance"
    assert (st / "last-dream").exists() and (st / "last-index-refresh").exists()
    assert json.loads((st / "dream" / "prune.json").read_text())["index_rebuilt"] is True
    ms = (st / "morning-summary.md").read_text()
    assert "## Autonomous canonical promotions" in ms and "## Heartbeat" in ms and "none promoted" in ms
    comps = [json.loads(ln)["component"] for ln in (st / "codex-usage.jsonl").read_text().splitlines()]
    assert comps == ["dream-gather", "dream-consolidate", "dream-promote", "dream"]


def test_index_failure_does_not_mark_throttle(home, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_run_deployed", lambda script, env=None: (2, "boom"))
    out = _run(m, [], mem0=FakeMem0(EV), judge=_judge(SIG, '{"insights":[]}', "[]"))
    assert "index build failed" in out["note"]
    assert not (home / ".mem0" / "maintenance" / "last-dream").exists()


def test_malformed_consolidate_json_does_not_mark(home, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_run_deployed", lambda script, env=None: (0, ""))
    out = _run(m, [], mem0=FakeMem0(EV), judge=_judge(SIG, "I cannot produce JSON today"))
    assert "malformed JSON" in out["note"] and not (home / ".mem0" / "maintenance" / "last-dream").exists()


def test_dedup_lock_skips_without_marking(home):
    m = _mod()
    (home / ".mem0" / "dedup.lock").write_text("x")
    out = _run(m, [], mem0=FakeMem0(EV), judge=_judge(SIG))
    assert "semantic-dedup mutex held" in out["note"] and not (home / ".mem0" / "maintenance" / "last-dream").exists()


def test_gate_verdict_logged_and_enforce_blocks(home, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_run_deployed", lambda script, env=None: (0, ""))
    monkeypatch.setattr(m.ap, "promotion_gate_verdict", lambda mid, text, rec, **kw: {
        "memoryId": mid, "candidatePreview": text[:140], "source": "l1a", "sourceClass": "untrusted", "siblingCount": 0,
        "siblingThreshold": 0.6, "wasReObserved": False, "corroborationCount": 1, "nearCanonicalCount": 0, "contradicts": False,
        "contradictionParsed": True, "contradictionCanonical": None, "codexMs": None, "codexTokens": 0,
        "gate": {"promote": False, "reason": "insufficient corroboration", "gate_class": "uncorroborated"}})
    canon = []
    monkeypatch.setattr(m, "_canonize", lambda mid, reason: (canon.append(mid), (0, '{"tier": "canonical"}'))[1])
    nom = '[{"memory_id":"e1","reason":"evergreen invariant","confidence":0.9}]'
    monkeypatch.setenv("MEM0_PROMOTION_GATE_MODE", "enforce")
    out = _run(m, [], mem0=FakeMem0(EV), judge=_judge(SIG, '{"insights":[]}', nom))
    assert canon == [] and out["promoted"] == 0
    rec = json.loads((home / ".mem0" / "promotion-gate.jsonl").read_text().splitlines()[-1])
    assert rec["mode"] == "enforce" and rec["gate_class"] == "uncorroborated" and rec["schema_version"] == "pg-v1"
    assert "GATE-BLOCKED" in (home / ".mem0" / "maintenance" / "morning-summary.md").read_text()
    monkeypatch.setenv("MEM0_PROMOTION_GATE_MODE", "shadow")
    out = _run(m, ["--force"], mem0=FakeMem0(EV), judge=_judge(SIG, '{"insights":[]}', nom))
    assert canon == ["e1"] and out["promoted"] == 1


def test_drift_canary_before_after_and_alarm(home, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_run_deployed", lambda script, env=None: (0, ""))
    monkeypatch.setenv("MEM0_EVAL_ROOT", str(home / "eval"))
    (home / "eval" / "eval" / "retrieval-drift").mkdir(parents=True)
    (home / "eval" / "eval" / "retrieval-drift" / "retrieval_drift.py").write_text("")
    argvs = []

    def ev(cmd):
        argvs.append(cmd)
        if "-h" in cmd:
            return (0, "--state")
        if "snapshot" in cmd:
            Path(cmd[cmd.index("--out") + 1]).write_text("{}")
            return (0, "snapshot: 7/7")
        if "compare" in cmd:
            return (2, "degraded")
        return (0, "")
    _run(m, [], mem0=FakeMem0(EV), judge=_judge(SIG, '{"insights":[]}', "[]"), eval_runner=ev)
    kinds = [c[c.index("retrieval_drift.py") + 1] for c in argvs if "retrieval_drift.py" in c and "-h" not in c]
    assert kinds == ["snapshot", "snapshot", "compare"] and "--state" in argvs[-1]
    outs = [c[c.index("--out") + 1] for c in argvs if "--out" in c]
    state_dir = str(home / ".mem0" / "maintenance" / "dream")
    assert outs and all(o.startswith(state_dir) for o in outs), "snapshots live under the dataset-backed state dir, never the system temp dir"
    rec = json.loads((home / ".mem0" / "consolidation-drift.jsonl").read_text().splitlines()[-1])
    assert rec["kind"] == "drift"


def test_orient_read_failure_degrades_to_empty(home):
    m = _mod()
    fm = FakeMem0(EV)
    fm.goals = lambda status, limit: (_ for _ in ()).throw(RuntimeError("blip"))
    out = _run(m, [], mem0=fm, judge=_judge('{"signals":[]}'))
    assert out["phase"] == "gather", "a transient orient read failure is logged, not a traceback"


def test_failed_phases_exit_5_and_skips_exit_0(home, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_run_deployed", lambda script, env=None: (2, "boom"))
    with pytest.raises(SystemExit) as e:
        m.main([], mem0=FakeMem0(EV), judge=_judge(SIG, '{"insights":[]}', "[]"), qdrant_http=None, eval_runner=lambda c: (0, ""))
    assert e.value.code == 5, "an index build failure must receipt ok:false"
    with pytest.raises(SystemExit) as e:
        m.main([], mem0=FakeMem0(EV), judge=_judge({"ok": False, "error_type": "usage_limit", "error": "x"}), qdrant_http=None, eval_runner=lambda c: (0, ""))
    assert e.value.code == 5
    (home / ".mem0" / "dedup.lock").write_text("x")
    with pytest.raises(SystemExit) as e:
        m.main([], mem0=FakeMem0(EV), judge=_judge(SIG), qdrant_http=None, eval_runner=lambda c: (0, ""))
    assert e.value.code == 0, "a deliberate skip is a quiet night"


def test_promotion_summary_append_failure_is_non_fatal(home, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_run_deployed", lambda script, env=None: (0, ""))
    orig = m.Dream._append_morning
    calls = []

    def flaky(self, section):
        calls.append(section)
        if len(calls) == 1:
            raise OSError("disk full")
        return orig(self, section)
    monkeypatch.setattr(m.Dream, "_append_morning", flaky)
    out = _run(m, [], mem0=FakeMem0(EV), judge=_judge(SIG, '{"insights":[]}', "[]"))
    assert out["phase"] == "done" and (home / ".mem0" / "maintenance" / "last-dream").exists()


def test_unreachable_authority_exits_4(home):
    m = _mod()
    with pytest.raises(SystemExit) as e:
        m.main([], mem0=FakeMem0(EV, healthy=False), judge=_judge(), qdrant_http=None, eval_runner=lambda c: (0, ""))
    assert e.value.code == 4


def test_extract_json_accepts_bare_arrays_and_fences():
    m = _mod()
    assert m.extract_json("[]", "signals") == {"signals": []}
    assert m.extract_json('```json\n{"insights":[{"text":"x"}]}\n```', "insights")["insights"][0]["text"] == "x"
    assert m.extract_json('prose {"signals":[{"kind":"x"}]} more', "signals")["signals"][0]["kind"] == "x"
    assert m.extract_json("nothing here", "signals") is None


def test_no_powershell_or_wsl_paths_in_the_port():
    t = (SCRIPTS / "dream-consolidate.py").read_text(encoding="utf-8")
    for bad in ("wsl.exe", "powershell", "/mnt/c", "USERPROFILE", "\\\\wsl.localhost", "/tmp/dream-drift"):
        assert bad not in t


def test_all_points_scrolls_qdrant_newest_first(home):
    import httpx
    m = _mod()
    pages = {None: ({"points": [{"id": "a", "payload": {"data": "old", "user_id": "u", "created_at": "2026-09-01T00:00:00Z", "tier": "evidence", "source": "l1a"}},
                                {"id": "hidden", "payload": {"data": "x", "user_id": "u", "created_at": "2026-09-11T00:00:00Z", "retrievable": False}}],
                     "next_page_offset": "p2"}),
             "p2": ({"points": [{"id": "b", "payload": {"data": "new", "user_id": "u", "created_at": "2026-09-11T06:00:00Z", "tier": "evidence"}}],
                     "next_page_offset": None})}
    seen = []

    def h(req):
        body = json.loads(req.content)
        seen.append(body.get("offset"))
        assert body["filter"]["must"][0]["match"]["value"] == "u"
        return httpx.Response(200, json={"result": pages[body.get("offset")]})
    c = m.Mem0Client("http://x", "k", "u", http=httpx.Client(transport=httpx.MockTransport(h)))
    pts = c.all_points()
    assert seen == [None, "p2"]
    assert [p["id"] for p in pts] == ["b", "a"], "newest first, unretrievable points dropped"
    assert pts[1]["metadata"]["source"] == "l1a" and pts[0]["memory"] == "new"
