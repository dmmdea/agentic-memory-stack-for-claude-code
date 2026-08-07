"""W3 (2026-08-07): retrieval-drift guard state for /health/deep — informational.

The nightly dream cycle runs a retrieval-drift guard (a before/after compare
around consolidation) that lives in a PRIVATE evaluation repo and writes its
state to ``~/.mem0/retrieval-drift-state.json``. This module only READS that
receipt: the guard's own death used to be silent (>=2 consecutive snapshot
failures meant nothing was comparing, and nothing said so). Surfacing the
state file on /health/deep gives the capability manifest a mouth for it.

Shape (fixed cross-track contract — do not rename keys):
    {state_present, last_compare_ts, age_hours, before_retrievable, n_total,
     hwm, hwm_seeded, consecutive_below_hwm, consecutive_snapshot_failures,
     alarm, missing (list), compat_fallback, error?}

The file may be ABSENT until the private side deploys — that is
``state_present: false`` with every other field null and NO error (a not-yet-
deployed guard is a fact, not a fault). A present-but-malformed file is
``state_present: true`` plus an error note. Field values pass through from the
file (the private repo owns the writer's schema); ``age_hours`` is computed
here from ``last_compare_ts``. The compat-fallback marker (a legacy guard that
predates --state writes only ``{compat_fallback, ts}``) therefore surfaces as
state_present with null compare fields — exactly what it is.

House split (sparse_health pattern): ``shape_drift_state`` is the pure mapper;
``drift_state_health`` is the thin I/O wrapper that NEVER raises.
"""

import json
import re
import time
import datetime as _dt
from pathlib import Path

DRIFT_STATE_PATH = Path.home() / ".mem0" / "retrieval-drift-state.json"

# Pass-through fields, in contract order. 'last_compare_ts' handled separately
# (age computation); 'state_present'/'age_hours'/'error' are computed here.
_PASSTHROUGH = ("before_retrievable", "n_total", "hwm", "hwm_seeded",
                "consecutive_below_hwm", "consecutive_snapshot_failures",
                "alarm", "missing", "compat_fallback")


def _null_shape(state_present):
    out = {"state_present": state_present, "last_compare_ts": None,
           "age_hours": None}
    for k in _PASSTHROUGH:
        out[k] = None
    return out


def parse_ts_epoch(ts):
    """Epoch seconds from a timestamp the writer may emit: ISO-8601 (including
    PowerShell 'o' with 7-digit fractions and trailing Z) or a bare epoch
    number. None on anything else — never raises."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    if not s:
        return None
    iso = s.replace("Z", "+00:00")
    # PowerShell 'o' emits 7 fractional digits; fromisoformat on older
    # interpreters wants <=6 — trim, digits-only, meaning-preserving.
    iso = re.sub(r"(\.\d{6})\d+", r"\1", iso)
    try:
        dt = _dt.datetime.fromisoformat(iso)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        # Naive stamps are local time on this stack (both sides of the WSL
        # boundary share the zone) — .timestamp() applies the local offset.
        return dt.timestamp()
    return dt.timestamp()


def shape_drift_state(raw, now_s=None):
    """Pure mapper: parsed state dict (or None for absent) -> contract shape."""
    now_s = time.time() if now_s is None else now_s
    if raw is None:
        return _null_shape(False)
    out = _null_shape(True)
    if not isinstance(raw, dict):
        out["error"] = f"state file is {type(raw).__name__}, expected object"
        return out
    out["last_compare_ts"] = raw.get("last_compare_ts")
    epoch = parse_ts_epoch(out["last_compare_ts"])
    if epoch is not None:
        out["age_hours"] = round((now_s - epoch) / 3600.0, 2)
    for k in _PASSTHROUGH:
        if k in raw:
            out[k] = raw[k]
    return out


def drift_state_health(path=None, now_s=None):
    """Informational /health/deep check — NEVER raises, never flips ok.

    Absent file -> state_present false, everything null, NO error (the private
    guard may not be deployed yet). Malformed file -> state_present true plus
    an error note (something IS writing there and it is not the guard's shape).
    """
    p = Path(path) if path is not None else DRIFT_STATE_PATH
    try:
        if not p.exists():
            return _null_shape(False)
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out = _null_shape(True)
        out["error"] = str(e)[:160]
        return out
    try:
        return shape_drift_state(raw, now_s=now_s)
    except Exception as e:  # fail-soft: a mapper bug must not 500 /health/deep
        out = _null_shape(True)
        out["error"] = str(e)[:160]
        return out
