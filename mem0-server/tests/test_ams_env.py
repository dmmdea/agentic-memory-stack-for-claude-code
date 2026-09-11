"""ams_env: the one resolver every brain-side job uses (spec §4: no ~/.mem0/api-key on the
authority, the URL is the tailnet bind, Codex auth lives in the secrets dataset)."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "wsl"


def _load():
    spec = importlib.util.spec_from_file_location("ams_env", SCRIPTS / "ams_env.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for v in ("MEM0_URL", "MEM0_API_KEY_FILE", "MEM0_KEY", "MEM0_API_KEY", "CODEX_HOME",
              "MEM0_EVAL_ROOT", "MEM0_DEFAULT_USER_ID"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / ".mem0").mkdir()
    return tmp_path


def test_url_precedence(home, monkeypatch):
    m = _load()
    assert m.mem0_url() == "http://127.0.0.1:18791"
    (home / ".mem0" / "authority-url").write_text("\nhttp://192.0.2.9:18791\n", encoding="utf-8")
    assert m.mem0_url() == "http://192.0.2.9:18791"
    monkeypatch.setenv("MEM0_URL", "http://x:1/")
    assert m.mem0_url() == "http://x:1"


def test_api_key_prefers_credential_file(home, monkeypatch):
    m = _load()
    assert m.api_key() == ""
    (home / ".mem0" / "api-key").write_text("file-key\n", encoding="utf-8")
    assert m.api_key() == "file-key"
    monkeypatch.setenv("MEM0_KEY", "env-key")
    assert m.api_key() == "env-key"
    cred = home / "creds" / "ams-api-key"
    cred.parent.mkdir()
    cred.write_text("cred-key", encoding="utf-8")
    monkeypatch.setenv("MEM0_API_KEY_FILE", str(cred))
    assert m.api_key() == "cred-key"


def test_codex_home_and_eval_root_from_stack_env(home, monkeypatch):
    m = _load()
    assert m.codex_home() == str(home / ".codex")
    sec = home / "secrets"
    (sec / "codex").mkdir(parents=True)
    (sec / "codex" / "auth.json").write_text("{}", encoding="utf-8")
    (home / ".mem0" / "stack.env").write_text(
        f"MEM0_SECRETS_DIR={sec}\nMEM0_EVAL_ROOT={home}/eval\nMEM0_DEFAULT_USER_ID=tenant\n# c\n", encoding="utf-8")
    assert m.codex_home() == str(sec / "codex")
    assert m.eval_root() == f"{home}/eval" and m.user_id() == "tenant"
    monkeypatch.setenv("CODEX_HOME", "/elsewhere")
    assert m.codex_home() == "/elsewhere"


def test_throttle_and_usage_ledger(home):
    m = _load()
    assert m.throttle_ok("dream", 100) is True
    m.mark_throttle("dream")
    assert m.throttle_ok("dream", 100) is False
    assert m.throttle_ok("dream", 0) is True
    m.write_usage("dream-gather", tokens_used=12, duration_ms=34, model_requested="gpt-5.6-terra", outcome="ok")
    rows = [json.loads(ln) for ln in m.usage_log_path().read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["component"] == "dream-gather" and rows[-1]["tokens_used"] == 12 and rows[-1]["outcome"] == "ok"
    with pytest.raises(ValueError):
        m.write_usage("x", outcome="bogus")


CHAIN_JOBS = ["semantic-dedup.py", "memory-index-build.py", "decay-scan.py", "l10-audit.py",
              "contradiction-sweep.py", "brand-scope-audit.py"]


@pytest.mark.parametrize("name", CHAIN_JOBS)
def test_chain_jobs_resolve_url_and_key_through_ams_env(name):
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "import ams_env" in text, f"{name} must import ams_env"
    assert not re.search(r'^\s*(MEM0|MEM0_URL)\s*=\s*"http://127\.0\.0\.1:18791"', text, re.M), \
        f"{name} still hardcodes the loopback URL"
    assert '.mem0" / "api-key").read_text' not in text, f"{name} still reads ~/.mem0/api-key directly"


def test_canonize_reads_the_systemd_credential_first():
    sh = (SCRIPTS / "mem0-canonize.sh").read_text(encoding="utf-8")
    assert "CREDENTIALS_DIRECTORY" in sh and "ams-canonical-key" in sh
    assert "MEM0_API_KEY_FILE" in sh
    body = sh[sh.index("resolve_canon_key() {"):]          # the resolver, not the header comment
    i_cred = body.index("CREDENTIALS_DIRECTORY")
    i_xdg = body.index("XDG_RUNTIME_DIR")
    assert i_cred < i_xdg, "the credential path must be resolved before the tmpfs/plaintext paths"
