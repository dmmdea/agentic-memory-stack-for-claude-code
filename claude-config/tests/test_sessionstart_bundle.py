"""B1 tests: SessionStart durable/evidence bundle enrichment (sessionstart_bundle.py).

The SessionStart banner already surfaces canonical + open goals + recent episodes, but NOT the
ranked durable/evidence facts the per-prompt UserPromptSubmit hook injects once a prompt exists. B1
enriches the banner with a thin, brand+initiative-scoped, recency-pseudo-query-ranked, K<=1,
DISTILLED precis of those facts, reusing the live /v1/context/bundle pipeline (checkpoint:false,
tier:small). Frontier-grounded (scope-first/rank-second; precision-over-recall; distill-not-dump).

These unit-test the PURE logic (query construction, distillation, K cap, render). The bundle HTTP
call + sqlite read live in main() and are exercised by the live e2e, not here, except the last
section (WP-2 follow-up), which drives main() in-process against a fixture HOME and a fake authority
to pin WHERE the recency seed comes from on each role. Run:
  python -m pytest claude-config/tests/test_sessionstart_bundle.py -v
"""
import importlib.util
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_MOD = _HERE.parent / "sessionstart_bundle.py"
_spec = importlib.util.spec_from_file_location("sessionstart_bundle", _MOD)
ssb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssb)

SCRIPT = _HERE.parent / "storage-cap-check.sh"


def _mem(text, score=0.75):
    return {"id": "x", "memory": text, "score": score}


# --- build_boot_query: recency goal is the primary signal, scope is the fallback ---

def test_build_boot_query_uses_recent_goal():
    q = ssb.build_boot_query("Fix the influencer invite flow", "brand-a", "brand-a-platform")
    assert "influencer invite flow" in q


def test_build_boot_query_falls_back_to_scope_tokens():
    q = ssb.build_boot_query(None, "ai-ecosystem", "agentic-memory-stack")
    assert "ai-ecosystem" in q and "agentic-memory-stack" in q


def test_build_boot_query_empty_when_no_signal():
    assert ssb.build_boot_query(None, None, None) == ""
    assert ssb.build_boot_query("   ", "", "") == ""


# --- distill: a thin precis, never a dump (length tax) ---

def test_distill_truncates_long_text_to_limit():
    assert len(ssb.distill("A" * 200, limit=120)) <= 120


def test_distill_keeps_short_text():
    assert ssb.distill("short fact", limit=120) == "short fact"


# --- select_facts: K cap, blank-skip, distillation ---

def test_select_facts_caps_to_k():
    mems = [_mem("a"), _mem("b"), _mem("c")]
    assert len(ssb.select_facts(mems, k=1)) == 1


def test_select_facts_skips_blank_memory():
    mems = [_mem("   "), _mem("real fact")]
    facts = ssb.select_facts(mems, k=2)
    assert facts == ["real fact"]


def test_select_facts_distills_long_text():
    facts = ssb.select_facts([_mem("Z" * 300)], k=1)
    assert len(facts[0]) <= 120


def test_select_facts_empty_input():
    assert ssb.select_facts([], k=1) == []


# --- format_block: advisory header + bullets; silent when empty ---

def test_format_block_renders_header_and_bullets():
    out = ssb.format_block(["fact one"])
    assert ssb.HEADER in out
    assert "  - [recall] fact one" in out


def test_format_block_silent_on_empty():
    assert ssb.format_block([]) == ""


# --- header wording: advisory, never imperative (mirrors the Phase-2a frame rule) ---

def test_header_is_advisory_not_imperative():
    assert "verify" in ssb.HEADER.lower()
    upper = ssb.HEADER.upper().lstrip()
    for kw in ("MUST ", "NEVER ", "ALWAYS ", "DO NOT", "DON'T", "YOU MUST"):
        assert not upper.startswith(kw), f"header must be advisory, not imperative: {ssb.HEADER!r}"


# --- integration: the helper must actually be invoked by the hook script ---

def test_helper_invoked_by_script():
    # The HEADER is printed by THIS helper, not echoed by bash; the real wiring guard is that the
    # script invokes the helper. (Drift guard against the call being removed.)
    assert "sessionstart_bundle.py" in SCRIPT.read_text(encoding="utf-8"), (
        "storage-cap-check.sh does not invoke sessionstart_bundle.py — B1 enrichment not wired in."
    )


def _to_wsl_path(p: Path) -> str:
    s = p.as_posix()  # e.g. D:/src/...
    if len(s) > 1 and s[1] == ":":
        s = "/mnt/" + s[0].lower() + s[2:]
    return s


def test_bash_syntax_ok():
    # storage-cap-check.sh is a WSL script; validate it in WSL (the deploy runtime), not the
    # ambient `bash` (which may be Git Bash or WSL and mishandles Windows paths). Skip if no WSL.
    import shutil
    import subprocess
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        pytest.skip("wsl not available")
    r = subprocess.run([wsl, "-e", "bash", "-lc", f"bash -n '{_to_wsl_path(SCRIPT)}'"], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()


# --- I/O: brand-scoped recent goal + abstention (the recency pseudo-query source) ---

def test_recent_goal_for_brand_scopes_and_abstains(tmp_path):
    import sqlite3 as sq
    db = tmp_path / "ep.db"
    con = sq.connect(db)
    con.execute("CREATE TABLE episodes (session_id TEXT, goal_text TEXT, ended_at TEXT)")
    con.execute("CREATE TABLE sessions (session_id TEXT, brand TEXT)")
    con.executemany("INSERT INTO episodes VALUES (?,?,?)", [
        ("s1", "ai goal newest", "2026-06-28T03:00:00"),
        ("s2", "brand-a goal", "2026-06-28T02:00:00"),
    ])
    con.executemany("INSERT INTO sessions VALUES (?,?)", [("s1", "ai-ecosystem"), ("s2", "brand-a")])
    con.commit()
    con.close()
    assert ssb.recent_goal_for_brand(str(db), "ai-ecosystem") == "ai goal newest"
    assert ssb.recent_goal_for_brand(str(db), "brand-a") == "brand-a goal"
    # brand with no episode -> abstain, NOT a cross-brand fallback
    assert ssb.recent_goal_for_brand(str(db), "nonexistent") is None
    # brandless -> global most-recent
    assert ssb.recent_goal_for_brand(str(db), None) == "ai goal newest"
    # missing db -> None (fail-silent)
    assert ssb.recent_goal_for_brand(str(tmp_path / "nope.db"), "x") is None


# --- deploy guard: the installer MUST copy the helper or B1 no-ops in real installs ---

def test_installer_deploys_helper():
    inst = _HERE.parent.parent / "install" / "2-windows-config.ps1"
    assert "sessionstart_bundle.py" in inst.read_text(encoding="utf-8"), (
        "install/2-windows-config.ps1 must copy sessionstart_bundle.py beside storage-cap-check.sh "
        "or B1 silently no-ops in real installs."
    )


# --- Phase 2: marker-driven query selection (conversation query post-compaction vs recency) ---

def test_choose_query_prefers_marker_with_frontier_k2():
    q, tier, k = ssb.choose_query_and_params("conversation query", "recency goal")
    assert q == "conversation query" and tier == "frontier" and k == 2


def test_choose_query_falls_back_to_recency_small_k1():
    q, tier, k = ssb.choose_query_and_params(None, "recency goal")
    assert q == "recency goal" and tier == "small" and k == 1
    # empty marker string is treated as no marker
    q2, tier2, k2 = ssb.choose_query_and_params("   ", "recency goal")
    assert q2 == "recency goal" and tier2 == "small" and k2 == 1


def test_choose_query_empty_when_no_signal():
    q, _tier, _k = ssb.choose_query_and_params(None, "")
    assert not q


def test_load_and_consume_marker_fresh(tmp_path):
    m = tmp_path / "precompact-query.json"
    m.write_text(json.dumps({"query": "what was I doing", "ts": 1000, "session_id": "s"}), encoding="utf-8")
    got = ssb.load_and_consume_marker(str(m), now=1010, max_age=300)
    assert got == "what was I doing"
    assert not m.exists()  # consume-once


def test_load_and_consume_marker_stale_is_dropped(tmp_path):
    m = tmp_path / "precompact-query.json"
    m.write_text(json.dumps({"query": "old", "ts": 1000, "session_id": "s"}), encoding="utf-8")
    got = ssb.load_and_consume_marker(str(m), now=9999, max_age=300)
    assert got is None
    assert not m.exists()  # a stale marker is cleaned up, not left to linger


def test_load_and_consume_marker_missing(tmp_path):
    assert ssb.load_and_consume_marker(str(tmp_path / "nope.json"), now=10, max_age=300) is None


# --- WP-2: the enrichment bundle stamps the hook contract -------------------------------------------
# hook_contract.missing counts every /v1/context/bundle body without hook_contract_version; this helper
# was the one repo caller that sent none (session-12 audit, brain-hook-contract-missing-two-sources).

def test_bundle_payload_stamps_the_hook_contract():
    p = ssb.build_bundle_payload("resume the invite flow", "brand-a", "brand-a-platform", "frontier")
    assert p["hook_contract_version"] == "20.0"
    assert p["hook_contract_version"] == ssb.HOOK_CONTRACT_VERSION
    assert p["checkpoint"] is False and p["tier"] == "frontier" and p["prompt"] == "resume the invite flow"
    assert p["brand"] == "brand-a" and p["initiative"] == "brand-a-platform"


def test_bundle_payload_stamps_even_without_scope():
    p = ssb.build_bundle_payload("q", None, None)
    assert p["hook_contract_version"] == "20.0"
    assert "brand" not in p and "initiative" not in p


def test_fetch_bundle_sends_the_stamped_payload(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return b'{"memories": []}'

    def fake_urlopen(req, timeout=None):
        seen["body"] = __import__("json").loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(ssb.urllib.request, "urlopen", fake_urlopen)
    assert ssb.fetch_bundle("http://authority.invalid", "k", "q", "brand-a", None) == []
    assert seen["body"]["hook_contract_version"] == "20.0"


def test_stamped_version_is_one_the_server_knows():
    import re
    src = (_HERE.parent.parent / "mem0-server" / "hook_contract.py").read_text(encoding="utf-8")
    known = re.search(r"KNOWN_HOOK_CONTRACT_VERSIONS\s*=\s*\{([^}]*)\}", src).group(1)
    assert f'"{ssb.HOOK_CONTRACT_VERSION}"' in known


# --- WP-2 follow-up: the enrichment recency seed comes from the authority on a replica ---------------------
# The SessionStart enrichment query is seeded by "what was I last doing": the newest episode goal. On a
# REPLICA the local ~/.mem0/episodic.db is a dormant copy frozen at the authority cutover, so seeding from it
# ranked today's durable facts against a weeks-old goal. The role decides the source (an absent role file is
# the brain): the brain reads its own episodic.db; a replica or client asks the authority (GET /v1/episodes)
# and, when that fails, has NO seed -- it never falls back to the frozen copy.
#
# main() is driven in-process against a fixture HOME and a fake authority behind urllib.request.urlopen. The
# seed is observed as the `prompt` of the POST /v1/context/bundle body. Nothing here touches the real
# ~/.mem0 or a network.

_REAL_CONNECT = sqlite3.connect  # captured before any spy replaces it; fixture setup uses this one

FROZEN_GOAL = "FROZEN-LOCAL-GOAL cutover-day work"
FRESH_GOAL = "AUTHORITY-FRESH-GOAL invite flow work"
BRAIN_GOAL = "LOCAL-BRAIN-GOAL last session on this box"
SCOPE = ("--brand", "brand-a", "--initiative", "brand-a-platform")
SCOPE_QUERY = ssb.build_boot_query(None, "brand-a", "brand-a-platform")  # what "no seed" falls back to


def _episode(goal, ended_at, brand=None):
    return {"id": 1, "session_id": "s", "goal_text": goal, "ended_at": ended_at, "brand": brand}


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self._body


class _Authority:
    """The authority behind urllib.request.urlopen. GET /v1/episodes behaves like mem0-server's
    episodic.recent(): newest first (ended_at DESC), `recent=` caps the rows, `brand=` narrows to that brand
    (unless honour_brand is False: an authority that ignores the parameter). POST /v1/context/bundle
    answers with `memories`. Every request is recorded, including the ones that then fail."""

    def __init__(self, memories=()):
        self.episodes = []
        self.memories = list(memories)
        self.honour_brand = True
        self.episodes_fault = None  # an exception to raise, or raw bytes to serve as the response body
        self.bundle_fault = None
        self.calls = []

    def down(self):
        self.episodes_fault = self.bundle_fault = urllib.error.URLError("connection refused")

    def bundles(self):
        return [c for c in self.calls if c["path"] == "/v1/context/bundle"]

    def episode_gets(self):
        return [c for c in self.calls if c["path"] == "/v1/episodes"]

    def urlopen(self, req, timeout=None):
        parts = urllib.parse.urlsplit(req.full_url)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        self.calls.append({
            "method": req.get_method(), "path": parts.path, "params": params, "timeout": timeout,
            "headers": {k.lower(): v for k, v in req.header_items()},
            "body": json.loads(req.data.decode("utf-8")) if req.data else None,
        })
        if req.get_method() == "GET" and parts.path == "/v1/episodes":
            if isinstance(self.episodes_fault, BaseException):
                raise self.episodes_fault
            if self.episodes_fault is not None:
                return _FakeResp(self.episodes_fault)
            rows = sorted(self.episodes, key=lambda r: r.get("ended_at") or "", reverse=True)
            if self.honour_brand and params.get("brand"):
                rows = [r for r in rows if r.get("brand") == params["brand"]]
            return _FakeResp(json.dumps(rows[: int(params.get("recent", 10))]).encode("utf-8"))
        if req.get_method() == "POST" and parts.path == "/v1/context/bundle":
            if isinstance(self.bundle_fault, BaseException):
                raise self.bundle_fault
            return _FakeResp(json.dumps({"memories": self.memories}).encode("utf-8"))
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)


class _Box:
    """A fixture HOME with the files main() reads (api-key, authority-url), an optional role file and an
    optional local episodic.db. It is never the real ~/.mem0."""

    def __init__(self, home):
        self.mem0 = home / ".mem0"
        self.mem0.mkdir(parents=True)
        (self.mem0 / "api-key").write_text("test-key\n", encoding="utf-8")
        (self.mem0 / "authority-url").write_text("http://authority.invalid:18791\n", encoding="utf-8")

    def role(self, text):
        (self.mem0 / "role").write_text(text, encoding="utf-8")

    def local_episodes(self, rows):
        """rows: (goal_text, ended_at, brand) -- this box's own episodic.db."""
        con = _REAL_CONNECT(str(self.mem0 / "episodic.db"))
        con.execute("CREATE TABLE episodes (session_id TEXT, goal_text TEXT, ended_at TEXT)")
        con.execute("CREATE TABLE sessions (session_id TEXT, brand TEXT)")
        for i, (goal, ended_at, brand) in enumerate(rows):
            con.execute("INSERT INTO episodes VALUES (?,?,?)", (f"s{i}", goal, ended_at))
            con.execute("INSERT INTO sessions VALUES (?,?)", (f"s{i}", brand))
        con.commit()
        con.close()

    def frozen_replica(self, role="replica\n"):
        """A replica after the authority cutover: its own episodic.db still holds the last pre-cutover goals."""
        self.role(role)
        self.local_episodes([
            (FROZEN_GOAL, "2026-09-10T08:16:07Z", "brand-a"),
            (FROZEN_GOAL + " (brand-b)", "2026-09-11T08:16:07Z", "brand-b"),
        ])


@pytest.fixture
def box(tmp_path, monkeypatch):
    b = _Box(tmp_path / "home")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))  # os.path.expanduser on Windows
    monkeypatch.delenv("MEM0_URL", raising=False)
    assert os.path.expanduser("~") == str(tmp_path / "home"), "the fixture HOME must shadow the real ~/.mem0"
    return b


@pytest.fixture
def authority(monkeypatch):
    a = _Authority(memories=[{"memory": "a durable fact", "score": 0.9}])
    monkeypatch.setattr(ssb.urllib.request, "urlopen", a.urlopen)
    return a


@pytest.fixture
def local_db_opens(monkeypatch):
    """Every sqlite3.connect() the code under test makes. A replica must never open its frozen copy."""
    opened = []

    def spy(database, *a, **kw):
        opened.append(str(database))
        return _REAL_CONNECT(database, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    return opened


def _boot(capsys, *args):
    """Run the SessionStart helper in-process; returns (exit code, stdout)."""
    rc = ssb.main(list(args))
    return rc, capsys.readouterr().out


def _prompts(authority):
    return [c["body"]["prompt"] for c in authority.bundles()]


# --- resolve_role: read the role file the way the banner's shell half does (absent = brain) ---

@pytest.mark.parametrize("content, expected", [
    (None, "brain"),           # no file: a single-machine install is its own brain
    ("", "brain"),
    ("  \n", "brain"),
    ("brain\n", "brain"),
    ("replica\n", "replica"),
    ("  Client \r\n", "client"),
], ids=["absent", "empty", "blank", "brain", "replica", "client-padded"])
def test_resolve_role_reads_the_role_file(tmp_path, content, expected):
    (tmp_path / ".mem0").mkdir()
    if content is not None:
        (tmp_path / ".mem0" / "role").write_text(content, encoding="utf-8")
    assert ssb.resolve_role(str(tmp_path)) == expected


# --- pick_authority_goal: the authority-side twin of recent_goal_for_brand's rule ---

@pytest.mark.parametrize("rows, brand, expected", [
    ([], None, None),
    (None, None, None),
    ("not a list", None, None),
    ({"goal_text": "a dict is not a list of rows"}, None, None),
    ([_episode("newest", "2026-09-29T10:00:00Z", "a"), _episode("older", "2026-09-01T10:00:00Z", "a")], None, "newest"),
    # blank goals and junk rows are skipped, like the local TRIM(goal_text) <> '' filter
    ([_episode(None, "3"), _episode("   ", "2"), _episode("", "1"), {}, 7, "x", None, _episode("third", "0")],
     None, "third"),
    ([{"goal_text": 5}], None, None),
    # brand given: only that brand's episode counts, first (newest) match wins
    ([_episode("b goal", "2", "b"), _episode("a goal", "1", "a"), _episode("a older", "0", "a")], "a", "a goal"),
    # ...and a brand with no episode abstains: never another brand's goal, never a brandless one
    ([_episode("b goal", "2", "b"), _episode("no brand", "1", None), {"goal_text": "no key"}], "a", None),
], ids=["empty", "none", "string", "dict", "newest-first", "blank-and-junk-skipped", "non-string-goal",
        "brand-match", "brand-abstains"])
def test_pick_authority_goal(rows, brand, expected):
    assert ssb.pick_authority_goal(rows, brand) == expected


# --- replica: the seed is the AUTHORITY's newest goal, never the frozen local copy ---

@pytest.mark.parametrize("role", ["replica\n", "client\n"], ids=["replica", "client"])
def test_replica_seeds_from_the_authority_not_its_frozen_local_db(box, authority, local_db_opens, capsys, role):
    box.frozen_replica(role)
    authority.episodes = [
        _episode("an older authority goal", "2026-09-20T10:00:00Z", "brand-a"),
        _episode(FRESH_GOAL, "2026-09-29T10:00:00Z", "brand-a"),
    ]
    rc, out = _boot(capsys, *SCOPE)
    assert rc == 0
    assert _prompts(authority) == [FRESH_GOAL]
    assert authority.bundles()[0]["body"]["tier"] == "small"
    assert "a durable fact" in out
    assert local_db_opens == []  # the dormant copy is never even opened
    (get,) = authority.episode_gets()
    assert get["method"] == "GET" and get["headers"]["x-api-key"] == "test-key"
    assert get["timeout"] is not None and get["timeout"] <= 2.0  # a session start is never held up by it


def test_brandless_replica_seeds_from_the_authoritys_newest_goal_overall(box, authority, local_db_opens, capsys):
    box.frozen_replica()
    authority.episodes = [
        _episode("brand-b newest", "2026-09-29T11:00:00Z", "brand-b"),
        _episode(FRESH_GOAL, "2026-09-28T10:00:00Z", "brand-a"),
    ]
    _boot(capsys, "--initiative", "brand-a-platform")
    assert _prompts(authority) == ["brand-b newest"]
    (get,) = authority.episode_gets()
    assert get["params"] == {"recent": "20"}  # no brand given, so none is sent
    assert local_db_opens == []


# --- replica, authority unreachable: NO seed, and no fallback to the frozen local copy ---

def test_replica_with_the_authority_down_has_no_seed_and_reads_no_local_db(box, authority, local_db_opens, capsys):
    box.frozen_replica()
    authority.episodes = [_episode(FRESH_GOAL, "2026-09-29T10:00:00Z", "brand-a")]
    authority.down()
    rc, out = _boot(capsys, *SCOPE)
    assert rc == 0 and out == ""
    assert local_db_opens == []
    # the existing no-seed path: the scope tokens, never the frozen goal
    assert _prompts(authority) == [SCOPE_QUERY]
    assert all(FROZEN_GOAL not in json.dumps(c) for c in authority.calls)


def test_replica_with_the_authority_down_and_no_scope_sends_nothing(box, authority, local_db_opens, capsys):
    box.frozen_replica()
    authority.down()
    rc, out = _boot(capsys)
    assert rc == 0 and out == ""
    assert authority.bundles() == []  # no seed and no scope: the helper abstains before any bundle call
    assert local_db_opens == []


@pytest.mark.parametrize("fault", [
    urllib.error.URLError("connection refused"),
    TimeoutError("timed out"),
    urllib.error.HTTPError("http://authority.invalid:18791/v1/episodes", 500, "boom", {}, None),
    b"<html>not json</html>",
    b'{"detail": "an object, not a list of episodes"}',
    b'[1, "x", null, {"goal_text": "   "}, {"goal_text": null}]',
], ids=["connection-refused", "timeout", "http-500", "not-json", "json-object", "junk-rows"])
def test_replica_whose_episode_read_fails_has_no_seed_not_the_local_copy(box, authority, local_db_opens, capsys, fault):
    box.frozen_replica()
    authority.episodes_fault = fault  # the bundle endpoint itself still answers
    rc, out = _boot(capsys, *SCOPE)
    assert rc == 0
    assert _prompts(authority) == [SCOPE_QUERY]
    assert "a durable fact" in out  # degraded to the scope query, not silenced
    assert local_db_opens == []


# --- brain: keeps the local read, exactly as before ---

@pytest.mark.parametrize("role", [None, "brain\n", "\n"], ids=["no-role-file", "brain", "blank-role-file"])
def test_brain_seeds_from_its_own_local_db(box, authority, local_db_opens, capsys, role):
    if role is not None:
        box.role(role)
    box.local_episodes([(BRAIN_GOAL, "2026-09-28T09:00:00Z", "brand-a")])
    authority.episodes = [_episode(FRESH_GOAL, "2026-09-29T10:00:00Z", "brand-a")]  # must not be consulted
    rc, out = _boot(capsys, *SCOPE)
    assert rc == 0 and "a durable fact" in out
    assert _prompts(authority) == [BRAIN_GOAL]
    assert authority.episode_gets() == []
    assert len(local_db_opens) == 1 and local_db_opens[0].endswith("episodic.db")


def test_brain_without_a_local_goal_does_not_ask_the_authority(box, authority, local_db_opens, capsys):
    authority.episodes = [_episode(FRESH_GOAL, "2026-09-29T10:00:00Z", "brand-a")]
    _boot(capsys, *SCOPE)  # no role file (brain) and no episodic.db
    assert _prompts(authority) == [SCOPE_QUERY]
    assert authority.episode_gets() == []


# --- the brand filter is honoured on the authority path, with the local query's rule ---

def test_replica_seed_is_the_requested_brands_newest_goal(box, authority, local_db_opens, capsys):
    box.frozen_replica()
    authority.episodes = [
        _episode("brand-b is newest overall", "2026-09-29T12:00:00Z", "brand-b"),
        _episode(FRESH_GOAL, "2026-09-28T10:00:00Z", "brand-a"),
        _episode("brand-a older", "2026-09-27T10:00:00Z", "brand-a"),
    ]
    _boot(capsys, *SCOPE)
    assert _prompts(authority) == [FRESH_GOAL]
    assert authority.episode_gets()[0]["params"] == {"recent": "20", "brand": "brand-a"}


def test_replica_url_encodes_the_brand_so_it_cannot_inject_query_parameters(box, authority, capsys):
    box.frozen_replica()
    odd = "brand a&recent=1&x=y"  # a space, '&' and '=' must all arrive as ONE brand value
    authority.episodes = [_episode(FRESH_GOAL, "2026-09-28T10:00:00Z", odd)]
    _boot(capsys, "--brand", odd)
    assert authority.episode_gets()[0]["params"] == {"recent": "20", "brand": odd}
    assert _prompts(authority) == [FRESH_GOAL]


def test_replica_seed_honours_the_brand_even_if_the_authority_ignores_the_filter(box, authority, capsys):
    box.frozen_replica()
    authority.honour_brand = False  # an authority that returns every brand's episodes regardless
    authority.episodes = [
        _episode("brand-b is newest overall", "2026-09-29T12:00:00Z", "brand-b"),
        _episode(FRESH_GOAL, "2026-09-28T10:00:00Z", "brand-a"),
    ]
    _boot(capsys, *SCOPE)
    assert _prompts(authority) == [FRESH_GOAL]


@pytest.mark.parametrize("honour_brand", [True, False], ids=["server-filters", "server-ignores-filter"])
def test_replica_brand_without_an_episode_abstains_rather_than_borrow_another_brands_goal(
        box, authority, local_db_opens, capsys, honour_brand):
    box.frozen_replica()
    authority.honour_brand = honour_brand
    authority.episodes = [
        _episode("brand-b goal", "2026-09-29T12:00:00Z", "brand-b"),
        _episode("no brand at all", "2026-09-28T12:00:00Z", None),
    ]
    _boot(capsys, *SCOPE)
    assert _prompts(authority) == [SCOPE_QUERY]  # the no-seed path, not brand-b's goal
    assert local_db_opens == []


def test_replica_finds_a_quiet_brands_goal_beyond_the_newest_twenty_episodes(box, authority, capsys):
    box.frozen_replica()
    authority.episodes = [_episode(f"brand-b goal {i:02d}", f"2026-09-29T10:{i:02d}:00Z", "brand-b") for i in range(25)]
    authority.episodes.append(_episode(FRESH_GOAL, "2026-09-01T10:00:00Z", "brand-a"))
    _boot(capsys, *SCOPE)
    # only asking the authority for THIS brand reaches it; a global recent=20 window would not
    assert _prompts(authority) == [FRESH_GOAL]


# --- a fresh PreCompact marker outranks the recency seed, so the seed is not even fetched ---

def test_a_fresh_precompact_marker_wins_and_costs_no_authority_read(box, authority, local_db_opens, capsys):
    box.frozen_replica()
    marker = box.mem0 / "precompact-query.json"
    marker.write_text(json.dumps({"query": "resume the invite flow", "ts": int(time.time()), "session_id": "s"}),
                      encoding="utf-8")
    authority.episodes = [_episode(FRESH_GOAL, "2026-09-29T10:00:00Z", "brand-a")]
    _boot(capsys, *SCOPE)
    (bundle,) = authority.bundles()
    assert bundle["body"]["prompt"] == "resume the invite flow" and bundle["body"]["tier"] == "frontier"
    assert authority.episode_gets() == []  # the seed would be discarded, so no round-trip is spent on it
    assert local_db_opens == []
    assert not marker.exists()  # still consumed exactly once
