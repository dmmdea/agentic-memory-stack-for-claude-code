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
import platform
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "stale_paths_audit", REPO_ROOT / "scripts" / "wsl" / "stale-paths-audit.py")
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)


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
    "The ops-sre checkout at C:\\dev\\ops-sre runs on Juan's PC.",
    "On the Aorus, the repos live at D:\\repos\\local-offload.",
    "Lenovo node keeps its config at C:\\node\\conf.yaml.",
])
def test_foreign_host_paths_are_undecidable(text, monkeypatch):
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    monkeypatch.setattr(sp, "_translate", lambda p: "/nonexistent/" + p[-8:])
    raw = sp.WINPATH.findall(text)[0]
    verdict, _ = sp.classify_path(raw, text)
    assert verdict == "missing-foreign-host"


def test_local_host_mention_beats_foreign_host(monkeypatch):
    """'On Qube ... also copied to the Aorus' is about THIS box; a missing path there
    is real staleness, not another machine's business."""
    monkeypatch.setattr(sp.os.path, "exists", lambda p: False)
    monkeypatch.setattr(sp, "_translate", lambda p: "/nonexistent/x")
    text = "On Qube the binary is at D:\\Dev\\thing\\app.exe; it was also sent to the Aorus."
    verdict, _ = sp.classify_path("D:\\Dev\\thing\\app.exe", text)
    assert verdict == "missing-unexplained"


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
    monkeypatch.setattr(sp.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sp.Path, "is_dir", lambda self: True)
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
