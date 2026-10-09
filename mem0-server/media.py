"""media — images, audio and video as memories, in the embedding space the text lives in.

EmbeddingGemma-2 maps text, images, audio and video into one 768-d space (embedder_profile: media=True,
served with its --mmproj projector). A media memory is a normal memory whose text is a caption the
caller writes ("the whiteboard after the 10-08 planning session"), plus one or more media items:

  * the caption goes through mem0's own add, so the memory keeps everything a text memory has (id,
    hash, history, the BM25 leg, entities, tiers, admission);
  * the dense vector is then replaced by ONE interleaved embedding of the caption (document prefix)
    followed by the media parts, so a text query finds the memory by what the media shows or says
    as well as by the caption, and a media query (search with media) finds it too;
  * the bytes are stored content-addressed under MEDIA_DIR (~/.mem0/media/<sha[:2]>/<sha>.<ext>)
    and the payload records {type, sha256, mime, bytes, filename} per item, so the stack backup
    carries them and GET /v1/memories/{id}/media/{n} returns them.

If the multimodal embed fails (projector not loaded, input too long for the ubatch), the memory keeps
its caption-only vector and the payload says media_embedded=false: a write is never lost to the media.

Types are decided by the bytes, not by what the caller declares: a mismatch is a 400.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

MEDIA_TYPES = ("image", "audio", "video")
MAX_ITEMS = int(os.environ.get("MEM0_MEDIA_MAX_ITEMS", "4"))
MAX_ITEM_BYTES = int(os.environ.get("MEM0_MEDIA_MAX_BYTES", str(20 * 1024 * 1024)))
# What each item costs in the embedder's one-ubatch window (EmbeddingGemma-2 card: an image is 280
# tokens, video 140 per frame, audio 25 per second). The text budget for an interleaved embed is the
# profile's budget minus these, so caption + media fit the --ubatch-size the alias is served with.
IMAGE_TOKENS = 280
VIDEO_TOKENS = 600          # a short clip: the server samples a few frames
AUDIO_TOKENS_PER_S = 25
AUDIO_TOKENS_UNKNOWN = 750  # 30 s, when the duration cannot be read from the header


def media_dir() -> Path:
    return Path(os.environ.get("MEM0_MEDIA_DIR") or (Path.home() / ".mem0" / "media"))


class MediaError(ValueError):
    """A caller error: bad base64, unknown or mismatched type, too large, too many. Maps to HTTP 400."""


@dataclass(frozen=True)
class Media:
    type: str        # image | audio | video
    mime: str
    ext: str
    data: bytes
    filename: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def tokens(self) -> int:
        if self.type == "image":
            return IMAGE_TOKENS
        if self.type == "video":
            return VIDEO_TOKENS
        secs = wav_seconds(self.data) if self.ext == "wav" else None
        return int(secs * AUDIO_TOKENS_PER_S) + 8 if secs is not None else AUDIO_TOKENS_UNKNOWN


def sniff(data: bytes) -> tuple[str, str, str] | None:
    """(type, mime, ext) from the magic bytes, or None."""
    h = data[:16]
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png", "png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg", "jpg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif", "gif"
    if h.startswith(b"RIFF") and h[8:12] == b"WEBP":
        return "image", "image/webp", "webp"
    if h.startswith(b"BM"):
        return "image", "image/bmp", "bmp"
    if h.startswith(b"RIFF") and h[8:12] == b"WAVE":
        return "audio", "audio/wav", "wav"
    if h.startswith(b"fLaC"):
        return "audio", "audio/flac", "flac"
    if h.startswith(b"ID3") or (len(h) > 1 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0):
        return "audio", "audio/mpeg", "mp3"
    if h[4:8] == b"ftyp":
        return "video", "video/mp4", "mp4"
    if h.startswith(b"\x1aE\xdf\xa3"):
        return "video", "video/webm", "webm"
    return None


def wav_seconds(data: bytes) -> float | None:
    """Duration of a PCM WAV from its header (fmt + data chunks), or None."""
    try:
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return None
        i, byte_rate, data_len = 12, None, None
        while i + 8 <= len(data) and (byte_rate is None or data_len is None):
            cid, size = data[i:i + 4], int.from_bytes(data[i + 4:i + 8], "little")
            if cid == b"fmt ":
                byte_rate = int.from_bytes(data[i + 16:i + 20], "little")
            elif cid == b"data":
                # a streamed WAV (an ffmpeg pipe) writes 0xFFFFFFFF here: trust the bytes that are there
                data_len = min(size, max(0, len(data) - (i + 8)))
            i += 8 + size + (size & 1)
        if byte_rate and data_len is not None:
            return data_len / byte_rate
    except Exception:  # noqa: BLE001 - a header we cannot read is "unknown", never an error
        return None
    return None


def decode(items) -> list[Media]:
    """Validate and decode request items ({type, data(base64), filename?}) -> Media. Raises MediaError."""
    if not items:
        return []
    if not isinstance(items, list):
        raise MediaError("media must be a list of {type, data, filename?}")
    if len(items) > MAX_ITEMS:
        raise MediaError(f"at most {MAX_ITEMS} media items per memory (got {len(items)})")
    out = []
    for n, it in enumerate(items):
        if hasattr(it, "model_dump"):
            it = it.model_dump()
        if not isinstance(it, dict):
            raise MediaError(f"media[{n}] is not an object")
        declared = str(it.get("type") or "").strip().lower()
        if declared not in MEDIA_TYPES:
            raise MediaError(f"media[{n}].type must be one of {', '.join(MEDIA_TYPES)}")
        raw = it.get("data") or ""
        if isinstance(raw, str) and raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        # refuse an oversized item from its base64 length, before decoding it (4 chars carry 3 bytes)
        if isinstance(raw, (str, bytes)) and len(raw) > (MAX_ITEM_BYTES + 2) // 3 * 4 + 4:
            raise MediaError(f"media[{n}] is larger than the cap of {MAX_ITEM_BYTES} bytes (MEM0_MEDIA_MAX_BYTES)")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise MediaError(f"media[{n}].data is not valid base64") from None
        if not data:
            raise MediaError(f"media[{n}] is empty")
        if len(data) > MAX_ITEM_BYTES:
            raise MediaError(f"media[{n}] is {len(data)} bytes; the cap is {MAX_ITEM_BYTES} (MEM0_MEDIA_MAX_BYTES)")
        found = sniff(data)
        if found is None:
            raise MediaError(f"media[{n}]: unrecognised format (images png/jpeg/gif/webp/bmp, audio wav/mp3/flac, video mp4/webm)")
        kind, mime, ext = found
        if kind != declared:
            raise MediaError(f"media[{n}] is declared {declared} but its bytes are {kind} ({mime})")
        fn = it.get("filename")
        out.append(Media(type=kind, mime=mime, ext=ext, data=data,
                         filename=(os.path.basename(str(fn))[:200] if fn else None)))
    return out


def store(m: Media) -> Path:
    """Write the bytes content-addressed (idempotent; atomic rename). Returns the path."""
    d = media_dir() / m.sha256[:2]
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{m.sha256}.{m.ext}"
    if not p.exists():
        # a unique temporary name: two adds of the same file at once both succeed (same bytes, same name)
        tmp = d / f".{m.sha256}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(m.data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        finally:
            if tmp.exists():
                tmp.unlink()
    return p


def path_for(meta: dict) -> Path | None:
    """The stored file for a payload media entry, or None when it is missing."""
    sha, ext = str(meta.get("sha256") or ""), str(meta.get("ext") or "")
    if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha) or not ext.isalnum():
        return None
    p = media_dir() / sha[:2] / f"{sha}.{ext}"
    return p if p.is_file() else None


def load(meta: dict) -> Media | None:
    p = path_for(meta)
    if p is None:
        return None
    return Media(type=meta.get("type"), mime=meta.get("mime"), ext=meta.get("ext"), data=p.read_bytes(),
                 filename=meta.get("filename"))


def payload_meta(m: Media) -> dict:
    return {"type": m.type, "sha256": m.sha256, "mime": m.mime, "ext": m.ext, "bytes": len(m.data),
            "filename": m.filename}


def content_part(m: Media) -> dict:
    """The OpenAI-style content part llama-server's /v1/embeddings takes for this item (no prefix)."""
    b64 = base64.b64encode(m.data).decode("ascii")
    if m.type == "image":
        return {"type": "image_url", "image_url": {"url": f"data:{m.mime};base64,{b64}"}}
    if m.type == "audio":
        return {"type": "input_audio", "input_audio": {"data": b64, "format": m.ext}}
    return {"type": "input_video", "input_video": {"url": f"data:{m.mime};base64,{b64}"}}


def tokens(items: list[Media]) -> int:
    return sum(m.tokens for m in items)
