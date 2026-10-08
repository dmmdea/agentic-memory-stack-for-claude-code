"""embedder-migrate.py — moving the store to another embedding space beside the old one.

Offline: Qdrant and the embedder are in-memory fakes, so these run with no stack. The load-bearing
claims:
  * a build copies ids (UUID strings AND integers), payloads and the sparse vectors verbatim and
    re-embeds only the dense vector, and never writes the source;
  * --catch-up re-embeds edited points and deletes points the source lost, bounded by --max-delete,
    and --dry-run lists what it would delete without writing;
  * no mode writes a collection the stack is using (the live-target guard) unless --force;
  * episodes are embedded from episodic.db's FULL summary, not the 800-character payload copy;
  * --verify passes on a faithful target and fails on a drifted one.
"""
import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "embedder-migrate.py"
SERVER = HERE.parents[2] / "mem0-server"


@pytest.fixture
def mig(tmp_path, monkeypatch):
    pytest.importorskip("mem0")  # the tool imports the prefix shim's truncation (egemma_embedder)
    for k in ("MEM0_EMBED_PROFILE", "MEM0_QDRANT_COLLECTION", "MEM0_COLLECTION", "MEM0_EPISODES_COLLECTION",
              "MEM0_EMBED_MODEL", "MEM0_EMBED_MODEL_EGEMMA2"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOMEDRIVE", home.drive)
    monkeypatch.setenv("HOMEPATH", str(home)[len(home.drive):])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    sys.path.insert(0, str(SERVER))
    spec = importlib.util.spec_from_file_location("embedder_migrate", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.IDENTITY_FILE = home / ".mem0" / "embed-identity.json"
    return m


class FakeQdrant:
    def __init__(self):
        self.cols = {}
        self.writes = []          # (collection, op) — proves which collections were written

    def add(self, name, points, sparse=True):
        params = {"vectors": {"size": 768, "distance": "Cosine", "on_disk": True}, "on_disk_payload": True}
        if sparse:
            params["sparse_vectors"] = {"bm25": {"modifier": "idf"}}
        self.cols[name] = {"info": {"config": {"params": params, "hnsw_config": {"m": 16},
                                               "optimizer_config": {"deleted_threshold": 0.02}},
                                    "payload_schema": {"user_id": {"data_type": "keyword"}}},
                           "points": {str(p["id"]): copy.deepcopy(p) for p in points}}

    def info(self, name):
        c = self.cols.get(name)
        return copy.deepcopy(c["info"]) if c else None

    def count(self, name):
        return len(self.cols[name]["points"])

    def scroll(self, name, with_vector, with_payload=True):
        for p in list(self.cols[name]["points"].values()):
            out = {"id": p["id"]}
            if with_payload:
                out["payload"] = copy.deepcopy(p.get("payload"))
            if with_vector:
                v = p.get("vector")
                if isinstance(with_vector, list) and isinstance(v, dict):
                    v = {k: v[k] for k in with_vector if k in v}
                out["vector"] = copy.deepcopy(v)
            yield out

    def retrieve(self, name, ids, with_vector=True):
        pts = self.cols[name]["points"]
        return [copy.deepcopy(pts[str(i)]) for i in ids if str(i) in pts]

    def create_like(self, source_info, target, dims):
        self.writes.append((target, "create"))
        self.cols[target] = {"info": copy.deepcopy(source_info), "points": {}}

    def upsert(self, name, points):
        self.writes.append((name, "upsert"))
        for p in points:
            self.cols[name]["points"][str(p["id"])] = copy.deepcopy(p)

    def delete(self, name, ids):
        self.writes.append((name, "delete"))
        for i in ids:
            assert not isinstance(i, str) or not i.isdigit(), "an integer id must go back as an integer"
            self.cols[name]["points"].pop(str(i), None)


def _vec(text):
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [((h[i % 32] / 255.0) - 0.5) for i in range(768)]


class FakeEmbedder:
    def __init__(self, profile):
        self.profile = profile
        self.calls = 0
        self.seen = []

    def embed_docs(self, texts):
        self.calls += 1
        self.seen.extend(texts)
        return [_vec(self.profile.doc_prefix + t) for t in texts]


def _store(q, n=4, with_episodes=True):
    mems = [{"id": f"00000000-0000-0000-0000-00000000000{i}", "payload": {"data": f"fact {i}", "user_id": "u"},
             "vector": {"": [0.1] * 768, "bm25": {"indices": [i], "values": [1.0]}}} for i in range(n)]
    q.add("mem0_egemma_768", mems)
    q.add("mem0_egemma_768_entities", [{"id": "10000000-0000-0000-0000-000000000001",
                                        "payload": {"data": "Qdrant"}, "vector": {"": [0.2] * 768}}])
    if with_episodes:
        q.add("episodes_egemma_768", [{"id": 37, "payload": {"summary": "S" * 800, "brand": "x"},
                                       "vector": [0.3] * 768}], sparse=False)


def _run(mig, monkeypatch, q, argv, live=frozenset()):
    holder = {}

    def make(base, model, profile):
        holder["emb"] = FakeEmbedder(profile)
        return holder["emb"]
    monkeypatch.setattr(mig, "Qdrant", lambda base: q)
    monkeypatch.setattr(mig, "Embedder", make)
    monkeypatch.setattr(mig, "live_collections", lambda url: (set(live), "test"))
    rc = mig.main(argv + ["--json"])
    return rc, holder.get("emb")


def _episodic_db(tmp_path, text):
    db = tmp_path / "episodic.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, summary_text TEXT)")
    con.execute("INSERT INTO episodes VALUES (37, ?)", (text,))
    con.commit()
    con.close()
    return db


def test_build_copies_ids_payloads_and_sparse_and_never_writes_the_source(mig, monkeypatch, tmp_path, capsys):
    q = FakeQdrant()
    _store(q)
    full = "S" * 800 + " and the rest of a long episode summary"
    rc, emb = _run(mig, monkeypatch, q, ["--to", "egemma2", "--episodic-db", str(_episodic_db(tmp_path, full))])
    assert rc == 0, capsys.readouterr().err
    assert {c for c, _ in q.writes} == {"mem0_eg2_768", "mem0_eg2_768_entities", "episodes_eg2_768"}
    src, dst = q.cols["mem0_egemma_768"]["points"], q.cols["mem0_eg2_768"]["points"]
    assert set(src) == set(dst)
    for k in src:
        assert dst[k]["payload"] == src[k]["payload"]
        assert dst[k]["vector"]["bm25"] == src[k]["vector"]["bm25"]          # sparse copied verbatim
        assert dst[k]["vector"][""] == _vec("title: none | text: " + src[k]["payload"]["data"])
    ep = q.cols["episodes_eg2_768"]["points"]["37"]
    assert ep["id"] == 37 and isinstance(ep["id"], int)                     # integer id stays an integer
    assert ep["vector"] == _vec("title: none | text: " + full)              # the FULL summary, not the payload's 800
    ident = json.loads(mig.IDENTITY_FILE.read_text())
    assert ident["mem0_eg2_768"]["profile"] == "egemma2" and ident["mem0_eg2_768"]["points"] == 4


def test_catch_up_reembeds_edits_and_deletes_what_the_source_lost(mig, monkeypatch, tmp_path):
    q = FakeQdrant()
    _store(q, with_episodes=False)
    assert _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories"])[0] == 0
    pts = q.cols["mem0_egemma_768"]["points"]
    first = sorted(pts)[0]
    pts[first]["payload"]["data"] = "edited fact"
    gone = sorted(pts)[1]
    del pts[gone]
    rc, emb = _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories", "--catch-up"])
    assert rc == 0
    dst = q.cols["mem0_eg2_768"]["points"]
    assert gone not in dst
    assert dst[first]["vector"][""] == _vec("title: none | text: edited fact")
    assert emb.seen == ["edited fact"]                                      # only the edited point re-embedded


def test_catch_up_dry_run_lists_deletions_and_writes_nothing(mig, monkeypatch, capsys):
    q = FakeQdrant()
    _store(q, with_episodes=False)
    _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories"])
    gone = sorted(q.cols["mem0_egemma_768"]["points"])[0]
    del q.cols["mem0_egemma_768"]["points"][gone]
    q.writes.clear()
    capsys.readouterr()
    rc, _ = _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories", "--catch-up", "--dry-run"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and q.writes == []
    assert rep["memories"]["to_delete"] == 1 and rep["memories"]["to_delete_ids"] == [gone]


def test_catch_up_refuses_more_deletions_than_the_cap(mig, monkeypatch, capsys):
    q = FakeQdrant()
    _store(q, n=6, with_episodes=False)
    _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories"])
    q.cols["mem0_egemma_768"]["points"].clear()                              # e.g. the wrong direction
    q.writes.clear()
    capsys.readouterr()
    rc, _ = _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories", "--catch-up", "--max-delete", "3"])
    assert rc == 1 and not [w for w in q.writes if w[1] == "delete"]
    assert len(q.cols["mem0_eg2_768"]["points"]) == 6
    assert "--max-delete 3" in json.loads(capsys.readouterr().out)["error"]


def test_a_collection_in_use_is_never_written(mig, monkeypatch, capsys):
    q = FakeQdrant()
    _store(q, with_episodes=False)
    rc, _ = _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories"], live={"mem0_eg2_768"})
    assert rc == 1 and q.writes == []
    assert "is in use" in capsys.readouterr().err
    rc, _ = _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories", "--force"], live={"mem0_eg2_768"})
    assert rc == 0 and q.writes


def test_the_active_profile_counts_as_in_use_when_mem0_is_down(mig, monkeypatch):
    # The real live_collections: no server answers, so stack.env decides. With the profile already
    # switched to egemma2, a forward catch-up would write the live space: refused.
    monkeypatch.setenv("MEM0_EMBED_PROFILE", "egemma2")
    names, note = mig.live_collections("http://127.0.0.1:9")
    assert "mem0_eg2_768" in names and "episodes_eg2_768" in names and "stack.env alone" in note
    monkeypatch.setenv("MEM0_EMBED_PROFILE", "egemma-300m")
    names, _ = mig.live_collections("http://127.0.0.1:9")
    assert "mem0_eg2_768" not in names


def test_verify_passes_on_a_faithful_target_and_fails_on_drift(mig, monkeypatch, capsys):
    q = FakeQdrant()
    _store(q, with_episodes=False)
    _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories,entities"])
    capsys.readouterr()
    rc, _ = _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories,entities", "--verify"])
    assert rc == 0, capsys.readouterr().out
    capsys.readouterr()
    k = sorted(q.cols["mem0_eg2_768"]["points"])[0]
    q.cols["mem0_eg2_768"]["points"][k]["vector"][""] = _vec("a different text")   # a stale vector
    q.cols["mem0_eg2_768"]["points"]["99999999-0000-0000-0000-000000000000"] = {
        "id": "99999999-0000-0000-0000-000000000000", "payload": {"data": "x"}, "vector": {"": [0.0] * 768}}
    rc, _ = _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "memories", "--verify", "--sample", "10"])
    rep = json.loads(capsys.readouterr().out)["memories"]
    assert rc == 2 and rep["extra"] == 1 and rep["below_min_cos"]


def test_same_profile_and_unknown_kind_are_refused(mig, monkeypatch, capsys):
    q = FakeQdrant()
    assert _run(mig, monkeypatch, q, ["--to", "egemma-300m"])[0] == 1
    assert _run(mig, monkeypatch, q, ["--to", "egemma2", "--kinds", "wiki"])[0] == 1
    assert q.writes == []
