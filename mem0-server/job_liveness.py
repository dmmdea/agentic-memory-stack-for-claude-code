"""W3 (2026-08-07): nightly-job liveness for /health/deep — informational.

Every nightly mouth of the stack (dream consolidation, prune/gather receipts,
backup manifests, the dedup report, the morning summary) leaves a file behind.
None of those files were watched: a dead scheduled task stayed invisible until
an operator happened to look. This check turns each receipt's AGE into a
/health/deep field so the capability manifest (capabilities.py) can say which
mouths have gone quiet — detection latency of one session/night is by design
(no watchdog; the check is read when a human or a session reads /health/deep).

Shape (fixed cross-track contract — do not rename keys):
    {role, last_dream_age_h, prune_age_h, gather_age_h, backup_manifest_age_h,
     dedup_report_age_h, morning_summary_age_h, morning_summary_sections_48h,
     error?}

Sources:
- role: env MEM0_ROLE, else ~/.mem0/stack.env (the WSL operator receipt).
- WSL-native: ~/.mem0/backups/manifest-*.json (newest mtime),
  ~/.mem0/dedup-report.jsonl (mtime — the dedup job unlinks+rewrites the report
  every run, so mtime is a real liveness signal even on zero-delete days).
- Windows-side via /mnt/c/Users/<MEM0_WIN_USER> (win user from stack.env):
  .claude/state/last-dream        — epoch-seconds CONTENT (age from the value,
                                    not the mtime — the marker is the throttle's
                                    own record of when the dream last ran),
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

import os
import re
import time
import datetime as _dt
from pathlib import Path

STACK_ENV_PATH = Path.home() / ".mem0" / "stack.env"
SECTION_WINDOW_H = 48.0
_TAIL_BYTES = 64 * 1024

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
    """Epoch seconds from a marker file's CONTENT (last-dream writes the epoch
    as text). None on empty/garbage — never raises."""
    try:
        return float(str(content).strip())
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


# ----------------------------------------------------------------------
# Collector
# ----------------------------------------------------------------------

def job_liveness_health(mem0_dir=None, win_home=None, now_s=None,
                        environ=None, stack_env=None):
    """Informational /health/deep check — NEVER raises, never flips ok.

    Every field is independently fail-soft: a missing file/env yields null for
    that field plus a note in ``error``; the remaining fields still populate.
    ``mem0_dir``/``win_home``/``now_s`` are injectable for headless tests; the
    production call site passes nothing."""
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
    }
    notes = []

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
