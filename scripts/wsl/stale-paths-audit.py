#!/usr/bin/env python3
"""Read-only staleness audit: memories that name filesystem paths which no longer exist.

WHY THIS EXISTS (memory-frontier, milestone-conditioned validity)
-----------------------------------------------------------------
The corpus has no notion of a memory's *validity*. A fact like "the 5060 Ti is the
only GPU" was true until a second card went in - a MILESTONE, not a date, which is
why a Zep-style ``valid_until: <date>`` was rejected. Before designing a validity
schema, the plan's gate asked for a hand-labelled staleness rate. A bench proxy
(bench/mem0-stale-rate.py, 2026-08-17) measured 56% over the first 50 mechanically
decidable memories and CLEARED the 20% materiality bar.

That proxy is strong enough to say "material"; it is NOT strong enough to specify a
schema. This tool is the next step: it scans the WHOLE corpus, fixes what the proxy
could not, and emits a worksheet a human labels by hand. The hand-labels are the
dataset the schema gets designed from - or the evidence that kills it (see EXIT).

WHAT THIS FIXES vs THE BENCH PROXY
----------------------------------
1. ROOT AVAILABILITY (a real instrument bug). ``G:`` and ``P:`` are not mounted
   under WSL. A WSL run of the naive check marks every ``G:`` path missing and
   invents staleness. Paths on an unreachable root are ``undecidable``, never stale,
   and the coverage gap is reported loudly. Run under the WINDOWS interpreter for
   full coverage; both are supported (stdlib only, no httpx).
2. FOREIGN HOSTS (proxy bias 2). A path that lives on ANOTHER machine in the fleet is
   correct there and always reads missing here. A memory naming another machine is
   ``undecidable``, not stale. Configure via MEM0_FOREIGN_HOSTS.
3. DELETION RECORDS (proxy bias 3, admitted under-catch). "The GLM model was deleted
   from V:/models/..." is a CORRECT memory whose subject IS the removal. Scoring it
   stale inverts its meaning. The vocabulary here is wider than the proxy's.
4. SELECTION BIAS (proxy bias 1, uncorrectable - so it is MEASURED instead of
   restated). Memories naming a path skew operational/ephemeral; durable memories
   rarely name one. This reports the decidable fraction of the corpus and cross-tabs
   staleness by tier and source, so the reader can see which stratum is rotting.

HARD GUARANTEES
---------------
- ZERO mutations. Reads Qdrant via the scroll API; writes only its own report and
  worksheet under ~/.mem0/. It never deletes, never PATCHes, never flags a memory in
  the store. This estate has already lost 688 MB to an automated process behaving
  exactly as designed.
- It does NOT write ~/.mem0/audit-flags.jsonl. That file is l10-audit's, on the
  ADMISSION axis (admission_gate.py, layer="server-search", schema_version "v18").
  Validity is a DIFFERENT axis and overloading admission reasons would corrupt both.
- No scheduler. This is a manual subcommand, run when an operator wants it.

EXIT / CHEAPEST KILL
--------------------
If the hand-labels show the stale memories are overwhelmingly operational ephemera
nobody ever recalls, the answer is NOT a validity schema - it is not writing those
memories in the first place. ``--summarise-worksheet`` reports exactly that split, so
the cheap outcome stays visible instead of being designed past.

Usage (either interpreter; Windows gives full drive coverage):
  stale-paths-audit.py                          # scan all, print report
  stale-paths-audit.py --sample 500 --seed 42   # reproducible subset
  stale-paths-audit.py --worksheet              # + emit hand-label worksheet
  stale-paths-audit.py --json                   # machine-readable
  stale-paths-audit.py --summarise-worksheet    # read back hand-labels, corrected rate
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import platform
import random
import re
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

QDRANT_URL = os.environ.get("MEM0_QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.environ.get("MEM0_QDRANT_COLLECTION", "mem0_egemma_768")


def _sidecar_dir() -> Path:
    """The stack's ONE sidecar directory, whichever interpreter we run under.

    Every other receipt in this system (audit-flags, contradiction-sweep, decay-report)
    lives in the WSL ~/.mem0. Running this audit from the Windows interpreter - which is
    the recommended way, because it sees G: and P: - would otherwise write receipts to
    C:\\Users\\<u>\\.mem0 and silently fork the audit trail in two. Explicit override:
    MEM0_SIDECAR_DIR."""
    override = os.environ.get("MEM0_SIDECAR_DIR")
    if override:
        return Path(override)
    if platform.system() == "Windows":
        share = Path("//wsl.localhost/" + os.environ.get("MEM0_WSL_DISTRO", ""))
        user = os.environ.get("MEM0_WSL_USER")
        if user:
            candidates = [share / "home" / user / ".mem0"]
        else:
            # Infer only when it is unambiguous: exactly one home directory that
            # already carries a .mem0. Guessing a username would silently fork the
            # audit trail, which is the failure this function exists to prevent.
            try:
                candidates = [h / ".mem0" for h in (share / "home").iterdir()
                              if (h / ".mem0").is_dir()]
            except OSError:
                candidates = []
        if len(candidates) == 1 and candidates[0].is_dir():
            return candidates[0]
        # Falling back to the local home is NOT a fallback: C:/Users/<u>/.mem0 already
        # exists and is written by other tooling, so receipts and - far worse - a
        # hand-labelled worksheet would land in a second plausible location with no
        # warning. Refuse instead, and name both candidates.
        raise SystemExit(
            "stale-paths-audit: cannot resolve the WSL sidecar directory.\n"
            "  tried : //wsl.localhost/"
            + os.environ.get("MEM0_WSL_DISTRO", "<unset>") + "/home/<user>/.mem0\n"
            "  local : " + str(Path.home() / ".mem0")
            + " (NOT used - it would fork the audit trail)\n"
            "  fix   : set MEM0_WSL_DISTRO (and MEM0_WSL_USER), or MEM0_SIDECAR_DIR.")
    return Path.home() / ".mem0"


MEM0 = _sidecar_dir()
REPORT = MEM0 / "stale-paths-audit.jsonl"
WORKSHEET = MEM0 / "stale-paths-worksheet.jsonl"
SCHEMA_VERSION = "sp-v1"
# The label vocabulary the worksheet advertises. Anything else is a typo, not a category.
VALID_LABELS = {"STALE", "VALID", "RECORDED", "FOREIGN", "EPHEMERAL"}
# WSL distro reachable from Windows as //wsl.localhost/<distro>. Set MEM0_WSL_DISTRO
# per box; unset means POSIX paths stay undecidable on Windows rather than guessed at.
WSL_DISTRO = os.environ.get("MEM0_WSL_DISTRO", "")
# The shared, synced Drive is mounted at a DIFFERENT drive letter per machine (the
# same Google Drive is D:\My Drive on one box and G:\My Drive on another). The letter
# is a property of the MACHINE, not of the memory - so a shared-Drive path recorded
# under another box's letter must be re-checked under every LOCAL root that carries
# the shared dir before it can be called missing. This was the single largest
# systematic error of the first hand-label round (gate verdict 2026-08-19): a whole
# class of perfectly valid D:\My Drive\... memories read as stale on the G: box.


def _normalize_shared_dir(value):
    """Normalise MEM0_SHARED_DRIVE_DIR. A trailing separator ("My Drive/") would pass
    root DETECTION yet make the per-path prefix match unsatisfiable - the alias dies
    silently while the run looks healthy - and an empty-but-set var would widen root
    detection to every mounted drive. Empty/unset falls back to the default; a value
    that normalises to nothing (e.g. "/") is refused loudly, matching _sidecar_dir."""
    v = (value or "My Drive").strip().strip("\\/").replace("\\", "/")
    if not v:
        raise SystemExit(
            "stale-paths-audit: MEM0_SHARED_DRIVE_DIR normalises to empty - "
            "set a real directory name or unset it.")
    return v


SHARED_DRIVE_DIR = _normalize_shared_dir(os.environ.get("MEM0_SHARED_DRIVE_DIR"))
_UNSET = object()
_UNC_ROOT = _UNSET
_WSL_HOME = _UNSET
_SHARED_ROOTS = _UNSET

# Path extraction MUST tolerate internal spaces. Stopping at the first space truncated
# every "Program Files", "My Drive" and "Port Directory" path - i.e. the most-cited roots
# in this corpus - which BOTH invented staleness (a truncated prefix does not exist) and
# deleted it (the stub was then dropped as a too-short artifact). A space is accepted only
# when another separator still follows inside the token, so trailing prose is not
# swallowed: "at C:\Program Files\a.exe on this box" keeps the path and drops " on this".
_SEG = r"[^\s\"',;)\]}<>|*?]"
# Drive paths, EITHER separator. A forward-slash drive path (C:/Users/...) is written
# constantly in this corpus and was invisible to a backslash-only pattern, so those
# memories were reported as "names no path at all" - a false statement about the corpus,
# not a silence. _translate already handled ":/" , proving they were meant to be in scope.
# (?<![A-Za-z]) and the (?!/) both exist to keep URLs out: without them "http://qube:18791"
# yields the "path" p://qube:18791, which never exists and was INVENTED as stale - it even
# reached the canonical tier in a full run.
WINPATH = re.compile(
    r"(?<![A-Za-z])[A-Za-z]:[\\/](?!/)(?:" + _SEG + r"|[ ](?=" + _SEG + r"*[\\/]))+")

# POSIX paths, plus ~/ (the single most common shape in this corpus) and the system
# roots. Anything not listed here is counted as an unmatched shape rather than folded
# into "no path at all".
# /dev and /proc are deliberately absent: they are prose fragments far more often than
# path claims here ("/dev/peptidos/" came out of a sentence about workspaces and was
# scored stale). Roots are added only when they earn it.
_POSIX_ROOTS = (r"~|/home/[A-Za-z0-9._-]+|/root|/mnt/[a-z]|/opt|/srv|/etc|/usr|/tmp"
                r"|/var/log|/var/lib")
POSIXPATH = re.compile(
    r"(?:" + _POSIX_ROOTS + r")/(?:" + _SEG + r"|[ ](?=" + _SEG + r"*/))+")

# UNC shares (\\host\share\...). Reachability is host-dependent, so these are surfaced
# as an explicitly unmatched shape rather than guessed at.
UNCPATH = re.compile(r"\\\\[A-Za-z0-9._-]+\\(?:" + _SEG + r"|[ ](?=" + _SEG + r"*[\\/]))+")

# A memory whose SUBJECT is the removal is correct, not stale. Wider than the proxy's
# list, which admitted under-catching.
DEAD_VERB = re.compile(
    r"\b("
    r"delet\w*|remov\w*|retir\w*|decommission\w*|deprecat\w*|"
    r"move[dn]?|migrat\w*|relocat\w*|supersed\w*|replac\w*|renam\w*|"
    r"purg\w*|prun\w*|clean(?:ed)?\s*up|wipe[dn]?|reclaim\w*|freed|"
    r"no longer|used to|formerly|previously|was at|were at|once lived|"
    r"stale|obsolete|dead|gone|vanish\w*|absent|missing|never existed|"
    r"rolled?\s*back|reverted|abandoned|archiv\w*|consolidat\w*"
    r")\b",
    re.I,
)

# Machines that are NOT this box. A path missing here but owned by one of these is
# undecidable, not stale (proxy bias 2). Estate topology is OPERATOR CONFIG, not source:
# set MEM0_FOREIGN_HOSTS to a comma-separated list of the other machines' names (and any
# alias that identifies them, e.g. a project dir only they hold). UNSET means the
# correction is OFF and cross-machine paths will read as stale - a known over-count that
# the report states explicitly rather than hiding.
def _host_pattern(names):
    words = [re.escape(n.strip()) for n in names if n.strip()]
    return re.compile(r"\b(" + "|".join(words) + r")\b", re.I) if words else None


FOREIGN_HOSTS = _host_pattern(os.environ.get("MEM0_FOREIGN_HOSTS", "").split(","))
# This box, by its own name - neutral and correct on any machine.
LOCAL_HOST = _host_pattern([os.environ.get("MEM0_LOCAL_HOST") or socket.gethostname()])


def _artifact(p: str) -> bool:
    """Extraction noise, not a real path claim."""
    if "\\\\" in p:          # double-escaped in the stored JSON
        return True
    # Placeholder-shaped only. A bare "__" test dropped every real __init__.py and
    # __pycache__ path in a Python estate, silently deleting their staleness.
    if "..." in p or "`" in p or "${" in p or re.search(r"__[A-Z][A-Z0-9_]*__", p):
        return True
    if len(p) < 8:
        return True
    if p.rstrip("\\/").endswith(":"):   # bare drive root
        return True
    return False


def _wsl_unc_root():
    """WSL filesystem as seen from Windows. Cached; None if not reachable."""
    global _UNC_ROOT
    if _UNC_ROOT is _UNSET:
        root = "//wsl.localhost/" + WSL_DISTRO
        _UNC_ROOT = root if os.path.exists(root) else None
    return _UNC_ROOT


def _wsl_home_dir():
    """The WSL home as seen from Windows, or None. Same unambiguity rule as the
    sidecar: infer only when exactly one home exists, never guess a username."""
    global _WSL_HOME
    if _WSL_HOME is _UNSET:
        _WSL_HOME = None
        unc = _wsl_unc_root()
        if unc:
            user = os.environ.get("MEM0_WSL_USER")
            if user:
                _WSL_HOME = unc + "/home/" + user
            else:
                try:
                    homes = [h for h in Path(unc + "/home").iterdir() if h.is_dir()]
                except OSError:
                    homes = []
                if len(homes) == 1:
                    _WSL_HOME = unc + "/home/" + homes[0].name
    return _WSL_HOME


def _translate(p: str):
    """Map a stored path onto this runtime. Returns None if the ROOT is unreachable
    here - which is a coverage gap, never evidence of staleness.

    Both directions are covered so ONE run decides everything: Windows reaches WSL
    through //wsl.localhost/<distro>, and WSL reaches Windows drives through /mnt/<d>.
    Neither runtime alone was enough - the first full run left 122 POSIX-path memories
    undecidable purely because it was launched from Windows."""
    win = platform.system() == "Windows"
    if p[1:3] in (":\\", ":/"):                      # X:\... or X:/...
        drive = p[0].lower()
        if win:
            # SYMMETRIC root check. The WSL branch below got this first; leaving the
            # Windows branch unguarded meant the RECOMMENDED runtime still invented
            # staleness for any absent drive (Q:, an unplugged USB), which is the same
            # bug class documented as closed. One instance fixed is not the class fixed.
            if not os.path.exists(drive.upper() + ":/"):
                return None
            return p
        # ismount, not is_dir: WSL leaves empty /mnt/<d> directories behind for drives
        # that are not actually mounted (measured: /mnt/o /mnt/s /mnt/t on this box).
        # is_dir() called those reachable, so every path on them translated, missed, and
        # was INVENTED as stale while the coverage banner stayed silent.
        if not os.path.ismount("/mnt/" + drive):
            return None
        return "/mnt/" + drive + "/" + p[3:].replace("\\", "/")
    if p.startswith("~"):
        # In THIS corpus "~/" overwhelmingly means the WSL home, not the Windows one.
        # Resolving it against Path.home() on Windows checked C:/Users/<u>/... and
        # invented staleness for files that exist perfectly well inside WSL.
        if not win:
            return str(Path.home()) + p[1:]
        unc = _wsl_unc_root()
        if not unc:
            return None                      # undecidable, not stale
        wsl_home = _wsl_home_dir()
        return (wsl_home + p[1:].replace("\\", "/")) if wsl_home else None
    if p.startswith("/"):
        if not win:
            return p
        # /mnt/<d>/rest is just a Windows drive wearing a WSL hat - go direct.
        if p[:5] == "/mnt/" and len(p) > 6 and p[6] == "/":
            return p[5].upper() + ":/" + p[7:]
        unc = _wsl_unc_root()
        return (unc + p) if unc else None
    return None


def _shared_drive_letters():
    """Lowercase local drive letters that carry the shared Drive dir at their root.
    Cached per run (the mount set does not change mid-audit); callers get a COPY so
    a mutation cannot corrupt the cache. MEM0_SHARED_DRIVE_LETTERS, when set, pins
    the letters (comma-separated) instead of detecting - the guard against a backup
    mirror or a second Drive account's mount masquerading as THE shared Drive."""
    global _SHARED_ROOTS
    if _SHARED_ROOTS is _UNSET:
        pinned = os.environ.get("MEM0_SHARED_DRIVE_LETTERS", "")
        if pinned.strip():
            # The pin fails LOUD, in both directions. Silently dropping a malformed
            # entry ("gg", "g;h") can collapse the pin to [] - every shared claim
            # flips to undecidable with no warning - and accepting an unresolvable
            # letter verbatim (a typo, or the Drive moved letters after the pin was
            # set) makes _alias_hit miss everywhere while the zero-roots guard stays
            # off: the mass false accusation, reinstated through the guard knob.
            letters, bad = [], []
            for tok in pinned.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                (letters if (len(tok) == 1 and tok.isalpha()) else bad).append(tok.lower())
            if bad or not letters:
                raise SystemExit(
                    "stale-paths-audit: MEM0_SHARED_DRIVE_LETTERS is malformed: "
                    + repr(pinned) + (" (bad entries: " + ", ".join(bad) + ")" if bad
                                      else " (no valid letters)")
                    + ". Use comma-separated single drive letters, e.g. \"g\" or \"d,g\".")
            win = platform.system() == "Windows"
            unresolved = [c for c in letters if not (
                (os.path.exists(c.upper() + ":/")
                 and os.path.exists(c.upper() + ":/" + SHARED_DRIVE_DIR)) if win
                else (os.path.ismount("/mnt/" + c)
                      and os.path.exists("/mnt/" + c + "/" + SHARED_DRIVE_DIR)))]
            if unresolved:
                raise SystemExit(
                    "stale-paths-audit: pinned shared-Drive letter(s) do not carry "
                    + repr(SHARED_DRIVE_DIR) + " on this box: " + ", ".join(unresolved)
                    + ". Fix the pin or unset MEM0_SHARED_DRIVE_LETTERS.")
            _SHARED_ROOTS = letters
        else:
            letters = []
            win = platform.system() == "Windows"
            for c in "abcdefghijklmnopqrstuvwxyz":
                if win:
                    # Root probe FIRST, same guard _translate uses: probing the shared
                    # dir directly on a disconnected mapped letter can block on an SMB
                    # timeout, and A:/B: can raise removable-media noise.
                    if (os.path.exists(c.upper() + ":/")
                            and os.path.exists(c.upper() + ":/" + SHARED_DRIVE_DIR)):
                        letters.append(c)
                elif (os.path.ismount("/mnt/" + c)
                      and os.path.exists("/mnt/" + c + "/" + SHARED_DRIVE_DIR)):
                    letters.append(c)
            _SHARED_ROOTS = letters
    return list(_SHARED_ROOTS)


def _shared_rest(p: str):
    """If p is a claim about the shared Drive dir - recorded under ANY drive letter,
    Windows or /mnt/<d> form - return its drive-relative part ("My Drive/..."),
    normalised to forward slashes. None for everything else. The shared root ITSELF
    ("D:\\My Drive") is a shared claim too - rejecting it re-invented the exact false
    accusation this exists to kill whenever the recorded letter existed locally."""
    m = re.match(r"^[A-Za-z]:[\\/](.+)$", p)
    if m:
        rest = m.group(1)
    elif (p.startswith("/mnt/") and len(p) > 6
          and p[5].isalpha() and p[6] == "/"):
        rest = p[7:]
    else:
        return None
    rest = rest.replace("\\", "/").rstrip("/")
    low = rest.lower()
    if low == SHARED_DRIVE_DIR.lower() or low.startswith(SHARED_DRIVE_DIR.lower() + "/"):
        return rest
    return None


def _alias_hit(p: str):
    """Shared-Drive alias resolution: if p is a shared-Drive claim (see _shared_rest)
    and the same relative path exists under a LOCAL shared-Drive root, return that
    local resolved path. Returns None otherwise. It never excuses a path outside the
    shared dir, and a shared-Drive path that is missing under every local root still
    falls through to the normal missing-* verdicts (a file deleted from the Drive is
    stale everywhere)."""
    rest = _shared_rest(p)
    if rest is None:
        return None
    win = platform.system() == "Windows"
    for letter in _shared_drive_letters():
        cand = (letter.upper() + ":/" + rest) if win else ("/mnt/" + letter + "/" + rest)
        if os.path.exists(cand):
            return cand
    return None


def _prose(text: str) -> str:
    """The text with path literals removed, so vocabulary tests read what the memory
    SAYS rather than what its filenames happen to spell."""
    return POSIXPATH.sub(" ", WINPATH.sub(" ", text))


def _records_removal(text: str, raw_path: str) -> bool:
    """True only when the removal vocabulary appears in the SENTENCE containing this
    path. Matching anywhere in the memory was far too loose: delete/archive/migrate/
    replace/missing/stale/consolidate are this estate's ordinary working vocabulary
    (and this tool's own), so an unrelated clause elsewhere excused a dead path and
    quietly moved it out of the STALE bucket."""
    # Mask the path FIRST, then split. Splitting on a bare "." also splits the path
    # itself (conf.yaml, oss.gguf), after which the path is in no fragment at all and
    # every genuine removal record silently reverted to STALE. Sentences end on
    # period-then-space, never mid-token.
    token = "\x00P\x00"
    masked = text.replace(raw_path, token)
    if token not in masked:                       # tolerate a trailing-punctuation trim
        masked = text.replace(raw_path.rstrip(".,);:`'\""), token)
    for sentence in re.split(r"[;\n]|\.(?=\s|$)", masked):
        if token in sentence:
            if DEAD_VERB.search(_prose(sentence.replace(token, " "))):
                return True
    return False


def classify_path(raw: str, text: str):
    """-> (verdict, resolved_path). Verdicts: artifact | root-unavailable |
    shared-root-unavailable | exists | missing-recorded | missing-foreign-host |
    missing-unexplained."""
    if _artifact(raw):
        return "artifact", None
    resolved = _translate(raw)
    if resolved is None:
        # The recorded root is unreachable HERE, but the path may be a shared-Drive
        # path wearing another machine's drive letter (e.g. D:\My Drive\... checked
        # on the box where the same Drive is G:). An alias hit is a decidable EXISTS,
        # not a coverage gap.
        alias = _alias_hit(raw)
        if alias is not None:
            return "exists", alias
        return "root-unavailable", None
    if os.path.exists(resolved):
        return "exists", resolved
    # Missing at the recorded letter - but the shared Drive may live at a different
    # letter on this box. Check the alias BEFORE any staleness verdict.
    alias = _alias_hit(raw)
    if alias is not None:
        return "exists", alias
    if _shared_rest(raw) is not None and not _shared_drive_letters():
        # A shared-Drive claim on a box where ZERO local roots carry the shared dir
        # (Drive app not mounted yet, crashed, or genuinely absent) CANNOT be
        # falsified here. "Could not check" is not "missing" - handing down a
        # missing-* verdict from an unmounted Drive would silently resurrect the
        # mass false accusation the alias exists to kill. A DISTINCT verdict, not
        # plain root-unavailable: the drive LETTERS may all exist here, so the
        # letter-based coverage banner stays silent - this cause needs its own
        # counter and its own console line to be visible at all.
        return "shared-root-unavailable", None
    # Missing here. Decide WHY before calling it stale.
    # Match the removal vocabulary against PROSE ONLY. Scanning the raw text let a
    # path excuse itself: D:\...\gone.md, D:\archive\..., V:\models\deleted\... all
    # contain a DEAD_VERB, so a genuinely stale memory was silently reclassified as a
    # correct deletion record. That failure direction DELETES staleness from the
    # dataset the schema is designed from, so it matters more than a false positive.
    if _records_removal(text, raw):
        return "missing-recorded", resolved
    if FOREIGN_HOSTS is not None and FOREIGN_HOSTS.search(text):
        # A memory that also names THIS box is making a local claim; the local claim wins.
        if LOCAL_HOST is None or not LOCAL_HOST.search(text):
            return "missing-foreign-host", resolved
    return "missing-unexplained", resolved


# Memory-level verdict precedence: the most alarming decidable outcome wins, but an
# undecidable reason never masquerades as fresh.
# missing-recorded sits BELOW the undecidables: a memory that also names an unchecked
# path must not be booked as decidable-and-correct on the strength of a different path.
PRECEDENCE = ["missing-unexplained", "root-unavailable", "shared-root-unavailable",
              "missing-foreign-host", "missing-recorded", "exists", "artifact"]


def classify_memory(text: str) -> dict:
    raw = {x.rstrip(".,);:`'\"") for x in WINPATH.findall(text)}
    raw |= {x.rstrip(".,);:`'\"") for x in POSIXPATH.findall(text)}
    if not raw:
        # A shape we cannot resolve is NOT "this memory names no path". Reporting it as
        # such was a false statement about the corpus; make the gap a number instead.
        if UNCPATH.search(text):
            return {"verdict": "unmatched-path-shape", "paths": {}}
        return {"verdict": "no-path", "paths": {}}
    per_path = {p: classify_path(p, text) for p in sorted(raw)}
    verdicts = {v for v, _ in per_path.values()}
    if verdicts == {"artifact"}:
        return {"verdict": "artifact-only", "paths": per_path}
    for v in PRECEDENCE:
        if v in verdicts:
            return {"verdict": v, "paths": per_path}
    return {"verdict": "no-path", "paths": per_path}


def _checked_endpoint() -> str:
    """Build the scroll URL, refusing any scheme urllib would honour but we never want.

    QDRANT_URL is env-controlled, and urllib happily opens file:// (and ftp://, data://).
    A hostile or fat-fingered MEM0_QDRANT_URL would otherwise turn a read-only audit into
    an arbitrary-file reader. Allow-list the two schemes this tool can legitimately use."""
    url = QDRANT_URL.rstrip("/") + "/collections/" + COLLECTION + "/points/scroll"
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            "refusing scheme " + repr(scheme) + " for MEM0_QDRANT_URL; expected http/https")
    return url


def scroll_all(limit):
    pts, offset = [], None
    endpoint = _checked_endpoint()
    while True:
        body = {"limit": 1000, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        # REFUTED, with the mitigation in code: the rule fires on any dynamic value
        # reaching urlopen. The risk it names is the file:// scheme, and
        # _checked_endpoint() allow-lists http/https and raises otherwise (pinned by
        # test_scheme_guard_refuses_file_url). The URL is an operator env var on a
        # local read-only diagnostic, not attacker-controlled input.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            res = json.load(r)["result"]
        pts.extend(res.get("points") or [])
        offset = res.get("next_page_offset")
        if not offset or (limit and len(pts) >= limit):
            break
    return pts[:limit] if limit else pts


def available_roots() -> dict:
    seen = {}
    for d in "cdefgpuv":
        if platform.system() == "Windows":
            seen[d.upper() + ":"] = os.path.exists(d.upper() + ":/")
        else:
            seen[d.upper() + ":"] = os.path.ismount("/mnt/" + d)
    return seen


def run_audit(args) -> int:
    roots = available_roots()
    unreachable = sorted(k for k, v in roots.items() if not v)
    # The WSL share is a root too. Omitting it meant a Windows run with no distro set
    # made every POSIX path undecidable while the banner - gated on drive letters only -
    # stayed silent and the report read as full coverage.
    if platform.system() == "Windows" and _wsl_unc_root() is None:
        unreachable.append("//wsl.localhost/<distro> (set MEM0_WSL_DISTRO)")
    try:
        points = scroll_all(None)
    except Exception as e:  # noqa: BLE001 - a read-only audit fails loud, never half-reports
        print("stale-paths-audit: Qdrant unreachable (" + QDRANT_URL + "/" + COLLECTION
              + "): " + str(e), file=sys.stderr)
        return 2

    corpus_total = len(points)
    rows = [p for p in points if isinstance(p.get("payload"), dict)]
    if args.sample:
        random.seed(args.seed)
        random.shuffle(rows)
        rows = rows[: args.sample]

    counts = collections.Counter()
    by_tier = collections.defaultdict(collections.Counter)
    by_source = collections.defaultdict(collections.Counter)
    stale_rows, undecidable_rows = [], []
    control_pool = collections.defaultdict(list)

    for p in rows:
        pay = p["payload"]
        text = pay.get("data") or pay.get("memory") or pay.get("text") or ""
        if not isinstance(text, str) or len(text) < args.min_len:
            counts["skipped-short"] += 1
            continue
        res = classify_memory(text)
        v = res["verdict"]
        counts[v] += 1
        tier = pay.get("tier") or "unknown"
        source = pay.get("source") or "unknown"
        by_tier[tier][v] += 1
        by_source[source][v] += 1
        if v in ("missing-unexplained", "missing-foreign-host", "root-unavailable",
                 "shared-root-unavailable"):
            missing = [rp for rp, (pv, _) in res["paths"].items() if pv == v]
            rec = {
                "memory_id": p.get("id"), "verdict_mechanical": v,
                "missing_paths": missing[:6], "tier": tier, "source": source,
                "created_at": pay.get("created_at"), "updated_at": pay.get("updated_at"),
                "text": text[: args.excerpt],
            }
            if v == "missing-unexplained":
                stale_rows.append(rec)
            else:
                undecidable_rows.append(rec)
        elif v in ("missing-recorded", "exists"):
            # Blind controls: rows the MECHANISM called not-stale. Labelling only the
            # accused makes recall unmeasurable, and recall is the direction that
            # matters - a rule that eats real staleness would look perfect without these.
            control_pool[v].append({
                "memory_id": p.get("id"), "verdict_mechanical": v,
                "missing_paths": [rp for rp, (pv, _) in res["paths"].items()
                                  if pv == v][:6],
                "tier": tier, "source": source,
                "created_at": pay.get("created_at"), "updated_at": pay.get("updated_at"),
                "text": text[: args.excerpt],
            })

    decidable = (counts["exists"] + counts["missing-recorded"]
                 + counts["missing-unexplained"])
    stale = counts["missing-unexplained"]
    rate = (stale / decidable * 100) if decidable else 0.0
    scanned = sum(v for k, v in counts.items() if k != "skipped-short")

    summary = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "host": socket.gethostname(), "runtime": platform.system(),
        "collection": COLLECTION, "corpus_points": corpus_total,
        "scanned": scanned, "sample": args.sample or None, "seed": args.seed,
        "counts": dict(counts), "decidable": decidable, "stale": stale,
        "stale_rate_pct": round(rate, 1),
        "unreachable_roots": unreachable,
        # Visibility for the alias seam: a run with ZERO shared roots decides no
        # shared-Drive claim, and MORE than one root means ambiguity (backup mirror /
        # second account) worth pinning via MEM0_SHARED_DRIVE_LETTERS.
        "shared_drive_dir": SHARED_DRIVE_DIR,
        "shared_drive_roots": _shared_drive_letters(),
        "wsl_unc_root": _wsl_unc_root(),
        "wsl_distro": WSL_DISTRO or None,
        "undecidable_root_unavailable": counts["root-unavailable"],
        "undecidable_shared_drive_unmounted": counts["shared-root-unavailable"],
        "undecidable_foreign_host": counts["missing-foreign-host"],
        "recorded_removals": counts["missing-recorded"],
        "foreign_hosts_configured": FOREIGN_HOSTS is not None,
        "decidable_fraction_pct": (round(decidable / scanned * 100, 1) if scanned else 0.0),
    }

    if args.json:
        print(json.dumps({"summary": summary,
                          "by_tier": {k: dict(v) for k, v in by_tier.items()},
                          "by_source": {k: dict(v) for k, v in by_source.items()}}, indent=1))
    else:
        _print_report(summary, by_tier, by_source, stale_rows, unreachable)

    try:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        with REPORT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary) + "\n")
        # stderr: --json promises machine-readable STDOUT, and a trailing human line
        # made json.loads(stdout) fail with "Extra data".
        print("\nreceipt appended -> " + str(REPORT), file=sys.stderr if args.json else sys.stdout)
    except OSError as e:
        print("WARNING: receipt not written: " + str(e), file=sys.stderr)

    if args.worksheet:
        # Draw the blind controls. Without this the control_pool was collected and
        # DISCARDED: the worksheet held only rows the mechanism accused, so recall was
        # structurally unmeasurable while the feature looked shipped.
        rng = random.Random(args.seed)
        controls = []
        for kind, cap in (("missing-recorded", args.controls),
                          ("exists", max(1, args.controls // 2))):
            pool = control_pool.get(kind, [])
            controls += rng.sample(pool, min(cap, len(pool)))

        # A worksheet is a LABELLING TASK, not a dump. Emitting every accused row
        # produced 890 rows with the controls diluted ~1:14, so labelling a realistic
        # few dozen would have caught ~4 controls - too few to measure recall, which is
        # the only reason the controls exist. Sample the accused down instead, so the
        # ratio the human actually labels is the ratio that was designed.
        accused, undec = stale_rows, undecidable_rows
        if not args.worksheet_all:
            accused = rng.sample(stale_rows, min(args.worksheet_size, len(stale_rows)))
            n_undec = max(1, args.worksheet_size // 4)
            undec = rng.sample(undecidable_rows, min(n_undec, len(undecidable_rows)))
        _emit_worksheet(accused, undec, summary,
                        control_rows=controls, force=args.force_worksheet,
                        sampled=not args.worksheet_all,
                        population={"stale": len(stale_rows),
                                    "undecidable": len(undecidable_rows)})
    return 0


def _print_report(s, by_tier, by_source, stale_rows, unreachable):
    print("stale-paths-audit  [" + s["runtime"] + " on " + s["host"] + "]  collection="
          + s["collection"])
    line = "corpus " + str(s["corpus_points"]) + " points | scanned " + str(s["scanned"])
    if s["sample"]:
        line += " (sample " + str(s["sample"]) + ", seed " + str(s["seed"]) + ")"
    print(line)
    if unreachable:
        print("\n  !! COVERAGE GAP: roots not reachable from this runtime: "
              + ", ".join(unreachable))
        print("     " + str(s["undecidable_root_unavailable"])
              + " memories held UNDECIDABLE because of it (NOT counted stale).")
        if s["runtime"] != "Windows":
            print("     Re-run under the Windows interpreter for full drive coverage.")
    shared_roots = s.get("shared_drive_roots") or []
    print("shared Drive \"" + str(s.get("shared_drive_dir")) + "\" local roots: "
          + (", ".join(shared_roots) if shared_roots else "NONE"))
    if not shared_roots:
        # The letter-based banner above cannot see this cause: the drive letters may
        # all exist while none carries the shared dir (Drive app down / not mounted).
        print("  !! SHARED DRIVE UNMOUNTED: shared-Drive claims are UNDECIDABLE on this box ("
              + str(s["undecidable_shared_drive_unmounted"]) + " memories held out, NOT counted stale).")
    print("\n  DECIDABLE")
    print("    paths still exist                  : " + str(s["counts"].get("exists", 0)))
    print("    missing, memory RECORDS the removal: " + str(s["recorded_removals"])
          + "   <- correct, not stale")
    print("    missing, unexplained  -> STALE     : " + str(s["stale"]))
    print("  UNDECIDABLE (never counted stale)")
    print("    root unavailable in this runtime   : " + str(s["undecidable_root_unavailable"]))
    print("    shared Drive not mounted here      : " + str(s["undecidable_shared_drive_unmounted"]))
    print("    path owned by another machine      : " + str(s["undecidable_foreign_host"]))
    if not s.get("foreign_hosts_configured"):
        print("      ^ MEM0_FOREIGN_HOSTS is unset, so this correction is OFF and paths")
        print("        owned by other machines are being counted STALE (over-count).")
    print("    extraction artifacts only          : " + str(s["counts"].get("artifact-only", 0)))
    print("    memory names no path at all        : " + str(s["counts"].get("no-path", 0)))
    rate_txt = (str(s["stale_rate_pct"]) + "%") if s["decidable"] else "n/a (nothing decidable)"
    print("\n  STALE RATE (of decidable)          : " + rate_txt)
    print("  decidable fraction of scanned      : " + str(s["decidable_fraction_pct"]) + "%")
    print("\n  SELECTION BIAS - read this before quoting the rate: memories that name a path")
    print("  skew operational/ephemeral. Durable memories (decisions, preferences, identity)")
    print("  rarely name one, so the rate above is an UPPER BOUND on the corpus.")

    rank = sorted(((t, c.get("missing-unexplained", 0), sum(c.values()))
                   for t, c in by_tier.items()), key=lambda x: -x[1])
    print("\n  stale by TIER (which stratum is rotting):")
    for t, st, tot in rank:
        if tot:
            print("    " + str(t)[:12].ljust(12) + str(st).rjust(4) + " stale / "
                  + str(tot).rjust(5) + " scanned  (" + format(st / tot * 100, "4.1f") + "%)")
    rank = sorted(((t, c.get("missing-unexplained", 0), sum(c.values()))
                   for t, c in by_source.items()), key=lambda x: -x[1])[:8]
    print("\n  stale by SOURCE (top 8):")
    for t, st, tot in rank:
        if tot:
            print("    " + str(t)[:38].ljust(38) + str(st).rjust(4) + " / " + str(tot).rjust(5)
                  + "  (" + format(st / tot * 100, "4.1f") + "%)")
    print("\n  --- sample of STALE candidates (first 8) ---")
    for r in stale_rows[:8]:
        head = r["missing_paths"][0][:78] if r["missing_paths"] else "?"
        print("    [" + str(r["tier"]) + "] " + head)
        print("       ..." + r["text"][:104].replace("\n", " ") + "...")


def _existing_labels(path) -> int:
    """How many rows in an existing worksheet already carry a hand-label."""
    if not path.exists():
        return 0
    n = 0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not r.get("_worksheet_header") and (r.get("label") or "").strip():
                n += 1
    except OSError:
        return 0
    return n


def _emit_worksheet(stale_rows, undecidable_rows, summary, control_rows=(), force=False,
                    sampled=False, population=None):
    """The actual deliverable: a hand-label worksheet. Mechanical verdicts are a
    PROPOSAL; the human's `label` is what the schema gets designed from."""
    already = _existing_labels(WORKSHEET)
    if already and not force:
        print("REFUSING to overwrite " + str(WORKSHEET) + ": it already carries "
              + str(already) + " hand-label(s) - the one artifact here that cannot be "
              "regenerated. Move it aside, or re-run with --force-worksheet to discard.",
              file=sys.stderr)
        return
    try:
        with WORKSHEET.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "_worksheet_header": True, "schema_version": SCHEMA_VERSION,
                "generated": summary["ts"], "runtime": summary["runtime"],
                "instructions": (
                    "Set label to one of: STALE (fact is wrong now) | VALID (still true) | "
                    "RECORDED (memory is ABOUT the removal) | FOREIGN (another machine) | "
                    "EPHEMERAL (was never worth storing). Add label_reason. "
                    "EPHEMERAL is the cheapest-kill signal: if most STALE rows are also "
                    "EPHEMERAL, do not build a validity schema - stop writing them."),
                "recall_hint": ("label_recalled: has this memory ever actually been "
                                "useful? yes/no/unknown"),
                # Without this a labeller cannot tell whether they are looking at the
                # whole population or a sample of it, and any rate they compute from
                # the sheet would be quietly mis-scaled.
                "sampled": bool(sampled),
                "population": population or {},
                "rows_accused": len(stale_rows),
                "rows_undecidable": len(undecidable_rows),
                "rows_control": len(control_rows),
            }) + "\n")
            # Controls are shuffled in so the human labels BLIND. Without them only
            # precision on STALE is computable; recall - "how much staleness did the
            # deletion-record rule eat?" - is the half that actually matters here, and
            # it needs labelled rows the mechanism called NOT-stale.
            rows = list(stale_rows) + list(undecidable_rows) + list(control_rows)
            random.Random(summary.get("seed") or 42).shuffle(rows)
            for r in rows:
                r = dict(r)
                r["label"] = ""
                r["label_reason"] = ""
                r["label_recalled"] = ""
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("worksheet (" + str(len(stale_rows)) + " stale + "
              + str(len(undecidable_rows)) + " undecidable + " + str(len(control_rows))
              + " blind controls) -> " + str(WORKSHEET))
        print("  hand-label the `label` field, then: "
              "stale-paths-audit.py --summarise-worksheet")
    except OSError as e:
        print("WARNING: worksheet not written: " + str(e), file=sys.stderr)


def summarise_worksheet() -> int:
    if not WORKSHEET.exists():
        print("no worksheet at " + str(WORKSHEET) + " - run with --worksheet first",
              file=sys.stderr)
        return 2
    rows, labelled = [], []
    try:
        raw_lines = WORKSHEET.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as e:
        print("ERROR: " + str(WORKSHEET) + " is not UTF-8 (" + str(e) + "). An editor "
              "re-saved it in another encoding; re-save as UTF-8 before summarising.",
              file=sys.stderr)
        return 3
    bad = []
    for ln, line in enumerate(raw_lines, 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError as e:
            # NEVER skip silently. A dropped line shrinks the denominator AND the
            # EPHEMERAL/STALE ratio, and that ratio is the build-vs-kill decision -
            # measured to invert outright on four unparseable rows, while the tool
            # still printed a 100%-complete-looking denominator.
            bad.append((ln, str(e)[:60]))
            continue
        if r.get("_worksheet_header"):
            continue
        rows.append(r)
        if (r.get("label") or "").strip():
            labelled.append(r)
    if bad:
        print("ERROR: " + str(len(bad)) + " unparseable worksheet line(s): "
              + str(bad[:3]) + " - fix them before trusting any number below; the "
              "decision threshold moves when rows are silently dropped.",
              file=sys.stderr)
        return 3
    if not labelled:
        print("worksheet has " + str(len(rows)) + " rows, 0 hand-labelled yet. "
              "Nothing to summarise.")
        return 1
    lab = collections.Counter((r["label"] or "").strip().upper() for r in labelled)
    # A mistyped label is silently excluded from the EPHEMERAL/STALE ratio - measured to
    # flip the verdict on four typos, while the bogus bucket was printed three lines
    # above the confident conclusion that had ignored it.
    unknown = {k: v for k, v in lab.items() if k not in VALID_LABELS}
    if unknown:
        print("ERROR: unrecognised label(s) " + str(unknown) + " - valid labels are "
              + str(sorted(VALID_LABELS)) + ". They are excluded from the ratio that "
              "decides build-vs-kill, so fix them before trusting the verdict.",
              file=sys.stderr)
        return 3
    mech = collections.Counter(r.get("verdict_mechanical") for r in labelled)
    agree = sum(1 for r in labelled
                if (r["label"] or "").strip().upper() == "STALE"
                and r.get("verdict_mechanical") == "missing-unexplained")
    mech_stale = mech.get("missing-unexplained", 0)
    recalled = collections.Counter((r.get("label_recalled") or "unknown").strip().lower()
                                   for r in labelled)
    print("hand-labelled " + str(len(labelled)) + " of " + str(len(rows)) + " worksheet rows\n")
    for k, v in lab.most_common():
        print("  " + str(k).ljust(10) + str(v).rjust(4) + "  ("
              + format(v / len(labelled) * 100, "4.1f") + "%)")
    prec = ""
    if mech_stale:
        prec = " (" + format(agree / mech_stale * 100, ".1f") + "%)"
    print("\n  mechanical precision on STALE: " + str(agree) + "/" + str(mech_stale) + prec)
    ephemeral = lab.get("EPHEMERAL", 0)
    stale = lab.get("STALE", 0)
    print("\n  --- DECISION SIGNAL ---")
    if 0 < stale + ephemeral < 30:
        print("  CAUTION: only " + str(stale + ephemeral) + " STALE+EPHEMERAL rows "
              "labelled - a handful of rows moves this verdict. Label ~30+ before acting.")
    if stale + ephemeral:
        share = ephemeral / (stale + ephemeral) * 100
        print("  EPHEMERAL share of (STALE + EPHEMERAL): " + format(share, ".1f") + "%")
        if share >= 60:
            print("  => CHEAPEST KILL is live: these are mostly memories that should never")
            print("     have been written. Fix the WRITE path (extraction filter), not the")
            print("     read path. Do NOT build a validity schema on this evidence.")
        else:
            print("  => Stale memories are substantively worth keeping-but-invalidating.")
            print("     A validity schema (valid_while: goal:<id> / superseded_by) is justified.")
    print("\n  ever-recalled: " + str(dict(recalled)))
    print("  (a memory that has never been recalled is not worth a schema to invalidate)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", type=int, default=0, help="random subset (0 = all)")
    ap.add_argument("--seed", type=int, default=42,
                    help="sample seed (default 42, bench parity)")
    ap.add_argument("--min-len", type=int, default=40, help="skip memories shorter than this")
    ap.add_argument("--excerpt", type=int, default=400, help="worksheet text excerpt chars")
    ap.add_argument("--worksheet", action="store_true", help="emit the hand-label worksheet")
    ap.add_argument("--worksheet-size", type=int, default=60,
                    help="accused rows to put in the worksheet (default 60 - a labelling "
                         "task, not a dump; use --worksheet-all for every row)")
    ap.add_argument("--worksheet-all", action="store_true",
                    help="emit EVERY accused row instead of a labelling-sized sample")
    ap.add_argument("--controls", type=int, default=40,
                    help="blind control rows the mechanism called NOT stale, so recall "
                         "is measurable and not just precision")
    ap.add_argument("--force-worksheet", action="store_true",
                    help="overwrite a worksheet that already carries hand-labels")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--summarise-worksheet", action="store_true",
                    help="read back hand-labels and report the corrected rate")
    args = ap.parse_args()
    if args.summarise_worksheet:
        return summarise_worksheet()
    return run_audit(args)


if __name__ == "__main__":
    sys.exit(main())
