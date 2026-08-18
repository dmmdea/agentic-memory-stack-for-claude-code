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
2. FOREIGN HOSTS (proxy bias 2). A path under ops-sre "runs on Juan's PC" is correct
   and always reads missing here. A memory naming another machine is ``undecidable``,
   not stale.
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
        wsl_home = Path("//wsl.localhost/" + os.environ.get("MEM0_WSL_DISTRO", "Ubuntu-ML")
                        + "/home/" + os.environ.get("MEM0_WSL_USER", "dmmdea") + "/.mem0")
        if wsl_home.is_dir():
            return wsl_home
    return Path.home() / ".mem0"


MEM0 = _sidecar_dir()
REPORT = MEM0 / "stale-paths-audit.jsonl"
WORKSHEET = MEM0 / "stale-paths-worksheet.jsonl"
SCHEMA_VERSION = "sp-v1"
# WSL distro reachable from Windows as //wsl.localhost/<distro>. Ubuntu-ML is this
# estate's live memory-stack distro (mem0-stack.config.psd1); override per box.
WSL_DISTRO = os.environ.get("MEM0_WSL_DISTRO", "Ubuntu-ML")
_UNSET = object()
_UNC_ROOT = _UNSET

# Windows absolute paths. Deliberately conservative: stops at whitespace and the
# punctuation that ends a sentence or a quoted fragment.
WINPATH = re.compile(r"[A-Za-z]:\\[^\s\"',;)\]}<>|*?]+")

# POSIX paths under roots this estate actually uses (WSL homes, /mnt mounts, apps).
POSIXPATH = re.compile(
    r"(?:/home/[A-Za-z0-9._-]+|/mnt/[a-z]|/opt|/srv|/var/log)/[^\s\"',;)\]}<>|*?]+")

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
# undecidable, not stale (proxy bias 2).
FOREIGN_HOSTS = {
    "aorus": re.compile(r"\baorus\b|\blaptop\b", re.I),
    "lenovo": re.compile(r"\blenovo\b|\bm720q\b", re.I),
    "juan": re.compile(r"\bjuan\b|\bops-sre\b", re.I),
    "dell": re.compile(r"\bdell\b", re.I),
    "workernode": re.compile(r"\bworkernode\b", re.I),
}
LOCAL_HOST = re.compile(r"\bqube\b", re.I)


def _artifact(p: str) -> bool:
    """Extraction noise, not a real path claim."""
    if "\\\\" in p:          # double-escaped in the stored JSON
        return True
    if "..." in p or "`" in p or "${" in p or "__" in p:
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
            return p
        if not Path("/mnt/" + drive).is_dir():
            return None      # e.g. G: and P: are not mounted under WSL
        return "/mnt/" + drive + "/" + p[3:].replace("\\", "/")
    if p.startswith("/"):
        if not win:
            return p
        # /mnt/<d>/rest is just a Windows drive wearing a WSL hat - go direct.
        if p[:5] == "/mnt/" and len(p) > 6 and p[6] == "/":
            return p[5].upper() + ":/" + p[7:]
        unc = _wsl_unc_root()
        return (unc + p) if unc else None
    return None


def _prose(text: str) -> str:
    """The text with path literals removed, so vocabulary tests read what the memory
    SAYS rather than what its filenames happen to spell."""
    return POSIXPATH.sub(" ", WINPATH.sub(" ", text))


def classify_path(raw: str, text: str):
    """-> (verdict, resolved_path). Verdicts: artifact | root-unavailable |
    exists | missing-recorded | missing-foreign-host | missing-unexplained."""
    if _artifact(raw):
        return "artifact", None
    resolved = _translate(raw)
    if resolved is None:
        return "root-unavailable", None
    if os.path.exists(resolved):
        return "exists", resolved
    # Missing here. Decide WHY before calling it stale.
    # Match the removal vocabulary against PROSE ONLY. Scanning the raw text let a
    # path excuse itself: D:\...\gone.md, D:\archive\..., V:\models\deleted\... all
    # contain a DEAD_VERB, so a genuinely stale memory was silently reclassified as a
    # correct deletion record. That failure direction DELETES staleness from the
    # dataset the schema is designed from, so it matters more than a false positive.
    if DEAD_VERB.search(_prose(text)):
        return "missing-recorded", resolved
    if not LOCAL_HOST.search(text):
        for _host, pat in FOREIGN_HOSTS.items():
            if pat.search(text):
                return "missing-foreign-host", resolved
    return "missing-unexplained", resolved


# Memory-level verdict precedence: the most alarming decidable outcome wins, but an
# undecidable reason never masquerades as fresh.
PRECEDENCE = ["missing-unexplained", "missing-recorded", "missing-foreign-host",
              "root-unavailable", "exists", "artifact"]


def classify_memory(text: str) -> dict:
    raw = {x.rstrip(".,);:`'\"") for x in WINPATH.findall(text)}
    raw |= {x.rstrip(".,);:`'\"") for x in POSIXPATH.findall(text)}
    if not raw:
        return {"verdict": "no-path", "paths": {}}
    per_path = {p: classify_path(p, text) for p in sorted(raw)}
    verdicts = {v for v, _ in per_path.values()}
    if verdicts == {"artifact"}:
        return {"verdict": "artifact-only", "paths": per_path}
    for v in PRECEDENCE:
        if v in verdicts:
            return {"verdict": v, "paths": per_path}
    return {"verdict": "no-path", "paths": per_path}


def scroll_all(limit):
    pts, offset = [], None
    while True:
        body = {"limit": 1000, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        req = urllib.request.Request(
            QDRANT_URL + "/collections/" + COLLECTION + "/points/scroll",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
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
            seen[d.upper() + ":"] = Path("/mnt/" + d).is_dir()
    return seen


def run_audit(args) -> int:
    roots = available_roots()
    unreachable = sorted(k for k, v in roots.items() if not v)
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
        if v in ("missing-unexplained", "missing-foreign-host", "root-unavailable"):
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
        "undecidable_root_unavailable": counts["root-unavailable"],
        "undecidable_foreign_host": counts["missing-foreign-host"],
        "recorded_removals": counts["missing-recorded"],
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
        print("\nreceipt appended -> " + str(REPORT))
    except OSError as e:
        print("WARNING: receipt not written: " + str(e), file=sys.stderr)

    if args.worksheet:
        _emit_worksheet(stale_rows, undecidable_rows, summary)
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
    print("\n  DECIDABLE")
    print("    paths still exist                  : " + str(s["counts"].get("exists", 0)))
    print("    missing, memory RECORDS the removal: " + str(s["recorded_removals"])
          + "   <- correct, not stale")
    print("    missing, unexplained  -> STALE     : " + str(s["stale"]))
    print("  UNDECIDABLE (never counted stale)")
    print("    root unavailable in this runtime   : " + str(s["undecidable_root_unavailable"]))
    print("    path owned by another machine      : " + str(s["undecidable_foreign_host"]))
    print("    extraction artifacts only          : " + str(s["counts"].get("artifact-only", 0)))
    print("    memory names no path at all        : " + str(s["counts"].get("no-path", 0)))
    print("\n  STALE RATE (of decidable)          : " + str(s["stale_rate_pct"]) + "%")
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


def _emit_worksheet(stale_rows, undecidable_rows, summary):
    """The actual deliverable: a hand-label worksheet. Mechanical verdicts are a
    PROPOSAL; the human's `label` is what the schema gets designed from."""
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
            }) + "\n")
            for r in stale_rows + undecidable_rows:
                r = dict(r)
                r["label"] = ""
                r["label_reason"] = ""
                r["label_recalled"] = ""
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("worksheet (" + str(len(stale_rows)) + " stale + "
              + str(len(undecidable_rows)) + " undecidable) -> " + str(WORKSHEET))
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
    for line in WORKSHEET.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("_worksheet_header"):
            continue
        rows.append(r)
        if (r.get("label") or "").strip():
            labelled.append(r)
    if not labelled:
        print("worksheet has " + str(len(rows)) + " rows, 0 hand-labelled yet. "
              "Nothing to summarise.")
        return 1
    lab = collections.Counter((r["label"] or "").strip().upper() for r in labelled)
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
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--summarise-worksheet", action="store_true",
                    help="read back hand-labels and report the corrected rate")
    args = ap.parse_args()
    if args.summarise_worksheet:
        return summarise_worksheet()
    return run_audit(args)


if __name__ == "__main__":
    sys.exit(main())
