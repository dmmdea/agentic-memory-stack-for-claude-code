"""Supersession: the one door that hides a fact behind a newer one (1.32.4).

POST /v1/memories/{id}/supersede is the only writer of `superseded_by`. Before it, a session that
learned a fact had gone stale had no door: it appended "SUPERSEDED <date> by mem0 <id>: ..." to the
text with memory_update, and the admission gate never reads text, so the stale fact kept surfacing
in default searches. The old metadata-PATCH writer (the actor string "supersession-resolve-v030")
is gone: an actor string is not a credential, and it could hide any record, canonical included.

Two scopes:
- full: the record as a whole is replaced by the winner. Sets `superseded_by` (the admission gate
  hides the record outside the history class), `superseded_at` and `superseded_via`.
- partial: one claim inside the record is out of date and the rest still stands. Appends
  {winner_id, detail, at} to `partially_superseded_by`, which the gate does NOT read: a partial
  supersession never hides a record.

Pure and qdrant-free, like admission_gate: the endpoint reads both payloads and calls precheck();
contradiction-sweep.py (--resolve-supersede, --supersede-markers) reaches the same rules through
the endpoint. The marker parser lets the server and the sweep recognise the hand-written markers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

SCOPES = ("full", "partial")
CLEAR_SCOPES = ("full", "partial", "all")

# The ledger actor for every write through the door. The server stamps it; a body never sets it.
ENDPOINT_ACTOR = "supersede-endpoint"

# A record of these tiers is never hidden or annotated through this door: a canonical changes only
# through the operator's signed path, an insight only through the consolidator or a signed token.
# A record with NO tier counts as canonical (fail-closed, as security_invariants.fetch_current_tier).
PROTECTED_TIERS = frozenset({"canonical", "insight"})

DETAIL_MAX_CHARS = 300
SOURCE_MAX_CHARS = 64
PARTIAL_MAX_ENTRIES = 20

# Payload keys this door owns. add() strips them and PATCH /metadata refuses them.
SUPERSEDE_KEYS = frozenset({"superseded_by", "superseded_at", "superseded_via",
                            "partially_superseded_by"})
_FULL_KEYS = ("superseded_by", "superseded_at", "superseded_via")
_PARTIAL_KEYS = ("partially_superseded_by",)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_RE = re.compile(rf"^{_UUID}$")
_SOURCE_RE = re.compile(r"[^A-Za-z0-9._:-]+")


@dataclass(frozen=True)
class Refusal:
    status: int     # the HTTP status the endpoint returns
    code: str       # stable machine-readable reason
    message: str    # what to do instead


def is_memory_id(value) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value.strip()))


def clean_source(value: Optional[str]) -> Optional[str]:
    """The caller's free-text label for audit (e.g. 'memory_supersede'); never authorises anything."""
    if not value:
        return None
    cleaned = _SOURCE_RE.sub("-", str(value)).strip("-")[:SOURCE_MAX_CHARS]
    return cleaned or None


def _tier(payload: dict) -> Optional[str]:
    return payload.get("tier")


def _retired(payload: dict) -> bool:
    return payload.get("retrievable") is False


def _brand(payload: dict, shared_brands: Iterable[str]) -> str:
    brand = str(payload.get("brand") or "").strip().lower()
    return "" if brand in {str(b).strip().lower() for b in shared_brands} else brand


def _partials(payload: dict) -> list:
    entries = payload.get("partially_superseded_by")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def precheck(loser_id: str, winner_id: str, loser: Optional[dict], winner: Optional[dict],
             scope: str = "full", detail: Optional[str] = None,
             shared_brands: Iterable[str] = ()) -> Optional[Refusal]:
    """The refusal matrix. None means the write may proceed (or is an idempotent no-op)."""
    if scope not in SCOPES:
        return Refusal(400, "bad-scope", f"scope must be one of {list(SCOPES)}")
    if not is_memory_id(loser_id) or not is_memory_id(winner_id):
        return Refusal(400, "bad-id", "both ids must be memory ids (UUIDs)")
    if loser_id.strip().lower() == winner_id.strip().lower():
        return Refusal(400, "self", "a record cannot supersede itself")
    if scope == "partial":
        if not isinstance(detail, str) or not detail.strip():
            return Refusal(400, "detail-required",
                           "a partial supersession must say which claim is out of date (detail)")
        if len(detail.strip()) > DETAIL_MAX_CHARS:
            return Refusal(400, "detail-too-long", f"detail is limited to {DETAIL_MAX_CHARS} characters")
    if loser is None:
        return Refusal(404, "loser-not-found", f"memory {loser_id} not found")
    if winner is None:
        return Refusal(404, "winner-not-found", f"winner {winner_id} not found")
    tier = _tier(loser)
    if tier is None or tier == "canonical":
        return Refusal(403, "loser-canonical",
                       "a canonical record leaves default retrieval only through the operator's "
                       "signed path (demote it first: mem0-canonize.sh --action demote)")
    if tier in PROTECTED_TIERS:
        return Refusal(403, f"loser-{tier}",
                       f"a {tier} record is changed only by its consolidator or with a signed token")
    if _retired(loser):
        return Refusal(409, "loser-retired", f"memory {loser_id} is retired")
    if _retired(winner):
        return Refusal(409, "winner-retired", f"winner {winner_id} is retired")
    if winner.get("superseded_by"):
        return Refusal(409, "winner-superseded",
                       f"winner {winner_id} is itself superseded by {winner.get('superseded_by')}: "
                       "point at the newest record")
    if str(loser.get("user_id") or "") != str(winner.get("user_id") or ""):
        return Refusal(403, "cross-tenant", "loser and winner belong to different users")
    lb, wb = _brand(loser, shared_brands), _brand(winner, shared_brands)
    if lb and wb and lb != wb:
        return Refusal(403, "cross-brand",
                       "loser and winner carry different brands; a fact never supersedes across brands")
    existing = loser.get("superseded_by")
    if existing and str(existing).strip().lower() != winner_id.strip().lower():
        return Refusal(409, "already-superseded",
                       f"memory {loser_id} is already superseded by {existing}; clear it first "
                       "(DELETE /v1/memories/{id}/supersede) to change the winner")
    if scope == "partial" and existing:
        return Refusal(409, "already-superseded",
                       f"memory {loser_id} is already fully superseded by {existing}; "
                       "a partial note adds nothing")
    return None


def is_noop(loser: dict, winner_id: str, scope: str, detail: Optional[str] = None) -> bool:
    """True when the same supersession is already recorded (a repeated call changes nothing)."""
    wid = winner_id.strip().lower()
    if scope == "full":
        return str(loser.get("superseded_by") or "").strip().lower() == wid
    want = (detail or "").strip()
    return any(str(e.get("winner_id") or "").strip().lower() == wid
               and str(e.get("detail") or "").strip() == want for e in _partials(loser))


def full_payload(winner_id: str, now_iso: str, source: Optional[str] = None) -> dict:
    return {"superseded_by": winner_id.strip(), "superseded_at": now_iso,
            "superseded_via": clean_source(source) or ENDPOINT_ACTOR}


def partial_payload(loser: dict, winner_id: str, detail: str, now_iso: str) -> dict:
    """The record's partial list with one entry appended (bounded, oldest dropped first)."""
    entry = {"winner_id": winner_id.strip(), "detail": detail.strip(), "at": now_iso}
    entries = _partials(loser)[-(PARTIAL_MAX_ENTRIES - 1):] + [entry]
    return {"partially_superseded_by": entries}


def clear_keys(scope: str) -> tuple:
    if scope == "full":
        return _FULL_KEYS
    if scope == "partial":
        return _PARTIAL_KEYS
    if scope == "all":
        return _FULL_KEYS + _PARTIAL_KEYS
    raise ValueError(f"scope must be one of {list(CLEAR_SCOPES)}")


def clear_precheck(loser_id: str, loser: Optional[dict], scope: str) -> Optional[Refusal]:
    if scope not in CLEAR_SCOPES:
        return Refusal(400, "bad-scope", f"scope must be one of {list(CLEAR_SCOPES)}")
    if not is_memory_id(loser_id):
        return Refusal(400, "bad-id", "the id must be a memory id (UUID)")
    if loser is None:
        return Refusal(404, "loser-not-found", f"memory {loser_id} not found")
    tier = _tier(loser)
    if tier is None or tier in PROTECTED_TIERS:
        return Refusal(403, f"loser-{tier or 'canonical'}",
                       "a canonical or insight record is changed only through its signed path")
    return None


def has_supersession(loser: dict, scope: str) -> bool:
    return any(loser.get(k) not in (None, "", []) for k in clear_keys(scope))


# ---- hand-written text markers ------------------------------------------------------------------
#
# The shape sessions used:  "SUPERSEDED 2026-09-30 by mem0 <uuid>: <why>"            -> full
#                           "SUPERSEDED 2026-09-30 by mem0 <uuid> (the 'X' figure only): ..." -> partial
# A marker must open a line or a sentence; anything else is a mention ("this was superseded by mem0
# <uuid> in August") and is never acted on. Anything ambiguous is PARTIAL, which never hides a
# record: the house default for an uncertain hide is to keep the record visible.

_MARKER_RE = re.compile(
    rf"(?P<pre>\b(?:partially|partly)\s+)?\bSUPERSEDED\b(?P<between>[^\n:]{{0,40}}?)"
    rf"\bby\s+(?:mem0\s+)?(?:(?:record|id|memory)\s+)?(?P<uuid>{_UUID})",
    re.IGNORECASE,
)
_PARTIAL_WORDS = re.compile(
    r"\b(only|partial|partially|partly|in part|figure|figures|except|just|portion|claim|line|"
    r"field|number|value)\b", re.IGNORECASE)
_QUALIFIER_END = re.compile(r":|\.\s|\.$|\n")


@dataclass(frozen=True)
class Marker:
    kind: str                 # "full" | "partial" | "mention" | "no-target"
    winner_id: Optional[str]
    start: int
    qualifier: str


def _anchored(text: str, start: int) -> bool:
    """The marker opens the text, a line, or a sentence (allowing bullets and emphasis)."""
    before = text[:start].rstrip(" \t*_-#>")
    return before == "" or before[-1] in "\n.!?;"


def find_markers(text: Optional[str]) -> list:
    if not text:
        return []
    out = []
    for m in _MARKER_RE.finditer(text):
        rest = text[m.end():m.end() + 160]
        end = _QUALIFIER_END.search(rest)
        qualifier = (rest[:end.start()] if end else rest).strip()
        if not _anchored(text, m.start()):
            kind = "mention"
        elif m.group("pre") or "(" in qualifier or _PARTIAL_WORDS.search(qualifier):
            kind = "partial"
        elif qualifier:
            kind = "partial"      # an unrecognised qualifier: ambiguous, so it never hides
        else:
            kind = "full"
        out.append(Marker(kind, m.group("uuid").lower(), m.start(), qualifier))
    return out


def classify_text(text: Optional[str]) -> Optional[Marker]:
    """The decisive marker in a record's text, or None when it carries none.

    Any anchored partial marker makes the record partial (part of it still stands); otherwise the
    LAST anchored full marker decides; otherwise a mention; otherwise an upper-case SUPERSEDED with
    no memory id is "no-target".
    """
    markers = find_markers(text)
    anchored = [m for m in markers if m.kind != "mention"]
    partial = [m for m in anchored if m.kind == "partial"]
    if partial:
        return partial[-1]
    if anchored:
        return anchored[-1]
    if markers:
        return markers[-1]
    if text and "SUPERSEDED" in text:
        return Marker("no-target", None, text.index("SUPERSEDED"), "")
    return None


SUPERSEDE_NOTE = (
    "This text carries a 'SUPERSEDED ... by mem0 <id>' marker, which the search filter does not "
    "read, so the record still surfaces in default searches. Record the supersession with "
    "memory_supersede (scope='full' hides this record behind the newer one; scope='partial' with a "
    "detail annotates one stale claim and keeps it visible) instead of appending a marker by hand."
)
