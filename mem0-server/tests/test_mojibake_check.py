"""Headless pins for mojibake_check (AMS-10 — audit 2026-08-07).

All mojibake fixtures are assembled by cp437-decoding real UTF-8 byte
sequences — never literal glyphs in source (this file crosses the same
encoding boundaries the detector polices).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mojibake_check import (  # noqa: E402
    contains_mojibake, scan_payloads, mojibake_health,
)


def _corrupt(s: str) -> str:
    """Forward-corrupt: what the broken PS 5.1 boundary did to UTF-8 text."""
    return s.encode("utf-8").decode("cp437")


EN_DASH = chr(0x2013)
ARROW = chr(0x2192)
E_ACUTE = chr(0x00E9)
GEQ = chr(0x2265)
N_TILDE = chr(0x00F1)
I_ACUTE = chr(0x00ED)


def test_detects_corrupted_forms():
    for ch in (EN_DASH, ARROW, E_ACUTE, GEQ):
        corrupted = _corrupt(f"before {ch} after")
        assert contains_mojibake(corrupted), f"missed corruption of U+{ord(ch):04X}"


def test_clean_text_including_real_spanish_is_not_flagged():
    for clean in (
        "plain ascii only",
        f"real accents: caf{E_ACUTE} espa{N_TILDE}ol l{I_ACUTE}nea",
        f"math {GEQ} and dash {EN_DASH} and arrow {ARROW}",
        "",
        None,
    ):
        assert not contains_mojibake(clean), f"false positive on {clean!r}"


def test_repair_roundtrip_is_invisible_to_detector():
    corrupted = _corrupt(f"a {EN_DASH} b {ARROW} c")
    repaired = corrupted.encode("cp437").decode("utf-8")
    assert repaired == f"a {EN_DASH} b {ARROW} c"
    assert not contains_mojibake(repaired)


def test_scan_payloads_counts_and_samples():
    bad = _corrupt(f"x {ARROW} y")
    pts = [
        ("id1", {"data": "clean"}),
        ("id2", {"data": bad}),
        ("id3", {"data": "clean", "text_lemmatized": bad}),
        ("id4", None),
    ]
    out = scan_payloads(iter(pts))
    assert out == {"scanned": 4, "hits": 2, "sample_ids": ["id2", "id3"], "allowlisted": 0}


class _Pt:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload


def test_mojibake_health_pages_and_never_raises():
    bad = _corrupt(f"q {GEQ} r")
    pages = [
        ([_Pt("a", {"data": "ok"}), _Pt("b", {"data": bad})], "next-1"),
        ([_Pt("c", {"data": "ok"})], None),
    ]
    calls = []

    def scroll_fn(offset, limit):
        calls.append(offset)
        return pages[len(calls) - 1]

    out = mojibake_health(scroll_fn, page_size=2, page_cap=50)
    assert out["ok"] is True and out["hits"] == 1 and out["scanned"] == 3
    assert out["sample_ids"] == ["b"] and out["elapsed_ms"] >= 0
    assert calls == [None, "next-1"]


def test_mojibake_health_page_cap_bounds_and_stays_honest():
    def endless(offset, limit):
        return ([_Pt("x", {"data": "ok"})], "again")

    out = mojibake_health(endless, page_size=1, page_cap=3)
    assert out["scanned"] == 3  # capped walk is visible, not hidden


def test_mojibake_health_fail_soft():
    def boom(offset, limit):
        raise RuntimeError("qdrant down")

    out = mojibake_health(boom)
    assert out["ok"] is True  # informational check never flips ok
    assert "error" in out


# ---- WP-4 task 4.9: a per-point allowlist, so a legitimate note that QUOTES mojibake as an example
# does not hold the tripwire at 'degraded' forever (a permanent yellow trains everyone to ignore it,
# and hides the day a real recurrence arrives) ----

def test_flagged_point_is_not_a_hit_but_an_unflagged_twin_still_is():
    bad = _corrupt(f"quoted example {ARROW} here")
    pts = [
        ("legit-note", {"data": bad, "mojibake_ok": True}),
        ("real-hit", {"data": bad}),
    ]
    out = scan_payloads(iter(pts))
    assert out == {"scanned": 2, "hits": 1, "sample_ids": ["real-hit"], "allowlisted": 1}


def test_only_a_literal_true_flag_allowlists():
    """The flag is deliberate: a string, 1 or a falsy value must not exempt a point."""
    bad = _corrupt(f"x {ARROW} y")
    pts = [(f"p{i}", {"data": bad, "mojibake_ok": v}) for i, v in enumerate(["true", 1, False, None, "yes"])]
    out = scan_payloads(iter(pts))
    assert out["hits"] == 5 and out["allowlisted"] == 0


def test_flag_also_covers_the_lemmatized_field():
    bad = _corrupt(f"x {ARROW} y")
    out = scan_payloads(iter([("a", {"data": "clean", "text_lemmatized": bad, "mojibake_ok": True})]))
    assert out["hits"] == 0 and out["allowlisted"] == 1


def test_health_scan_requests_the_flag_field_and_honours_it():
    """The server's scroll must actually FETCH mojibake_ok, or the allowlist is a dead letter."""
    from mojibake_check import PAYLOAD_KEYS
    assert "mojibake_ok" in PAYLOAD_KEYS and "data" in PAYLOAD_KEYS
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert "with_payload=list(PAYLOAD_KEYS)" in src, "app.py must scroll the allowlist key too"
    bad = _corrupt(f"q {GEQ} r")
    out = mojibake_health(lambda off, lim: ([_Pt("a", {"data": bad, "mojibake_ok": True})], None))
    assert out["hits"] == 0 and out["allowlisted"] == 1 and out["scanned"] == 1
