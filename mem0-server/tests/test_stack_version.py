"""MEM-17 (2026-07-03): /health reports the stack semver.

/health used to expose only "2.0.4-v012" (the mem0 lib pin + an ancient phase
tag) — a runtime gave no signal of WHICH stack release it actually runs (the
v1.11.0 P0 shipped through exactly that blindness). app.py now resolves a
STACK_VERSION once at startup: MEM0_STACK_VERSION env > ./VERSION beside app.py
(deploy.sh stamps it) > ../VERSION (repo checkout) > "unknown", and surfaces it
as "stack" on /health and /health/deep.

Endpoint assertions call the app functions DIRECTLY (import app) — the live
:18791 server keeps running pre-remediation code until the orchestrator
deploys, so an HTTP assertion on the new field would be testing the wrong bytes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402  (heavy import; mem0 init runs once, shared across the suite)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---- pure resolution order (tmp dirs; no server state touched) ----

def test_env_override_wins(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    monkeypatch.setenv("MEM0_STACK_VERSION", "7.7.7-hotfix")
    assert app._resolve_stack_version(app_dir=tmp_path) == "7.7.7-hotfix"


def test_version_beside_app_wins_over_parent(tmp_path, monkeypatch):
    monkeypatch.delenv("MEM0_STACK_VERSION", raising=False)
    appdir = tmp_path / "mem0-server"
    appdir.mkdir()
    (appdir / "VERSION").write_text("2.0.0\n", encoding="utf-8")      # deploy.sh stamp
    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")    # repo root
    assert app._resolve_stack_version(app_dir=appdir) == "2.0.0"


def test_repo_parent_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("MEM0_STACK_VERSION", raising=False)
    appdir = tmp_path / "mem0-server"
    appdir.mkdir()
    (tmp_path / "VERSION").write_text("1.11.1\n", encoding="utf-8")   # repo checkout layout
    assert app._resolve_stack_version(app_dir=appdir) == "1.11.1"


def test_unknown_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv("MEM0_STACK_VERSION", raising=False)
    appdir = tmp_path / "empty"
    appdir.mkdir()
    assert app._resolve_stack_version(app_dir=appdir) == "unknown"


def test_empty_or_whitespace_version_file_falls_through(tmp_path, monkeypatch):
    """A truncated/blank VERSION stamp must not report an empty string."""
    monkeypatch.delenv("MEM0_STACK_VERSION", raising=False)
    appdir = tmp_path / "mem0-server"
    appdir.mkdir()
    (appdir / "VERSION").write_text("   \n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("1.11.1\n", encoding="utf-8")
    assert app._resolve_stack_version(app_dir=appdir) == "1.11.1"


# ---- wiring: the endpoints carry the startup-resolved value ----

def test_health_reports_stack():
    h = app.health()
    assert h["stack"] == app.STACK_VERSION
    # historical field untouched — dashboards pattern-match it
    assert h["version"] == "2.0.4-v012"


def test_stack_version_resolves_repo_version_in_checkout():
    """Running from the repo checkout (this suite), resolution #3 must find the
    repo VERSION — the test env sets no MEM0_STACK_VERSION and mem0-server/ has
    no committed VERSION copy (deploy.sh stamps that only in the app dir)."""
    expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert app.STACK_VERSION == expected
    assert app.STACK_VERSION != "unknown"


def test_health_deep_reports_stack():
    d = app.health_deep()
    assert d["stack"] == app.STACK_VERSION


# ---- deploy parity: the stamp step must ship with the app change ----

def test_deploy_sh_stamps_app_dir_version():
    """deploy.sh must copy the repo VERSION into the app dir — without the
    stamp a deployed runtime reports stack:"unknown" (resolution #2 is the
    production path; #3 only works from a repo checkout)."""
    text = (REPO_ROOT / "scripts" / "wsl" / "deploy.sh").read_text(encoding="utf-8")
    assert '"$REPO_ROOT/VERSION" "$APP_DIR/VERSION"' in text
