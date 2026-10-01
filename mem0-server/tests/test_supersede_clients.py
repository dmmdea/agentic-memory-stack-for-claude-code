"""The supersede door's clients (1.32.4): the MCP shim tools, the outbox replay, the readers that must
not feed superseded facts into MEMORY.md or the dream, and the docs that must agree with the code.

POST /v1/memories/{id}/supersede is the only writer of superseded_by (mem0-server/supersession.py).
Everything here is headless: the shim and replay-ops modules load against an isolated home with a
throwaway key file, and every HTTP call is a recording fake, so nothing reaches a live service.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SCRIPTS = REPO_ROOT / "scripts" / "wsl"
sys.path.insert(0, str(HERE.parent))

from _home_isolation import apply_home  # noqa: E402

LOSER = "11111111-1111-4111-8111-111111111111"
WINNER = "22222222-2222-4222-8222-222222222222"


def _load(name: str, path: Path, monkeypatch, tmp_path: Path):
    """Import a hyphenated script under an isolated home that holds a throwaway api key."""
    monkeypatch.setenv("MEM0_URL", "http://authority.invalid:18791")
    apply_home(monkeypatch, tmp_path)
    (tmp_path / ".mem0").mkdir(exist_ok=True)
    (tmp_path / ".mem0" / "api-key").write_text("test-key", encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"{path.name} is not importable here: {e}")
    return mod


@pytest.fixture()
def shim(monkeypatch, tmp_path):
    return _load("shim_supersede_ut", SCRIPTS / "mem0-mcp-shim.py", monkeypatch, tmp_path)


@pytest.fixture()
def ro(monkeypatch, tmp_path):
    return _load("replay_ops_supersede_ut", SCRIPTS / "replay-ops.py", monkeypatch, tmp_path)


def _tool(shim, name):
    t = getattr(shim, name)
    return getattr(t, "fn", t)       # fastmcp 3.x wraps @mcp.tool functions in a FunctionTool


def _answer(monkeypatch, shim, status=200, body=None, raises=None):
    """Replace httpx.request with a recorder that answers `status`/`body` (or raises `raises`)."""
    seen = []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        seen.append({"method": method, "url": url, "json": json, "params": params})
        if raises:
            raise raises
        return httpx.Response(status, json={"ok": True} if body is None else body,
                              request=httpx.Request(method, url))
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    return seen


def _outbox(shim):
    if not shim.OUTBOX.exists():
        return []
    return [json.loads(ln) for ln in shim.OUTBOX.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---- memory_supersede -----------------------------------------------------------------------------

def test_memory_supersede_posts_the_endpoint_body(shim, monkeypatch):
    seen = _answer(monkeypatch, shim, body={"ok": True, "noop": False, "hidden": False})
    out = _tool(shim, "memory_supersede")(LOSER, WINNER, scope="partial",
                                          detail="the port figure only", reason="renumbered")
    assert out == {"ok": True, "noop": False, "hidden": False}
    assert seen[0]["method"] == "POST"
    assert seen[0]["url"].endswith(f"/v1/memories/{LOSER}/supersede")
    assert seen[0]["json"] == {"winner_id": WINNER, "scope": "partial",
                               "detail": "the port figure only", "reason": "renumbered",
                               "source": "memory_supersede"}


def test_memory_supersede_defaults_to_a_full_supersession(shim, monkeypatch):
    seen = _answer(monkeypatch, shim)
    _tool(shim, "memory_supersede")(LOSER, WINNER)
    assert seen[0]["json"] == {"winner_id": WINNER, "scope": "full", "detail": None,
                               "reason": None, "source": "memory_supersede"}


@pytest.mark.parametrize("failure", [
    {"raises": httpx.ConnectError("refused")},
    {"status": 503, "body": {"detail": "upstream embedder rate-limited"}},
])
def test_memory_supersede_queues_when_the_authority_is_unusable(shim, monkeypatch, failure):
    _answer(monkeypatch, shim, **failure)
    out = _tool(shim, "memory_supersede")(LOSER, WINNER, scope="partial", detail="d", reason="r")
    assert out["queued"] is True and out["op"] == "supersede" and out["event"] == "QUEUED_OFFLINE"
    (rec,) = _outbox(shim)
    assert rec["op"] == "supersede" and rec["key"] == out["key"]
    assert rec["args"] == {"memory_id": LOSER, "superseded_by": WINNER, "scope": "partial",
                           "detail": "d", "reason": "r"}


@pytest.mark.parametrize("status", [400, 403, 404, 409])
def test_a_refusal_from_the_door_propagates_and_is_never_queued(shim, monkeypatch, status):
    """The refusal matrix answers 4xx. Queueing one would replay a bad op forever; the caller has to
    read the code (loser-canonical, winner-superseded, ...) and act."""
    _answer(monkeypatch, shim, status=status, body={"detail": "loser-canonical: signed path only"})
    with pytest.raises(httpx.HTTPStatusError):
        _tool(shim, "memory_supersede")(LOSER, WINNER)
    assert _outbox(shim) == []


# ---- memory_unsupersede ---------------------------------------------------------------------------

def test_memory_unsupersede_deletes_with_scope_and_reason_as_query_params(shim, monkeypatch):
    seen = _answer(monkeypatch, shim, body={"ok": True, "noop": False})
    out = _tool(shim, "memory_unsupersede")(LOSER, scope="all", reason="wrong winner")
    assert out == {"ok": True, "noop": False}
    assert seen[0]["method"] == "DELETE"
    assert seen[0]["url"].endswith(f"/v1/memories/{LOSER}/supersede")
    assert seen[0]["params"] == {"scope": "all", "reason": "wrong winner"}
    assert seen[0]["json"] is None


def test_memory_unsupersede_omits_an_empty_reason(shim, monkeypatch):
    seen = _answer(monkeypatch, shim)
    _tool(shim, "memory_unsupersede")(LOSER)
    assert seen[0]["params"] == {"scope": "full"}


def test_memory_unsupersede_queues_offline_and_propagates_a_refusal(shim, monkeypatch):
    _answer(monkeypatch, shim, raises=httpx.ConnectError("refused"))
    out = _tool(shim, "memory_unsupersede")(LOSER, scope="partial", reason="r")
    assert out["queued"] is True and out["op"] == "unsupersede"
    (rec,) = _outbox(shim)
    assert rec["op"] == "unsupersede"
    assert rec["args"] == {"memory_id": LOSER, "scope": "partial", "reason": "r"}
    _answer(monkeypatch, shim, status=403, body={"detail": "loser-canonical"})
    with pytest.raises(httpx.HTTPStatusError):
        _tool(shim, "memory_unsupersede")(LOSER)
    assert len(_outbox(shim)) == 1, "a refusal queues nothing"


# ---- the caller reads the server's reason, not just the status line -------------------------------

_CODES = {
    409: "winner-superseded: point at the newest record in the chain (memory_get_by_id shows it)",
    403: "cross-brand: a record of one brand cannot be hidden behind another brand's winner",
    404: "not-found: no record has that id",
}


def _call(shim, tool, **kw):
    if tool == "memory_supersede":
        return _tool(shim, tool)(LOSER, WINNER, **kw)
    return _tool(shim, tool)(LOSER, **kw)


@pytest.mark.parametrize("tool", ["memory_supersede", "memory_unsupersede"])
@pytest.mark.parametrize("status", sorted(_CODES))
def test_a_refusal_carries_the_status_and_the_servers_reason_to_the_caller(shim, monkeypatch, tool, status):
    """raise_for_status alone says "Client error '409 Conflict' for url ...": a session could not tell
    loser-canonical from winner-superseded from cross-brand, so it could not act on the refusal."""
    _answer(monkeypatch, shim, status=status, body={"detail": _CODES[status]})
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _call(shim, tool)
    msg = str(ei.value)
    assert str(status) in msg and _CODES[status] in msg
    assert ei.value.response.status_code == status, "callers can still branch on the status"
    assert _outbox(shim) == [], "a refusal is never queued"


@pytest.mark.parametrize("tool", ["memory_supersede", "memory_unsupersede"])
def test_a_long_refusal_detail_is_capped_and_a_non_text_detail_is_rendered(shim, monkeypatch, tool):
    _answer(monkeypatch, shim, status=409, body={"detail": "winner-superseded: " + "x" * 2000})
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _call(shim, tool)
    assert "winner-superseded: " in str(ei.value) and len(str(ei.value)) < 450
    # a request the server's own validation rejects answers a list of error objects, not a string
    _answer(monkeypatch, shim, status=422, body={"detail": [{"loc": ["body", "scope"],
                                                             "msg": "unexpected scope"}]})
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _call(shim, tool, scope="bogus")
    assert "422" in str(ei.value) and "unexpected scope" in str(ei.value)


@pytest.mark.parametrize("tool", ["memory_supersede", "memory_unsupersede"])
def test_a_refusal_without_a_json_body_still_names_the_status(shim, monkeypatch, tool):
    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        return httpx.Response(400, text="the proxy rejected the request body",
                              request=httpx.Request(method, url))
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _call(shim, tool)
    assert "400" in str(ei.value) and "the proxy rejected the request body" in str(ei.value)


@pytest.mark.parametrize("tool", ["memory_supersede", "memory_unsupersede"])
def test_a_server_error_other_than_503_keeps_the_plain_status_error_and_is_not_queued(shim, monkeypatch, tool):
    """Only a 4xx is a refusal with a reason to relay; a 500 is not, and it must not queue either."""
    _answer(monkeypatch, shim, status=500, body={"detail": "boom"})
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _call(shim, tool)
    assert "Server error '500" in str(ei.value)
    assert _outbox(shim) == []


def test_the_other_mutating_tools_keep_the_plain_status_error(shim, monkeypatch):
    """The reason relay is scoped to the supersede door: memory_delete and memory_promote behave as before."""
    _answer(monkeypatch, shim, status=409, body={"detail": "loser-canonical: nope"})
    for call in (lambda: _tool(shim, "memory_delete")(LOSER),
                 lambda: _tool(shim, "memory_promote")(LOSER)):
        with pytest.raises(httpx.HTTPStatusError) as ei:
            call()
        assert "Client error '409" in str(ei.value) and "loser-canonical" not in str(ei.value)


# ---- memory_update: text only, and the server's marker note reaches the caller --------------------

def test_memory_update_passes_the_servers_supersede_note_through_unchanged(shim, monkeypatch):
    answer = {"ok": True, "supersede_note": "This text carries a 'SUPERSEDED ... by mem0 <id>' marker",
              "supersede_marker": {"kind": "full", "winner_id": WINNER}}
    seen = _answer(monkeypatch, shim, body=answer)
    out = _tool(shim, "memory_update")(LOSER, f"SUPERSEDED 2026-09-30 by mem0 {WINNER}: old")
    assert out == answer
    assert seen[0]["method"] == "PUT" and seen[0]["json"] == {
        "text": f"SUPERSEDED 2026-09-30 by mem0 {WINNER}: old"}, "memory_update sends the text, nothing else"


# ---- search / recall: a partial supersession never hides, but the caller is told ------------------

_PARTIAL = [{"winner_id": WINNER, "detail": "the old port number", "at": "2026-09-30T00:00:00+00:00"}]


def test_memory_search_notes_results_that_carry_a_partial_supersession(shim, monkeypatch):
    _answer(monkeypatch, shim, body={"results": [
        {"memory": "a", "metadata": {"partially_superseded_by": _PARTIAL}},
        {"memory": "b", "partially_superseded_by": _PARTIAL},          # top-level, as recall reads created_at
        {"memory": "c", "metadata": {}},
    ], "rejected_superseded": 2, "rejected_contradicted": 0})
    out = _tool(shim, "memory_search")("ports")
    assert "2 result" in out["partial_supersession_note"]
    assert "partially_superseded_by" in out["partial_supersession_note"]
    assert "withheld_note" in out and "2 superseded" in out["withheld_note"], "the two notes sit side by side"


def test_memory_search_has_no_partial_note_without_a_partial_supersession(shim, monkeypatch):
    _answer(monkeypatch, shim, body={"results": [{"memory": "a", "metadata": {"tier": "stable"}},
                                                 {"memory": "b", "metadata": {"partially_superseded_by": []}}]})
    assert "partial_supersession_note" not in _tool(shim, "memory_search")("anything")
    _answer(monkeypatch, shim, body={})
    assert "partial_supersession_note" not in _tool(shim, "memory_search")("anything")


def test_memory_recall_notes_bundle_memories_that_carry_a_partial_supersession(shim, monkeypatch):
    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        if url.endswith("/bundle"):
            body = {"memories": [{"memory": "a", "metadata": {"partially_superseded_by": _PARTIAL}},
                                 {"memory": "b", "metadata": {}}],
                    "goals": [], "open_questions": [], "rejected_superseded": 1}
        else:
            body = {"results": []}
        return httpx.Response(200, json=body, request=httpx.Request(method, url))
    monkeypatch.setattr(shim.httpx, "request", fake_request)
    out = _tool(shim, "memory_recall")("ports")
    assert "1 result" in out["partial_supersession_note"]
    assert "withheld_note" in out
    monkeypatch.setattr(shim.httpx, "request", lambda method, url, **kw: httpx.Response(
        200, json={"memories": [{"memory": "b", "metadata": {}}], "goals": [], "open_questions": [],
                   "results": []}, request=httpx.Request(method, url)))
    assert "partial_supersession_note" not in _tool(shim, "memory_recall")("ports")


# ---- the docstrings are the one instruction channel that ships with the deploy --------------------

def _doc(shim, name):
    return " ".join((_tool(shim, name).__doc__ or "").split())


def test_memory_search_lists_the_history_query_class(shim):
    assert "'history'" in _doc(shim, "memory_search")


def test_memory_update_says_text_only_and_never_to_append_a_marker(shim):
    doc = _doc(shim, "memory_update")
    assert "memory_supersede" in doc and "SUPERSEDED" in doc and "text" in doc.lower()


def test_memory_supersede_docstring_teaches_the_full_and_partial_cases_the_refusals_and_the_undo(shim):
    doc = _doc(shim, "memory_supersede")
    for needle in ("scope='full'", "scope='partial'", "detail", "canonical", "insight",
                   "memory_unsupersede", "history"):
        assert needle in doc, f"memory_supersede's docstring must mention {needle!r}"
    undo = _doc(shim, "memory_unsupersede")
    assert "history" in undo and "full" in undo and "partial" in undo


# ---- replay-ops: the outbox replays both ops ------------------------------------------------------

class _Replay:
    """Records the httpx calls replay-ops makes and answers each with `status`."""

    def __init__(self, ro, monkeypatch, status=200):
        self.calls = []
        self.status = status

        def respond(method, url, **kw):
            self.calls.append({"method": method, "url": url, **kw})
            return httpx.Response(self.status, json={"ok": True}, request=httpx.Request(method, url))
        monkeypatch.setattr(ro.httpx, "post", lambda url, **kw: respond("POST", url, **kw))
        monkeypatch.setattr(ro.httpx, "delete", lambda url, **kw: respond("DELETE", url, **kw))
        monkeypatch.setattr(ro, "_authority_reachable", lambda url: True)


def test_replay_dispatches_a_supersede_to_the_endpoint(ro, monkeypatch):
    rec = _Replay(ro, monkeypatch)
    ro.AUTHORITY = "http://authority.invalid:18791"
    ro.dispatch("supersede", {"memory_id": LOSER, "superseded_by": WINNER, "scope": "partial",
                              "detail": "the old figure", "reason": "r"})
    (call,) = rec.calls
    assert call["method"] == "POST"
    assert call["url"] == f"http://authority.invalid:18791/v1/memories/{LOSER}/supersede"
    assert call["json"] == {"winner_id": WINNER, "scope": "partial", "detail": "the old figure",
                            "reason": "r", "source": "memory_supersede"}


def test_replay_dispatches_an_unsupersede_with_query_params(ro, monkeypatch):
    rec = _Replay(ro, monkeypatch)
    ro.AUTHORITY = "http://authority.invalid:18791"
    ro.dispatch("unsupersede", {"memory_id": LOSER, "scope": "all", "reason": "wrong winner"})
    (call,) = rec.calls
    assert call["method"] == "DELETE"
    assert call["url"] == f"http://authority.invalid:18791/v1/memories/{LOSER}/supersede"
    assert call["params"] == {"scope": "all", "reason": "wrong winner"}


def _queue(tmp_path, *ops, prefix="k"):
    """An outbox holding `ops`; the replayed-key ledger dedups by key, so two queues in one test
    need different prefixes."""
    ob = tmp_path / "outbox.jsonl"
    ob.write_text("\n".join(json.dumps({"op": op, "args": args, "key": f"{prefix}{i}"})
                            for i, (op, args) in enumerate(ops)) + "\n", encoding="utf-8")
    return ob


_SUP = ("supersede", {"memory_id": LOSER, "superseded_by": WINNER, "scope": "full",
                      "detail": None, "reason": None})


def test_replay_a_successful_supersede_is_replayed_and_a_permanent_4xx_goes_to_conflicts(
        ro, monkeypatch, tmp_path):
    rec = _Replay(ro, monkeypatch, status=200)
    stats = ro.replay(_queue(tmp_path, _SUP), "http://authority.invalid", "k")
    assert stats["replayed"] == 1 and stats["conflicts"] == 0 and len(rec.calls) == 1

    rec = _Replay(ro, monkeypatch, status=409)
    stats = ro.replay(_queue(tmp_path, _SUP, ("unsupersede", {"memory_id": LOSER}), prefix="b"),
                      "http://authority.invalid", "k")
    assert stats["conflicts"] == 2 and stats["kept"] == 0 and stats["replayed"] == 0
    confs = [json.loads(ln) for ln in (tmp_path / "mutation-conflicts.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [c["op"] for c in confs] == ["supersede", "unsupersede"] and all(c["status"] == 409 for c in confs)
    assert not (tmp_path / "outbox.replaying.jsonl").exists()


def test_replay_keeps_a_supersede_that_met_a_503_and_everything_behind_it(ro, monkeypatch, tmp_path):
    rec = _Replay(ro, monkeypatch, status=503)
    ob = _queue(tmp_path, _SUP, ("unsupersede", {"memory_id": LOSER}))
    stats = ro.replay(ob, "http://authority.invalid", "k")
    assert stats["replayed"] == 0 and stats["conflicts"] == 0 and stats["kept"] == 2
    assert stats["stopped_retryable"]["op"] == "supersede"
    kept = [json.loads(ln)["op"] for ln in (tmp_path / "outbox.replaying.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert kept == ["supersede", "unsupersede"], "503 is retryable: nothing is dropped, order is kept"
    assert not (tmp_path / "mutation-conflicts.jsonl").exists()
    assert len(rec.calls) == 1, "the drain stops at the first 503"


def test_replay_a_supersede_missing_its_ids_is_a_deterministic_conflict(ro, monkeypatch, tmp_path):
    _Replay(ro, monkeypatch)
    stats = ro.replay(_queue(tmp_path, ("supersede", {"memory_id": LOSER})), "http://authority.invalid", "k")
    assert stats["conflicts"] == 1 and stats["kept"] == 0


# ---- readers: a superseded fact must not feed MEMORY.md or the nightly dream ------------------------
#
# The admission gate hides superseded_by on a search; these two jobs read the store directly, so they
# apply the same rule themselves. A PARTIAL supersession (partially_superseded_by) never hides: the
# record still stands apart from the one claim it names. The audits (l10, brand-backfill,
# brand-scope-audit) deliberately keep seeing everything.

def test_memory_index_build_leaves_superseded_records_out_and_keeps_partial_ones(monkeypatch, tmp_path):
    m = _load("memory_index_build_ut", SCRIPTS / "memory-index-build.py", monkeypatch, tmp_path)
    live = {"data": "the live stable fact", "tier": "stable", "source": "src"}
    points = [
        {"id": "a" * 8 + "-live", "payload": live},
        {"id": "b" * 8 + "-gone", "payload": {**live, "data": "the superseded stable fact",
                                              "superseded_by": WINNER}},
        {"id": "c" * 8 + "-part", "payload": {**live, "data": "the partially superseded fact",
                                              "partially_superseded_by": _PARTIAL}},
        {"id": "d" * 8 + "-temp", "payload": {**live, "data": "the superseded temporal fact",
                                              "tier": "temporal", "superseded_by": WINNER}},
    ]
    out = tmp_path / "MEMORY.md"
    monkeypatch.setattr(m, "scroll_all", lambda: points)
    monkeypatch.setattr(m, "OUT", out)
    m.main()
    text = out.read_text(encoding="utf-8")
    assert "the live stable fact" in text
    assert "the partially superseded fact" in text, "a partial supersession never hides a record"
    assert "the superseded stable fact" not in text and "the superseded temporal fact" not in text


def test_dream_gather_leaves_superseded_records_out_and_keeps_partial_ones(monkeypatch, tmp_path):
    m = _load("dream_consolidate_ut", SCRIPTS / "dream-consolidate.py", monkeypatch, tmp_path)

    def answer(request):
        body = {"result": {"points": [
            {"id": "live", "payload": {"data": "live", "user_id": "u", "created_at": "2026-09-11T06:00:00Z",
                                       "tier": "evidence"}},
            {"id": "gone", "payload": {"data": "stale", "user_id": "u", "created_at": "2026-09-11T05:00:00Z",
                                       "tier": "evidence", "superseded_by": WINNER}},
            {"id": "part", "payload": {"data": "partly stale", "user_id": "u",
                                       "created_at": "2026-09-11T04:00:00Z", "tier": "evidence",
                                       "partially_superseded_by": _PARTIAL}},
        ], "next_page_offset": None}}
        return httpx.Response(200, json=body)
    client = m.Mem0Client("http://x", "k", "u", http=httpx.Client(transport=httpx.MockTransport(answer)))
    assert [p["id"] for p in client.all_points()] == ["live", "part"]


# ---- the protocol snippet the installers append to CLAUDE.md ------------------------------------------

def _protocol() -> str:
    return (REPO_ROOT / "claude-config" / "claude-md-memory-protocol.md").read_text(encoding="utf-8")


def test_protocol_teaches_correcting_a_fact_through_the_door():
    text = _protocol()
    assert text.startswith("## Memory tier protocol (agentic-memory-stack)"), (
        "the installers append the snippet only when this exact heading is absent")
    i = text.index("**Correcting a fact.**")
    section = " ".join(text[i:].split())
    for needle in ("memory_get_by_id", "memory_add", "memory_supersede", 'scope="partial"', "detail=",
                   "memory_update", "memory_unsupersede", "canonical", "never append"):
        assert needle.lower() in section.lower(), f"the 'Correcting a fact' step must mention {needle!r}"
    assert "SUPERSEDED" in section, "it names the marker text sessions must not write"


def test_protocol_no_longer_claims_there_is_no_supersession_field():
    text = " ".join(_protocol().split())
    assert "`valid_from`/`valid_to`/`supersedes` schema" not in text
    assert "superseded_by" in text and "memory_supersede" in text


# ---- the docs agree with the code ---------------------------------------------------------------------

def _doc_text(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _refusal_codes() -> set:
    """Every code supersession.py can refuse with, read from its source so a new code cannot be added
    without the contract naming it. The two f-string codes expand over the protected tiers."""
    src = (HERE.parent / "supersession.py").read_text(encoding="utf-8")
    codes = set()
    for m in re.finditer(r'Refusal\(\s*\d+,\s*(f?)"([a-z{}\' -]+)"', src):
        if m.group(1):
            tiers = re.search(r"PROTECTED_TIERS = frozenset\(\{([^}]*)\}\)", src).group(1)
            codes |= {f"loser-{t.strip().strip(chr(34))}" for t in tiers.split(",")}
        else:
            codes.add(m.group(2))
    assert len(codes) >= 14, codes     # the parse found the matrix
    return codes


def test_api_contracts_names_every_refusal_code_of_the_door():
    doc = _doc_text("docs/api-contracts.md")
    missing = sorted(c for c in _refusal_codes() if f"`{c}`" not in doc)
    assert not missing, f"docs/api-contracts.md does not document the refusal code(s) {missing}"


def test_api_contracts_names_every_forbidden_metadata_key_and_the_hide_key_rule():
    from security_invariants import METADATA_FORBIDDEN_KEYS, RETRIEVAL_HIDE_KEYS
    doc = _doc_text("docs/api-contracts.md")
    start = doc.index("### `PATCH /v1/memories/{mid}/metadata`")
    section = doc[start:doc.index("### `POST /v1/memories/{mid}/supersede`")]
    missing = sorted(k for k in METADATA_FORBIDDEN_KEYS if f"`{k}`" not in section)
    assert not missing, f"the PATCH /metadata contract does not list the forbidden key(s) {missing}"
    assert "hide key" in section and all(f"`{k}`" in section for k in RETRIEVAL_HIDE_KEYS)


def test_api_contracts_documents_both_routes_and_both_tools_and_the_put_note():
    doc = _doc_text("docs/api-contracts.md")
    for heading in ("### `POST /v1/memories/{mid}/supersede`", "### `DELETE /v1/memories/{mid}/supersede`",
                    '### `memory_supersede(memory_id, superseded_by, scope="full", detail=None, reason=None)`',
                    '### `memory_unsupersede(memory_id, scope="full", reason=None)`'):
        assert heading in doc, f"docs/api-contracts.md has no section {heading!r}"
    assert "supersede_note" in doc and "partial_supersession_note" in doc
    assert "never changes what a search returns" in doc, "memory_update is text only"


def test_the_system_docs_describe_the_door_where_they_describe_the_writers():
    for rel, needles in {
        "docs/systems/mem0-api.md": ("/supersede", "memory_supersede", "memory_unsupersede", "supersession.py",
                                     "METADATA_FORBIDDEN_KEYS"),
        "docs/systems/reconciliation.md": ("--supersede-markers", "--apply-partial", "--only", "--unsupersede",
                                           "--resolve-supersede", "memory_supersede", "dangling",
                                           "refused:canonical"),
        "docs/systems/memory-model.md": ("memory_supersede", "memory_unsupersede"),
        "docs/systems/admission-gate.md": ("partially_superseded_by", "/supersede"),
        "docs/operations.md": ("--unsupersede", "--supersede-markers", "--resolve-supersede"),
        "docs/glossary.md": ("## Supersession",),
        "ARCHITECTURE.md": ("/supersede", "partially_superseded_by"),
    }.items():
        text = _doc_text(rel)
        missing = [n for n in needles if n not in text]
        assert not missing, f"{rel} does not mention {missing}"


def test_every_new_sweep_flag_is_in_the_reconciliation_runbook():
    src = (SCRIPTS / "contradiction-sweep.py").read_text(encoding="utf-8")
    doc = _doc_text("docs/systems/reconciliation.md")
    for flag in ("--resolve-supersede", "--unsupersede", "--supersede-markers", "--apply-partial", "--only"):
        assert f'"{flag}"' in src, f"{flag} is no longer a sweep flag: update this pin and the runbook"
        assert flag in doc, f"{flag} is not in docs/systems/reconciliation.md"


def test_operations_no_longer_sends_a_superseded_record_to_unstamp():
    ops = _doc_text("docs/operations.md")
    assert "**Superseded / contradicts-canonical** → that's reconciliation; `--unstamp` if wrong" not in ops
    line = next(ln for ln in ops.splitlines() if ln.startswith("- **Superseded"))
    assert "--unsupersede" in line and "--unstamp" in line, line
    assert "`SUPERSEDED" in ops and "--supersede-markers" in ops, "a known-issues row and the runbook line"


def test_the_offline_docs_list_supersede_and_unsupersede_among_the_queued_writes():
    for rel in ("docs/systems/offline-travel.md", "docs/flows/offline-outbox-replay.md"):
        line = next(ln for ln in _doc_text(rel).splitlines() if "This covers every mutating tool" in ln)
        assert "`supersede`" in line and "`unsupersede`" in line, f"{rel}: {line}"


def test_the_markers_runbook_states_the_narrow_full_rule_the_rows_and_the_orphan_report():
    """The runbook is what the operator reads before --apply, so its description of FULL has to be the
    parser's: a cue it names must make a marker partial, and 'not only' must not."""
    import supersession
    doc = " ".join(_doc_text("docs/systems/reconciliation.md").split())
    for needle in ("between `SUPERSEDED` and `by`", "scope cue", "`marker_text`", "`orphaned_supersessions`",
                   "inside a sentence"):
        assert needle in doc, f"docs/systems/reconciliation.md does not say {needle!r}"
    for cue in ("only", "in part", "figure", "except", "still holds", "the rest remains", "reverted"):
        assert f"`{cue}`" in doc, f"the runbook does not name the scope cue {cue!r}"
        marker = supersession.classify_text(f"SUPERSEDED 2026-09-30 by mem0 {WINNER}: the port, {cue} here")
        assert marker.kind == "partial", f"{cue!r} is named as a scope cue but the parser says {marker.kind}"
    full = supersession.classify_text(f"SUPERSEDED 2026-09-30 by mem0 {WINNER}: not only the port moved")
    assert full.kind == "full", "'not only' is not a scope cue"
