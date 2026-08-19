"""Tests for scripts/wsl/stale-paths-audit.py (memory-frontier: milestone validity).

The audit exists to produce a HAND-LABEL dataset, so its mechanical verdicts must be
conservative in one specific direction: nothing may be called STALE unless the path is
genuinely missing AND unexplained. Every test below guards a way the instrument could
manufacture false staleness - which is the failure mode that would corrupt the dataset
the schema gets designed from.

Headless: no Qdrant, no network. Classification is pure over (path, text).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "stale_paths_audit", REPO_ROOT / "scripts" / "wsl" / "stale-paths-audit.py")
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)


@pytest.fixture(autouse=True)
def _reset_run_caches(monkeypatch):
    """The module caches per-run state (_SHARED_ROOTS, _UNC_ROOT, _WSL_HOME). Without a
    reset, whichever test first trips a cache under ITS monkeypatched filesystem leaks
    that state into every later test - order-dependent and runtime-dependent (verified:
    the G:-root test caches _SHARED_ROOTS=[] via mocked probes)."""
    monkeypatch.setattr(sp, "_SHARED_ROOTS", sp._UNSET)
    monkeypatch.setattr(sp, "_UNC_ROOT", sp._UNSET)
    monkeypatch.setattr(sp, "_WSL_HOME", sp._UNSET)
    monkeypatch.delenv("MEM0_SHARED_DRIVE_LETTERS", raising=False)
    # SHARED_DRIVE_DIR binds at import, so an operator's MEM0_SHARED_DRIVE_DIR would
    # otherwise leak into every hardcoded "My Drive" fixture below.
    monkeypatch.setattr(sp, "SHARED_DRIVE_DIR", "My Drive")


# --- the instrument bug: an unreachable ROOT is never staleness -----------------

def test_unreachable_root_is_undecidable_never_stale(monkeypatch):
    """G: and P: are not mounted under WSL. The naive check marked every G: path
    missing and invented staleness. A root we cannot see is a COVERAGE gap."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sp.Path, "is_dir", lambda self: False)
    verdict, resolved = sp.classify_path(r"G:\My Drive\AI Ecosystem\thing.md",
                                         "A note about thing.md")
    assert verdict == "root-unavailable"
    assert resolved is None


def test_root_unavailable_excluded_from_the_rate():
    """Undecidable outcomes must not sit in the denominator either - a coverage gap
    must not quietly deflate the rate the way it once inflated it."""
    assert "root-unavailable" not in ("exists", "missing-recorded", "missing-unexplained")
    # precedence must rank the undecidable reasons BELOW the decidable ones,
    # so a memory with one stale path is stale even if another path is unreachable
    assert sp.PRECEDENCE.index("missing-unexplained") < sp.PRECEDENCE.index("root-unavailable")


# --- bias 2: other machines ------------------------------------------------------

@pytest.mark.parametrize("text", [
    "The ops checkout at C:\\dev\\ops-sre runs on the buildbox.",
    "On the sidecar node, the repos live at D:\\repos\\thing.",
])
def test_foreign_host_paths_are_undecidable(text, monkeypatch):
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    monkeypatch.setattr(sp, "_translate", lambda p: "/nonexistent/" + p[-8:])
    monkeypatch.setattr(sp, "FOREIGN_HOSTS", sp._host_pattern(["buildbox", "sidecar"]))
    monkeypatch.setattr(sp, "LOCAL_HOST", sp._host_pattern(["thisbox"]))
    raw = sp.WINPATH.findall(text)[0]
    verdict, _ = sp.classify_path(raw, text)
    assert verdict == "missing-foreign-host"


def test_local_host_mention_beats_foreign_host(monkeypatch):
    """A memory naming THIS box is making a local claim; a missing path there is real
    staleness, not another machine's business."""
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    monkeypatch.setattr(sp, "_translate", lambda p: "/nonexistent/x")
    monkeypatch.setattr(sp, "FOREIGN_HOSTS", sp._host_pattern(["buildbox"]))
    monkeypatch.setattr(sp, "LOCAL_HOST", sp._host_pattern(["thisbox"]))
    text = "On thisbox the binary is at D:\\Dev\\thing\\app.exe; also sent to the buildbox."
    verdict, _ = sp.classify_path("D:\\Dev\\thing\\app.exe", text)
    assert verdict == "missing-unexplained"


def test_unset_foreign_hosts_disables_the_correction(monkeypatch):
    """UNSET must mean the correction is OFF (an over-count the report states) rather
    than silently matching everything or crashing."""
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    monkeypatch.setattr(sp, "_translate", lambda p: "/nonexistent/x")
    monkeypatch.setattr(sp, "FOREIGN_HOSTS", sp._host_pattern([]))
    monkeypatch.setattr(sp, "LOCAL_HOST", sp._host_pattern(["thisbox"]))
    text = "The checkout at C:\\dev\\ops-sre runs on the buildbox."
    verdict, _ = sp.classify_path("C:\\dev\\ops-sre", text)
    assert verdict == "missing-unexplained"


def test_host_pattern_is_none_when_empty():
    assert sp._host_pattern([]) is None
    assert sp._host_pattern(["", "  "]) is None
    assert sp._host_pattern(["boxA"]).search("on boxA today")


# --- bias 3: the memory whose SUBJECT is the removal -----------------------------

@pytest.mark.parametrize("text", [
    "The GLM model was deleted from V:\\models\\glm to reclaim space.",
    "Laguna weights removed from V:\\models\\laguna (72.1 GB reclaimed).",
    "gpt-oss-20b GGUF was purged from V:\\models\\oss.",
    "The worktree at D:\\Dev\\worktrees\\old was retired 2026-07-01.",
    "Config no longer lives at C:\\old\\path\\conf.yaml.",
    "The build dir D:\\tmp\\build was cleaned up after the release.",
    "Weights migrated from D:\\models\\a to V:\\models\\a.",
])
def test_recorded_removal_is_not_stale(text, monkeypatch):
    """Scoring these stale INVERTS their meaning - the memory is correct."""
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    monkeypatch.setattr(sp, "_translate", lambda p: "/nonexistent/x")
    raw = sp.WINPATH.findall(text)[0]
    verdict, _ = sp.classify_path(raw, text)
    assert verdict == "missing-recorded"


@pytest.mark.parametrize("text,path", [
    ("Outputs went to D:\\Dev\\deadpath\\gone.md today.", "D:\\Dev\\deadpath\\gone.md"),
    ("The report lives at D:\\archive\\2026\\report.md.", "D:\\archive\\2026\\report.md"),
    ("Weights are at V:\\models\\deleted-experiments\\a.gguf.",
     "V:\\models\\deleted-experiments\\a.gguf"),
    ("Config at C:\\old\\removed-stuff\\conf.yaml.", "C:\\old\\removed-stuff\\conf.yaml"),
])
def test_removal_vocabulary_in_the_PATH_does_not_excuse_staleness(text, path, monkeypatch):
    """Regression: DEAD_VERB once scanned the raw text, so a path spelling 'gone',
    'archive' or 'deleted' excused itself as a deletion record. That direction makes
    real staleness VANISH from the hand-label dataset."""
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    monkeypatch.setattr(sp, "_translate", lambda p: "/nonexistent/x")
    verdict, _ = sp.classify_path(path, text)
    assert verdict == "missing-unexplained"


def test_prose_strips_paths_but_keeps_sentence_vocabulary():
    prose = sp._prose("The model was deleted from V:\\models\\gone\\a.gguf last week.")
    assert "deleted" in prose
    assert "V:\\models" not in prose


def test_plain_missing_path_is_stale(monkeypatch):
    """The control for the test above: no removal vocabulary => genuinely stale."""
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    monkeypatch.setattr(sp, "_translate", lambda p: "/nonexistent/x")
    text = "The task report is at D:\\repos\\thing\\report.md and covers phase 2."
    verdict, _ = sp.classify_path("D:\\repos\\thing\\report.md", text)
    assert verdict == "missing-unexplained"


# --- parse artifacts -------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "C:\\\\Users\\\\dmmde",      # double-escaped in the stored JSON
    "C:\\Users\\...",            # truncated
    "C:\\a`b",                   # markdown fence residue
    "C:\\x",                     # too short
    "C:\\",                      # bare root
    "D:\\${VAR}\\x",             # unexpanded template
])
def test_artifacts_are_not_path_claims(bad):
    assert sp._artifact(bad) is True


def test_real_path_is_not_an_artifact():
    assert sp._artifact("D:\\Dev\\worktrees\\ams-deploy3\\mem0-server\\app.py") is False


# --- cross-runtime translation (one run must decide everything) ------------------

def test_windows_maps_mnt_back_to_drive(monkeypatch):
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    assert sp._translate("/mnt/c/Users/dmmde") == "C:/Users/dmmde"


def test_windows_reaches_wsl_home_over_unc(monkeypatch):
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp, "_UNC_ROOT", "//wsl.localhost/Ubuntu-ML")
    assert sp._translate("/home/dmmdea/.mem0") == "//wsl.localhost/Ubuntu-ML/home/dmmdea/.mem0"


def test_windows_without_wsl_share_is_undecidable(monkeypatch):
    """No WSL share => POSIX paths are UNDECIDABLE, never stale."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp, "_UNC_ROOT", None)
    assert sp._translate("/home/dmmdea/.mem0") is None


def test_wsl_maps_drive_to_mnt(monkeypatch):
    # ismount, not Path.is_dir: _translate moved to ismount (empty /mnt/<d> stubs) and
    # the stale is_dir stub silently made this test depend on the REAL /mnt/c mount -
    # green under WSL, red under the recommended Windows interpreter.
    monkeypatch.setattr(sp.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sp.os.path, "ismount", lambda p: True)
    assert sp._translate("C:\\Users\\dmmde") == "/mnt/c/Users/dmmde"


# --- memory-level precedence ------------------------------------------------------

def test_memory_with_one_stale_path_is_stale(monkeypatch):
    """A memory naming a live path AND a dead one is stale - the worst decidable
    outcome wins, otherwise a single surviving path would whitewash the record."""
    monkeypatch.setattr(sp, "_translate", lambda p: p)
    monkeypatch.setattr(sp.os.path, "exists", lambda p: "alive" in p)
    text = "Outputs went to D:\\Dev\\alive\\out.md and D:\\Dev\\deadpath\\gone.md today."
    assert sp.classify_memory(text)["verdict"] == "missing-unexplained"


def test_memory_with_no_path_is_no_path():
    assert sp.classify_memory("Daniel prefers concise answers.")["verdict"] == "no-path"


def test_memory_with_only_artifacts_is_not_stale():
    out = sp.classify_memory("See C:\\\\Users\\\\x for the double-escaped example text.")
    assert out["verdict"] == "artifact-only"


# --- endpoint scheme guard (semgrep dynamic-urllib finding) -----------------------

@pytest.mark.parametrize("bad_url", [
    "file:///C:/Windows/win.ini",
    "ftp://example.invalid/x",
    "data:text/plain;base64,AAAA",
])
def test_scheme_guard_refuses_file_url(bad_url, monkeypatch):
    """urllib honours file:// / ftp:// / data://, and MEM0_QDRANT_URL is env-controlled,
    so an unguarded urlopen would turn a read-only audit into an arbitrary-file reader."""
    monkeypatch.setattr(sp, "QDRANT_URL", bad_url)
    with pytest.raises(ValueError, match="refusing scheme"):
        sp._checked_endpoint()


@pytest.mark.parametrize("ok_url", ["http://127.0.0.1:6333", "https://qdrant.internal:6333"])
def test_scheme_guard_allows_http(ok_url, monkeypatch):
    monkeypatch.setattr(sp, "QDRANT_URL", ok_url)
    assert sp._checked_endpoint().startswith(ok_url.rstrip("/"))
    assert sp._checked_endpoint().endswith("/points/scroll")


# --- read-only guarantee ----------------------------------------------------------

def test_audit_never_mutates_the_store():
    """The estate has already lost 688 MB to an automated process behaving as designed.
    This script must contain no delete/patch/put verb against mem0 or Qdrant."""
    src = (REPO_ROOT / "scripts" / "wsl" / "stale-paths-audit.py").read_text(encoding="utf-8")
    for forbidden in ('"DELETE"', "'DELETE'", '"PATCH"', "'PATCH'", '"PUT"', "'PUT'",
                      "/points/delete", "method=\"POST\", data=None"):
        assert forbidden not in src, f"audit must stay read-only; found {forbidden}"
    # the only endpoint it may POST to is the scroll (read) API
    assert "/points/scroll" in src


def test_audit_does_not_write_the_admission_axis_log():
    """audit-flags.jsonl belongs to l10-audit on the ADMISSION axis. Validity is a
    different axis; overloading admission reasons would corrupt both."""
    src = (REPO_ROOT / "scripts" / "wsl" / "stale-paths-audit.py").read_text(encoding="utf-8")
    assert "audit-flags.jsonl" not in src.replace(
        "~/.mem0/audit-flags.jsonl", "")  # docstring mention of the boundary is fine
    assert 'REPORT = MEM0 / "stale-paths-audit.jsonl"' in src


# --- worksheet round-trip ----------------------------------------------------------

def test_worksheet_summary_flags_the_cheapest_kill(tmp_path, monkeypatch, capsys):
    """If the hand-labels say these are mostly EPHEMERAL, the tool must say so - the
    cheap outcome is 'stop writing them', not 'build a validity schema'."""
    ws = tmp_path / "ws.jsonl"
    import json as _j
    rows = [{"_worksheet_header": True}]
    rows += [{"memory_id": f"m{i}", "verdict_mechanical": "missing-unexplained",
              "label": "EPHEMERAL", "label_recalled": "no"} for i in range(7)]
    rows += [{"memory_id": f"s{i}", "verdict_mechanical": "missing-unexplained",
              "label": "STALE", "label_recalled": "yes"} for i in range(3)]
    ws.write_text("\n".join(_j.dumps(r) for r in rows), encoding="utf-8")
    monkeypatch.setattr(sp, "WORKSHEET", ws)
    assert sp.summarise_worksheet() == 0
    out = capsys.readouterr().out
    assert "CHEAPEST KILL is live" in out
    assert "EPHEMERAL share" in out


def test_worksheet_summary_endorses_schema_when_labels_are_substantive(
        tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws.jsonl"
    import json as _j
    rows = [{"_worksheet_header": True}]
    rows += [{"memory_id": f"s{i}", "verdict_mechanical": "missing-unexplained",
              "label": "STALE", "label_recalled": "yes"} for i in range(8)]
    rows += [{"memory_id": "e1", "verdict_mechanical": "missing-unexplained",
              "label": "EPHEMERAL", "label_recalled": "no"}]
    ws.write_text("\n".join(_j.dumps(r) for r in rows), encoding="utf-8")
    monkeypatch.setattr(sp, "WORKSHEET", ws)
    assert sp.summarise_worksheet() == 0
    out = capsys.readouterr().out
    assert "validity schema" in out
    assert "CHEAPEST KILL is live" not in out


def test_unlabelled_worksheet_reports_nothing_to_summarise(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws.jsonl"
    import json as _j
    ws.write_text(_j.dumps({"_worksheet_header": True}) + "\n"
                  + _j.dumps({"memory_id": "m1", "label": ""}) + "\n", encoding="utf-8")
    monkeypatch.setattr(sp, "WORKSHEET", ws)
    assert sp.summarise_worksheet() == 1
    assert "0 hand-labelled" in capsys.readouterr().out


# --- review round 2: findings from the fresh-context specialists ------------------
# Each of these reproduces a bug that was measured on the live corpus, not imagined.

@pytest.mark.parametrize("text,expected", [
    (r"Spec lives at G:\My Drive\AI Ecosystem\spec.md today.",
     r"G:\My Drive\AI Ecosystem\spec.md"),
    (r"Installed at C:\Program Files\Foo\bar.exe on this box.",
     r"C:\Program Files\Foo\bar.exe"),
    (r"Ports live in P:\Port Directory\ports.md here.", r"P:\Port Directory\ports.md"),
])
def test_paths_with_spaces_are_not_truncated(text, expected):
    """Stopping at the first space truncated this estate's most-cited roots. The stub
    then either did not exist (INVENTED stale) or was dropped as too short (DELETED
    stale). Trailing prose must still be excluded."""
    assert sp.WINPATH.findall(text) == [expected]


def test_trailing_prose_is_not_swallowed_by_the_space_rule():
    got = sp.WINPATH.findall(r"Installed at C:\Program Files\Foo\bar.exe on this box.")
    assert got == [r"C:\Program Files\Foo\bar.exe"]


@pytest.mark.parametrize("text", [
    "Skill at ~/.claude/skills/demo/SKILL.md today.",
    "Script at C:/Users/someone/scripts/check.ps1 runs nightly.",
    "Config /etc/gdm3/custom.conf was edited.",
])
def test_previously_invisible_path_shapes_are_extracted(text):
    """394 memories asserting a path were reported as 'names no path at all' - a false
    statement about the corpus, and 77% the size of the whole decidable population."""
    assert sp.WINPATH.findall(text) or sp.POSIXPATH.findall(text)


def test_windows_branch_also_guards_unreachable_roots(monkeypatch):
    """The WSL branch got this check first; leaving the Windows branch unguarded meant
    the RECOMMENDED runtime still invented staleness. One instance fixed is not the
    class fixed."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    assert sp._translate(r"Q:\bin\launcher.exe") is None


def test_dotted_path_still_matches_its_own_sentence():
    """Splitting sentences on a bare '.' also split conf.yaml, after which the path was
    in no fragment and every genuine removal record reverted to STALE."""
    assert sp._records_removal(r"Config no longer lives at C:\old\conf.yaml.",
                               r"C:\old\conf.yaml") is True
    assert sp._records_removal(r"The model was purged from V:\models\oss.gguf today.",
                               r"V:\models\oss.gguf") is True


def test_unparseable_worksheet_line_refuses_a_verdict(tmp_path, monkeypatch, capsys):
    """Silently skipping a malformed line shrinks the denominator AND the EPHEMERAL
    ratio - measured to invert build-vs-kill on four broken rows while printing a
    100%-complete-looking denominator."""
    ws = tmp_path / "ws.jsonl"
    import json as _j
    good = [_j.dumps({"_worksheet_header": True})]
    good += [_j.dumps({"memory_id": f"e{i}", "verdict_mechanical": "missing-unexplained",
                       "label": "EPHEMERAL"}) for i in range(9)]
    good += ['{"memory_id": "bad", "label": "EPHEMERAL", "label_reason": "he said "x""}']
    ws.write_text("\n".join(good), encoding="utf-8")
    monkeypatch.setattr(sp, "WORKSHEET", ws)
    assert sp.summarise_worksheet() == 3
    assert "unparseable" in capsys.readouterr().err


def test_mistyped_label_refuses_a_verdict(tmp_path, monkeypatch, capsys):
    """A typo is silently excluded from the ratio that decides build-vs-kill."""
    ws = tmp_path / "ws.jsonl"
    import json as _j
    rows = [_j.dumps({"_worksheet_header": True})]
    rows += [_j.dumps({"memory_id": f"e{i}", "verdict_mechanical": "missing-unexplained",
                       "label": "EPHEMRAL"}) for i in range(4)]
    rows += [_j.dumps({"memory_id": f"s{i}", "verdict_mechanical": "missing-unexplained",
                       "label": "STALE"}) for i in range(7)]
    ws.write_text("\n".join(rows), encoding="utf-8")
    monkeypatch.setattr(sp, "WORKSHEET", ws)
    assert sp.summarise_worksheet() == 3
    assert "unrecognised label" in capsys.readouterr().err


def test_worksheet_refuses_to_destroy_existing_labels(tmp_path, monkeypatch, capsys):
    """The hand-labels are the one artifact here that cannot be regenerated."""
    ws = tmp_path / "ws.jsonl"
    import json as _j
    ws.write_text(_j.dumps({"_worksheet_header": True}) + "\n"
                  + _j.dumps({"memory_id": "m1", "label": "STALE"}) + "\n",
                  encoding="utf-8")
    monkeypatch.setattr(sp, "WORKSHEET", ws)
    sp._emit_worksheet([{"memory_id": "new"}], [],
                       {"ts": "t", "seed": 42, "runtime": "Windows"})
    assert "REFUSING to overwrite" in capsys.readouterr().err
    assert "STALE" in ws.read_text(encoding="utf-8")      # labels survived


def test_aggregation_keeps_undecidables_out_of_the_denominator(monkeypatch, tmp_path):
    """The previous guard was a tautology (a literal tuple-membership check) and three
    separate mutations of the aggregation passed it. This drives run_audit for real."""
    pts = [
        {"id": "a", "payload": {"data": r"Report at D:\gone\a.md covers phase two.",
                                "tier": "evidence"}},
        {"id": "b", "payload": {"data": r"Spec at D:\gone\b.md covers phase three.",
                                "tier": "evidence"}},
        {"id": "c", "payload": {"data": r"Notes live at Q:\unreachable\c.md for now.",
                                "tier": "evidence"}},
    ]
    monkeypatch.setattr(sp, "scroll_all", lambda limit: pts)
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    # D: root reachable, the FILES under it are not; Q: root unreachable.
    monkeypatch.setattr(sp.os.path, "exists",
                        lambda p: str(p).rstrip("/\\").upper() == "D:")
    monkeypatch.setattr(sp, "REPORT", tmp_path / "r.jsonl")

    class A:
        sample = 0
        seed = 42
        min_len = 10
        excerpt = 200
        worksheet = False
        json = True
        controls = 0
        force_worksheet = False
        worksheet_size = 60
        worksheet_all = True
    assert sp.run_audit(A()) == 0
    summary = json.loads((tmp_path / "r.jsonl").read_text(encoding="utf-8").strip())
    assert summary["stale"] == 2
    assert summary["undecidable_root_unavailable"] == 1
    assert summary["decidable"] == 2, "an unreachable root must not enter the denominator"
    assert summary["stale_rate_pct"] == 100.0


def test_blind_controls_actually_reach_the_worksheet(tmp_path, monkeypatch):
    """Regression: control_pool was populated and then DISCARDED - _emit_worksheet took a
    control_rows argument that no caller supplied, and no CLI flag existed. The worksheet
    held only rows the mechanism ACCUSED, so recall was unmeasurable while the feature
    looked shipped. The whole 57-test suite passed while this was dead code."""
    pts = [
        {"id": "stale1", "payload": {"data": r"Report at D:\gone\a.md covers phase two.",
                                     "tier": "evidence"}},
        {"id": "ctrl1", "payload": {"data": r"The model was deleted from D:\gone\b.md last week.",
                                    "tier": "evidence"}},
    ]
    monkeypatch.setattr(sp, "scroll_all", lambda limit: pts)
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp.os.path, "exists", lambda p: str(p).rstrip("/\\").upper() == "D:")
    monkeypatch.setattr(sp, "REPORT", tmp_path / "r.jsonl")
    ws = tmp_path / "ws.jsonl"
    monkeypatch.setattr(sp, "WORKSHEET", ws)

    class A:
        sample = 0
        seed = 42
        min_len = 10
        excerpt = 200
        worksheet = True
        json = True
        controls = 40
        force_worksheet = False
        worksheet_size = 60
        worksheet_all = True
    assert sp.run_audit(A()) == 0

    verdicts = []
    for line in ws.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if not r.get("_worksheet_header"):
            verdicts.append(r["verdict_mechanical"])
    assert "missing-unexplained" in verdicts, "the accused row must be present"
    assert "missing-recorded" in verdicts, \
        "a blind CONTROL row must reach the worksheet, or recall cannot be computed"


def test_controls_are_labelled_blind(tmp_path, monkeypatch):
    """Control rows must carry an empty `label` like every other row - a control the
    human can identify as a control is not a control."""
    rows = [{"memory_id": "a", "verdict_mechanical": "missing-unexplained"}]
    ctrls = [{"memory_id": "c", "verdict_mechanical": "missing-recorded"}]
    ws = tmp_path / "ws.jsonl"
    monkeypatch.setattr(sp, "WORKSHEET", ws)
    sp._emit_worksheet(rows, [], {"ts": "t", "seed": 42, "runtime": "Windows"},
                       control_rows=ctrls)
    for line in ws.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if not r.get("_worksheet_header"):
            assert r["label"] == "", "every row, control included, must start unlabelled"


def _fake_points(n_stale, n_recorded):
    pts = []
    for i in range(n_stale):
        pts.append({"id": f"s{i}", "payload": {
            "data": rf"Report number {i} is at D:\gone\r{i}.md for reference.",
            "tier": "evidence"}})
    for i in range(n_recorded):
        pts.append({"id": f"c{i}", "payload": {
            "data": rf"The file was deleted from D:\gone\c{i}.md last week.",
            "tier": "evidence"}})
    return pts


class _Args:
    sample = 0
    seed = 42
    min_len = 10
    excerpt = 200
    worksheet = True
    json = True
    controls = 40
    force_worksheet = True
    worksheet_size = 60
    worksheet_all = False


def _run_worksheet(monkeypatch, tmp_path, pts, **over):
    monkeypatch.setattr(sp, "scroll_all", lambda limit: pts)
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp.os.path, "exists", lambda p: str(p).rstrip("/\\").upper() == "D:")
    monkeypatch.setattr(sp, "REPORT", tmp_path / "r.jsonl")
    ws = tmp_path / "ws.jsonl"
    monkeypatch.setattr(sp, "WORKSHEET", ws)
    a = _Args()
    for k, v in over.items():
        setattr(a, k, v)
    assert sp.run_audit(a) == 0
    rows, header = [], None
    for line in ws.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("_worksheet_header"):
            header = r
        else:
            rows.append(r)
    return header, rows


def test_worksheet_is_a_labelling_task_not_a_dump(monkeypatch, tmp_path):
    """890 accused rows with controls diluted ~1:14 meant labelling a realistic few dozen
    would catch ~4 controls - too few to measure recall, the only reason controls exist."""
    header, rows = _run_worksheet(monkeypatch, tmp_path, _fake_points(400, 100))
    accused = [r for r in rows if r["verdict_mechanical"] == "missing-unexplained"]
    controls = [r for r in rows if r["verdict_mechanical"] in ("missing-recorded", "exists")]
    assert len(accused) == 60, "accused rows must be sampled down to a labellable size"
    assert len(controls) >= 20, "controls must not be diluted away by the accused rows"
    # the ratio a human actually labels is the ratio that was designed
    assert len(controls) / len(rows) > 0.2


def test_worksheet_header_states_it_is_a_sample(monkeypatch, tmp_path):
    """A labeller who cannot tell sample from population will mis-scale any rate."""
    header, _ = _run_worksheet(monkeypatch, tmp_path, _fake_points(400, 100))
    assert header["sampled"] is True
    assert header["population"]["stale"] == 400
    assert header["rows_accused"] == 60


def test_worksheet_all_emits_every_accused_row(monkeypatch, tmp_path):
    header, rows = _run_worksheet(monkeypatch, tmp_path, _fake_points(400, 100),
                                  worksheet_all=True)
    accused = [r for r in rows if r["verdict_mechanical"] == "missing-unexplained"]
    assert len(accused) == 400
    assert header["sampled"] is False


# --- shared-Drive alias (gate verdict 2026-08-19) ---------------------------------
# The single largest systematic error of the hand-label round: D:\My Drive\... is the
# OTHER box's letter for the same synced Drive that is G:\ here. The letter belongs to
# the machine, not the memory, so a shared-Drive path must be re-checked under every
# local root carrying the shared dir before any staleness verdict.

def test_shared_drive_alias_rescues_other_boxes_letter(monkeypatch):
    """D:\\My Drive\\... recorded on the other box must read EXISTS on the box where
    the same Drive is G: - this exact shape was mass-accused in the first run."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp, "_SHARED_ROOTS", ["g"])
    monkeypatch.setattr(sp, "_translate", lambda p: "D:/My Drive/Eco/spec.md")
    monkeypatch.setattr(sp.os.path, "exists", lambda p: p == "G:/My Drive/Eco/spec.md")
    verdict, resolved = sp.classify_path(r"D:\My Drive\Eco\spec.md",
                                         r"Spec at D:\My Drive\Eco\spec.md today.")
    assert verdict == "exists"
    assert resolved == "G:/My Drive/Eco/spec.md"


def test_shared_drive_alias_also_rescues_an_unreachable_root(monkeypatch):
    """When the recorded LETTER does not even exist here, the alias still decides:
    an alias hit is a decidable EXISTS, not a root-unavailable coverage gap."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp, "_SHARED_ROOTS", ["g"])
    monkeypatch.setattr(sp, "_translate", lambda p: None)
    monkeypatch.setattr(sp.os.path, "exists", lambda p: p == "G:/My Drive/Eco/spec.md")
    verdict, resolved = sp.classify_path(r"D:\My Drive\Eco\spec.md",
                                         r"Spec at D:\My Drive\Eco\spec.md today.")
    assert verdict == "exists"
    assert resolved == "G:/My Drive/Eco/spec.md"


def test_alias_accepts_the_mnt_form(monkeypatch):
    """/mnt/d/My Drive/... is the same claim in WSL clothing."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sp, "_SHARED_ROOTS", ["g"])
    monkeypatch.setattr(sp.os.path, "exists", lambda p: p == "/mnt/g/My Drive/Eco/spec.md")
    assert sp._alias_hit("/mnt/d/My Drive/Eco/spec.md") == "/mnt/g/My Drive/Eco/spec.md"


def test_alias_never_excuses_a_path_outside_the_shared_dir(monkeypatch):
    """D:\\Dev\\... is NOT on the shared Drive; a missing file there must stay stale
    even when shared roots exist locally. The alias is a normalisation, not a pardon."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp, "_SHARED_ROOTS", ["g"])
    monkeypatch.setattr(sp, "_translate", lambda p: p.replace("\\", "/"))
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    verdict, _ = sp.classify_path(r"D:\Dev\thing\report.md",
                                  r"The report is at D:\Dev\thing\report.md today.")
    assert verdict == "missing-unexplained"


def test_shared_path_missing_under_every_root_is_still_stale(monkeypatch):
    """A file deleted FROM the Drive is stale everywhere - the alias must not convert
    'missing under all roots' into anything but the normal missing-* path."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp, "_SHARED_ROOTS", ["g"])
    monkeypatch.setattr(sp, "_translate", lambda p: p.replace("\\", "/"))
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    verdict, _ = sp.classify_path(r"D:\My Drive\Eco\gone.md",
                                  r"Notes at D:\My Drive\Eco\gone.md cover phase two.")
    assert verdict == "missing-unexplained"


def test_alias_ignores_non_path_shapes():
    assert sp._alias_hit("http://qube:18791") is None
    assert sp._alias_hit("relative/My Drive/x") is None


def test_shared_root_detection_is_local_and_cached(monkeypatch):
    """Detection probes the bare root FIRST (the _translate guard - a disconnected
    mapped letter blocks on SMB), then <letter>:/<shared dir>; the result is cached
    for the run."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    probed = []

    def fake_exists(p):
        probed.append(p)
        return p in ("G:/", "D:/", "G:/My Drive", "D:/My Drive")
    monkeypatch.setattr(sp.os.path, "exists", fake_exists)
    assert sp._shared_drive_letters() == ["d", "g"]
    probed.clear()
    assert sp._shared_drive_letters() == ["d", "g"]
    assert probed == [], "second call must hit the cache, not the filesystem"


def test_shared_root_cache_returns_a_copy(monkeypatch):
    monkeypatch.setattr(sp, "_SHARED_ROOTS", ["g"])
    got = sp._shared_drive_letters()
    got.append("z")
    assert sp._shared_drive_letters() == ["g"], "caller mutation must not corrupt the cache"


def test_zero_shared_roots_makes_shared_claims_undecidable(monkeypatch):
    """The box has NO local shared root (Drive app not mounted yet, crashed, or absent):
    it cannot falsify any shared-Drive claim. 'Could not check' must never become
    'missing' - that would silently resurrect the mass false accusation wholesale."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp, "_SHARED_ROOTS", [])
    monkeypatch.setattr(sp, "_translate", lambda p: p.replace("\\", "/"))
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    verdict, resolved = sp.classify_path(r"D:\My Drive\Eco\spec.md",
                                         r"Spec at D:\My Drive\Eco\spec.md today.")
    # A DISTINCT verdict: the drive letters may all exist here, so the letter-based
    # coverage banner cannot see this cause - it needs its own counter to be visible.
    assert verdict == "shared-root-unavailable"
    assert resolved is None
    assert sp.PRECEDENCE.index("missing-unexplained") \
        < sp.PRECEDENCE.index("shared-root-unavailable") \
        < sp.PRECEDENCE.index("missing-recorded")
    # control: a NON-shared path on the same rootless box still decides normally
    verdict2, _ = sp.classify_path(r"D:\Dev\thing\report.md",
                                   r"Report at D:\Dev\thing\report.md today.")
    assert verdict2 == "missing-unexplained"


def test_shared_root_itself_is_a_shared_claim(monkeypatch):
    """'D:\\My Drive' (the root, no tail) must alias too - rejecting it re-invents the
    false accusation whenever the recorded letter exists locally without the Drive."""
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp, "_SHARED_ROOTS", ["g"])
    monkeypatch.setattr(sp.os.path, "exists", lambda p: p == "G:/My Drive")
    assert sp._alias_hit(r"D:\My Drive") == "G:/My Drive"
    assert sp._alias_hit(r"D:\My Drive\_") is None   # tail still has to exist


def test_pinned_letters_override_detection(monkeypatch):
    """MEM0_SHARED_DRIVE_LETTERS pins the roots (backup-mirror / second-account guard).
    Pinned letters are still VALIDATED against the filesystem - accepting them verbatim
    let a typo'd pin reinstate the mass false accusation (round-2 review, CRITICAL)."""
    monkeypatch.setenv("MEM0_SHARED_DRIVE_LETTERS", " G , h ")
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp.os.path, "exists",
                        lambda p: p in ("G:/", "H:/", "G:/My Drive", "H:/My Drive"))
    assert sp._shared_drive_letters() == ["g", "h"]
    # detection letters must NOT be scanned when a pin is set: only the pin was probed
    monkeypatch.setattr(sp, "_SHARED_ROOTS", sp._UNSET)
    monkeypatch.setattr(sp.os.path, "exists", lambda p: True)   # every letter would match
    assert sp._shared_drive_letters() == ["g", "h"], "pin must override detection"


def test_malformed_pin_is_refused_not_dropped(monkeypatch):
    """Silently dropping bad entries can collapse the pin to [] - every shared claim
    flips to undecidable with no warning. Refuse loudly instead."""
    monkeypatch.setenv("MEM0_SHARED_DRIVE_LETTERS", "gg, 7")
    with pytest.raises(SystemExit, match="malformed"):
        sp._shared_drive_letters()
    monkeypatch.setattr(sp, "_SHARED_ROOTS", sp._UNSET)
    monkeypatch.setenv("MEM0_SHARED_DRIVE_LETTERS", "g;h")   # semicolon slip
    with pytest.raises(SystemExit, match="malformed"):
        sp._shared_drive_letters()


def test_unresolvable_pin_is_refused(monkeypatch):
    """A pin naming a letter that does not carry the shared dir here (typo, or the
    Drive moved letters) would make the alias miss everywhere while the zero-roots
    guard stays off - the false-accusation class, reinstated through the guard knob."""
    monkeypatch.setenv("MEM0_SHARED_DRIVE_LETTERS", "z")
    monkeypatch.setattr(sp.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sp.os.path, "exists", lambda p: p in ("G:/", "G:/My Drive"))
    with pytest.raises(SystemExit, match="do not carry"):
        sp._shared_drive_letters()


@pytest.mark.parametrize("raw,expected", [
    (None, "My Drive"),
    ("", "My Drive"),
    ("My Drive/", "My Drive"),
    ("My Drive\\", "My Drive"),
    ("  My Drive  ", "My Drive"),
])
def test_shared_dir_env_is_normalised(raw, expected):
    """A trailing separator passes root DETECTION but makes the prefix match
    unsatisfiable - the alias would die silently while the run looks healthy."""
    assert sp._normalize_shared_dir(raw) == expected


def test_shared_dir_env_normalising_to_empty_is_refused():
    with pytest.raises(SystemExit, match="normalises to empty"):
        sp._normalize_shared_dir("/")


def test_mnt_form_requires_a_drive_letter():
    assert sp._shared_rest("/mnt/1/My Drive/x") is None
    assert sp._shared_rest("/mnt/d/My Drive/x") == "My Drive/x"
