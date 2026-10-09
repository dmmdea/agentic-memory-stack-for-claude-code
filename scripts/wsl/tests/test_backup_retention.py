"""Backup and DR hygiene: stack-backup retention, the manifest writer and the pCloud copy.

Every script runs for real under a scratch HOME. The Qdrant REST API is a fake HTTP server in
this process (the scripts take its base from MEM0_QDRANT_URL); nothing here reads or writes the
operator's real ~/.mem0 or talks to a live Qdrant.

Run: python3 -m pytest scripts/wsl/tests/test_backup_retention.py -q  (bash, curl, jq, python3)
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

WSL_DIR = Path(__file__).resolve().parents[1]
BACKUP = WSL_DIR / "stack-backup.sh"
MANIFEST = WSL_DIR / "stack-backup-manifest.sh"
PCLOUD = WSL_DIR / "ams-pcloud-copy.sh"

pytestmark = pytest.mark.skipif(
    any(shutil.which(t) is None for t in ("bash", "curl", "jq", "python3", "sha256sum")),
    reason="bash/curl/jq/python3/sha256sum not available",
)

PRIMARY = "mem0_egemma_768"
SECONDARIES = ("episodes_egemma_768", "mem0_egemma_768_entities", "wiki_pages_egemma_768")
DAY = 86400


# --------------------------------------------------------------------------- fake Qdrant


class FakeQdrant:
    """The slice of the Qdrant REST API the backup uses: list collections, read a collection,
    list/create/delete snapshots. A created snapshot is a real file under the snapshot root
    plus the sha256 `.checksum` file Qdrant writes beside it."""

    def __init__(self, snap_root: Path, collections, corrupt_checksum: bool = False,
                 fail_delete: bool = False, fail_create=(), fail_collection_list: bool = False, fail_snapshot_list: bool = False,
                 collection_list_body: str | None = None):
        self.snap_root = snap_root
        self.collections = list(collections)
        self.corrupt_checksum = corrupt_checksum
        self.fail_delete = fail_delete  # every snapshot DELETE answers 500
        self.fail_create = set(fail_create)  # collections whose snapshot POST answers 500
        self.fail_collection_list = fail_collection_list  # GET /collections answers 500
        self.fail_snapshot_list = fail_snapshot_list  # GET /collections/<c>/snapshots answers 500
        self.collection_list_body = collection_list_body  # GET /collections answers 200 with this raw body
        self.deleted: list[tuple[str, str]] = []
        self.created: list[tuple[str, str]] = []
        self._n = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep pytest output pristine
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parts = self.path.strip("/").split("/")
                if parts == ["collections"]:
                    if fake.fail_collection_list:
                        return self._send({"status": "boom"}, 500)
                    if fake.collection_list_body is not None:
                        raw = fake.collection_list_body.encode()
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        return self.wfile.write(raw)
                    return self._send({"result": {"collections": [{"name": c} for c in fake.collections]}})
                if len(parts) == 2 and parts[1] in fake.collections:
                    return self._send({"result": {"points_count": 42}})
                if len(parts) == 3 and parts[2] == "snapshots" and parts[1] in fake.collections:
                    if fake.fail_snapshot_list:
                        return self._send({"status": "boom"}, 500)
                    return self._send({"result": fake.list_snapshots(parts[1])})
                self._send({"status": "not found"}, 404)

            def do_POST(self):
                parts = self.path.strip("/").split("/")
                if len(parts) == 3 and parts[2] == "snapshots" and parts[1] in fake.collections:
                    if parts[1] in fake.fail_create:
                        return self._send({"status": "boom"}, 500)
                    return self._send({"result": {"name": fake.create_snapshot(parts[1])}})
                self._send({"status": "not found"}, 404)

            def do_DELETE(self):
                parts = self.path.strip("/").split("/")
                if len(parts) == 4 and parts[2] == "snapshots" and parts[1] in fake.collections:
                    if fake.fail_delete:
                        return self._send({"status": "boom"}, 500)
                    fake.delete_snapshot(parts[1], parts[3])
                    return self._send({"result": True})
                self._send({"status": "not found"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _dir(self, coll):
        d = self.snap_root / coll
        d.mkdir(parents=True, exist_ok=True)
        return d

    def seed(self, coll, name, age_days, payload=b"old-snapshot"):
        """A snapshot that already exists server-side (listed by the API)."""
        f = self._dir(coll) / name
        f.write_bytes(payload)
        (self._dir(coll) / (name + ".checksum")).write_text(hashlib.sha256(payload).hexdigest())
        t = time.time() - age_days * DAY
        os.utime(f, (t, t))

    def list_snapshots(self, coll):
        out = []
        for f in sorted(self._dir(coll).glob("*.snapshot")):
            if f.name.startswith("qdrant-"):
                continue  # a hand-made one-off: on disk, not something the API created
            out.append({"name": f.name, "creation_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(f.stat().st_mtime)),
                        "size": f.stat().st_size})
        return out

    def create_snapshot(self, coll):
        with self._lock:
            self._n += 1
            name = f"{coll}-node-{int(time.time())}-{self._n:03d}.snapshot"
        payload = (f"snapshot-of-{coll}-{name}" * 40).encode()
        f = self._dir(coll) / name
        f.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        if self.corrupt_checksum:
            digest = "0" * 64
        (self._dir(coll) / (name + ".checksum")).write_text(digest)
        self.created.append((coll, name))
        return name

    def delete_snapshot(self, coll, name):
        for suffix in ("", ".checksum"):
            (self._dir(coll) / (name + suffix)).unlink(missing_ok=True)
        self.deleted.append((coll, name))

    def remaining(self, coll):
        return sorted(p.name for p in self._dir(coll).glob("*.snapshot"))


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".mem0" / "backups").mkdir(parents=True)
    return h


@pytest.fixture
def qdrant(home):
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY, *SECONDARIES])
    yield fake
    fake.close()


def _env(home: Path, qdrant: FakeQdrant | None = None, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEM0_", "AMS_"))}
    # Every variable a platform resolves `~` from: HOME alone leaves a child that reads USERPROFILE
    # (or HOMEDRIVE + HOMEPATH) writing into the real profile.
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "HOMEDRIVE": os.path.splitdrive(str(home))[0], "HOMEPATH": os.path.splitdrive(str(home))[1],
                "MEM0_WIN_USER": "scratch", "LC_ALL": "C"})
    if qdrant is not None:
        env["MEM0_QDRANT_URL"] = qdrant.url
    env.update(extra)
    return env


def _run(script: Path, home: Path, qdrant=None, args=(), **extra) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], env=_env(home, qdrant, **extra),
                          capture_output=True, text=True, timeout=120)


def _touch(path: Path, content: bytes = b"x", age_s: float = 0) -> Path:
    path.write_bytes(content)
    if age_s:
        t = time.time() - age_s
        os.utime(path, (t, t))
    return path


def _ts(day: int, hms: str = "030000") -> str:
    return f"202609{day:02d}-{hms}"


# --------------------------------------------------------------------------- 3.1 prune


def test_prune_ignores_sidecars(home, qdrant):
    """9 real backups + 8 fresh sidecars (+ a fresh .tmp): only the oldest .db goes."""
    b = home / ".mem0" / "backups"
    dbs = [_touch(b / f"episodic-{_ts(d)}.db", b"sqlite", age_s=(20 - d) * DAY) for d in range(1, 10)]
    for d in range(2, 10):  # sidecars beside the 8 newest; the WAL is non-empty so the orphan sweep leaves them
        _touch(b / f"episodic-{_ts(d)}.db-wal", b"wal-frames")
        _touch(b / f"episodic-{_ts(d)}.db-shm", b"\0" * 32768)
    _touch(b / f"episodic-{_ts(9, '040000')}.db.tmp", b"partial")

    r = _run(BACKUP, home, qdrant)
    assert r.returncode == 0, r.stderr

    assert not dbs[0].exists(), "the oldest episodic .db must be pruned"
    assert all(p.exists() for p in dbs[1:]), "the 8 newest .db must survive"
    assert len(list(b.glob("episodic-*.db-wal"))) == 8
    assert len(list(b.glob("episodic-*.db-shm"))) == 8
    assert (b / f"episodic-{_ts(9, '040000')}.db.tmp").exists(), "a fresh .tmp is left alone (swept only when stale)"


def test_orphan_sidecars_of_empty_wal_are_swept(home, qdrant):
    b = home / ".mem0" / "backups"
    _touch(b / f"episodic-{_ts(1)}.db", b"sqlite")
    _touch(b / f"episodic-{_ts(1)}.db-wal", b"")  # empty WAL: a read-only opener's leftover
    _touch(b / f"episodic-{_ts(1)}.db-shm", b"\0" * 32768)
    _touch(b / f"episodic-{_ts(2)}.db", b"sqlite")
    _touch(b / f"episodic-{_ts(2)}.db-wal", b"unflushed-frames")  # real content: never touched
    _touch(b / f"episodic-{_ts(2)}.db-shm", b"\0" * 32768)

    r = _run(BACKUP, home, qdrant)
    assert r.returncode == 0, r.stderr

    assert not (b / f"episodic-{_ts(1)}.db-wal").exists()
    assert not (b / f"episodic-{_ts(1)}.db-shm").exists()
    assert (b / f"episodic-{_ts(1)}.db").exists()
    assert (b / f"episodic-{_ts(2)}.db-wal").exists()
    assert (b / f"episodic-{_ts(2)}.db-shm").exists()


def test_manifests_are_pruned_with_their_sets(home, qdrant):
    b = home / ".mem0" / "backups"
    for d in range(1, 12):
        _touch(b / f"manifest-{_ts(d)}.json", b"{}")
    r = _run(BACKUP, home, qdrant)
    assert r.returncode == 0, r.stderr
    left = sorted(p.name for p in b.glob("manifest-*.json"))
    # 11 seeded + tonight's, keep 8
    assert len(left) == 8
    assert f"manifest-{_ts(1)}.json" not in left


# --------------------------------------------------------------------------- 3.2 qdrant


def test_server_side_snapshots_pruned_after_verified_copy(home, qdrant):
    for i in range(1, 6):
        qdrant.seed(PRIMARY, f"{PRIMARY}-node-{i:02d}.snapshot", age_days=10 - i)
    snapdir = qdrant.snap_root / PRIMARY
    stray_old = snapdir / "qdrant-20260801.snapshot"
    stray_new = snapdir / "qdrant-20260925.snapshot"
    for f in (stray_old, stray_new):
        f.write_bytes(b"one-off")
    old = time.time() - 30 * DAY
    os.utime(stray_old, (old, old))
    recent = time.time() - 5 * DAY
    os.utime(stray_new, (recent, recent))

    r = _run(BACKUP, home, qdrant)
    assert r.returncode == 0, r.stderr

    created = [n for c, n in qdrant.created if c == PRIMARY]
    assert len(created) == 1
    kept = [n for n in qdrant.remaining(PRIMARY) if not n.startswith("qdrant-")]
    assert kept == sorted([created[0], f"{PRIMARY}-node-05.snapshot"]), "the newest 2 stay server-side"
    assert {n for c, n in qdrant.deleted if c == PRIMARY} == {f"{PRIMARY}-node-0{i}.snapshot" for i in (1, 2, 3, 4)}
    assert not stray_old.exists(), "a one-off qdrant-*.snapshot older than 14 days is pruned"
    assert stray_new.exists(), "a recent one-off is kept"
    local = list((home / ".mem0" / "backups").glob("qdrant-*.snapshot"))
    assert len(local) == 1
    assert local[0].read_bytes() == (snapdir / created[0]).read_bytes()


def test_unverified_copy_keeps_server_snapshots(home):
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY], corrupt_checksum=True)
    try:
        for i in range(1, 5):
            fake.seed(PRIMARY, f"{PRIMARY}-node-{i:02d}.snapshot", age_days=10 - i)
        r = _run(BACKUP, home, fake)
        assert r.returncode != 0
        assert "checksum" in r.stderr
        assert fake.deleted == [], "nothing is deleted server-side unless the copy verified"
        assert list((home / ".mem0" / "backups").glob("qdrant-*.snapshot")) == [], "a copy that failed verification is not kept"
    finally:
        fake.close()


def test_secondary_collections_are_snapshotted_into_the_set(home, qdrant):
    r = _run(BACKUP, home, qdrant)
    assert r.returncode == 0, r.stderr
    b = home / ".mem0" / "backups"
    for kind in ("episodes", "entities", "wiki"):
        assert len(list(b.glob(f"qcol-{kind}-*.snapshot"))) == 1, kind
    assert len(list(b.glob("qdrant-*.snapshot"))) == 1
    m = json.loads(next(b.glob("manifest-*.json")).read_text())
    for key in ("qdrant_episodes", "qdrant_entities", "qdrant_wiki"):
        assert m["files"][key], key
    assert "rebuild" in m["deliberately_excluded"]
    # each secondary is trimmed server-side like the primary (only the fresh one is there: nothing to delete)
    assert sorted(c for c, _ in qdrant.created) == sorted([PRIMARY, *SECONDARIES])


def _outcome(tmp_path: Path) -> Path:
    return tmp_path / "outcome"


def _outcome_line(path: Path):
    """The C1 outcome: one line, `<status>[:<reason>] <json counts>`; None when the job wrote none."""
    if not path.exists() or not path.read_text().strip():
        return None
    lines = path.read_text().splitlines()
    assert len(lines) == 1, f"exactly one outcome line, got {lines!r}"
    head, _, counts = lines[0].partition(" ")
    status, _, reason = head.partition(":")
    return status, reason, json.loads(counts)


def test_failed_server_prune_reads_degraded_not_ok(home, tmp_path):
    """A DELETE that fails every night restores the unbounded server-side growth: the step still
    exits 0 (the backup is done) but its receipt must say degraded, not a bare ok."""
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY], fail_delete=True)
    try:
        for i in range(1, 6):
            fake.seed(PRIMARY, f"{PRIMARY}-node-{i:02d}.snapshot", age_days=10 - i)
        out = _outcome(tmp_path)
        r = _run(BACKUP, home, fake, AMS_OUTCOME_FILE=str(out))
        assert r.returncode == 0, r.stderr
        status, reason, counts = _outcome_line(out)
        assert status == "degraded" and "server-prune-failed" in reason
        assert counts["server_prune_failed"] >= 1
        assert len(fake.remaining(PRIMARY)) > 2, "the failed DELETEs left the old snapshots in place"
    finally:
        fake.close()


def test_failed_server_snapshot_list_reads_degraded_not_ok(home, tmp_path):
    """The prune's own GET /collections/<c>/snapshots failing must not read as "nothing to prune":
    curl's failure used to be swallowed by the pipe into jq (no pipefail), so the step printed
    no WARN and wrote no outcome while the store grew unbounded again."""
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY], fail_snapshot_list=True)
    try:
        for i in range(1, 6):
            fake.seed(PRIMARY, f"{PRIMARY}-node-{i:02d}.snapshot", age_days=10 - i)
        out = _outcome(tmp_path)
        r = _run(BACKUP, home, fake, AMS_OUTCOME_FILE=str(out))
        assert r.returncode == 0, r.stderr
        assert "could not list server-side snapshots" in r.stderr, r.stderr
        status, reason, counts = _outcome_line(out)
        assert status == "degraded" and "server-prune-failed" in reason
        assert counts["server_prune_failed"] >= 1
        assert len(fake.remaining(PRIMARY)) > 2, "nothing was pruned"
    finally:
        fake.close()


def test_failed_secondary_snapshot_reads_degraded_not_ok(home, tmp_path):
    """The _entities snapshot is the only copy of that collection: its silent absence is degraded."""
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY, *SECONDARIES],
                      fail_create={"mem0_egemma_768_entities"})
    try:
        out = _outcome(tmp_path)
        r = _run(BACKUP, home, fake, AMS_OUTCOME_FILE=str(out))
        assert r.returncode == 0, r.stderr
        status, reason, counts = _outcome_line(out)
        assert status == "degraded" and "secondary-snapshot-failed" in reason
        assert counts["secondary_snapshot_failed"] == 1
        assert not list((home / ".mem0" / "backups").glob("qcol-entities-*.snapshot"))
    finally:
        fake.close()


def test_secondary_collection_listing_failure_reads_degraded(home, tmp_path):
    """If GET /collections fails, no secondary is snapshotted at all: that is not a clean night."""
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY, *SECONDARIES], fail_collection_list=True)
    try:
        out = _outcome(tmp_path)
        r = _run(BACKUP, home, fake, AMS_OUTCOME_FILE=str(out))
        assert r.returncode == 0, r.stderr
        status, reason, _ = _outcome_line(out)
        assert status == "degraded" and "secondary-snapshot-failed" in reason
    finally:
        fake.close()


def test_both_failures_share_one_outcome_line(home, tmp_path):
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY, *SECONDARIES],
                      fail_delete=True, fail_create={"wiki_pages_egemma_768"})
    try:
        for i in range(1, 5):
            fake.seed(PRIMARY, f"{PRIMARY}-node-{i:02d}.snapshot", age_days=10 - i)
        out = _outcome(tmp_path)
        r = _run(BACKUP, home, fake, AMS_OUTCOME_FILE=str(out))
        assert r.returncode == 0, r.stderr
        status, reason, counts = _outcome_line(out)  # asserts a single line
        assert status == "degraded"
        assert "secondary-snapshot-failed" in reason and "server-prune-failed" in reason
        assert counts["secondary_snapshot_failed"] == 1 and counts["server_prune_failed"] >= 1
    finally:
        fake.close()


def test_clean_run_writes_no_degraded_outcome(home, qdrant, tmp_path):
    out = _outcome(tmp_path)
    r = _run(BACKUP, home, qdrant, AMS_OUTCOME_FILE=str(out))
    assert r.returncode == 0, r.stderr
    line = _outcome_line(out)
    assert line is None or line[0] == "ok"


# --------------------------------------------------------------------------- 3.3 manifest


def _seed_set(b: Path, ts: str):
    _touch(b / f"qdrant-{ts}.snapshot", b"vectors")
    _touch(b / f"history-{ts}.db", b"history-bytes")
    _touch(b / f"tier-ledger-{ts}.jsonl", b'{"a":1}\n')
    _touch(b / f"episodic-{ts}.db", b"episodic-bytes")


def test_manifest_stamps_version_sha_and_checksums_and_tolerates_bad_stack_env(home, qdrant):
    b = home / ".mem0" / "backups"
    ts = "20260929-030237"
    _seed_set(b, ts)
    app = home / "apps" / "mem0-server"
    app.mkdir(parents=True)
    (app / "VERSION").write_text("9.8.7\n")
    (app / "DEPLOYED_SHA").write_text("0123456789abcdef0123456789abcdef01234567\n")
    # the exact failure of the 09-21 night: an unquoted space value that bash would execute
    (home / ".mem0" / "stack.env").write_text(
        "MEM0_WIN_USER=scratch\nMEM0_WIKI_SOURCES=someone@hostone someone@hosttwo\nMEM0_REPO_ROOT_WSL=/nonexistent\n")

    r = _run(MANIFEST, home, qdrant, args=[ts])
    assert r.returncode == 0, r.stderr
    m = json.loads((b / f"manifest-{ts}.json").read_text())
    assert m["app_version"] == "v9.8.7"
    assert m["git_sha"] == "0123456789abcdef0123456789abcdef01234567"
    for name in (f"qdrant-{ts}.snapshot", f"history-{ts}.db", f"tier-ledger-{ts}.jsonl", f"episodic-{ts}.db"):
        want = (b / name).read_bytes()
        assert m["checksums"][name] == {"size": len(want), "sha256": hashlib.sha256(want).hexdigest()}
    assert m["files"]["qdrant_snapshot"] == f"qdrant-{ts}.snapshot"


def test_manifest_without_deploy_stamp_says_unknown(home, qdrant):
    b = home / ".mem0" / "backups"
    ts = "20260929-030237"
    _seed_set(b, ts)
    r = _run(MANIFEST, home, qdrant, args=[ts], MEM0_REPO_ROOT_WSL=str(home / "nowhere"))
    assert r.returncode == 0, r.stderr
    m = json.loads((b / f"manifest-{ts}.json").read_text())
    assert m["git_sha"] == "unknown"
    assert m["app_version"] == "unknown"


def test_manifest_reads_the_backup_db_without_leaving_sidecars(home, qdrant):
    """A plain open of a read-only WAL-mode backup leaves -wal/-shm beside it (the debris that
    once made the prune delete real backups); the manifest reads it immutably instead."""
    import sqlite3
    b = home / ".mem0" / "backups"
    ts = "20260929-030237"
    _seed_set(b, ts)
    db = b / f"episodic-{ts}.db"
    db.unlink()
    con = sqlite3.connect(db)
    con.execute("pragma journal_mode=wal")
    con.execute("create table sessions(x)")
    con.execute("create table episodes(x)")
    con.execute("create table goals(x)")
    con.execute("create table open_questions(x)")
    con.executemany("insert into sessions values(?)", [(1,), (2,), (3,)])
    con.commit()
    con.close()
    for stray in b.glob("episodic-*.db-*"):
        stray.unlink()
    db.chmod(0o444)
    r = _run(MANIFEST, home, qdrant, args=[ts])
    assert r.returncode == 0, r.stderr
    m = json.loads((b / f"manifest-{ts}.json").read_text())
    assert m["counts"]["episodic_sessions"] == 3
    assert sorted(p.name for p in b.glob("episodic-*")) == [db.name], "no -wal/-shm left behind"


def test_stack_backup_survives_a_bad_stack_env(home, qdrant):
    (home / ".mem0" / "stack.env").write_text("MEM0_WIKI_SOURCES=someone@hostone someone@hosttwo\n")
    r = _run(BACKUP, home, qdrant, MEM0_WIN_USER="")
    assert r.returncode == 0, r.stderr
    assert "command not found" not in r.stderr


# --------------------------------------------------------------------------- 3.4 pcloud


def _set(d: Path, stamp: str, age_s: float = 0, kinds=("history", "qdrant"), manifest: bool = True):
    """One backup set in `d`: data files and (last) its manifest."""
    for k in kinds:
        ext = {"qdrant": "snapshot", "tier-ledger": "jsonl"}.get(k, "db")
        _touch(d / f"{k}-{stamp}.{ext}", f"{k}-{stamp}".encode(), age_s)
    if manifest:
        _touch(d / f"manifest-{stamp}.json", b'{"files":{}}', age_s)


def _pcloud_env(home: Path, tmp_path: Path):
    parent = tmp_path / "cloud"
    parent.mkdir(exist_ok=True)
    dst = parent / "host"
    return dst, {"MEM0_PCLOUD_DIR": str(dst)}


def _receipt(home: Path, step: str, ok: bool):
    d = home / ".mem0" / "maintenance"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "receipts.jsonl").open("a") as f:
        f.write(json.dumps({"ts": "2026-09-29T08:02:37Z", "step": step, "ok": ok, "exit": 0 if ok else 1,
                            "duration_ms": 5, "receipt_id": "x", "note": ""}) + "\n")


def test_pcloud_retention_keeps_newest_complete(home, tmp_path):
    src = home / ".mem0" / "backups"
    dst, env = _pcloud_env(home, tmp_path)
    dst.mkdir()
    for d in range(1, 11):  # ten complete sets already in the cloud
        _set(dst, _ts(d))
    _touch(dst / f"tier-ledger-{_ts(3, '030304')}.jsonl", b"partial")  # a dead partial: no manifest
    _set(dst, _ts(30), manifest=False)  # an in-flight newer partial: not ours to delete
    _set(src, _ts(11))  # tonight's set, freshly written
    _receipt(home, "stack-backup", True)

    r = _run(PCLOUD, home, **env)
    assert r.returncode == 0, r.stderr

    sets = sorted(p.name[len("manifest-"):-len(".json")] for p in dst.glob("manifest-*.json"))
    assert sets == [_ts(d) for d in range(5, 12)], "the newest 7 complete sets remain"
    for d in range(1, 5):
        assert not list(dst.glob(f"*-{_ts(d)}.*")), f"set of day {d} must be fully deleted"
    assert not list(dst.glob(f"*-{_ts(3, '030304')}.*")), "a dead partial older than the newest complete set goes"
    assert (dst / f"history-{_ts(30)}.db").exists(), "a partial newer than the newest complete set is left"
    assert (dst / f"history-{_ts(11)}.db").read_bytes() == (src / f"history-{_ts(11)}.db").read_bytes()


def test_pcloud_retention_never_deletes_newest_complete(home, tmp_path):
    src = home / ".mem0" / "backups"
    dst, env = _pcloud_env(home, tmp_path)
    dst.mkdir()
    _set(dst, _ts(1))
    _set(src, _ts(2))
    r = _run(PCLOUD, home, AMS_PCLOUD_KEEP_SETS="0", **env)
    assert r.returncode == 0, r.stderr
    assert (dst / f"manifest-{_ts(2)}.json").exists()
    assert (dst / f"history-{_ts(2)}.db").exists()


def test_pcloud_refuses_a_stale_set(home, tmp_path):
    src = home / ".mem0" / "backups"
    dst, env = _pcloud_env(home, tmp_path)
    dst.mkdir()
    _set(dst, _ts(1))
    _set(src, _ts(2), age_s=30 * 3600)
    outcome = tmp_path / "outcome"
    r = _run(PCLOUD, home, AMS_OUTCOME_FILE=str(outcome), **env)
    assert r.returncode != 0
    status, counts = outcome.read_text().strip().split(" ", 1)
    assert status == "failed:stale-set"
    assert json.loads(counts)["age_h"] == 30
    assert not (dst / f"manifest-{_ts(2)}.json").exists(), "nothing is copied"
    assert (dst / f"manifest-{_ts(1)}.json").exists(), "and nothing is pruned"


def test_pcloud_refuses_when_stack_backup_failed(home, tmp_path):
    src = home / ".mem0" / "backups"
    dst, env = _pcloud_env(home, tmp_path)
    dst.mkdir()
    _set(src, _ts(2))
    _receipt(home, "stack-backup", True)
    _receipt(home, "stack-backup", False)  # the LATEST receipt is what counts
    outcome = tmp_path / "outcome"
    r = _run(PCLOUD, home, AMS_OUTCOME_FILE=str(outcome), **env)
    assert r.returncode != 0
    assert outcome.read_text().startswith("failed:")
    assert not list(dst.glob("*"))


def _fake_cp(tmp_path: Path, body: str) -> Path:
    b = tmp_path / "fakebin"
    b.mkdir(exist_ok=True)
    real = shutil.which("cp")
    cp = b / "cp"
    cp.write_text(f'#!/usr/bin/env bash\n{body}\nexec {real} "$@"\n')
    cp.chmod(cp.stat().st_mode | stat.S_IEXEC)
    return b


def test_pcloud_copies_data_first_manifest_last_and_skips_sidecars(home, tmp_path):
    src = home / ".mem0" / "backups"
    dst, env = _pcloud_env(home, tmp_path)
    _set(src, _ts(2), kinds=("history", "qdrant", "episodic", "tier-ledger"))
    _touch(src / f"episodic-{_ts(2)}.db-wal", b"")
    _touch(src / f"episodic-{_ts(2)}.db-shm", b"\0" * 32768)
    log = tmp_path / "cp.log"
    fake = _fake_cp(tmp_path, f'echo "${{@: -1}}" >> "{log}"')
    r = _run(PCLOUD, home, PATH=f"{fake}:{os.environ['PATH']}", **env)
    assert r.returncode == 0, r.stderr
    order = [Path(p).name for p in log.read_text().split()]
    assert order[-1].endswith(f"manifest-{_ts(2)}.json.tmp"), order
    assert not any("manifest" in n for n in order[:-1]), order
    assert not (dst / f"episodic-{_ts(2)}.db-wal").exists()
    assert not (dst / f"episodic-{_ts(2)}.db-shm").exists()
    assert (dst / f"episodic-{_ts(2)}.db").exists()


def test_pcloud_size_mismatch_leaves_no_manifest_and_prunes_nothing(home, tmp_path):
    src = home / ".mem0" / "backups"
    dst, env = _pcloud_env(home, tmp_path)
    dst.mkdir()
    _set(dst, _ts(1))
    _set(src, _ts(2))
    # a copy that silently truncates the history file
    fake = _fake_cp(tmp_path, 'case "${@: -1}" in *history-*) head -c 3 "${@: -2:1}" > "${@: -1}"; exit 0;; esac')
    r = _run(PCLOUD, home, PATH=f"{fake}:{os.environ['PATH']}", **env)
    assert r.returncode != 0
    assert "size" in r.stderr
    assert not (dst / f"manifest-{_ts(2)}.json").exists(), "the manifest is copied last, only after every file verified"
    assert (dst / f"manifest-{_ts(1)}.json").exists(), "retention does not run on a failed copy"


# ------------------------------------------------- more than one collection of a secondary kind


def _extras_of(b: Path, kind: str):
    return sorted(p.name for p in b.glob(f"qcol-{kind}+*.snapshot"))


def test_a_second_collection_of_a_kind_is_snapshotted_and_is_not_degraded(home, tmp_path):
    """Snapshot EVERY collection that matches a secondary kind. Skipping the second one silently
    left it out of the set (and the old code then counted the skip as degraded, so a healthy
    night with two wiki collections read red while still not backing the second one up)."""
    extra_wiki, extra_ep = "wiki_pages_other_768", "episodes_other_768"
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY, *SECONDARIES, extra_wiki, extra_ep])
    try:
        out = _outcome(tmp_path)
        r = _run(BACKUP, home, fake, AMS_OUTCOME_FILE=str(out))
        assert r.returncode == 0, r.stderr
        assert "not snapshotted" not in r.stderr, r.stderr
        line = _outcome_line(out)
        assert line is None or line[0] == "ok", f"a second collection of a kind is not a degradation: {line}"
        b = home / ".mem0" / "backups"
        # every matching collection has its own snapshot in the set, none overwrites another
        assert sorted(c for c, _ in fake.created) == sorted([PRIMARY, *SECONDARIES, extra_wiki, extra_ep])
        assert len(list(b.glob("qcol-wiki-*.snapshot"))) == 1 and len(_extras_of(b, "wiki")) == 1
        assert len(list(b.glob("qcol-episodes-*.snapshot"))) == 1 and len(_extras_of(b, "episodes")) == 1
        assert len(list(b.glob("qcol-entities-*.snapshot"))) == 1
        # the manifest names and checksums the extras too (a file the manifest does not list is not restorable)
        m = json.loads(next(b.glob("manifest-*.json")).read_text())
        listed = sorted(m["qdrant_extra_collections"])
        assert listed == sorted(_extras_of(b, "wiki") + _extras_of(b, "episodes"))
        for name in listed:
            assert m["checksums"][name]["sha256"] == hashlib.sha256((b / name).read_bytes()).hexdigest()
    finally:
        fake.close()


def test_extra_collection_snapshots_keep_eight_each_and_never_eat_the_first(home):
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY, *SECONDARIES, "wiki_pages_other_768"])
    try:
        b = home / ".mem0" / "backups"
        for d in range(1, 11):
            _touch(b / f"qcol-wiki-{_ts(d)}.snapshot", b"first")
            _touch(b / f"qcol-wiki+wiki_pages_other_768-{_ts(d)}.snapshot", b"extra")
        r = _run(BACKUP, home, fake)
        assert r.returncode == 0, r.stderr
        assert len(list(b.glob("qcol-wiki-[0-9]*.snapshot"))) == 8
        assert len(_extras_of(b, "wiki")) == 8, "each collection keeps its own newest 8"
        assert not (b / f"qcol-wiki-{_ts(1)}.snapshot").exists()
        assert not (b / f"qcol-wiki+wiki_pages_other_768-{_ts(1)}.snapshot").exists()
    finally:
        fake.close()


@pytest.mark.parametrize("body", ["this is not json", '{"status": "ok"}', '{"result": {"collections": null}}'])
def test_a_collection_list_jq_cannot_read_reads_degraded(home, tmp_path, body):
    """curl succeeds (HTTP 200) but the body has no collection list: jq exits non-zero, and that
    status must not be masked by the `| sort` after it (a pipe reports its LAST command)."""
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY, *SECONDARIES], collection_list_body=body)
    try:
        out = _outcome(tmp_path)
        r = _run(BACKUP, home, fake, AMS_OUTCOME_FILE=str(out))
        assert r.returncode == 0, r.stderr
        assert "could not list Qdrant collections" in r.stderr, r.stderr
        status, reason, counts = _outcome_line(out)
        assert status == "degraded" and "secondary-snapshot-failed" in reason
        assert counts["secondary_snapshot_failed"] >= 1
    finally:
        fake.close()


def test_manifest_lists_no_extra_collections_when_there_are_none(home, qdrant):
    r = _run(BACKUP, home, qdrant)
    assert r.returncode == 0, r.stderr
    b = home / ".mem0" / "backups"
    m = json.loads(next(b.glob("manifest-*.json")).read_text())
    assert m["qdrant_extra_collections"] == []


def test_manifest_reads_the_unknown_stamp_as_unknown(home, qdrant):
    """The installers' shared stamp contract writes the word `unknown` when it cannot name a commit."""
    b = home / ".mem0" / "backups"
    ts = "20260929-030237"
    _seed_set(b, ts)
    app = home / "apps" / "mem0-server"
    app.mkdir(parents=True)
    (app / "DEPLOYED_SHA").write_text("unknown\n")
    r = _run(MANIFEST, home, qdrant, args=[ts], MEM0_REPO_ROOT_WSL=str(home / "nowhere"))
    assert r.returncode == 0, r.stderr
    assert json.loads((b / f"manifest-{ts}.json").read_text())["git_sha"] == "unknown"


def _git_checkout(path: Path) -> str:
    """A real git checkout with one commit under `path`; returns its HEAD sha."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")
    path.mkdir(parents=True)
    cfg = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([git, "init", "-q", str(path)], check=True, capture_output=True, timeout=60)
    subprocess.run([git, *cfg, "-C", str(path), "commit", "-q", "--allow-empty", "-m", "x"],
                   check=True, capture_output=True, timeout=60)
    return subprocess.run([git, "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True, timeout=60).stdout.strip()


@pytest.mark.parametrize("stamp", ["unknown\n", "", "not a sha\n", "0123abc\n"])
def test_a_present_stamp_is_authoritative_and_never_asks_a_checkout(home, qdrant, stamp):
    """The installers' stamp contract (install/deploy-stamp.sh): one line, a 40-hex sha or the word
    `unknown`. A stamp that names no commit reads unknown even when a checkout is reachable: that
    checkout is on whatever commit it is on, which is not evidence of what was deployed."""
    b = home / ".mem0" / "backups"
    ts = "20260929-030237"
    _seed_set(b, ts)
    head = _git_checkout(home / "checkout")
    app = home / "apps" / "mem0-server"
    app.mkdir(parents=True)
    (app / "DEPLOYED_SHA").write_text(stamp)
    r = _run(MANIFEST, home, qdrant, args=[ts], MEM0_REPO_ROOT_WSL=str(home / "checkout"))
    assert r.returncode == 0, r.stderr
    got = json.loads((b / f"manifest-{ts}.json").read_text())["git_sha"]
    assert got == "unknown", f"stamp {stamp!r} read as {got} (the reachable checkout is at {head})"


def test_without_any_stamp_the_checkout_the_script_runs_from_is_asked(home, qdrant):
    """No stamp file at all: nothing deployed this tree, the script is running from a checkout, and
    that checkout's HEAD is the release."""
    b = home / ".mem0" / "backups"
    ts = "20260929-030237"
    _seed_set(b, ts)
    head = _git_checkout(home / "checkout")
    r = _run(MANIFEST, home, qdrant, args=[ts], MEM0_REPO_ROOT_WSL=str(home / "checkout"))
    assert r.returncode == 0, r.stderr
    assert json.loads((b / f"manifest-{ts}.json").read_text())["git_sha"] == head


def test_the_sha_an_installer_stamps_is_the_sha_the_manifest_names(home, qdrant, tmp_path):
    """Writer and reader end to end: the installers' real stamp library into the installers' app dir,
    then the manifest writer with no app-dir override. A checkout -> its HEAD; a tree that cannot name
    a commit -> `unknown`, and a checkout that is reachable does not override that."""
    lib = WSL_DIR.parents[1] / "install" / "deploy-stamp.sh"
    b = home / ".mem0" / "backups"
    ts = "20260929-030237"
    _seed_set(b, ts)
    app = home / "apps" / "mem0-server"
    app.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    head = _git_checkout(checkout)

    def stamp(source: Path):
        subprocess.run(["bash", "-c", f'. "{lib.as_posix()}"; deploy_stamp_write "{source.as_posix()}" "{app.as_posix()}"'],
                       check=True, capture_output=True, timeout=60)

    stamp(checkout)
    r = _run(MANIFEST, home, qdrant, args=[ts])
    assert r.returncode == 0, r.stderr
    assert json.loads((b / f"manifest-{ts}.json").read_text())["git_sha"] == head

    bare = tmp_path / "bare"
    bare.mkdir()
    stamp(bare)  # a tarball install: no .git, no stamp of its own
    r = _run(MANIFEST, home, qdrant, args=[ts], MEM0_REPO_ROOT_WSL=str(checkout))
    assert r.returncode == 0, r.stderr
    assert json.loads((b / f"manifest-{ts}.json").read_text())["git_sha"] == "unknown"


# ------------------------------------- the outcome contract, read by the real ams-step.sh
#
# The tests above pin the outcome line these scripts write with a parser of this suite's own. Only the
# reader that ships can notice the two drifting apart, so these run each script the way the chain does
# (`ams-step.sh <step> bash <script>`) and assert on the receipt line ams-step.sh appends.

STEP = WSL_DIR / "ams-step.sh"


def _under_step(step: str, script: Path, home: Path, qdrant=None, **extra):
    """Run `script` as chain step `step`; return the process and the LAST receipt line written for it."""
    r = subprocess.run(["bash", str(STEP), step, "bash", str(script)], env=_env(home, qdrant, **extra),
                       capture_output=True, text=True, timeout=180)
    rp = home / ".mem0" / "maintenance" / "receipts.jsonl"
    rows = [json.loads(ln) for ln in rp.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return r, [x for x in rows if x["step"] == step][-1]


def _degraded_night(home: Path):
    """A night whose primary set is whole but whose server-side DELETE and one secondary snapshot failed."""
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [PRIMARY, *SECONDARIES],
                      fail_delete=True, fail_create={"wiki_pages_egemma_768"})
    try:
        for i in range(1, 5):
            fake.seed(PRIMARY, f"{PRIMARY}-node-{i:02d}.snapshot", age_days=10 - i)
        return _under_step("stack-backup", BACKUP, home, fake)
    finally:
        fake.close()


def test_the_real_step_reads_a_degraded_backup_as_degraded_with_its_counts(home):
    r, rec = _degraded_night(home)
    assert r.returncode == 0, r.stderr
    assert (rec["ok"], rec["status"], rec["exit"]) == (True, "degraded", 0), rec
    assert rec["note"] == "secondary-snapshot-failed,server-prune-failed", rec
    assert rec["work"]["secondary_snapshot_failed"] == 1 and rec["work"]["server_prune_failed"] >= 1, rec


def test_the_real_step_reads_a_clean_backup_as_ok(home, qdrant):
    r, rec = _under_step("stack-backup", BACKUP, home, qdrant)
    assert r.returncode == 0, r.stderr
    assert (rec["ok"], rec["status"], rec["exit"], rec["note"], rec["work"]) == (True, "ok", 0, "", {}), rec


def test_a_degraded_night_still_travels_off_box(home, tmp_path):
    """`degraded` keeps ok:true, so the off-box copy's receipt gate lets the (whole) primary set through;
    it must never read a degraded night as a failed one."""
    _, night = _degraded_night(home)
    assert night["status"] == "degraded"
    dst, env = _pcloud_env(home, tmp_path)
    r, rec = _under_step("pcloud-copy", PCLOUD, home, **env)
    assert r.returncode == 0, r.stderr
    assert (rec["ok"], rec["status"]) == (True, "ok"), rec
    assert list(dst.glob("manifest-*.json")), "the set was copied"


def test_the_real_step_receipts_a_refused_pcloud_copy_as_failed_with_its_counts(home, tmp_path):
    """A refusal exits non-zero AND writes the outcome line: the receipt is failed either way, and the
    reader still carries the line's counts into `work`."""
    _set(home / ".mem0" / "backups", _ts(2), age_s=30 * 3600)
    dst, env = _pcloud_env(home, tmp_path)
    r, rec = _under_step("pcloud-copy", PCLOUD, home, **env)
    assert r.returncode == 5, r.stderr
    assert (rec["ok"], rec["status"], rec["exit"]) == (False, "failed", 5), rec
    assert rec["work"] == {"age_h": 30}, rec
    assert "REFUSING" in rec["note"] and "30 h old" in rec["note"], rec
    assert not dst.exists() or not list(dst.glob("*"))


def test_a_real_failed_stack_backup_receipt_stops_the_pcloud_copy(home, tmp_path):
    """The receipt gate reads the receipt ams-step.sh really writes, not a hand-made one."""
    _set(home / ".mem0" / "backups", _ts(2))
    failing = tmp_path / "failing-backup.sh"
    failing.write_text("echo 'stack.env: line 16: boom' >&2\nexit 1\n")
    _, bad = _under_step("stack-backup", failing, home)
    assert (bad["ok"], bad["status"]) == (False, "failed"), bad
    dst, env = _pcloud_env(home, tmp_path)
    r, rec = _under_step("pcloud-copy", PCLOUD, home, **env)
    assert r.returncode == 5, r.stderr
    assert (rec["ok"], rec["status"], rec["exit"]) == (False, "failed", 5), rec
    assert "stack-backup" in rec["note"], rec
    assert not dst.exists() or not list(dst.glob("*"))


def test_the_real_step_receipts_a_copy_size_mismatch_as_failed_with_the_file(home, tmp_path):
    dst, env = _pcloud_env(home, tmp_path)
    dst.mkdir()
    _set(dst, _ts(1))
    _set(home / ".mem0" / "backups", _ts(2))
    fake = _fake_cp(tmp_path, 'case "${@: -1}" in *history-*) head -c 3 "${@: -2:1}" > "${@: -1}"; exit 0;; esac')
    r, rec = _under_step("pcloud-copy", PCLOUD, home, PATH=f"{fake}:{os.environ['PATH']}", **env)
    assert r.returncode == 6, r.stderr
    assert (rec["ok"], rec["status"], rec["exit"]) == (False, "failed", 6), rec
    assert rec["work"] == {"file": f"history-{_ts(2)}.db"}, rec


# ------------------------------------------------------------------------------------------------
# The embedding space (mem0-server/embedder_profile.py): two spaces side by side, the manifest that
# says which one a set is in, and the restores that refuse the wrong one
# ------------------------------------------------------------------------------------------------

import sys  # noqa: E402

sys.path.insert(0, str(WSL_DIR.parents[1] / "mem0-server"))
import embedder_profile as EP  # noqa: E402

REPO = WSL_DIR.parents[1]
P300 = EP.PROFILES["egemma-300m"]
P2 = EP.PROFILES["egemma2"]


def _space(p):
    return [p.memories, p.entities, p.episodes, p.wiki]


def _holds(path: Path, coll: str) -> bool:
    """FakeQdrant writes `snapshot-of-<collection>-<name>` repeated: which collection a copied file holds."""
    return path.read_bytes().startswith(f"snapshot-of-{coll}-".encode())


def _one(b: Path, pattern: str) -> Path:
    found = sorted(b.glob(pattern))
    assert len(found) == 1, (pattern, [f.name for f in found])
    return found[0]


def _manifest(b: Path) -> dict:
    return json.loads(_one(b, "manifest-*.json").read_text())


def _both_spaces(home):
    return FakeQdrant(home / "qdrant-server" / "snapshots", [*_space(P300), *_space(P2)])


def test_the_new_spaces_collections_sort_before_the_old_ones():
    """The bug this section pins: the fixed manifest slot used to go to the first collection of a kind in
    NAME order, and EmbeddingGemma-2's names sort before EmbeddingGemma-300m's ('2' < 'e')."""
    assert P2.episodes < P300.episodes and P2.wiki < P300.wiki and P2.entities < P300.entities


@pytest.mark.parametrize("active,other", [(P300, P2), (P2, P300)], ids=["default-space", "new-space"])
def test_the_active_space_owns_the_fixed_names_when_two_spaces_sit_side_by_side(home, active, other):
    """With both spaces in Qdrant, the ACTIVE space's collection of each kind is the fixed slot
    (qdrant-<TS>, qcol-<kind>-<TS>: what the manifest's fixed keys name), whatever the sort order;
    the other space is backed up too, every collection as a "+" extra, its memories as qcol-memories+."""
    fake = _both_spaces(home)
    try:
        extra = {} if active is P300 else {"MEM0_EMBED_PROFILE": active.name}
        r = _run(BACKUP, home, fake, **extra)
        assert r.returncode == 0, r.stderr
        b = home / ".mem0" / "backups"
        assert _holds(_one(b, "qdrant-*.snapshot"), active.memories)
        for kind in ("episodes", "entities", "wiki"):
            assert _holds(_one(b, f"qcol-{kind}-[0-9]*.snapshot"), getattr(active, kind)), kind
            assert _holds(_one(b, f"qcol-{kind}+*.snapshot"), getattr(other, kind)), kind
        assert _holds(_one(b, "qcol-memories+*.snapshot"), other.memories)
        assert sorted(c for c, _ in fake.created) == sorted([*_space(active), *_space(other)]), "every collection is snapshotted once"
        m = _manifest(b)
        assert m["embed_profile"] == active.name
        assert m["files"]["qdrant_snapshot"] and m["files"]["qdrant_episodes"]
        # the other space rides as extras, listed and checksummed
        assert len(m["qdrant_extra_collections"]) == 4
        for name in m["qdrant_extra_collections"]:
            assert m["checksums"][name]["sha256"] == hashlib.sha256((b / name).read_bytes()).hexdigest()
    finally:
        fake.close()


@pytest.mark.parametrize("active", [P300, P2], ids=["default-space", "new-space"])
def test_the_manifest_records_the_space_and_which_collection_each_file_holds(home, active):
    fake = _both_spaces(home)
    try:
        (home / ".mem0" / "stack.env").write_text(f"MEM0_EMBED_PROFILE={active.name}\n")   # the receipt, not the env
        r = _run(BACKUP, home, fake)
        assert r.returncode == 0, r.stderr
        b = home / ".mem0" / "backups"
        m = _manifest(b)
        assert (m["embed_profile"], m["embed_model"], m["template_version"]) == (active.name, active.model, active.template_version)
        assert m["collections"] == {"memories": active.memories, "entities": active.entities,
                                    "episodes": active.episodes, "wiki": active.wiki}
        held = m["qdrant_collections"]
        assert held[m["files"]["qdrant_snapshot"]] == active.memories
        assert held[m["files"]["qdrant_episodes"]] == active.episodes
        assert held[m["files"]["qdrant_entities"]] == active.entities
        assert held[m["files"]["qdrant_wiki"]] == active.wiki
        other = P2 if active is P300 else P300
        assert sorted(held[n] for n in m["qdrant_extra_collections"]) == sorted(_space(other))
        # every file the map names is one the manifest lists, and every Qdrant file it lists is in the map
        listed = {v for k, v in m["files"].items() if k.startswith("qdrant") and isinstance(v, str)} | set(m["qdrant_extra_collections"])
        assert set(held) == listed
        # the identity fields come right after the release stamp, before the file lists
        keys = list(m)
        assert keys.index("git_sha") < keys.index("embed_profile") < keys.index("files")
    finally:
        fake.close()


def test_without_a_profile_the_default_space_is_backed_up_exactly_as_before(home, qdrant):
    """No MEM0_EMBED_PROFILE anywhere: the names, the slots and the manifest's fixed keys are the ones
    every earlier set had (the other tests in this file pin them); the set only gains its identity."""
    r = _run(BACKUP, home, qdrant)
    assert r.returncode == 0, r.stderr
    b = home / ".mem0" / "backups"
    m = _manifest(b)
    assert (m["embed_profile"], m["embed_model"]) == (P300.name, P300.model)
    assert (b / f"qcol-episodes-{m['backup_ts_raw']}.snapshot").exists()
    assert not list(b.glob("qcol-*+*.snapshot")), "no other space in Qdrant, no extras"
    assert m["qdrant_extra_collections"] == []


def test_an_operator_override_of_the_memories_collection_still_names_the_active_space(home):
    """MEM0_QDRANT_COLLECTION renames the bound memories collection (and the <name>_entities mem0 derives
    from it); the backup follows, as it did when the name was a literal default."""
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", ["custom_mem", "custom_mem_entities", P300.episodes, P300.wiki])
    try:
        r = _run(BACKUP, home, fake, MEM0_QDRANT_COLLECTION="custom_mem")
        assert r.returncode == 0, r.stderr
        b = home / ".mem0" / "backups"
        assert _holds(_one(b, "qdrant-*.snapshot"), "custom_mem")
        assert _holds(_one(b, "qcol-entities-[0-9]*.snapshot"), "custom_mem_entities")
        m = _manifest(b)
        assert m["collections"]["memories"] == "custom_mem" and m["collections"]["entities"] == "custom_mem_entities"
    finally:
        fake.close()


def test_the_other_spaces_memories_snapshots_keep_their_own_window(home):
    fake = _both_spaces(home)
    try:
        b = home / ".mem0" / "backups"
        for d in range(1, 11):
            _touch(b / f"qcol-memories+{P2.memories}-{_ts(d)}.snapshot", b"anchor")
        r = _run(BACKUP, home, fake)
        assert r.returncode == 0, r.stderr
        assert len(list(b.glob(f"qcol-memories+{P2.memories}-[0-9]*.snapshot"))) == 8
        assert len(list(b.glob("qdrant-[0-9]*.snapshot"))) == 1, "the primary kind is a different window"
    finally:
        fake.close()


def test_a_failed_snapshot_of_the_other_space_reads_degraded(home, tmp_path):
    """The rollback anchor is exactly what the operator reaches for when a migration goes wrong: its
    silent absence from the set is a degradation, not a clean night."""
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [*_space(P300), *_space(P2)], fail_create={P2.memories})
    try:
        out = _outcome(tmp_path)
        r = _run(BACKUP, home, fake, AMS_OUTCOME_FILE=str(out))
        assert r.returncode == 0, r.stderr
        status, reason, counts = _outcome_line(out)
        assert status == "degraded" and "secondary-snapshot-failed" in reason and counts["secondary_snapshot_failed"] == 1
    finally:
        fake.close()


def test_an_unknown_profile_is_a_loud_failure_that_snapshots_nothing(home, qdrant):
    """A hand-edited MEM0_EMBED_PROFILE must not fall back to some other space's names: the primary set is
    missing (rc 1, the night is red), the local files are still backed up, and the manifest says unknown."""
    r = _run(BACKUP, home, qdrant, MEM0_EMBED_PROFILE="no-such-space")
    assert r.returncode != 0
    assert "no-such-space" in r.stderr and "NOT be backed up" in r.stderr, r.stderr
    assert qdrant.created == []
    assert _manifest(home / ".mem0" / "backups")["embed_profile"] == "unknown"


def test_a_checkout_without_the_profile_module_falls_back_to_the_pre_profile_names_and_says_so(home, tmp_path):
    """The one literal that stays: if embedder_profile.py cannot be found (a scripts dir deployed away from
    the server), a nightly that dies is worse than one that backs up the names every store was built on."""
    deployed = tmp_path / "isolated" / "scripts"
    deployed.mkdir(parents=True)
    for name in ("stack-backup.sh", "stack-backup-manifest.sh", "embed-profile.sh"):
        shutil.copy(WSL_DIR / name, deployed / name)
    fake = FakeQdrant(home / "qdrant-server" / "snapshots", [*_space(P300)])
    try:
        r = subprocess.run(["bash", str(deployed / "stack-backup.sh")], env=_env(home, fake), capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert "embedder_profile.py not found" in r.stderr
        b = home / ".mem0" / "backups"
        assert _holds(_one(b, "qdrant-*.snapshot"), P300.memories)
        assert _manifest(b)["embed_profile"] == "unknown"
    finally:
        fake.close()


def test_every_script_that_names_a_collection_asks_embedder_profile():
    """No collection literal in the backup path: a name that is not asked of the module is a name that
    survives the next migration unchanged."""
    for rel in ("stack-backup.sh", "stack-backup-manifest.sh", "stack-restore.sh", "egemma-rollback-prune.sh"):
        code = "\n".join(ln for ln in (WSL_DIR / rel).read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
        for p in (P300, P2):
            for n in _space(p):
                assert n not in code, (rel, n)
    lib = (WSL_DIR / "embed-profile.sh").read_text(encoding="utf-8")
    assert lib.count("mem0_egemma_768") == 1 and "pre-profile" in lib, "the single documented fallback literal"


# --------------------------------------------------------------------------- ams-step: the profile


def _step_echo(tmp_path: Path, stack_env: str | None, **extra):
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True, exist_ok=True)
    if stack_env is not None:
        (home / ".mem0" / "stack.env").write_text(stack_env, encoding="utf-8")
    r = subprocess.run(["bash", str(STEP), "demo", "bash", "-c", "echo profile=[$MEM0_EMBED_PROFILE]"],
                       env=_env(home, **extra), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_a_chain_step_sees_the_embedding_profile_from_stack_env(tmp_path):
    """The step units set none of it; a python job that fell back to the default profile would read and
    write another space's collections than the server is bound to."""
    assert "profile=[egemma2]" in _step_echo(tmp_path, "MEM0_WSL_USER=t\nMEM0_EMBED_PROFILE=egemma2\n")
    assert "profile=[egemma2]" in _step_echo(tmp_path / "crlf", "MEM0_EMBED_PROFILE=egemma2\r\n")


def test_the_environment_outranks_stack_env_for_a_chain_step(tmp_path):
    assert "profile=[egemma-300m]" in _step_echo(tmp_path, "MEM0_EMBED_PROFILE=egemma2\n", MEM0_EMBED_PROFILE="egemma-300m")


def test_a_chain_step_on_a_box_without_a_recorded_profile_gets_none(tmp_path):
    assert "profile=[]" in _step_echo(tmp_path, "MEM0_WSL_USER=t\n")
    assert "profile=[]" in _step_echo(tmp_path / "bare", None)


# --------------------------------------------------------------------------- wiki-index.sh (replica wrapper)


def test_the_replica_wrapper_passes_the_embedding_profile_on_to_the_builder(tmp_path):
    """wiki-index.sh embeds with THIS box's profile into the brain's collection of that space: the profile and
    the scoped alias overrides come from the replica's stack.env, the environment wins."""
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    (home / ".mem0" / "stack.env").write_text("MEM0_EMBED_PROFILE=egemma2\nMEM0_EMBED_MODEL_EGEMMA2=eg2-local\n", encoding="utf-8")
    b = tmp_path / "bin"
    b.mkdir()
    ssh = b / "ssh"
    ssh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    py = b / "fakepy"
    py.write_text('#!/usr/bin/env bash\necho "profile=${MEM0_EMBED_PROFILE:-unset} alias=${MEM0_EMBED_MODEL_EGEMMA2:-unset}" >> "$FAKE_LOG"\n', encoding="utf-8")
    for f in (ssh, py):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log"

    def run(**extra):
        return subprocess.run(["bash", str(WSL_DIR / "wiki-index.sh"), "search", "q"],
                              env=_env(home, PATH=f"{b}{os.pathsep}{os.environ['PATH']}", WIKI_PY=str(py), WIKI_BRAIN_SSH="brain",
                                       FAKE_LOG=str(log), WIKI_TUNNEL_PORT="16999", **extra),
                              capture_output=True, text=True, timeout=60)

    r = run()
    assert r.returncode == 0, r.stderr
    assert log.read_text().strip() == "profile=egemma2 alias=eg2-local"
    log.unlink()
    r = run(MEM0_EMBED_PROFILE="egemma-300m")
    assert r.returncode == 0, r.stderr
    assert log.read_text().startswith("profile=egemma-300m"), "the environment outranks the receipt"


# --------------------------------------------------------------------------- stack-restore.sh


RESTORE = WSL_DIR / "stack-restore.sh"


def _restorable_set(home, qdrant, **extra):
    """A complete set + its manifest, written by the real manifest writer."""
    b = home / ".mem0" / "backups"
    ts = "20261007-033000"
    _seed_set(b, ts)
    r = _run(MANIFEST, home, qdrant, args=[ts], **extra)
    assert r.returncode == 0, r.stderr
    return ts


def _restore_dry_run(home, ts, tmp_path, **extra):
    return subprocess.run(["bash", str(RESTORE), "--snapshot", ts, "--dry-run"],
                          env=_env(home, DRILL_LOG=str(tmp_path / "drill.jsonl"), **extra),
                          capture_output=True, text=True, timeout=120)


def test_stack_restore_shows_which_space_the_set_is_in(home, qdrant, tmp_path):
    ts = _restorable_set(home, qdrant)
    r = _restore_dry_run(home, ts, tmp_path)
    assert r.returncode == 0, r.stderr
    assert f"embed_profile  : {P300.name} (model alias {P300.model}, template {P300.template_version})" in r.stdout, r.stdout
    assert f"qdrant_collection: {P300.memories} (what qdrant_snapshot holds)" in r.stdout
    assert "WARN" not in r.stderr


def test_stack_restore_warns_but_does_not_refuse_when_the_box_is_in_another_space(home, qdrant, tmp_path):
    """The staging restore only uploads a snapshot into a side collection, in any space (a rollback drill
    restores the OLD space on purpose); the refusal belongs to the replica restores."""
    ts = _restorable_set(home, qdrant)
    r = _restore_dry_run(home, ts, tmp_path, MEM0_EMBED_PROFILE=P2.name)
    assert r.returncode == 0, r.stderr
    assert f"made in embedding profile '{P300.name}' but this box is configured for '{P2.name}'" in r.stderr, r.stderr
    assert f"--embed-profile {P300.name}" in r.stderr, "the warning says how to switch the box"


def test_stack_restore_reads_a_set_from_before_profiles_as_the_legacy_space(home, qdrant, tmp_path):
    ts = _restorable_set(home, qdrant)
    mp = home / ".mem0" / "backups" / f"manifest-{ts}.json"
    m = json.loads(mp.read_text())
    for k in ("embed_profile", "embed_model", "template_version", "collections", "qdrant_collections"):
        m.pop(k)
    mp.write_text(json.dumps(m))
    r = _restore_dry_run(home, ts, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "none recorded (a set from before embedding profiles: the legacy space)" in r.stdout, r.stdout
    assert f"qdrant_collection: {EP.PROFILES[EP.LEGACY_PROFILE].memories}" in r.stdout


# --------------------------------------------------------------------------- restore-replica.sh


REPLICA_RESTORE = WSL_DIR.parents[1] / "scripts" / "travel" / "restore-replica.sh"


class FakeLlamaSwap:
    """GET /v1/models listing the given aliases, the way llama-swap does."""

    def __init__(self, aliases):
        fake = self
        self.aliases = list(aliases)

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"object": "list", "data": [{"id": a, "object": "model"} for a in fake.aliases]}).encode()
                self.send_response(200 if self.path.rstrip("/").endswith("/models") else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _replica_box(tmp_path, manifest: dict | None, stack_env: str = "", aliases=()):
    """A replica home, a fake brain (a local backup dir reached through a stub ssh) and a fake llama-swap."""
    home = tmp_path / "home"
    (home / ".mem0").mkdir(parents=True)
    (home / ".mem0" / "role").write_text("replica\n")
    (home / ".mem0" / "authority-url").write_text("http://brain.example.test:18791\n")
    brain = tmp_path / "brain-backups"
    brain.mkdir()
    (home / ".mem0" / "replica.env").write_text(f"BRAIN_SSH='brain'\nBRAIN_BACKUP_DIR='{brain}'\nBRAIN_WSL=''\n")
    if stack_env:
        (home / ".mem0" / "stack.env").write_text(stack_env)
    ts = "20261007-033000"
    if manifest is not None:
        base = {"ts": "2026-10-07T03:30:00Z", "backup_ts_raw": ts,
                "files": {"qdrant_snapshot": f"qdrant-{ts}.snapshot", "episodic_db": f"episodic-{ts}.db", "history_db": f"history-{ts}.db"},
                "counts": {"qdrant_points": 42}}
        base.update(manifest)
        (brain / f"manifest-{ts}.json").write_text(json.dumps(base))
        for f in base["files"].values():
            (brain / f).write_bytes(b"x")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    ssh = bin_ / "ssh"   # run the remote command line locally: skip the option pairs and the host
    ssh.write_text('#!/usr/bin/env bash\nwhile [ $# -gt 0 ]; do case "$1" in -o) shift 2;; -*) shift;; *) break;; esac; done\nshift\nexec bash -c "$*"\n')
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    for name in ("systemctl", "loginctl"):   # the script's EXIT trap stops the user units: never the real ones
        stub = bin_ / name
        stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    swap = FakeLlamaSwap(aliases)
    return home, swap, bin_


def _replica_restore(home, swap, bin_, *args, **extra):
    return subprocess.run(["bash", str(REPLICA_RESTORE), "--dry-run", *args],
                          env=_env(home, PATH=f"{bin_}{os.pathsep}{os.environ['PATH']}", MEM0_EMBED_BASE_URL=swap.url, **extra),
                          capture_output=True, text=True, timeout=120)


def test_the_replica_restore_takes_profile_and_collection_from_the_manifest(tmp_path):
    home, swap, bin_ = _replica_box(tmp_path, {"embed_profile": P2.name, "collections": {"memories": P2.memories}},
                                    stack_env=f"MEM0_EMBED_PROFILE={P2.name}\n", aliases=[P2.model])
    try:
        r = _replica_restore(home, swap, bin_)
        assert r.returncode == 0, r.stderr
        assert f"embedding profile {P2.name}: alias '{P2.model}' served locally; restoring into collection '{P2.memories}'" in r.stdout, r.stdout
        assert "[dry-run]" in r.stdout
    finally:
        swap.close()


def test_a_set_from_before_profiles_restores_into_the_legacy_space(tmp_path):
    home, swap, bin_ = _replica_box(tmp_path, {}, aliases=[P300.model])   # no embed_profile, no collections
    try:
        r = _replica_restore(home, swap, bin_)
        assert r.returncode == 0, r.stderr
        assert f"records no embedding profile (made before profiles): the legacy space, {P300.name}" in r.stdout
        assert f"restoring into collection '{P300.memories}'" in r.stdout
    finally:
        swap.close()


def test_the_replica_restore_refuses_a_set_in_another_space_than_the_replicas(tmp_path):
    """The failure this guards: the upload would restore healthy-looking vectors that the replica's own
    server (bound to another space) never reads, or worse reads with the wrong model."""
    home, swap, bin_ = _replica_box(tmp_path, {"embed_profile": P2.name, "collections": {"memories": P2.memories}},
                                    aliases=[P2.model, P300.model])   # the replica is on the default space
    try:
        r = _replica_restore(home, swap, bin_)
        assert r.returncode != 0
        assert f"set 20261007-033000 is in embedding profile '{P2.name}' but this replica is configured for '{P300.name}'" in r.stderr, r.stderr
        assert f"install/linux-replica.sh --embed-profile {P2.name}" in r.stderr, "the refusal names the fix"
        # llama-swap serves the ALIAS, not the profile's name: an operator told to serve 'egemma2' adds an entry
        # nothing asks for
        assert f"Serve '{P2.model}' (profile '{P2.name}') on llama-swap :11436" in r.stderr, r.stderr
        assert P2.model != P2.name
    finally:
        swap.close()
    # and the other way: a legacy set on a replica that has moved to the new space
    home, swap, bin_ = _replica_box(tmp_path / "other", {}, stack_env=f"MEM0_EMBED_PROFILE={P2.name}\n", aliases=[P2.model])
    try:
        r = _replica_restore(home, swap, bin_)
        assert r.returncode != 0 and f"in embedding profile '{P300.name}' but this replica is configured for '{P2.name}'" in r.stderr, r.stderr
    finally:
        swap.close()


def test_the_replica_restore_refuses_when_the_local_embedder_does_not_serve_the_profiles_alias(tmp_path):
    """Serving SOME embedder is not enough: the alias of the set's profile is what the replica's mem0 asks for."""
    home, swap, bin_ = _replica_box(tmp_path, {"embed_profile": P2.name, "collections": {"memories": P2.memories}},
                                    stack_env=f"MEM0_EMBED_PROFILE={P2.name}\n", aliases=[P300.model])
    try:
        r = _replica_restore(home, swap, bin_)
        assert r.returncode != 0
        assert f"does not serve '{P2.model}'" in r.stderr and "b11452" in r.stderr, r.stderr
    finally:
        swap.close()


def test_the_replicas_own_scoped_alias_is_what_has_to_be_served(tmp_path):
    """The replica may serve the GGUF under its own name (embedder_profile: MEM0_EMBED_MODEL_<PROFILE>)."""
    home, swap, bin_ = _replica_box(tmp_path, {"embed_profile": P2.name, "collections": {"memories": P2.memories}},
                                    stack_env=f"MEM0_EMBED_PROFILE={P2.name}\nMEM0_EMBED_MODEL_EGEMMA2=eg2-on-this-box\n",
                                    aliases=["eg2-on-this-box"])
    try:
        r = _replica_restore(home, swap, bin_)
        assert r.returncode == 0, r.stderr
        assert "alias 'eg2-on-this-box' served locally" in r.stdout
    finally:
        swap.close()


def test_the_replica_restore_refuses_a_collection_its_server_would_not_bind(tmp_path):
    home, swap, bin_ = _replica_box(tmp_path, {"embed_profile": P2.name, "collections": {"memories": "custom_name"}},
                                    stack_env=f"MEM0_EMBED_PROFILE={P2.name}\n", aliases=[P2.model])
    try:
        r = _replica_restore(home, swap, bin_)
        assert r.returncode != 0
        assert f"holds collection 'custom_name' but this replica's server binds '{P2.memories}'" in r.stderr, r.stderr
        # --collection is the explicit override: the operator binds the name himself
        r = _replica_restore(home, swap, bin_, "--collection", "custom_name")
        assert r.returncode == 0, r.stderr
        assert "restoring into collection 'custom_name'" in r.stdout
    finally:
        swap.close()


def test_the_replica_restore_refuses_a_set_without_a_resolvable_profile(tmp_path):
    home, swap, bin_ = _replica_box(tmp_path, {"embed_profile": "unknown"}, aliases=[P300.model])
    try:
        r = _replica_restore(home, swap, bin_)
        assert r.returncode != 0 and "without a resolvable embedding profile" in r.stderr, r.stderr
    finally:
        swap.close()


def test_the_replica_restore_checks_the_restored_server_is_bound_to_the_restored_space():
    """The live half (upload, start mem0, /health/deep) needs systemd and a Qdrant, so it is pinned by text:
    the restored server must report the set's profile and collection, not merely `ok`."""
    sh = REPLICA_RESTORE.read_text(encoding="utf-8")
    assert ".embed_profile.profile" in sh and "is bound to embedding profile" in sh
    assert "is bound to collection" in sh
    assert sh.index('bound_profile="$(printf') > sh.index('printf \'%s\' "$deep" | jq -e \'.ok == true\'')
    code = "\n".join(ln for ln in sh.splitlines() if not ln.lstrip().startswith("#"))
    for p in (P300, P2):
        for n in _space(p):
            assert n not in code, n
    assert 'COLLECTION=""' in code, "the default collection is no longer a literal"


# --------------------------------------------------------------------------- media memories (1.35.0)

def test_the_media_files_travel_in_the_set_as_one_tar(home, qdrant):
    """Media memories point at files under ~/.mem0/media: the set carries them (one tar, listed in the
    manifest, pruned with the set); a box without media writes no tar and the manifest says null."""
    import tarfile
    r = _run(BACKUP, home, qdrant)
    assert r.returncode == 0, r.stderr
    b = home / ".mem0" / "backups"
    assert not list(b.glob("media-*.tar"))
    assert json.loads(next(b.glob("manifest-*.json")).read_text())["files"]["media"] is None
    for f in b.iterdir():
        f.unlink()
    d = home / ".mem0" / "media" / "ab"
    d.mkdir(parents=True)
    (d / ("ab" + "0" * 62 + ".png")).write_bytes(b"\x89PNG\r\n\x1a\n-one")
    (d / ("ab" + "1" * 62 + ".png.tmp")).write_bytes(b"half-written")
    r = _run(BACKUP, home, qdrant)
    assert r.returncode == 0, r.stderr
    tars = list(b.glob("media-*.tar"))
    assert len(tars) == 1
    with tarfile.open(tars[0]) as t:
        names = sorted(n for n in t.getnames() if not n.endswith("/") and n not in (".", "./ab"))
    assert names == ["./ab/ab" + "0" * 62 + ".png"], names
    m = json.loads(next(b.glob("manifest-*.json")).read_text())
    assert m["files"]["media"] == tars[0].name


def test_stack_restore_plans_the_media_extraction(home, qdrant, tmp_path):
    ts = _restorable_set(home, qdrant)
    b = home / ".mem0" / "backups"
    (b / f"media-{ts}.tar").write_bytes(b"\0" * 1024)
    mp = b / f"manifest-{ts}.json"
    m = json.loads(mp.read_text())
    m["files"]["media"] = f"media-{ts}.tar"
    mp.write_text(json.dumps(m))
    r = _restore_dry_run(home, ts, tmp_path)
    assert r.returncode == 0, r.stderr
    assert f"5g. media          : {b}/media-{ts}.tar -> {home}/.mem0/media (additive" in r.stdout, r.stdout


def test_both_restores_extract_media_without_rewriting_a_file():
    """Names are content hashes, so an existing name already holds the same bytes: extraction is
    additive (--skip-old-files) and never takes the archive's owners."""
    import re
    for script in (RESTORE, REPLICA_RESTORE):
        code = "\n".join(ln for ln in script.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
        assert re.search(r'tar -C "\$MEDIA_DST" --skip-old-files --no-same-owner -xf ', code), script


def test_both_restores_list_the_media_tar_before_extracting_it():
    """The two bash restores cannot run their extraction here (they need a live stack), so the order is pinned
    in their text: the tar is listed (tar -tvf, inside media_tar_problem) and a refusal comes BEFORE the
    extraction. stack-restore.sh handles it like its other media failure (a warning that counts), restore-replica.sh
    like its own (fail)."""
    for script, refusal in ((RESTORE, 'echo "WARN: media restore REFUSED, nothing extracted ($MEDIA_PROBLEM)'),
                            (REPLICA_RESTORE, 'fail "media restore from $MEDIA_FILE refused, nothing extracted ($MEDIA_PROBLEM)"')):
        code = "\n".join(ln for ln in script.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
        assert 'tar -tvf "$1"' in code, script
        call = code.index('MEDIA_PROBLEM="$(media_tar_problem "')
        assert code.index("media_tar_problem() {") < call < code.index('tar -C "$MEDIA_DST" --skip-old-files'), script
        assert refusal in code[call:], script
    restore = RESTORE.read_text(encoding="utf-8")
    assert "WARNS=$((WARNS+1))" in restore[restore.index("media restore REFUSED"):restore.index("media restore FAILED")]


def _media_tar_problem_fn(script: Path) -> str:
    text = script.read_text(encoding="utf-8")
    start = text.index("media_tar_problem() {")
    return text[start:text.index("\n}\n", start) + 3]


def _tar_with(path: Path, *members):
    """A real tar holding the given (TarInfo, data-or-None) members, in order."""
    import io
    import tarfile
    with tarfile.open(path, "w") as t:
        for info, data in members:
            t.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def _ti(name, kind="file", link=""):
    import tarfile
    ti = tarfile.TarInfo(name)
    ti.mtime = 1_700_000_000
    ti.uid = ti.gid = 1000
    ti.mode = 0o644
    if kind == "file":
        ti.type, ti.size = tarfile.REGTYPE, 5
    elif kind == "dir":
        ti.type, ti.mode = tarfile.DIRTYPE, 0o755
    elif kind == "symlink":
        ti.type, ti.linkname = tarfile.SYMTYPE, link
    elif kind == "hardlink":
        ti.type, ti.linkname = tarfile.LNKTYPE, link
    elif kind == "fifo":
        ti.type = tarfile.FIFOTYPE
    return ti


_GOOD = ("./ab/ab" + "0" * 62 + ".png")


@pytest.mark.skipif(shutil.which("tar") is None, reason="tar not available")
@pytest.mark.parametrize("script", [RESTORE, REPLICA_RESTORE], ids=["stack-restore.sh", "restore-replica.sh"])
@pytest.mark.parametrize("case,members,refused", [
    ("what stack-backup.sh writes: ./, a directory, a regular file", [(_ti("./", "dir"), None), (_ti("./ab", "dir"), None), (_ti(_GOOD), b"12345")], None),
    ("names with spaces and a dotted name are plain names", [(_ti("./ab/a b..png"), b"12345"), (_ti("./.hidden"), b"12345")], None),
    ("an empty tar", [], None),
    ("a symlink out of the media directory", [(_ti("./ab", "symlink", "/etc"), None), (_ti(_GOOD), b"12345")], "not a regular file or directory"),
    ("a symlink to a relative target", [(_ti("./link", "symlink", "../../x"), None)], "not a regular file or directory"),
    ("a hard link", [(_ti(_GOOD), b"12345"), (_ti("./ab/other", "hardlink", _GOOD), None)], "not a regular file or directory"),
    ("a fifo", [(_ti("./pipe", "fifo"), None)], "not a regular file or directory"),
    ("an absolute name", [(_ti("/etc/cron.d/evil"), b"12345")], "absolute or '..' name"),
    ("a '..' component", [(_ti("./ab/../../evil"), b"12345")], "absolute or '..' name"),
    ("a bare '..' directory", [(_ti("..", "dir"), None)], "absolute or '..' name"),
])
def test_the_media_tar_check_refuses_anything_but_relative_regular_files_and_directories(tmp_path, script, case, members, refused):
    """Run for real: the bash function both restores carry, against tars made by Python's tarfile and listed by
    this box's tar. Its exit 0 means 'do not extract' and it prints why; 1, silently, means every entry is fine."""
    tar = _tar_with(tmp_path / "media.tar", *members)
    fn = _media_tar_problem_fn(script)
    r = subprocess.run(["bash", "-c", fn + '\nmedia_tar_problem "$1"; echo "rc=$?"', "x", str(tar)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out, rc = r.stdout.rsplit("rc=", 1)
    if refused is None:
        assert rc.strip() == "1" and out.strip() == "", (case, r.stdout)
    else:
        assert rc.strip() == "0" and refused in out, (case, r.stdout)


@pytest.mark.skipif(shutil.which("tar") is None, reason="tar not available")
@pytest.mark.parametrize("script", [RESTORE, REPLICA_RESTORE], ids=["stack-restore.sh", "restore-replica.sh"])
def test_the_media_tar_check_refuses_a_tar_it_cannot_list(tmp_path, script):
    bad = tmp_path / "media.tar"
    bad.write_bytes(b"this is not a tar archive" * 100)
    r = subprocess.run(["bash", "-c", _media_tar_problem_fn(script) + '\nmedia_tar_problem "$1"; echo "rc=$?"', "x", str(bad)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.rstrip().endswith("rc=0") and "tar could not list it" in r.stdout, r.stdout
