"""W3 (2026-08-07): nightly-job liveness for /health/deep — informational.

Every nightly mouth of the stack (dream consolidation, prune/gather receipts,
backup manifests, the dedup report, the morning summary) leaves a file behind.
None of those files were watched: a dead scheduled task stayed invisible until
an operator happened to look. This check turns each receipt's AGE into a
/health/deep field so the capability manifest (capabilities.py) can say which
mouths have gone quiet — detection latency of one session/night is by design
(no watchdog; the check is read when a human or a session reads /health/deep).

Shape (fixed cross-track contract — do not rename keys; W4 ADDS keys, and
additions are safe because every consumer reads by name):
    {role, last_dream_age_h, prune_age_h, gather_age_h, backup_manifest_age_h,
     dedup_report_age_h, morning_summary_age_h, morning_summary_sections_48h,
     l1a_attempt_age_h, l1a_success_age_h, sessionstart_banner_age_h,
     mcp_shim_receipt_age_h, mcp_shim_host_match, mcp_shim_stack_version,
     brand_scope_age_h, brand_scope_misscoped,
     outbox_depth, outbox_replayed_age_h, outbox_drain_log_age_h,
     error?}

W4 (2026-08-07) added the SESSION-scoped receipts that turn the manifest's
eight blind rows into real verdicts. Two design rules carry over from the
nightly ones:

- **Two-signal, never one (review F12).** ``last-l1a`` stamps only on a
  SUCCESSFUL extraction, so "sessions happened, nothing worth extracting"
  looks identical to "the extractor is dead". l1a-extract.ps1 therefore also
  writes ``last-l1a-attempt`` UNCONDITIONALLY at entry, before every early
  return, and the verdict needs attempt-fresh AND success-stale-by-days before
  it says anything bad — capped at 'degraded', never 'dead'.
- **Host-keyed receipts (review F13).** The MCP shim receipt is written by
  whichever box launched the shim; ~/.mem0 travels in stack backups, so a
  restored receipt from another machine must not read as local liveness. The
  receipt carries its host and its stack version (a stale launch-path copy —
  the AMS-02 class — is then caught server-side by version skew).

Sources:
- role: env MEM0_ROLE, else ~/.mem0/stack.env (the WSL operator receipt).
- WSL-native: ~/.mem0/backups/manifest-*.json (newest mtime),
  ~/.mem0/dedup-report.jsonl (mtime — the dedup job unlinks+rewrites the report
  every run, so mtime is a real liveness signal even on zero-delete days),
  ~/.mem0/last-sessionstart-banner (epoch CONTENT — one unconditional line at
  the top of claude-config/storage-cap-check.sh, before its cold-server gate),
  ~/.mem0/mcp-shim-receipt.json (the shim's own start receipt),
  ~/.mem0/brand-scope-status.json (the nightly brand-scope audit's verdict),
  ~/.mem0/outbox.jsonl + outbox.replayed.jsonl + outbox-drain.log (the replica
  offline-outbox legs).
- Windows-side via /mnt/c/Users/<MEM0_WIN_USER> (win user from stack.env):
  .claude/state/last-dream        — epoch-seconds CONTENT (age from the value,
                                    not the mtime — the marker is the throttle's
                                    own record of when the dream last ran),
  .claude/state/last-l1a          — epoch CONTENT; SUCCESS stamp (Mark-Throttle),
  .claude/state/last-l1a-attempt  — epoch CONTENT; UNCONDITIONAL entry stamp,
  .claude/state/dream/prune.json  — consolidation-completed receipt (mtime),
  .claude/state/dream/gather.json — gather-phase receipt (mtime),
  .claude/state/dream/morning-summary.md — mtime + count of '## ' section
                                    headers whose embedded timestamp is within
                                    48h. The file carries mixed/mojibake dashes
                                    (it crosses the CP437 boundary mojibake_check
                                    polices), so the parser keys on the DIGITS
                                    of the timestamp, never on the dash glyphs.
                                    Tail-read (last ~64KB), never whole-file.

House split (sparse_health / mojibake_check pattern): pure computations
(``parse_epoch_content``, ``age_hours``, ``count_recent_sections``) are
separated from the I/O collector ``job_liveness_health``, which NEVER raises —
every field is independently fail-soft (missing file/env -> null + error note).
"""

import json
import os
import re
import socket
import time
import datetime as _dt
from pathlib import Path

# The brand-scope audit writes an ISO 'ts'; drift_state already owns the
# tolerant ISO/epoch parser (PowerShell 'o' 7-digit fractions included). One
# parser, one set of edge cases — reused rather than re-derived. drift_state
# imports nothing local, so there is no cycle.
from drift_state import parse_ts_epoch

STACK_ENV_PATH = Path.home() / ".mem0" / "stack.env"
SECTION_WINDOW_H = 48.0
_TAIL_BYTES = 64 * 1024
# Windows-side epoch stamps are written by BOTH pwsh 7 (scheduled tasks) and
# Windows PowerShell 5.1 (Claude Code hooks). 5.1's `Set-Content -Encoding UTF8`
# emits a BOM, which survives read_text(encoding='utf-8') as U+FEFF and is NOT
# removed by str.strip() — so every WSL-side epoch parse must strip it
# explicitly or a 5.1-written stamp reads as unparseable garbage (review F13).
_BOM = "\ufeff"

# Timestamp embedded in a morning-summary section header, e.g.
# '## Autonomous canonical promotions <dash> 2026-08-07 03:00 (review/demote...)'
# The dash between title and timestamp may be ASCII, en/em dash, or the CP437
# mojibake pair of either — matching digits only makes all of those irrelevant.
_SECTION_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})")


def read_stack_env(path=None):
    """Parse ~/.mem0/stack.env into a dict. Never raises; missing -> {}.

    The shell consumers source it (``. ~/.mem0/stack.env``); this mirrors that
    for the flat KEY=VALUE subset the installer writes ('#' comments and blank
    lines skipped, first '=' splits, whitespace stripped)."""
    p = Path(path) if path is not None else STACK_ENV_PATH
    env = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip()
    return env


def get_role(environ=None, stack_env=None, role_file=None):
    """Box role ('brain'|'replica'|...) from env MEM0_ROLE, else stack.env,
    else the ~/.mem0/role receipt install/2-windows-config.ps1 writes (diff-
    review fix 3: the Windows installer is the role AUTHORITY; ignoring its
    receipt let a stale stack.env default a replica to brain and convict its
    by-design-absent brain jobs as dead_required). None when no source
    declares one (fail-soft — unknown role convicts nothing)."""
    environ = os.environ if environ is None else environ
    role = environ.get("MEM0_ROLE") or (
        stack_env if stack_env is not None else read_stack_env()
    ).get("MEM0_ROLE")
    if not (isinstance(role, str) and role.strip()):
        p = Path(role_file) if role_file is not None else (Path.home() / ".mem0" / "role")
        try:
            role = p.read_text(encoding="utf-8")
        except OSError:
            role = None
    if isinstance(role, str) and role.strip():
        return role.strip().lower()
    return None


# ----------------------------------------------------------------------
# Pure computations (headless-testable, no I/O)
# ----------------------------------------------------------------------

def age_hours(ts_epoch_s, now_epoch_s):
    """Hours elapsed from ts to now, rounded; None on a null timestamp."""
    if ts_epoch_s is None:
        return None
    return round((float(now_epoch_s) - float(ts_epoch_s)) / 3600.0, 2)


def parse_epoch_content(content):
    """Epoch seconds from a marker file's CONTENT (last-dream/last-l1a write
    the epoch as text). None on empty/garbage — never raises.

    BOM-tolerant by contract (review F13): the Windows writers are
    ``Set-Content -Encoding UTF8``, and Windows PowerShell 5.1 — which is what
    runs the Claude Code hooks — spells that with a leading U+FEFF. str.strip()
    does not remove U+FEFF, so without this the very stamps the two-signal rule
    depends on would parse as None and read as 'no signal' forever."""
    try:
        return float(str(content).lstrip(_BOM).strip().lstrip(_BOM))
    except (TypeError, ValueError):
        return None


def count_recent_sections(text, now_dt, window_h=SECTION_WINDOW_H):
    """Count '## ' section headers whose embedded 'YYYY-MM-DD HH:MM' timestamp
    is within window_h of now_dt (naive local time — the writer stamps
    Get-Date local time and both sides of the WSL boundary share the zone).

    Encoding-tolerant by construction: only the digits are matched, so ASCII
    dashes, en/em dashes, and their CP437 mojibake glyph pairs all parse the
    same. Headers without a parseable timestamp are skipped, not counted."""
    count = 0
    for line in (text or "").splitlines():
        if not line.startswith("## "):
            continue
        m = _SECTION_TS_RE.search(line)
        if not m:
            continue
        try:
            ts = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                              int(m.group(4)), int(m.group(5)))
        except ValueError:
            continue
        if (now_dt - ts).total_seconds() / 3600.0 <= window_h:
            count += 1
    return count


# ----------------------------------------------------------------------
# I/O helpers (thin; the collector catches everything)
# ----------------------------------------------------------------------

def tail_text(path, max_bytes=_TAIL_BYTES):
    """Last max_bytes of a file, decoded error-tolerant (the morning summary
    can carry mojibake byte sequences). When the read starts mid-file the
    first (possibly partial) line is dropped so a truncated header cannot be
    miscounted. Raises OSError to the caller — the collector fail-softs."""
    p = Path(path)
    size = p.stat().st_size
    offset = max(0, size - max_bytes)
    with open(p, "rb") as f:
        f.seek(offset)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    if offset > 0:
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return text


def _mtime_age(path, now_s):
    return age_hours(Path(path).stat().st_mtime, now_s)


def _epoch_stamp_age(path, now_s):
    """Age (hours) from an epoch-CONTENT stamp file, or None when the file is
    absent or its content does not parse. Raises OSError to the caller."""
    p = Path(path)
    if not p.exists():
        return None
    return age_hours(parse_epoch_content(p.read_text(encoding="utf-8")), now_s)


def normalize_host(name):
    """Lowercased short hostname (domain stripped). '' / None -> None."""
    if not isinstance(name, str):
        return None
    h = name.strip().lower().split(".", 1)[0]
    return h or None


def count_lines(path):
    """Non-blank line count of a small JSONL file. Raises OSError to the
    caller; the collector fail-softs."""
    p = Path(path)
    if not p.exists():
        return None
    return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())


# ----------------------------------------------------------------------
# Collector
# ----------------------------------------------------------------------

def job_liveness_health(mem0_dir=None, win_home=None, now_s=None,
                        environ=None, stack_env=None, hostname=None):
    """Informational /health/deep check — NEVER raises, never flips ok.

    Every field is independently fail-soft: a missing file/env yields null for
    that field plus a note in ``error``; the remaining fields still populate.
    ``mem0_dir``/``win_home``/``now_s``/``hostname`` are injectable for headless
    tests; the production call site passes nothing."""
    now_s = time.time() if now_s is None else now_s
    mem0_dir = Path(mem0_dir) if mem0_dir is not None else Path.home() / ".mem0"
    senv = stack_env if stack_env is not None else read_stack_env()
    out = {
        # role receipt travels with the injected sidecar dir (fix 3 + test
        # isolation: the live ~/.mem0/role must not leak into a tmp fixture).
        "role": get_role(environ=environ, stack_env=senv,
                         role_file=mem0_dir / "role"),
        "last_dream_age_h": None,
        "prune_age_h": None,
        "gather_age_h": None,
        "backup_manifest_age_h": None,
        "dedup_report_age_h": None,
        "morning_summary_age_h": None,
        "morning_summary_sections_48h": None,
        # --- W4 session-scoped receipts ---
        "l1a_attempt_age_h": None,
        "l1a_success_age_h": None,
        "sessionstart_banner_age_h": None,
        "mcp_shim_receipt_age_h": None,
        "mcp_shim_host_match": None,
        "mcp_shim_stack_version": None,
        "brand_scope_age_h": None,
        "brand_scope_misscoped": None,
        "outbox_depth": None,
        "outbox_replayed_age_h": None,
        "outbox_drain_log_age_h": None,
        # --- W6 D5: job-queue aggregates, read from the FILE mirror
        # (~/.mem0/jobs-heartbeat.json, refreshed by every jobs.py run) —
        # never the sqlite db (the /health/deep budget note + banner purity).
        # jobs_heartbeat_age_h age-gates the rest: a dead queue must not
        # present a stale mirror as current (F7d).
        "jobs_heartbeat_age_h": None,
        "jobs_queued": None,
        "jobs_running": None,
        "jobs_failed_24h": None,
        "jobs_reaped_24h": None,
        "jobs_oldest_running_age_h": None,
        "jobs_oldest_queued_age_h": None,
    }
    notes = []

    try:
        hb_path = mem0_dir / "jobs-heartbeat.json"
        if hb_path.exists():
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            hb_ts = parse_ts_epoch(hb.get("ts"))
            if hb_ts is not None:
                out["jobs_heartbeat_age_h"] = round((now_s - hb_ts) / 3600, 2)
            out["jobs_queued"] = hb.get("queued")
            out["jobs_running"] = hb.get("running")
            out["jobs_failed_24h"] = hb.get("failed_24h")
            out["jobs_reaped_24h"] = hb.get("reaped_24h")
            if hb.get("oldest_running_age_s") is not None:
                out["jobs_oldest_running_age_h"] = round(
                    float(hb["oldest_running_age_s"]) / 3600, 2)
            if hb.get("oldest_queued_age_s") is not None:
                out["jobs_oldest_queued_age_h"] = round(
                    float(hb["oldest_queued_age_s"]) / 3600, 2)
        # No mirror = no queue adoption yet on this box — silent, not an error.
    except (OSError, ValueError) as e:
        notes.append(f"jobs_heartbeat: {e.__class__.__name__}")

    # --- WSL-native sources ---
    try:
        manifests = sorted((mem0_dir / "backups").glob("manifest-*.json"),
                           key=lambda p: p.stat().st_mtime)
        if manifests:
            out["backup_manifest_age_h"] = _mtime_age(manifests[-1], now_s)
        else:
            notes.append("backup_manifest: none found")
    except OSError as e:
        notes.append(f"backup_manifest: {e.__class__.__name__}")

    try:
        dedup = mem0_dir / "dedup-report.jsonl"
        if dedup.exists():
            out["dedup_report_age_h"] = _mtime_age(dedup, now_s)
        else:
            notes.append("dedup_report: missing")
    except OSError as e:
        notes.append(f"dedup_report: {e.__class__.__name__}")

    # W4: SessionStart banner stamp — one unconditional `date +%s >` line at the
    # TOP of storage-cap-check.sh, ahead of its cold-server gate, so the stamp
    # proves the HOOK FIRED even on a morning when the server was still starting
    # and every server-dependent section was skipped.
    try:
        out["sessionstart_banner_age_h"] = _epoch_stamp_age(
            mem0_dir / "last-sessionstart-banner", now_s)
        if out["sessionstart_banner_age_h"] is None:
            notes.append("sessionstart_banner: missing/unparseable")
    except OSError as e:
        notes.append(f"sessionstart_banner: {e.__class__.__name__}")

    # W4 (F13): MCP shim start receipt — HOST-KEYED. ~/.mem0 travels in stack
    # backups, so a receipt restored from another box must never read as local
    # liveness; the stack_version rides along so a stale launch-path copy (the
    # AMS-02 class: Windows .claude/scripts not refreshed by the installer) is
    # caught here rather than discovered 12 days later.
    try:
        rcpt = mem0_dir / "mcp-shim-receipt.json"
        if rcpt.exists():
            d = json.loads(rcpt.read_text(encoding="utf-8").lstrip(_BOM))
            if isinstance(d, dict):
                out["mcp_shim_receipt_age_h"] = age_hours(
                    parse_epoch_content(d.get("ts")), now_s)
                me = normalize_host(hostname if hostname is not None
                                    else socket.gethostname())
                theirs = normalize_host(d.get("host"))
                out["mcp_shim_host_match"] = (
                    None if (me is None or theirs is None) else me == theirs)
                sv = d.get("stack_version")
                out["mcp_shim_stack_version"] = str(sv) if sv else None
            else:
                notes.append("mcp_shim_receipt: not a JSON object")
        else:
            notes.append("mcp_shim_receipt: missing")
    except (OSError, ValueError) as e:
        notes.append(f"mcp_shim_receipt: {e.__class__.__name__}")

    # W4: brand-scope audit verdict (the WRITE-side corroborator for the
    # brand-isolation row; the READ side is the in-process admission probe).
    try:
        bss = mem0_dir / "brand-scope-status.json"
        if bss.exists():
            d = json.loads(bss.read_text(encoding="utf-8").lstrip(_BOM))
            if isinstance(d, dict):
                out["brand_scope_age_h"] = age_hours(parse_ts_epoch(d.get("ts")), now_s)
                n = d.get("n_misscoped")
                out["brand_scope_misscoped"] = int(n) if isinstance(n, int) else None
            else:
                notes.append("brand_scope: not a JSON object")
        else:
            notes.append("brand_scope: missing")
    except (OSError, ValueError, TypeError) as e:
        notes.append(f"brand_scope: {e.__class__.__name__}")

    # W4: offline-outbox legs (replica-scoped row; on a brain box these stay
    # null/zero and F8 keeps the row out of dead_required regardless).
    try:
        # 2026-09-01: depth counts BOTH queue files. A retryable-stopped drain keeps
        # its unfinished ops in outbox.replaying.jsonl; counting only outbox.jsonl
        # reported depth None while a queued update sat stranded for 15 hours —
        # the offline-outbox capability row read "unknown" over a real backlog.
        _ob = count_lines(mem0_dir / "outbox.jsonl")
        _rp = count_lines(mem0_dir / "outbox.replaying.jsonl")
        out["outbox_depth"] = None if (_ob is None and _rp is None) else (_ob or 0) + (_rp or 0)
    except OSError as e:
        notes.append(f"outbox: {e.__class__.__name__}")
    for field, name in (("outbox_replayed_age_h", "outbox.replayed.jsonl"),
                        ("outbox_drain_log_age_h", "outbox-drain.log")):
        try:
            f = mem0_dir / name
            if f.exists():
                out[field] = _mtime_age(f, now_s)
        except OSError as e:
            notes.append(f"{name}: {e.__class__.__name__}")

    # --- Windows-side sources (via /mnt/c) ---
    if win_home is None:
        win_user = (environ or os.environ).get("MEM0_WIN_USER") or senv.get("MEM0_WIN_USER")
        if win_user:
            win_home = Path("/mnt/c/Users") / win_user
        else:
            notes.append("win_state: MEM0_WIN_USER unset (env + stack.env)")
    if win_home is not None:
        win_home = Path(win_home)
        state_dir = win_home / ".claude" / "state"
        dream_dir = state_dir / "dream"

        try:
            marker = state_dir / "last-dream"
            if marker.exists():
                epoch = parse_epoch_content(marker.read_text(encoding="utf-8"))
                if epoch is None:
                    notes.append("last_dream: unparseable content")
                else:
                    # Age from the VALUE, not the mtime — the marker records
                    # when the dream ran, and a copy/restore changes mtime.
                    out["last_dream_age_h"] = age_hours(epoch, now_s)
            else:
                notes.append("last_dream: missing")
        except OSError as e:
            notes.append(f"last_dream: {e.__class__.__name__}")

        # W4 (F12): the l1a two-signal pair. last-l1a is the SUCCESS stamp
        # (Mark-Throttle); last-l1a-attempt is written unconditionally at the
        # extractor's entry, before every early return. Both are epoch CONTENT
        # written by PS 5.1, i.e. BOM-prefixed — parse_epoch_content strips it.
        for field, name in (("l1a_attempt_age_h", "last-l1a-attempt"),
                            ("l1a_success_age_h", "last-l1a")):
            try:
                out[field] = _epoch_stamp_age(state_dir / name, now_s)
                if out[field] is None:
                    notes.append(f"{name}: missing/unparseable")
            except OSError as e:
                notes.append(f"{name}: {e.__class__.__name__}")

        for field, name in (("prune_age_h", "prune.json"),
                            ("gather_age_h", "gather.json")):
            try:
                f = dream_dir / name
                if f.exists():
                    out[field] = _mtime_age(f, now_s)
                else:
                    notes.append(f"{name}: missing")
            except OSError as e:
                notes.append(f"{name}: {e.__class__.__name__}")

        try:
            ms = dream_dir / "morning-summary.md"
            if ms.exists():
                out["morning_summary_age_h"] = _mtime_age(ms, now_s)
                out["morning_summary_sections_48h"] = count_recent_sections(
                    tail_text(ms), _dt.datetime.fromtimestamp(now_s))
            else:
                notes.append("morning_summary: missing")
        except OSError as e:
            notes.append(f"morning_summary: {e.__class__.__name__}")

    if notes:
        out["error"] = "; ".join(notes)[:300]
    return out
