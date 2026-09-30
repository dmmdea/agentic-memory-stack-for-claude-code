# mem0-server/tests/test_ams_store_judge_wrapper.py
"""scripts/wsl/ams-store-judge-apply.sh - the hub's nightly judge loop.

The wrapper is exercised for real (bash) against a FAKE ams-store binary that logs every
call and prints scripted results, so what is asserted is the wrapper's own behaviour:

- the migration cap per store (15 over the compaction trigger, 5 otherwise; P5-6);
- the brand map is passed only when MEM0_BRAND_MAP is set (contract C3);
- the step outcome it writes for the chain (contract C1): counts, and `degraded` for a night
  that offered migrations, migrated none and had corpus write failures;
- the hub checkout is linted every night, after the apply, and its counts join the outcome.

No live stack: the fake binary is a shell script in tmp_path.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from _home_isolation import home_env

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "wsl" / "ams-store-judge-apply.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

FAKE_BIN = r"""#!/usr/bin/env bash
# fake ams-store: log the call, print the scripted result for the subcommand.
sub="$1"; shift
printf '%s %s\n' "$sub" "$*" >> "$FAKE_DIR/calls.log"
case "$sub" in
  sync) exit "${FAKE_SYNC_EXIT:-0}" ;;
  judge-apply)
    ws=""
    while [ $# -gt 0 ]; do
      [ "$1" = "--workspace" ] && ws="$2"
      shift
    done
    if [ -f "$FAKE_DIR/apply-$ws.json" ]; then cat "$FAKE_DIR/apply-$ws.json"; else echo '{}'; fi
    exit "${FAKE_APPLY_EXIT:-0}" ;;
  lint)
    out=""
    while [ $# -gt 0 ]; do
      [ "$1" = "--summary-out" ] && out="$2"
      shift
    done
    [ -n "$out" ] && cp "$FAKE_DIR/lint-summary.json" "$out"
    exit "${FAKE_LINT_EXIT:-0}" ;;
esac
exit 0
"""


def _apply_json(offered=0, migrated=0, add_failed=0, updated=0):
    # The shape judge-apply --json prints (indented, one key per line).
    return json.dumps({"workspace": "x", "status": "applied", "migrated": migrated,
                       "offered": offered, "updated": updated, "add_failed": add_failed,
                       "mem0_orphan": []}, indent=2)


class Hub:
    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.checkout = tmp_path / "checkout"
        self.projects = self.checkout / "projects"
        self.state = self.checkout / "state"
        self.projects.mkdir(parents=True)
        self.state.mkdir(parents=True)
        self.bin = tmp_path / "ams-store"
        self.bin.write_text(FAKE_BIN)
        self.bin.chmod(0o755)
        self.plan = tmp_path / "plan.json"
        self.plan.write_text('{"version":1,"stores":[]}')
        self.outcome = tmp_path / "outcome"
        (tmp_path / "lint-summary.json").write_text(json.dumps(
            {"counts": {"total": 4, "actionable": 3, "unparsed_pointer": 2}}, indent=2))

    def store(self, ws, index_bytes=100, index_lines=3, result=None):
        d = self.projects / ws / "memory"
        d.mkdir(parents=True)
        line = "- [x](x.md) - " + "y" * max(1, index_bytes // max(1, index_lines) - 20) + "\n"
        (d / "MEMORY.md").write_text(line * index_lines)
        if result is not None:
            (self.dir / f"apply-{ws}.json").write_text(result)

    def run(self, env_extra=None, outcome=True):
        # The wrapper reads $HOME/.mem0 (stack.env, the default plan path): the child's home is the
        # sandbox on every platform, so it goes through the shared helper, never HOME alone.
        env = home_env(self.dir)
        env.update({
            "AMS_STORE_BIN": str(self.bin),
            "AMS_STORE_CHECKOUT": str(self.checkout),
            "AMS_STORE_PLAN": str(self.plan),
            "FAKE_DIR": str(self.dir),
        })
        env.pop("MEM0_BRAND_MAP", None)
        env.pop("AMS_OUTCOME_FILE", None)
        if outcome:
            env["AMS_OUTCOME_FILE"] = str(self.outcome)
        env.update(env_extra or {})
        return subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True, env=env,
                              cwd=str(self.dir), timeout=120)

    def calls(self):
        p = self.dir / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def outcome_line(self):
        return self.outcome.read_text().strip()


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path)


def _parse_outcome(line):
    status, _, counts = line.partition(" ")
    return status, json.loads(counts)


def test_script_parses():
    r = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_over_trigger_store_gets_the_larger_migration_cap(hub):
    hub.store("big-bytes", index_bytes=21000, index_lines=100)
    hub.store("big-lines", index_bytes=4000, index_lines=170)
    hub.store("small", index_bytes=1000, index_lines=10)
    r = hub.run()
    assert r.returncode == 0, r.stderr
    apply_calls = {c.split("--workspace ")[1].split()[0]: c for c in hub.calls() if c.startswith("judge-apply")}
    assert "--max-migrations 15" in apply_calls["big-bytes"]
    assert "--max-migrations 15" in apply_calls["big-lines"]
    assert "--max-migrations 5 " in apply_calls["small"] + " "


def test_brand_map_is_passed_only_when_configured(hub):
    hub.store("ws1")
    hub.run()
    assert not any("--brand-map" in c for c in hub.calls() if c.startswith("judge-apply"))
    (hub.dir / "calls.log").unlink()
    r = hub.run({"MEM0_BRAND_MAP": "/etc/brands.json"})
    assert r.returncode == 0, r.stderr
    assert any("--brand-map /etc/brands.json" in c for c in hub.calls() if c.startswith("judge-apply"))


def test_brand_map_path_falls_back_to_stack_env(hub):
    hub.store("ws1")
    (hub.dir / ".mem0").mkdir()
    (hub.dir / ".mem0" / "stack.env").write_text('OTHER=1\nMEM0_BRAND_MAP="/srv/brands.json"\n')
    r = hub.run()
    assert r.returncode == 0, r.stderr
    assert any("--brand-map /srv/brands.json" in c for c in hub.calls() if c.startswith("judge-apply"))


def test_outcome_carries_the_summed_counts_and_the_hub_lint_counts(hub):
    hub.store("a", result=_apply_json(offered=4, migrated=3, updated=1))
    hub.store("b", result=_apply_json(offered=2, migrated=1, add_failed=1))
    r = hub.run()
    assert r.returncode == 0, r.stderr
    status, counts = _parse_outcome(hub.outcome_line())
    assert status == "ok"
    assert counts == {"stores": 2, "offered": 6, "migrated": 4, "add_failed": 1, "updated": 1,
                      "actionable": 3, "unparsed_pointer": 2}


def test_a_night_of_only_failed_writes_is_degraded_not_green(hub):
    # The 2026-09-24 night: 69 offered, every add failed, the step exited 0 and read green.
    hub.store("a", result=_apply_json(offered=29, migrated=0, add_failed=26))
    hub.store("b", result=_apply_json(offered=40, migrated=0, add_failed=0))
    r = hub.run()
    assert r.returncode == 0, "fail-closed stays exit 0: a transient embedder outage must not turn the chain red"
    status, counts = _parse_outcome(hub.outcome_line())
    assert status == "degraded:add-failed-26"
    assert counts["offered"] == 69 and counts["migrated"] == 0 and counts["add_failed"] == 26


def test_some_migrations_landing_is_not_degraded(hub):
    hub.store("a", result=_apply_json(offered=10, migrated=2, add_failed=5))
    hub.run()
    status, _ = _parse_outcome(hub.outcome_line())
    assert status == "ok"


def test_nothing_offered_is_not_degraded(hub):
    hub.store("a", result=_apply_json(offered=0, migrated=0, add_failed=0))
    hub.run()
    status, _ = _parse_outcome(hub.outcome_line())
    assert status == "ok"


def test_the_hub_checkout_is_linted_after_the_apply(hub):
    hub.store("a", result=_apply_json(offered=1, migrated=1))
    r = hub.run()
    assert r.returncode == 0, r.stderr
    calls = hub.calls()
    lint = [i for i, c in enumerate(calls) if c.startswith("lint")]
    applies = [i for i, c in enumerate(calls) if c.startswith("judge-apply")]
    assert len(lint) == 1, calls
    assert lint[0] > max(applies), "lint must run after the apply"
    assert "--summary-out " + str(hub.state / "lint-summary.json") in calls[lint[0]]
    assert (hub.state / "lint-summary.json").exists()


def test_a_failed_lint_leaves_its_counts_out_but_still_writes_the_outcome(hub):
    hub.store("a", result=_apply_json(offered=1, migrated=1))
    r = hub.run({"FAKE_LINT_EXIT": "1"})
    assert r.returncode == 0, r.stderr
    status, counts = _parse_outcome(hub.outcome_line())
    assert status == "ok"
    assert "actionable" not in counts and "unparsed_pointer" not in counts
    assert counts["migrated"] == 1


def test_no_outcome_file_no_problem(hub):
    hub.store("a", result=_apply_json(offered=1, migrated=1))
    r = hub.run(outcome=False)
    assert r.returncode == 0, r.stderr
    assert not hub.outcome.exists()


def test_a_missing_plan_is_a_deterministic_night_with_an_ok_outcome(hub):
    hub.plan.unlink()
    hub.store("a")
    r = hub.run()
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith("judge-apply") for c in hub.calls())
    status, counts = _parse_outcome(hub.outcome_line())
    assert status == "ok" and counts["stores"] == 0
    assert any(c.startswith("lint") for c in hub.calls()), "the checkout is linted even on a plan-less night"


def test_the_journal_carries_each_stores_result(hub):
    hub.store("a", result=_apply_json(offered=1, migrated=0, add_failed=1))
    r = hub.run()
    assert "ams-store-judge: a max_migrations=5" in r.stdout
    assert '"add_failed": 1' in r.stdout
