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

Qdrant-free, like admission_gate: precheck() is the refusal matrix, run_supersede() and
run_unsupersede() are the whole write transaction over a small store interface (read / set_payload /
delete_keys) so the endpoint is a thin adapter and the transaction is testable headless.
contradiction-sweep.py (--resolve-supersede, --supersede-markers) reaches the same rules through the
endpoint. The marker parser lets the server and the sweep recognise the hand-written markers.

Related rules elsewhere: the cascade delete never deletes a protected record through a supersession
link (cascade_protected), and PATCH /tier never promotes a superseded record into a protected tier
(promotion_refusal).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

SCOPES = ("full", "partial")
CLEAR_SCOPES = ("full", "partial", "all")

# The ledger actor and the stored `superseded_via` for every write through the door. The server
# stamps both; a request never sets them. A caller's own label goes to the ledger line only, as
# `source`, and is caller-declared: it describes, it never proves.
ENDPOINT_ACTOR = "supersede-endpoint"

# A record of these tiers is never hidden or annotated through this door: a canonical changes only
# through the operator's signed path, an insight only through the consolidator or a signed token.
# A record with NO tier counts as canonical (fail-closed, as security_invariants.fetch_current_tier).
PROTECTED_TIERS = frozenset({"canonical", "insight"})

DETAIL_MAX_CHARS = 300
REASON_MAX_CHARS = 500
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


class Refused(Exception):
    """run_supersede / run_unsupersede refused the request; .refusal says why."""

    def __init__(self, refusal: Refusal):
        super().__init__(f"{refusal.code}: {refusal.message}")
        self.refusal = refusal


class LedgerUnavailable(Exception):
    """The write-ahead intent line could not be appended, so nothing was written."""


def is_memory_id(value) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value.strip()))


def _norm_id(value: str) -> str:
    """Memory ids are stored lower-case so the exact-match cascade scroll and the gate agree."""
    return value.strip().lower()


def clean_source(value: Optional[str]) -> Optional[str]:
    """The caller's free-text label for audit (e.g. 'memory_supersede'); never authorises anything."""
    if not value:
        return None
    cleaned = _SOURCE_RE.sub("-", str(value)).strip("-")[:SOURCE_MAX_CHARS]
    return cleaned or None


def _tier(payload: dict) -> Optional[str]:
    return payload.get("tier")


def _retired(payload: dict) -> bool:
    return payload.get("retrievable") is False or bool(payload.get("retired_at"))


def _brand(payload: dict, shared_brands: Iterable[str]) -> str:
    brand = str(payload.get("brand") or "").strip().lower()
    return "" if brand in {str(b).strip().lower() for b in shared_brands} else brand


def _partials(payload: dict) -> list:
    entries = payload.get("partially_superseded_by")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _too_long(value: Optional[str], cap: int) -> bool:
    return isinstance(value, str) and len(value.strip()) > cap


def precheck(loser_id: str, winner_id: str, loser: Optional[dict], winner: Optional[dict],
             scope: str = "full", detail: Optional[str] = None,
             shared_brands: Iterable[str] = (), reason: Optional[str] = None) -> Optional[Refusal]:
    """The refusal matrix. None means the write may proceed (or is an idempotent no-op)."""
    if scope not in SCOPES:
        return Refusal(400, "bad-scope", f"scope must be one of {list(SCOPES)}")
    if not is_memory_id(loser_id) or not is_memory_id(winner_id):
        return Refusal(400, "bad-id", "both ids must be memory ids (UUIDs)")
    if _norm_id(loser_id) == _norm_id(winner_id):
        return Refusal(400, "self", "a record cannot supersede itself")
    if scope == "partial" and (not isinstance(detail, str) or not detail.strip()):
        return Refusal(400, "detail-required",
                       "a partial supersession must say which claim is out of date (detail)")
    if _too_long(detail, DETAIL_MAX_CHARS):
        return Refusal(400, "detail-too-long", f"detail is limited to {DETAIL_MAX_CHARS} characters")
    if _too_long(reason, REASON_MAX_CHARS):
        return Refusal(400, "reason-too-long", f"reason is limited to {REASON_MAX_CHARS} characters")
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
    if wb and not lb:
        # A neutral (or shared-brand) fact is visible in every scope; a branded winner only in its
        # own. Hiding the neutral fact behind it would erase the fact for every other brand.
        return Refusal(403, "cross-brand",
                       "a brand-neutral record cannot be superseded by a branded one: the fact would "
                       "disappear for every other brand; supersede it with a neutral record")
    existing = loser.get("superseded_by")
    if existing and _norm_id(str(existing)) != _norm_id(winner_id):
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
    wid = _norm_id(winner_id)
    if scope == "full":
        return _norm_id(str(loser.get("superseded_by") or "")) == wid
    want = (detail or "").strip()
    return any(_norm_id(str(e.get("winner_id") or "")) == wid
               and str(e.get("detail") or "").strip() == want for e in _partials(loser))


def full_payload(winner_id: str, now_iso: str) -> dict:
    return {"superseded_by": _norm_id(winner_id), "superseded_at": now_iso,
            "superseded_via": ENDPOINT_ACTOR}


def partial_payload(loser: dict, winner_id: str, detail: str, now_iso: str) -> dict:
    """The record's partial list with one entry appended (bounded, oldest dropped first)."""
    entry = {"winner_id": _norm_id(winner_id), "detail": detail.strip(), "at": now_iso}
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


def clear_precheck(loser_id: str, loser: Optional[dict], scope: str,
                   reason: Optional[str] = None) -> Optional[Refusal]:
    if scope not in CLEAR_SCOPES:
        return Refusal(400, "bad-scope", f"scope must be one of {list(CLEAR_SCOPES)}")
    if not is_memory_id(loser_id):
        return Refusal(400, "bad-id", "the id must be a memory id (UUID)")
    if _too_long(reason, REASON_MAX_CHARS):
        return Refusal(400, "reason-too-long", f"reason is limited to {REASON_MAX_CHARS} characters")
    if loser is None:
        return Refusal(404, "loser-not-found", f"memory {loser_id} not found")
    tier = _tier(loser)
    if tier is None or tier in PROTECTED_TIERS:
        return Refusal(403, f"loser-{tier or 'canonical'}",
                       "a canonical or insight record is changed only through its signed path")
    return None


def has_supersession(loser: dict, scope: str) -> bool:
    return any(loser.get(k) not in (None, "", []) for k in clear_keys(scope))


# ---- the write transaction (the endpoint is a thin adapter over these) --------------------------

def run_supersede(store, ledger: Callable[[dict], None], *, mid: str, winner_id: str,
                  scope: str = "full", detail: Optional[str] = None, reason: Optional[str] = None,
                  source: Optional[str] = None, shared_brands: Iterable[str] = (),
                  now_iso: str) -> dict:
    """Read both records, run the matrix, append the intent line, write. The caller holds both locks.

    store: .read(id) -> payload dict or None (raises on a store error); .set_payload(id, dict).
    ledger: appends one dict to the audit ledger (raises on failure).
    Raises Refused (with the HTTP status), LedgerUnavailable (nothing written), or whatever the
    store raises. Returns the response body plus "_entry", the completion ledger line.
    """
    loser = store.read(_norm_id(mid)) if is_memory_id(mid) else None
    winner = store.read(_norm_id(winner_id)) if is_memory_id(winner_id) else None
    refusal = precheck(mid, winner_id, loser, winner, scope=scope, detail=detail,
                       shared_brands=shared_brands, reason=reason)
    if refusal:
        raise Refused(refusal)
    body = {"ok": True, "memory_id": mid, "winner_id": _norm_id(winner_id), "scope": scope}
    if is_noop(loser, winner_id, scope, detail):
        return {**body, "noop": True, "hidden": bool(loser.get("superseded_by"))}
    if scope == "full":
        payload = full_payload(winner_id, now_iso)
    else:
        payload = partial_payload(loser, winner_id, detail, now_iso)
    payload["updated_at"] = now_iso
    entry = {"event": "supersede", "memory_id": mid, "winner_id": _norm_id(winner_id),
             "scope": scope, "detail": (detail or "").strip() or None, "actor": ENDPOINT_ACTOR,
             "source": clean_source(source), "reason": (reason or "").strip() or None,
             "prior_tier": loser.get("tier"), "transport": "rest-api"}
    # Write-ahead audit (the AMS-22 pattern): no record is hidden without a ledger line first.
    try:
        ledger({**entry, "event": "supersede-intent", "status": "intent"})
    except Exception as e:  # noqa: BLE001 - any append failure refuses the write
        raise LedgerUnavailable(str(e)) from e
    store.set_payload(_norm_id(mid), payload)
    return {**body, "noop": False, "hidden": scope == "full", "_entry": entry}


def run_unsupersede(store, ledger: Callable[[dict], None], *, mid: str, scope: str = "full",
                    reason: Optional[str] = None, now_iso: str) -> dict:
    """Undo a supersession. store: .read(id), .delete_keys(id, keys), .set_payload(id, dict)."""
    if not is_memory_id(mid):
        raise Refused(Refusal(400, "bad-id", "the id must be a memory id (UUID)"))
    loser = store.read(_norm_id(mid))
    refusal = clear_precheck(mid, loser, scope, reason=reason)
    if refusal:
        raise Refused(refusal)
    body = {"ok": True, "memory_id": mid, "scope": scope}
    if not has_supersession(loser, scope):
        return {**body, "noop": True}
    keys = list(clear_keys(scope))
    entry = {"event": "unsupersede", "memory_id": mid, "scope": scope,
             "cleared": {k: loser.get(k) for k in keys if loser.get(k) not in (None, "", [])},
             "actor": ENDPOINT_ACTOR, "reason": (reason or "").strip() or None,
             "prior_tier": loser.get("tier"), "transport": "rest-api"}
    try:
        ledger({**entry, "event": "unsupersede-intent", "status": "intent"})
    except Exception as e:  # noqa: BLE001
        raise LedgerUnavailable(str(e)) from e
    store.delete_keys(_norm_id(mid), keys)
    store.set_payload(_norm_id(mid), {"updated_at": now_iso})
    return {**body, "noop": False, "_entry": entry}


# ---- rules the other write paths share ------------------------------------------------------------

def cascade_protected(payload: Optional[dict]) -> bool:
    """A cascade delete (DELETE ...?cascade=true) walks superseded_by links that any API-key holder can
    now create. It must never delete a protected record through such a link, nor one it cannot read."""
    if payload is None:
        return True
    tier = _tier(payload)
    return tier is None or tier in PROTECTED_TIERS


def promotion_refusal(payload: Optional[dict], target_tier: str) -> Optional[Refusal]:
    """PATCH /tier into a protected tier refuses a record that is still superseded: it would be a
    hidden canonical, and the supersession link would expose it to an unsigned cascade delete."""
    if target_tier in PROTECTED_TIERS and payload and payload.get("superseded_by"):
        return Refusal(409, "superseded-record",
                       f"the record is superseded by {payload.get('superseded_by')}; clear it first "
                       "(DELETE /v1/memories/{id}/supersede) before moving it into the "
                       f"{target_tier} tier")
    return None


# ---- hand-written text markers ------------------------------------------------------------------
#
# The shape sessions used:  "SUPERSEDED 2026-09-30 by mem0 <uuid>: <why>"                      -> full
#                           "SUPERSEDED 2026-09-30 by mem0 <uuid> (the 'X' figure only): ..."  -> partial
# FULL is the narrow case: the marker opens a line or a sentence, nothing but an optional date sits
# between SUPERSEDED and "by", nothing sits between the id and the colon or the sentence end, and the
# reason that follows (up to 300 characters, across lines) carries no scope-limiting cue. Everything
# else that opens a line or sentence is PARTIAL, which never hides a record: the house default for
# an uncertain hide is to keep the record visible. A marker inside a sentence is a mention and is
# never acted on.

_MARKER_RE = re.compile(
    rf"(?P<pre>\b(?:partially|partly)\s+)?\bSUPERSEDED\b(?P<between>[^\n]{{0,60}}?)"
    rf"\bby\s+(?:mem0\s+)?(?:(?:record|id|memory)\s+)?(?P<uuid>{_UUID})",
    re.IGNORECASE,
)
_DATE_ONLY = re.compile(
    r"^\s*(?:on\s+)?\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?\s*$", re.IGNORECASE)
_QUALIFIER_END = re.compile(r":|\.\s|\.$|\n")
_PARTIAL_CUES = re.compile(
    r"\b(only|partial|partially|partly|in part|except|excepting|figures?|portion|"
    r"still (?:holds?|stands?|valid|true|current|correct|applies)|"
    r"rest (?:of (?:it|the record|the fact) )?(?:still|is|stays|remains)|"
    r"remains? (?:valid|true|current|correct)|"
    r"revert(?:ed|s)?|current again|no longer superseded|un-?superseded)\b",
    re.IGNORECASE)
_BROADENING = re.compile(r"\bnot only\b", re.IGNORECASE)
_REASON_WINDOW = 300
_ANCHOR_STRIP = " \t*_#>"
_ANCHOR_CHARS = "\n.!?;:([|\u2014\u2013"


@dataclass(frozen=True)
class Marker:
    kind: str                 # "full" | "partial" | "mention" | "no-target"
    winner_id: Optional[str]
    start: int
    qualifier: str


def _anchored(text: str, start: int) -> bool:
    """The marker opens the text, a line, a sentence, a bracket, or follows a dash or a pipe (allowing
    bullets and emphasis). A hyphen counts only standing alone (" - "), never inside a word."""
    before = text[:start].rstrip(_ANCHOR_STRIP)
    if before == "" or before[-1] in _ANCHOR_CHARS:
        return True
    if before.endswith("-"):
        prev = before[:-1]
        return prev == "" or prev[-1] in " \t\n"
    return False


def find_markers(text: Optional[str]) -> list:
    if not text:
        return []
    out = []
    for m in _MARKER_RE.finditer(text):
        rest = text[m.end():m.end() + 160]
        end = _QUALIFIER_END.search(rest)
        qualifier = (rest[:end.start()] if end else rest).strip()
        window = _BROADENING.sub(" ", text[m.end():m.end() + _REASON_WINDOW])
        between = (m.group("between") or "").strip().strip("*_`").strip()
        date_only = not between or bool(_DATE_ONLY.match(between))
        if not _anchored(text, m.start()):
            kind = "mention"
        elif m.group("pre") or not date_only:
            kind = "partial"      # "SUPERSEDED in part by", "SUPERSEDED (the price only) by", ...
        elif qualifier or _PARTIAL_CUES.search(window):
            kind = "partial"      # a qualifier before the colon, or a scope cue in the reason
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
