import importlib.util, json, sys
from pathlib import Path
import httpx, pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "wsl"
sys.path.insert(0, str(SCRIPTS))
import codex_usage as cu  # noqa: E402


def test_plan_window_shape_check():
    assert cu.plan_window({"rate_limit": {"primary_window": {"used_percent": 41.6, "reset_after_seconds": 172800}}}) == {"used_percent": 42, "resets_in_days": 2.0, "note": ""}
    w = cu.plan_window({"rate_limit": {}})
    assert w["used_percent"] is None and "unexpected response shape" in w["note"]
    assert "not numeric" in cu.plan_window({"rate_limit": {"primary_window": {"used_percent": "x", "reset_after_seconds": 1}}})["note"]
    assert cu.plan_window(None)["used_percent"] is None


def test_probe_window_uses_codex_home_and_writes_the_row(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path)); (tmp_path / ".mem0").mkdir()
    ch = tmp_path / "sec" / "codex"; ch.mkdir(parents=True)
    (ch / "auth.json").write_text(json.dumps({"tokens": {"access_token": "tok"}}), encoding="utf-8")
    seen = {}
    def h(req):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"rate_limit": {"primary_window": {"used_percent": 2, "reset_after_seconds": 276480}}})
    w = cu.probe_window(str(ch), http=httpx.Client(transport=httpx.MockTransport(h)))
    assert seen["auth"] == "Bearer tok" and w["used_percent"] == 2 and w["resets_in_days"] == 3.2
    rows = [json.loads(l) for l in (tmp_path / ".mem0" / "maintenance" / "codex-usage.jsonl").read_text().splitlines()]
    assert rows[-1]["component"] == "codex-window" and rows[-1]["used_percent"] == 2
    w2 = cu.probe_window(str(tmp_path / "nowhere"), http=httpx.Client(transport=httpx.MockTransport(h)))
    assert w2["used_percent"] is None and "window unavailable" in w2["note"]


def test_quota_gate_reserve():
    assert cu.quota_gate({"used_percent": 75}) == {"allow": True, "reason": "window 75% used <= 75% (25% reserve kept)"}
    assert cu.quota_gate({"used_percent": 76})["allow"] is False
    assert cu.quota_gate({"used_percent": None, "note": "window unavailable (x)"})["allow"] is True


def test_report_aggregates_like_the_ps_version(tmp_path):
    led = tmp_path / "codex-usage.jsonl"
    rows = [{"ts": "2099-01-01T00:00:00+00:00", "component": "dream-gather", "tokens_used": 100, "duration_ms": 300, "outcome": "ok", "model_requested": "a", "model_resolved": "a"},
            {"ts": "2099-01-01T00:00:01+00:00", "component": "dream-gather", "tokens_used": 50, "duration_ms": 100, "outcome": "timeout", "model_requested": "a", "model_resolved": "b"},
            {"ts": "2099-01-01T00:00:02+00:00", "component": "dream-gather", "tokens_used": 0, "duration_ms": "N/A", "outcome": "skipped_no_candidates", "model_requested": "a", "model_resolved": "unparsed"},
            {"ts": "2000-01-01T00:00:00+00:00", "component": "old", "tokens_used": 9}]
    led.write_text("\n".join(json.dumps(r) for r in rows) + "\ntorn line\n", encoding="utf-8")
    import datetime as dt
    rep = cu.report(days=7, ledger=led, now=dt.datetime(2099, 1, 2, tzinfo=dt.timezone.utc), window={"used_percent": None, "note": "n"})
    j = rep["jobs"][0]
    assert j["job"] == "dream-gather" and j["calls"] == 3 and j["tokens"] == 150 and j["failed"] == 1 and j["drift"] == 1 and j["unparsed"] == 1 and j["bad_duration"] == 1
    assert j["p50_ms"] == 300 and j["max_ms"] == 300 and rep["total_calls"] == 3


# --- additions beyond the plan's literal file -----------------------------------------------

def _window_row(used, age_hours):
    import datetime as dt
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_hours)
    return {"ts": ts.isoformat(), "component": "codex-window", "used_percent": used,
            "resets_in_days": 1.5, "note": ""}


def test_last_window_returns_fresh_row_and_ignores_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path)); (tmp_path / ".mem0").mkdir()
    led = tmp_path / ".mem0" / "maintenance"; led.mkdir()
    ledger = led / "codex-usage.jsonl"
    # no ledger at all -> None, never raises
    assert cu.last_window() is None
    # stale row written LAST, fresh row first: newest by ts wins, not by line order
    ledger.write_text(json.dumps(_window_row(70, 1)) + "\n" + json.dumps({"ts": "x", "component": "codex-window"}) + "\n"
                      + json.dumps(_window_row(90, 48)) + "\n", encoding="utf-8")
    w = cu.last_window()
    assert w is not None and w["used_percent"] == 70
    # only a 2-day-old row -> ignored under the 12 h default, returned under a wider window
    ledger.write_text(json.dumps(_window_row(90, 48)) + "\n", encoding="utf-8")
    assert cu.last_window() is None
    assert cu.last_window(max_age_h=72)["used_percent"] == 90


@pytest.mark.parametrize("used,code,verb", [(70, 0, "allow"), (90, 3, "deny")])
def test_cli_gate_exit_codes(tmp_path, used, code, verb):
    import os, subprocess
    home = tmp_path / "home"; (home / ".mem0" / "maintenance").mkdir(parents=True)
    (home / ".mem0" / "maintenance" / "codex-usage.jsonl").write_text(json.dumps(_window_row(used, 1)) + "\n", encoding="utf-8")
    env = dict(os.environ, HOME=str(home), CODEX_HOME=str(tmp_path / "no-codex"))
    for v in ("MEM0_URL", "MEM0_API_KEY_FILE", "MEM0_KEY", "MEM0_API_KEY"):
        env.pop(v, None)
    r = subprocess.run([sys.executable, str(SCRIPTS / "codex-usage-report.py"), "--gate"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == code, r.stdout + r.stderr
    assert r.stdout.startswith(verb + " ") and f"window {used}% used" in r.stdout
    # the fresh row was used as-is: no probe row was appended to the ledger
    rows = (home / ".mem0" / "maintenance" / "codex-usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
