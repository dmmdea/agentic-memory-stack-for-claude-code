"""AMS-10 (audit 2026-08-07): CP437 mojibake detector + /health/deep counter.

A PowerShell 5.1 capture boundary decoded UTF-8 subprocess output with the
OEM console codepage (CP437), storing mojibake glyph pairs in memory text
(e.g. an en dash became the two glyphs produced by cp437-decoding bytes
0xE2 0x80). The boundary is fixed at the source; this module is the
permanent tripwire that the corpus stays clean.

The detector matches a character from the set produced by cp437-decoding a
UTF-8 LEAD byte (0xC2-0xF4) immediately followed by one from the set
produced by cp437-decoding a CONTINUATION byte (0x80-0xBF). Zero false
positives were observed across the full corpus during calibration: real
accented text cannot be followed by a cp437-decoded continuation glyph.

Both character classes are built PROGRAMMATICALLY from byte ranges — never
literal non-ASCII characters in source. This file itself crosses the same
encoding boundaries it polices; a literal glyph here would be mangled by the
exact defect class being detected (this happened twice during the audit).
"""

import re

_LEAD_CHARS = bytes(range(0xC2, 0xF5)).decode("cp437")
_CONT_CHARS = bytes(range(0x80, 0xC0)).decode("cp437")

# One lead glyph followed by one continuation glyph = CP437-decoded UTF-8.
MOJIBAKE_RE = re.compile(
    "[" + re.escape(_LEAD_CHARS) + "][" + re.escape(_CONT_CHARS) + "]"
)


def contains_mojibake(text):
    """True when the CP437 glyph-pair signature is present."""
    return bool(text) and MOJIBAKE_RE.search(text) is not None


def scan_payloads(payload_iter, text_keys=("data", "text_lemmatized"), max_samples=5):
    """Pure scan over an iterable of ``(point_id, payload_dict)``.

    Returns ``{"scanned": int, "hits": int, "sample_ids": [...]}`` — the
    /health/deep shape minus timing, so it unit-tests headless.
    """
    scanned = 0
    hits = 0
    samples = []
    for pid, payload in payload_iter:
        scanned += 1
        p = payload or {}
        for key in text_keys:
            if contains_mojibake(p.get(key) or ""):
                hits += 1
                if len(samples) < max_samples:
                    samples.append(str(pid))
                break
    return {"scanned": scanned, "hits": hits, "sample_ids": samples}


def mojibake_health(scroll_fn, page_size=1000, page_cap=50):
    """Informational /health/deep check: never raises, never flips ok.

    ``scroll_fn(offset, limit)`` must return ``(points, next_offset)`` where
    each point has ``.id`` and ``.payload`` (qdrant-client scroll shape).
    ``page_cap`` bounds the walk as the corpus grows (cap sized corpus+headroom
    per review F12); ``scanned`` stays honest so a capped scan is visible.
    """
    import time as _time
    started = _time.monotonic()
    out = {"ok": True, "scanned": 0, "hits": 0, "sample_ids": [], "elapsed_ms": 0}
    try:
        offset = None
        pages = 0
        def _iter():
            nonlocal offset, pages
            while pages < page_cap:
                points, nxt = scroll_fn(offset, page_size)
                pages += 1
                for pt in points:
                    yield pt.id, (pt.payload or {})
                if nxt is None:
                    return
                offset = nxt
        res = scan_payloads(_iter())
        out.update(res)
    except Exception as e:  # fail-soft: a probe bug must not 500 /health/deep
        out["error"] = str(e)[:120]
    out["elapsed_ms"] = int((_time.monotonic() - started) * 1000)
    return out
