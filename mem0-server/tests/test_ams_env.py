"""ams_env: the one resolver every brain-side job uses (spec §4: no ~/.mem0/api-key on the
authority, the URL is the tailnet bind, Codex auth lives in the secrets dataset)."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "wsl"


from _home_isolation import apply_home  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("ams_env", SCRIPTS / "ams_env.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def home(tmp_path, monkeypatch):
    apply_home(monkeypatch, tmp_path)
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


# ---- WP-4 task 4.1: dense_vector() is the one extractor for a Qdrant point's dense vector ----

REAL_SHAPE = {"": [0.25] * 768, "bm25": {"indices": [3, 9, 27], "values": [0.5, 0.25, 0.125]}}


def test_dense_vector_real_named_vector_shape():
    """The live collection returns {"": [768 floats], "bm25": {"indices": [...], "values": [...]}}
    for with_vector=true. The dense leg is the unnamed key; the sparse dict must never be returned."""
    m = _load()
    vec = m.dense_vector({"vector": REAL_SHAPE})
    assert vec == [0.25] * 768
    assert len(vec) == 768


def test_dense_vector_bare_list_and_fallbacks():
    m = _load()
    assert m.dense_vector({"vector": [1.0, 2.0]}) == [1.0, 2.0]
    # the unnamed entry wins even when another list-valued entry comes first
    assert m.dense_vector({"vector": {"other": [9.0], "": [1.0, 2.0]}}) == [1.0, 2.0]
    # no "" key: first list-valued entry, skipping a sparse dict
    assert m.dense_vector({"vector": {"bm25": {"indices": [1], "values": [1.0]}, "dense": [4.0]}}) == [4.0]
    # sparse only / empty / missing / wrong types -> None (callers count it, never guess)
    assert m.dense_vector({"vector": {"bm25": {"indices": [1], "values": [1.0]}}}) is None
    assert m.dense_vector({"vector": {}}) is None
    assert m.dense_vector({}) is None
    assert m.dense_vector({"vector": "nope"}) is None
    assert m.dense_vector(None) is None


# ---- 1.32.4 WP-2: wait_for_embedder polls /health/embedder instead of one-shot-probing a cold seat ----
#
# The embedder unloads after 5 idle minutes and takes seconds to come back, so the first GET of a job
# often answers 503 (or refuses the connection) while that very request starts the load. A one-shot
# probe read that as 'embedder down' and skipped the whole run. The wait polls for a bounded window.

class _Ok:
    def raise_for_status(self):
        return None


def test_wait_for_embedder_true_after_k_cold_probes():
    import httpx
    m = _load()
    urls, sleeps, k = [], [], 3

    def get(url, timeout=None):
        urls.append(url)
        if len(urls) <= k:
            raise httpx.ConnectError("cold")
        return _Ok()

    assert m.wait_for_embedder("http://authority.invalid:18791", 120, 15, sleep=sleeps.append, get=get) is True
    assert urls == ["http://authority.invalid:18791/health/embedder"] * (k + 1)
    assert sleeps == [15] * k, "one step between probes, and no sleep after the one that answered"


def test_wait_for_embedder_treats_a_503_response_as_not_yet():
    """A 503 is a RESPONSE, not an exception: the cold seat answers it while it loads."""
    import httpx
    m = _load()
    answers = [503, 503, 200]
    sleeps = []

    def handler(request):
        return httpx.Response(answers.pop(0))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ok = m.wait_for_embedder("http://authority.invalid:18791", 60, 10, sleep=sleeps.append, get=client.get)
    assert ok is True and sleeps == [10, 10]


def test_wait_for_embedder_gives_up_after_the_window_and_the_probe_count_is_deterministic():
    import httpx
    m = _load()
    calls, sleeps = [], []

    def get(url, timeout=None):
        calls.append(url)
        raise httpx.ReadTimeout("still loading")

    assert m.wait_for_embedder("http://x:1", 60, 15, sleep=sleeps.append, get=get) is False
    assert len(calls) == 5 and sleeps == [15, 15, 15, 15], "60 s / 15 s: five probes, four waits"


def test_wait_for_embedder_with_no_window_probes_once_and_never_sleeps():
    import httpx
    m = _load()
    calls, sleeps = [], []

    def get(url, timeout=None):
        calls.append(url)
        raise httpx.ConnectError("down")

    assert m.wait_for_embedder("http://x:1", 0, 15, sleep=sleeps.append, get=get) is False
    assert len(calls) == 1 and sleeps == []
    assert m.wait_for_embedder("http://x:1", 30, 0, sleep=sleeps.append, get=get) is False, "a zero step cannot loop"
    assert len(calls) == 2 and sleeps == []


def test_wait_for_embedder_defaults_to_httpx_get_and_trims_the_trailing_slash(monkeypatch):
    import httpx
    m = _load()
    seen = []
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: seen.append((url, timeout)) or _Ok())
    assert m.wait_for_embedder("http://x:1/", 0, 15) is True
    assert seen == [("http://x:1/health/embedder", 30.0)]


def test_ams_env_stays_stdlib_only_at_import():
    """httpx is imported inside wait_for_embedder: ams_env is loaded by jobs that never poll."""
    import ast
    tree = ast.parse((SCRIPTS / "ams_env.py").read_text(encoding="utf-8"))
    top = {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    top |= {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
    assert "httpx" not in top
