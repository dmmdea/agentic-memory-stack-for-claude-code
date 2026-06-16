# L10 Post-Hoc Memory Audit

**File:** `scripts/wsl/l10-audit.py`
**Timer:** `systemd/l10-audit.timer` (every 6h)
**Output:** `~/.mem0/audit-flags.jsonl`, `~/.mem0/l10-state.json`

## Purpose

L10 is a heuristic post-hoc audit pass that scans every Qdrant point (bypassing mem0's `get_all` top-k cap via the Qdrant scroll API) and writes idempotent flag records for memories that match cheap, deterministic signals. It does NOT auto-promote or mutate any tier — canonical promotion requires explicit user direction via `mem0-canonize.sh`.

## Flag types

| Flag | Trigger |
|---|---|
| `oversize` | `len(text) > 800 chars` |
| `possible-injection` | text contains "ignore previous instructions" or similar |
| `possible-credential` | text contains `password:`, `api_key:`, `bearer `, etc. |
| `missing-provenance` | no `source` field in payload |
| `canonical-without-actor` | tier=canonical but no `tier_actor` recorded |

## Incremental operation

L10 maintains `~/.mem0/l10-state.json` with:
- `last_audit_ts` — Unix timestamp of last run
- `audited_keys` — set of `"{memory_id}:{flag_type}"` already flagged (bounded at 5000)
- `reviewed_keys` — set of keys an operator has marked as reviewed (see Reviewing flags below)
- `last_durable_candidates` — top 50 evidence-tier memories older than 30d with no flags

New runs only re-flag new records or new flag types on existing records.

## v0.17 F.2.7 — Slow-drip detection

The original `delta>20` spike check (in `Test-MemoryStack.ps1`) catches sudden bursts but misses gradual accumulation (e.g., +1 flag/day stays under delta indefinitely). Three orthogonal thresholds added in v0.17:

### Threshold 1: Cumulative unreviewed > 50

**Rationale:** 50 unreviewed flags is the point where manual review becomes a meaningful backlog. Below 50 the list is browsable in a single sitting; above 50 it starts to pile up. The threshold was chosen conservatively — typical healthy runs accumulate 2–5 new flags/week.

### Threshold 2: Average new flags/day > 3.0 for last 5 days (slope)

**Rationale:** 3 new flags/day = ~21/week. A spike of 3/day doubling weekly suggests either a new content pattern or a misconfigured source that is repeatedly writing bad memories. The 5-day window smooths single-bad-session spikes while catching trends that persist more than a couple of days.

### Threshold 3: Any flag persists unreviewed > 7 days

**Rationale:** Flags are cheap to write and easy to ignore. A flag that has been sitting unreviewed for a week is a signal that the operator either forgot about it or is avoiding it. 7 days was chosen as long enough to not trigger on a normal weekend gap, but short enough to catch neglect before it becomes a habit.

## Reviewing flags

To mark a flag as reviewed and suppress future alerts for it:

```bash
# Add to state["reviewed_keys"]:
python3 -c "
import json; from pathlib import Path
state = json.loads(Path.home().joinpath('.mem0/l10-state.json').read_text())
state.setdefault('reviewed_keys', []).append('<memory_id>:<flag_type>')
Path.home().joinpath('.mem0/l10-state.json').write_text(json.dumps(state, indent=2))
print('marked reviewed')
"
```

## Alert output

Slow-drip alerts are written to stderr. In systemd journal: `journalctl --user -u l10-audit.service | grep SLOWDRIP`.

The three alert prefixes:
- `L10 audit WARNING: SLOWDRIP-CUMULATIVE: ...`
- `L10 audit WARNING: SLOWDRIP-SLOPE: ...`
- `L10 audit WARNING: SLOWDRIP-PERSIST: ...`

## v0.18+ deferrals

- Cross-brand contamination detector (flag memories where `brand` field differs from `user_id` pattern)
- Embedding drift detector (cosine similarity between a memory's embedding and its stored text)
- Auto-purge of `retrievable=false` records after 90-day no-read window (target 2026-09-13)
