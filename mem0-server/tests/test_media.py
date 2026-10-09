"""1.35.0 media memories (media.py): images, audio and video embedded into the memories' space.

The load-bearing claims:
  * a type is decided by the bytes, never by what the caller declares, and every caller error is a
    MediaError (an HTTP 400), never a stored file or a 500;
  * files are content-addressed and written atomically, and a payload entry can only ever name a
    file under the media directory;
  * each item becomes the content part llama-server's /v1/embeddings takes for its type;
  * the MCP shim reads Windows paths, refuses unknown files, and never queues a media memory offline.

Headless (CI runs it). The embedder, mem0's search path and the server endpoints are in
test_media_server.py, which needs the mem0 package and a Qdrant.
"""
import base64
import importlib.util
import io
import os
import sys
import uuid
import wave
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import media  # noqa: E402


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
BMP = b"BM" + b"\x00" * 64
FLAC = b"fLaC" + b"\x00" * 64
MP3_ID3 = b"ID3\x04" + b"\x00" * 64
MP3_SYNC = b"\xff\xfb\x90\x00" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64


def _wav(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def _b64(data):
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    d = tmp_path / "media"
    monkeypatch.setenv("MEM0_MEDIA_DIR", str(d))
    return d


# ---------------------------------------------------------------- media.py: types from bytes

@pytest.mark.parametrize("data,expected", [
    (PNG, ("image", "image/png", "png")), (JPG, ("image", "image/jpeg", "jpg")),
    (GIF, ("image", "image/gif", "gif")), (WEBP, ("image", "image/webp", "webp")),
    (BMP, ("image", "image/bmp", "bmp")), (_wav(0.1), ("audio", "audio/wav", "wav")),
    (FLAC, ("audio", "audio/flac", "flac")), (MP3_ID3, ("audio", "audio/mpeg", "mp3")),
    (MP3_SYNC, ("audio", "audio/mpeg", "mp3")), (MP4, ("video", "video/mp4", "mp4")),
    (WEBM, ("video", "video/webm", "webm")),
])
def test_sniff_reads_the_type_from_the_magic_bytes(data, expected):
    assert media.sniff(data) == expected


def test_sniff_rejects_what_it_does_not_know():
    assert media.sniff(b"%PDF-1.7 not media") is None
    assert media.sniff(b"") is None


def test_decode_accepts_base64_and_data_urls_and_keeps_only_a_basename():
    out = media.decode([{"type": "image", "data": _b64(PNG), "filename": "../../etc/passwd.png"},
                        {"type": "audio", "data": "data:audio/wav;base64," + _b64(_wav(0.5))}])
    assert [m.type for m in out] == ["image", "audio"]
    assert out[0].data == PNG and out[0].filename == "passwd.png"
    assert out[1].ext == "wav" and out[1].filename is None


@pytest.mark.parametrize("items,needle", [
    ("not a list", "must be a list"),
    ([{"type": "image", "data": "!!not base64!!"}], "not valid base64"),
    ([{"type": "image", "data": ""}], "empty"),
    ([{"type": "document", "data": _b64(PNG)}], "type must be one of"),
    ([{"type": "audio", "data": _b64(PNG)}], "declared audio but its bytes are image"),
    ([{"type": "image", "data": _b64(b"%PDF-1.7")}], "unrecognised format"),
    (["png"], "is not an object"),
])
def test_decode_refuses_caller_errors_as_media_errors(items, needle):
    with pytest.raises(media.MediaError, match=needle):
        media.decode(items)


def test_decode_caps_the_count_and_the_size(monkeypatch):
    with pytest.raises(media.MediaError, match="at most"):
        media.decode([{"type": "image", "data": _b64(PNG)}] * (media.MAX_ITEMS + 1))
    monkeypatch.setattr(media, "MAX_ITEM_BYTES", 16)
    with pytest.raises(media.MediaError, match="cap of 16"):
        media.decode([{"type": "image", "data": _b64(PNG)}])
    # refused from its length before any decoding: an oversized string that is not even base64
    with pytest.raises(media.MediaError, match="cap of 16"):
        media.decode([{"type": "image", "data": "!" * 10_000}])
    # at the cap exactly it decodes, and the decoded size is checked too
    monkeypatch.setattr(media, "MAX_ITEM_BYTES", len(PNG))
    assert media.decode([{"type": "image", "data": _b64(PNG)}])[0].data == PNG
    monkeypatch.setattr(media, "MAX_ITEM_BYTES", len(PNG) - 1)
    with pytest.raises(media.MediaError, match="cap"):
        media.decode([{"type": "image", "data": _b64(PNG)}])


def test_a_streamed_wav_is_timed_by_the_bytes_it_has():
    """ffmpeg writing to a pipe cannot seek back: its WAV says 0xFFFFFFFF bytes of data."""
    wav = bytearray(_wav(1.0))
    i = wav.index(b"data")
    wav[i + 4:i + 8] = (0xFFFFFFFF).to_bytes(4, "little")
    assert media.wav_seconds(bytes(wav)) == pytest.approx(1.0)


def test_audio_tokens_follow_the_wav_duration_and_fall_back_for_other_formats():
    assert media.wav_seconds(_wav(2.0)) == pytest.approx(2.0)
    assert media.wav_seconds(PNG) is None
    wav = media.decode([{"type": "audio", "data": _b64(_wav(2.0))}])[0]
    assert wav.tokens == 2 * media.AUDIO_TOKENS_PER_S + 8
    mp3 = media.decode([{"type": "audio", "data": _b64(MP3_ID3)}])[0]
    assert mp3.tokens == media.AUDIO_TOKENS_UNKNOWN
    img = media.decode([{"type": "image", "data": _b64(PNG)}])[0]
    assert media.tokens([img, wav]) == media.IMAGE_TOKENS + wav.tokens


# ---------------------------------------------------------------- media.py: storage

def test_store_is_content_addressed_atomic_and_idempotent(media_dir):
    m = media.decode([{"type": "image", "data": _b64(PNG), "filename": "a.png"}])[0]
    p = media.store(m)
    assert p == media_dir / m.sha256[:2] / f"{m.sha256}.png" and p.read_bytes() == PNG
    before = p.stat().st_mtime_ns
    assert media.store(m) == p and p.stat().st_mtime_ns == before, "an existing name is never rewritten"
    assert not list(media_dir.rglob("*.tmp"))
    meta = media.payload_meta(m)
    assert meta == {"type": "image", "sha256": m.sha256, "mime": "image/png", "ext": "png", "bytes": len(PNG),
                    "filename": "a.png"}
    assert media.path_for(meta) == p
    back = media.load(meta)
    assert back.data == PNG and back.type == "image"


def test_concurrent_stores_of_one_file_all_succeed(media_dir):
    import threading
    m = media.decode([{"type": "image", "data": _b64(PNG)}])[0]
    errors, paths = [], []

    def go():
        try:
            paths.append(media.store(m))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=go) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(paths)) == 1 and paths[0].read_bytes() == PNG
    assert not list(media_dir.rglob("*.tmp"))


@pytest.mark.parametrize("meta", [
    {"sha256": "../../etc/passwd", "ext": "png"},
    {"sha256": "A" * 64, "ext": "png"},
    {"sha256": "a" * 64, "ext": "png/../../x"},
    {"sha256": "a" * 64, "ext": "png"},             # well-formed, but no such file
    {},
])
def test_path_for_names_only_existing_files_under_the_media_dir(media_dir, meta):
    assert media.path_for(meta) is None
    assert media.load(meta) is None


def test_a_crafted_payload_cannot_reach_a_file_outside_the_media_dir(tmp_path, monkeypatch):
    """The payload's sha256 builds the path: a 64-character 'sha' that walks up must name nothing, even
    when the file it walks to exists."""
    store = tmp_path / "media" / "store"
    store.mkdir(parents=True)
    monkeypatch.setenv("MEM0_MEDIA_DIR", str(store))
    secret = tmp_path / ("s" * 61 + ".png")
    secret.write_bytes(PNG)
    sha = "../" + "s" * 61                    # media_dir/".."/"../sss.png" == tmp_path/sss.png
    assert len(sha) == 64 and (store / sha[:2] / f"{sha}.png").resolve() == secret.resolve()
    assert media.path_for({"sha256": sha, "ext": "png"}) is None
    assert media.load({"sha256": sha, "ext": "png", "type": "image"}) is None


def test_content_parts_have_the_shapes_llama_server_takes():
    img, wav, vid = media.decode([{"type": "image", "data": _b64(PNG)}, {"type": "audio", "data": _b64(_wav(0.2))},
                                  {"type": "video", "data": _b64(MP4)}])
    assert media.content_part(img) == {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64(PNG)}}
    assert media.content_part(wav) == {"type": "input_audio", "input_audio": {"data": _b64(wav.data), "format": "wav"}}
    assert media.content_part(vid) == {"type": "input_video", "input_video": {"url": "data:video/mp4;base64," + _b64(MP4)}}


# ---------------------------------------------------------------- the MCP shim

SHIM_PATH = Path(__file__).resolve().parents[2] / "scripts" / "wsl" / "mem0-mcp-shim.py"


@pytest.fixture
def shim(monkeypatch, tmp_path):
    from _home_isolation import apply_home
    monkeypatch.setenv("MEM0_URL", "http://authority.invalid:18791")
    apply_home(monkeypatch, tmp_path)
    (tmp_path / ".mem0").mkdir()
    (tmp_path / ".mem0" / "api-key").write_text("test-key", encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("shim_media_ut", SHIM_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        pytest.skip(f"shim import needs fastmcp: {e}")
    return mod


def _fn(tool):
    return getattr(tool, "fn", tool)


def test_shim_reads_windows_paths_and_refuses_unknown_files(shim, tmp_path, monkeypatch):
    if os.path.isdir("/mnt"):
        assert shim._local_path("C:\\Users\\x\\pic.png") == "/mnt/c/Users/x/pic.png"
        assert shim._local_path("D:/data/clip.mp4") == "/mnt/d/data/clip.mp4"
    assert shim._local_path("/home/u/pic.png") == "/home/u/pic.png"
    f = tmp_path / "board.png"
    f.write_bytes(PNG)
    assert shim._read_media([str(f)]) == [{"type": "image", "data": _b64(PNG), "filename": "board.png"}]
    (tmp_path / "notes.txt").write_text("x")
    with pytest.raises(ValueError, match="not a supported media file"):
        shim._read_media([str(tmp_path / "notes.txt")])


def test_shim_never_queues_a_media_memory_offline(shim, tmp_path, monkeypatch):
    monkeypatch.setattr(shim, "OUTBOX", tmp_path / "outbox.jsonl")
    monkeypatch.setattr(shim.httpx, "request", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    f = tmp_path / "board.png"
    f.write_bytes(PNG)
    res = _fn(shim.memory_add)(text="the whiteboard", media_paths=[str(f)])
    assert "not queued" in res["error"]
    assert not (tmp_path / "outbox.jsonl").exists()


def test_shim_sends_media_with_infer_false(shim, tmp_path, monkeypatch):
    sent = {}

    def request(method, url, **kw):
        sent.update(url=url, json=kw.get("json"), timeout=kw.get("timeout"))
        return httpx.Response(200, json={"results": [{"id": "x", "event": "ADD", "media_embedded": True}]},
                              request=httpx.Request(method, url))
    monkeypatch.setattr(shim.httpx, "request", request)
    f = tmp_path / "board.png"
    f.write_bytes(PNG)
    _fn(shim.memory_add)(text="the whiteboard", infer=True, media_paths=[str(f)])
    assert sent["json"]["infer"] is False and sent["json"]["media"][0]["type"] == "image"
    _fn(shim.memory_search)(query="whiteboard", media_paths=[str(f)])
    assert sent["url"].endswith("/v1/memories/search") and sent["json"]["media"][0]["data"] == _b64(PNG)


def test_shim_get_media_writes_the_file(shim, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    mid = str(uuid.uuid4())

    def get(url, headers=None, timeout=None):
        assert url.endswith(f"/v1/memories/{mid}/media/0")
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png",
                              "content-disposition": 'attachment; filename="board.png"'},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(shim.httpx, "get", get)
    out = _fn(shim.memory_get_media)(mid, 0)
    assert out["mime"] == "image/png" and out["bytes"] == len(PNG) and out["source"] == "authority"
    p = Path(out["path"])
    assert p.parent == tmp_path / "ams-media" and p.name == f"{mid}-0.png" and p.read_bytes() == PNG
