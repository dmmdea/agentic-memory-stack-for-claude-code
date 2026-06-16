# Tier Policy

Tiers are the trust layer of semantic memory. Every memory has a tier that controls who can write it, who can promote it, and how Claude should weight it against other sources. Tier enforcement is **server-side**: no amount of clever metadata will bypass it — the server returns `403`.

## Tier Matrix

| Tier | Who can write (POST) | Who can promote (PATCH /tier) | Typical lifetime | Trust level |
|---|---|---|---|---|
| `evidence` | Any caller | Any actor (incl. `claude-autonomous`) | Days–weeks | Advisory — verify before acting |
| `temporal` | Any caller | Any actor | Set by `valid_until` metadata | Time-scoped — check window |
| `stable` | Not directly | Any actor after manual review | Months | Background context |
| `insight` | `c1-consolidator` or `dream-consolidator` only (source enforcement) | Actor containing `c1` or `consolidator` only | Weeks–months | Synthesized — trust unless contradicted |
| `canonical` | Never via POST | Actor must be `user-direct` + non-empty `reason` | Indefinite | Ground truth — the operator explicitly locked this in |

## Server-Enforced Constants (from `mem0-server/app.py`)

```python
ADD_ALLOWED_TIERS = {"evidence", "temporal"}        # POST /v1/memories
CANONICAL_REQUIRES_USER_DIRECT = True               # actor must be "user-direct" for canonical
INSIGHT_REQUIRES_C1 = True                          # actor must contain "c1" or "consolidator"
MAX_MEMORY_CHARS = 1500                             # 413 if exceeded; break into atomic facts
```

These constants are quoted from the live source. Do not assume they changed without reading `app.py`.

**Enforcement details:**

- `POST /v1/memories` with `metadata.tier=canonical` → `403 "add: tier='canonical' not allowed on POST"`.
- `POST /v1/memories` with `metadata.tier=insight` and `source` not containing `c1` or `consolidator` → `403`.
- `PATCH /v1/memories/{id}/tier` with `tier=canonical` and `actor != "user-direct"` → `403`.
- `PATCH /v1/memories/{id}/tier` with `tier=canonical` and empty `reason` → `400`.
- `PATCH /v1/memories/{id}/tier` with `tier=insight` and actor not containing `c1` or `consolidator` → `403`.
- `PATCH /v1/memories/{id}/tier` with missing `actor` → `400 "actor is required"`.

## Actor Values

| Actor string | Meaning | Allowed for |
|---|---|---|
| `user-direct` | the operator explicitly said to lock this in | canonical + any tier |
| `c1-consolidator` | Nightly C1 consolidator | insight + any tier |
| `dream-consolidator` | Dream-skill consolidator (D.1) | insight + any tier |
| `claude-autonomous` | Claude acting without explicit the operator direction | evidence, stable, temporal only |

## Ledger Format

Every tier change (promotion or demotion) and every insight add appends one JSON line to `~/.mem0/tier-ledger.jsonl`:

```jsonl
{"ts": "2026-06-09T03:12:44+00:00", "event": "tier-change", "memory_id": "7f3a...", "tier": "canonical", "actor": "user-direct", "reason": "the operator said to lock this in"}
{"ts": "2026-06-09T03:12:50+00:00", "event": "add", "memory_id": "9c2b...", "tier": "insight", "actor": "c1-consolidator", "reason": "C1 add (window_evidence_count=18)"}
```

**Event types (v0.13):**
- `tier-change` — any PATCH /tier call that succeeds (promotion or demotion)
- `add` — insight add via POST (C1 path only)
- `metadata-merge` — PATCH /metadata (added in C.1, lands Phase C)
- `decay-delete` — DELETE called by decay scanner (added in D.2, lands Phase D)

The ledger is append-only. Never truncate it. It is the audit trail for all consequential memory operations.

**Location:** `~/.mem0/tier-ledger.jsonl` (WSL home). The same path is accessible from Windows at `\\wsl.localhost\Ubuntu\home\youruser\.mem0\tier-ledger.jsonl`.

## Rationale

**Why canonical cannot be added directly:** The audit (2026-06-08) found that the old `memory_promote` MCP tool hardcoded `actor="claude"`, meaning autonomous Claude could silently elevate any evidence to canonical. This removed the "the operator explicitly decided" guarantee. The server now enforces `actor=user-direct` — a string that only a human-instructed action would send.

**Why insight is consolidator-only:** The 3am Codex consolidator sees the full evidence window (last 36h, 30 memories) and synthesizes cross-cutting patterns. An in-conversation Claude seeing a smaller slice would produce lower-quality, noisier insights that contaminate the cross-session signal. Restricting insight writes to the consolidator keeps the tier semantically clean.

**Why the ledger is single-append (v0.13 simplification):** The prior v0.12 design used a two-phase intent/complete protocol with a `change_id` field — "write intent, then write complete." This was over-engineered for a single-process FastAPI server where the Qdrant write and the ledger write happen sequentially in the same request. The intent line provided no recovery benefit (if the server crashed between intent and complete, the memory was already written to Qdrant — there was nothing to roll back). Simplified to single-append after the successful Qdrant update. All enforcement is preserved.

---

## Canonical Promotion (v0.14+)

Canonical promotion has a dedicated credential layer that keeps it outside agent-controlled space. No agentic Claude path — including the MCP shim — can reach `tier=canonical`.

### Credential file

`~/.mem0/canonical-key` (mode 600, WSL home) is generated once by `generate-canonical-key.sh`. It is separate from the regular API key and must never be committed to the repo or echoed in logs.

### CLI (day-to-day procedure for the operator)

```bash
bash /path/to/agentic-memory-stack-for-claude-code/scripts/wsl/mem0-canonize.sh <memory_id> "<reason>"
```

`mem0-canonize.sh` is the **only** tool that reads `canonical-key` and signs the request. Since v0.19 Phase G it computes an HMAC-SHA256 token over the format-2 message `<ts>|<nonce>|promote|<memory_id>|<reason>` and sends three extra headers:

| Header | Value |
|---|---|
| `X-User-Direct-Token` | Base64(HMAC-SHA256(canonical-key, `<ts>|<nonce>|promote|<mid>|<reason>`)) |
| `X-User-Direct-Ts` | ISO 8601 UTC timestamp (must be within 5 min of server clock) |
| `X-User-Direct-Nonce` | uuid4, single-use (replay-protected via `~/.mem0/canonical-replay.jsonl`) |

### Server enforcement

The server validates the headers when `tier=canonical` is requested (v0.20 Phase G: all via `security_invariants.validate_hmac_user_direct`, action `promote`):

1. Rejects if `X-User-Direct-Nonce` is missing (403 — format-1 tier promotion was removed in v0.20; the message names the format-2 payload and mem0-canonize.sh).
2. Rejects if `canonical-key` is absent (runtime tmpfs / DPAPI / plaintext all empty) (503).
3. Rejects if token or ts header is missing (403 — tells the caller to use the CLI).
4. Rejects if `X-User-Direct-Ts` is >5 min skewed from server UTC (403).
5. Rejects on HMAC mismatch (403) — verified BEFORE the nonce is burned (MED-8).
6. Rejects a reused nonce (403 replay detected).

### MCP shim behaviour

`memory_promote(tier='canonical', ...)` raises `ValueError` immediately — it never reaches the server. Claude must ask the operator to run the CLI instead.

### Ledger transport field (v0.14)

Tier-ledger entries gain a `transport` field: `"cli-user-direct"` for HMAC-validated promotions, `"rest-api"` for all other tier changes. This makes canonical entries auditable.

---

---

## Canonical Immutability (v0.17 Phase A — closes Codex HIGH-2)

### The gap that was closed

Before v0.17 Phase A, the "front door" was locked (PATCH /tier to canonical required HMAC) but the "side doors" were open: PUT /v1/memories/{id}, DELETE /v1/memories/{id}, and PATCH /v1/memories/{id}/metadata could all mutate or destroy a canonical-tier memory without any HMAC gate. A REST caller with only the regular API key could silently overwrite or delete a locked canonical decision.

v0.17 Phase A closes all three side doors by wiring the same HMAC credential requirement into those endpoints before the underlying operation executes.

### Policy matrix

| Current tier | Action | Gate required |
|---|---|---|
| canonical | PUT (update text) | HMAC user-direct token (format 2) |
| canonical | DELETE | HMAC user-direct token (format 2) |
| canonical | PATCH /metadata | HMAC user-direct token (format 2) |
| canonical | PATCH /tier (promote/demote/re-promote) | HMAC user-direct token (format 2, action `promote`, since v0.19 G); nonce-less format-1 **rejected since v0.20 G** (403) |
| insight | PUT | actor ∈ INSIGHT_ALLOWED_ACTORS OR HMAC user-direct (format 2) |
| insight | DELETE | actor ∈ INSIGHT_ALLOWED_ACTORS OR HMAC user-direct (format 2) |
| insight | PATCH /metadata | actor ∈ INSIGHT_ALLOWED_ACTORS OR HMAC user-direct (format 2) |
| stable / evidence / temporal | any | no extra gate (existing flow) |

### Two signed-payload formats

**Format 1** — tier promotion legacy (v0.14–v0.19; deprecated in v0.19, **REMOVED in v0.20 Phase G**):
```
<ts>|<memory_id>|<reason>
```
Produced by: pre-v0.19 `mem0-canonize.sh <mid> "<reason>"` (no nonce, no replay protection)
Status: **rejected.** A tier promotion without `X-User-Direct-Nonce` → 403 `"X-User-Direct-Nonce required: format-1 tier promotion was removed in v0.20 — sign format-2 <ts>|<nonce>|promote|<mid>|<reason> (mem0-canonize.sh does this)"`. The inline format-1 gate in app.py is deleted; even a validly-signed format-1 token is refused before any validation. No shipped consumer produces format-1 (verified at v0.19 G and re-swept at v0.20 G).

**Format 2** — all signed operations (v0.17+; nonce REQUIRED in the signed payload since v0.18 MED-7; tier promotion since v0.19 G):
```
<ts>|<nonce>|<action>|<memory_id>|<reason>
```
where action ∈ {promote, put, delete, patch_metadata, merge_goals}
Used by: `bash mem0-canonize.sh [<no flag → promote> | --action put|delete|patch_metadata] <mid> "<reason>"` (the CLI generates the uuid4 nonce and sends it as `X-User-Direct-Nonce`)
Validated by: `security_invariants.validate_hmac_user_direct()` called from the respective endpoint. The v0.17 no-nonce variant (`<ts>|<action>|<memory_id>|<reason>`) is no longer accepted (v0.18 MED-7).

**Format-2 promote (v0.19 Phase G)** — PATCH /tier canonical promotion:
- Signed payload: `<ts>|<nonce>|promote|<memory_id>|<reason>`
- Headers: `X-User-Direct-Token` (base64 HMAC-SHA256 over the payload), `X-User-Direct-Ts` (ISO 8601 UTC, 300s skew window), `X-User-Direct-Nonce` (uuid4, burned in `~/.mem0/canonical-replay.jsonl`)
- MED-8 semantics hold on this path: the HMAC is verified BEFORE the nonce is recorded, so invalid-token spam cannot burn nonces or grow the replay store; a valid token with a reused nonce → 403 replay.
- Producers: `mem0-canonize.sh` promotion path (no `--action` flag) and Test-MemoryStack.ps1 I3.

The action word is inside the signed payload, so the formats/actions are **intentionally non-interchangeable**: a promote token cannot be replayed as a put/delete/patch_metadata token (and vice versa) — the server validates against the expected action for each endpoint, so a mismatch produces HMAC mismatch (403).

### mem0-canonize.sh CLI (v0.17+ with --action flag)

```bash
# Tier promotion (same invocation since v0.14; signs format-2 action=promote since v0.19 G):
bash mem0-canonize.sh <mid> "<reason>"

# Update text on a canonical record (v0.17 new):
bash mem0-canonize.sh --action put <mid> "<reason>" --text "<new memory text>"

# Delete a canonical record (v0.17 new):
bash mem0-canonize.sh --action delete <mid> "<reason>"

# Patch metadata on a canonical record (v0.17 new):
bash mem0-canonize.sh --action patch_metadata <mid> "<reason>" --metadata-json '{"key": "value"}'
```

The CLI is the **single signing surface**. Never manually construct the HMAC + curl — the CLI ensures the signed payload format matches what the server expects.

### Shared security module

All new gate logic lives in `mem0-server/security_invariants.py`. Key exports:

- `fetch_current_tier(client, collection_name, memory_id)` — Qdrant payload lookup
- `validate_hmac_user_direct(memory_id, action, reason, token, ts)` — format-2 HMAC validation
- `validate_insight_actor(actor, token, ts, memory_id, action, reason)` — insight allowlist OR HMAC
- `assert_writable(client, collection_name, memory_id, action, token, ts, actor, reason)` — policy matrix orchestrator

The PATCH /tier gate routes through `security_invariants.validate_hmac_user_direct()` (format 2, action `promote`) for every canonical promotion (v0.19 G). v0.20 Phase G removed the nonce-less format-1 inline gate from app.py — a promotion without `X-User-Direct-Nonce` is rejected 403 before any validation, and the `warn_deprecated_format1_tier_promotion` helper was retired with it.

### TOCTOU note (accepted risk, v0.18+)

`fetch_current_tier` and the actual mutation are not a single atomic Qdrant operation. A theoretical race exists where tier changes between the fetch and the mutation. This is accepted risk for v0.17: exploiting it requires both the regular API key AND the canonical-key simultaneously — an attacker with both could edit the record directly. v0.18+ may address this with optimistic locking.

---

---

## Threat Model (v0.17 F.1.3 — explicit)

This stack runs on the operator's personally-owned laptop. The threat model is:

**Single-user laptop, single-user trust scope.**

What this means concretely:
- Same-user processes can read `~/.mem0/canonical-key` and `~/.mem0/api-key`. Mode 600 protects from OTHER OS users on the same machine — it does NOT protect from Claude, Codex, MCP shims, or any subprocess running as `youruser`.
- The canonical-tier hardening (HMAC, nonce replay protection) defends against MCP/REST surface attacks but NOT against direct same-user shell execution. An agent with shell access can read the canonical-key and sign valid tokens.
- The defense for that case is "MCP shim and Codex have no code path that reads canonical-key" — not filesystem permissions. It is protected by absence of code, not by isolation.

**What this stack DOES protect:**
- API key file mode 600 + loopback-only services (`127.0.0.1:18791`) → defends against OTHER OS users on the same machine.
- HMAC + nonce replay (v0.17 F.1) → defends against captured token replay within the skew window (300s).
- PATCH /tier + PUT + DELETE + PATCH /metadata gates → defends against MCP/REST surface attacks attempting tier mutation.
- Brand isolation in storage → defends against cross-brand bleed in normal retrieval (recall surface).
- `_canonical_intent` evidence excluded from default search (v0.17 F.1.2) → defends against privilege-escalation oracle attack (batch-promote via marker scan).

**What this stack does NOT protect:**
- the operator's user agent (Claude, Codex, an MCP shim) compromising itself or being prompt-injected into reading `canonical-key`. Such an agent has full same-user filesystem access.
- A malicious local process running as `youruser` after agent compromise.
- An adversary with physical access to the unlocked laptop.

**Threat-model upgrade path (v0.18+):**
- DPAPI / Windows Credential Manager or hardware-backed isolation for canonical-key.
- Out-of-band confirmation for canonical promotion (e.g., touch-confirm on a separate device).
- Full retrieval admission gate (scope match, tier policy, recency, brand/workspace, contradiction/supersession, task relevance, rejected-candidate logging).

---

## HMAC Nonce / Replay Protection (v0.17 F.1.1)

### Problem

Phase A (v0.17) validates HMAC signature and timestamp skew (300s window) but does not prevent replay within the window. A captured `X-User-Direct-Token` can be replayed multiple times during the 5-minute validity window.

### Fix: nonce tracking

Each signed mutation request (PUT/DELETE/PATCH-metadata) now includes a unique `X-User-Direct-Nonce` header (uuid4 generated by `mem0-canonize.sh`). The server records used nonces in `~/.mem0/canonical-replay.jsonl` and rejects any request whose nonce has been seen.

**Backward compatibility (CLOSED in v0.18 MED-7):** The v0.17 fallback — accepting a no-nonce token signed in the Phase A format (`<ts>|<action>|<mid>|<reason>`) — left a 300s replay window and was REMOVED in v0.18. On every format-2 mutation (PUT/DELETE/PATCH-metadata/merge_goals) the nonce is now mandatory: missing `X-User-Direct-Nonce` → 403.

**GC policy:** The replay store prunes entries older than 600s (2× skew window) lazily on each call. No separate maintenance task is needed. A full corpus GC is a separate v0.18+ task.

**Signed-payload format with nonce (v0.17 F.1):**
```
<ts>|<nonce>|<action>|<memory_id>|<reason>
```
The nonce is included in the signed payload (not just the header) so an attacker cannot strip it and downgrade to the non-nonce format.

**Replay store location:** `~/.mem0/canonical-replay.jsonl` (WSL home). Each entry: `{"nonce": "<uuid4>", "ts": "<ISO8601>"}`.

### Format-1 (PATCH /tier promotion) nonce status — deprecation closed out (v0.18 LOW-4 → v0.19 G → v0.20 G)

Current state, verified against `app.py` + `security_invariants.py` at v0.20 Phase G:

- **Format-2 mutation tokens** (PUT / DELETE / PATCH-metadata / merge_goals): nonce **required** since v0.18 MED-7. No replay window remains on this path.
- **Format-2 promote tokens** (PATCH /tier, payload `<ts>|<nonce>|promote|<memory_id>|<reason>`, since v0.19 G): nonce **required**, replay-protected, MED-8 HMAC-before-nonce ordering. This is what `mem0-canonize.sh` and Test-MemoryStack I3 sign — and since v0.20 it is the **only** accepted tier-promotion token.
- **Format-1 tier-promotion tokens** (PATCH /tier, payload `<ts>|<memory_id>|<reason>`): **REJECTED since v0.20 G.** Missing `X-User-Direct-Nonce` → immediate 403 naming format-2 + mem0-canonize.sh; the inline gate and its deprecation-WARN helper are deleted. The v0.18 LOW-4 residual 300s replay window is fully closed — no nonce-less HMAC path remains anywhere in the server.

**Deprecation schedule (closed):**

| Version | Format-1 (nonce-less) status |
|---|---|
| v0.18 | Accepted; documented residual 300s replay window |
| v0.19 (**SHIPPED**, Phase G) | Format-2 promote (`promote` ∈ VALID_HMAC_ACTIONS, nonce + replay protection) live; format-1 still accepted but logs a deprecation WARN per use; `mem0-canonize.sh` promotion path + Test-MemoryStack I3 switched to format-2 (zero WARNs from shipped consumers) |
| v0.20 (current — **SHIPPED**, Phase G) | **Rejected** — `X-User-Direct-Nonce` required on PATCH /tier canonical promotion; format-1 tokens → 403 naming format-2 + mem0-canonize.sh; inline gate + WARN helper removed; consumers re-swept format-2-only |

---

## Delete Semantics (v0.17 F.1.4)

mem0 v2.0.4 SDK's `delete()` accepts `delete_linked: bool = False`. When `True`, deleting a memory also removes older memories it superseded transitively.

**Policy:**

| Delete path | `delete_linked` | Rationale |
|---|---|---|
| Automatic / scheduled (decay-scan, semantic-dedup) | `False` (explicit) | Preserve supersession chain for ledger replay and stack-restore |
| User-direct CLI (`mem0-canonize.sh --action delete`) | `False` (default) | Safe default — chain preserved |
| User-direct CLI with `--cascade` flag | `True` (opt-in) | Full chain removal; irreversible |

**Usage:**
```bash
# Default — preserves chain:
bash mem0-canonize.sh --action delete <mid> "<reason>"

# Cascade — removes linked memories too (irreversible):
bash mem0-canonize.sh --action delete <mid> "<reason>" --cascade
```

The `cascade` field is recorded in the tier-ledger `delete` event for audit purposes.

---

## `_canonical_intent` Evidence Filter (v0.17 F.1.2)

v0.16.1 introduced client-side auto-downgrade: when an agent tries to set `tier='canonical'` and the gate blocks it, the memory is written as `tier='evidence'` with `metadata._canonical_intent=True` as a "promote me when the operator confirms" marker.

**Problem:** These records surface in default search results. This is a privilege-escalation oracle: if a bad agent grabs `canonical-key`, it can read the marker and batch-promote all `_canonical_intent` memories silently.

**Fix (v0.17 F.1.2):** The search endpoint excludes `_canonical_intent` records (truthy check since v0.18 MED-5) from default results. To retrieve them explicitly, pass `filters.include_canonical_intent: True` in the search body.

**Not a privilege barrier (v0.18 LOW-5):** `include_canonical_intent` is a server-side *post-filter directive*, not an authorization gate. The flag is stripped before the Qdrant query (it never reaches the vector store) and only controls whether the search endpoint's default exclusion of `_canonical_intent` records is applied. Any caller holding the regular API key can pass `filters.include_canonical_intent: true` and see the markers — no extra credential is checked. Its value is oracle-convenience reduction (markers don't surface in DEFAULT retrieval, so a casually compromised agent doesn't trip over them). The actual privilege barrier remains the HMAC canonical-key requirement on promotion: an attacker who enumerates the markers still cannot promote anything without `~/.mem0/canonical-key`.

---

## Recovering from a Bad Insight

When a consolidator posts an insight that turns out to be wrong, stale, or cross-brand contaminated, use this playbook.

### Step 1 — Find the bad insight

Option A — MCP search:
```python
memory_search(query="<phrase from the bad insight>", user_id="youruser")
```

Option B — grep the tier-ledger:
```bash
grep '"tier": "insight"' ~/.mem0/tier-ledger.jsonl | grep '"event": "add"'
```

Note the `memory_id`.

### Step 2 — Inspect lineage

Fetch the record and read `metadata.source_memory_ids`. These are the evidence records the consolidator used as input. Understanding lineage tells you whether the problem was bad evidence or bad synthesis.

### Step 3 — Decide: demote or delete

**Demote (preferred — preserves history):** PATCH `/tier` with `tier='evidence'` or `tier='stable'`, `actor='user-direct'`, and a reason. Demoting from `insight` → `evidence` does **not** require the HMAC token — only canonical promotions need it. The MCP shim's `memory_demote` works for this:

```python
memory_demote(memory_id="<id>", tier="evidence", reason="bad insight: <reason>")
```

The demotion is ledger-logged as `event=tier-change`.

**Hard-delete (when demotion isn't enough):** DELETE `/v1/memories/{id}?actor=user-direct&reason=...`. This is also ledger-logged (v0.13 change). Use this when the insight content is actively harmful or misleading and should not survive in any tier.

### Step 4 — Leave source evidence intact

Source evidence is intentionally NOT cascade-deleted. It remains as raw evidence for future consolidator runs. The consolidator's next pass will see the demoted/deleted insight is gone and may produce a better synthesis from the same evidence.

### Step 5 — Optional: flag source evidence

If the evidence itself is suspect, PATCH its metadata with `consolidator_warning` so future consolidator runs see the negative signal:

```bash
curl -X PATCH http://127.0.0.1:18791/v1/memories/<source_id>/metadata \
  -H "X-API-Key: $(cat ~/.mem0/api-key)" \
  -H "Content-Type: application/json" \
  -d '{"metadata": {"consolidator_warning": "produced bad insight <insight_id>; verify before re-using"}, "actor": "user-direct", "reason": "flag after bad insight"}'
```

Note: `consolidator_warning` is a free-form key — no allowlist update needed. The restricted-key allowlist in `app.py` covers only internal system keys.

---

## Retired-Record Purge Plan (v0.17 F.4.2)

### Background

v0.13 C.2 marked 366 `backfill-v012` records `retrievable=false` to exclude them from search results while preserving them in Qdrant for ledger replay during stack-restore. These records are invisible to all standard searches (they are filtered out at the search endpoint) but still occupy Qdrant storage.

### v0.17 F.4.2 additions (this phase)

1. **`retired_at` metadata stamp** — `scripts/wsl/stamp-retired-at.py` performs a one-time idempotent backfill: for every Qdrant point with `retrievable=false` that lacks a `retired_at` timestamp, it writes `retired_at = "<run_timestamp>"`. Future retired records will gain `retired_at` at the time the `retrievable=false` flag is written.

2. **90-day hold target** — Target purge date is **2026-09-13** (90 days from the stamp run on 2026-06-11). Records stamped on that date will be eligible for purge on 2026-09-13.

### v0.18 purge script

`scripts/wsl/purge-retired.py --dry-run` will ship in v0.18. Eligibility criteria:

| Condition | Action |
|---|---|
| `retrievable=true` | Never purge (live data) |
| `retrievable=false` AND `retired_at < now - 90 days` NOT met | Skip (too recent) |
| `retrievable=false` AND `retired_at >= 90 days ago` AND any ledger activity in last 30 days | Skip (recently revisited) |
| `retrievable=false` AND `retired_at >= 90 days ago` AND no recent activity | **Purge** |

**Purge procedure:**
1. Find records with `retrievable=false AND retired_at <= now - 90d`.
2. Verify no recent PATCH /metadata calls touched them (grep tier-ledger for `memory_id` in last 30 days).
3. DELETE via mem0 endpoint with `delete_linked=false` (preserve any non-retired linked memories).
4. Log to `~/.mem0/purge-report.jsonl`.

### Rationale for 90-day hold

The hold period ensures:
- Any ongoing ledger replay / stack-restore drill that references these records can complete.
- If a restore scenario requires a retired record's embedding or payload, it has not yet been purged.
- the operator has time to notice if a mistaken retirement needs reversal (PATCH `retrievable=true`).

### stamp-retired-at.py usage

```bash
# One-time backfill (idempotent — safe to re-run):
/home/youruser/apps/mem0-server/.venv/bin/python \
  /path/to/agentic-memory-stack-for-claude-code/scripts/wsl/stamp-retired-at.py

# Dry-run (print what would be stamped, no writes):
/home/youruser/apps/mem0-server/.venv/bin/python \
  /path/to/agentic-memory-stack-for-claude-code/scripts/wsl/stamp-retired-at.py --dry-run
```

## Ledger Audit Baseline (v0.17 → v0.19)

The v0.17 ledger-audit baseline includes:
- 5 non-monotonic timestamps — pre-v0.17 parallel-request races; will not recur post-v0.17 F.4.4 schema_version stamping.
- 23 orphan memory_ids — pre-migration records (mem0 v0.12 backfill that predates ledger conventions); accepted as historical.

Recorded to ~/.mem0/ledger-audit-baseline.json via `ledger-audit.py --baseline`; routine runs use `--accept-baseline` to subtract it before the exit-code decision.

Since v0.19 (M8) the baseline is **identity-based**: `--baseline` records the orphan `memory_id` SET (`orphan_ids`) and the non-monotonic `(prev_ts, ts)` pairs (`monotonic_keys`), and `--accept-baseline` subtracts those two categories **by identity** — a new orphan or violation always surfaces even if an equal number of historical ones disappeared (count subtraction was fungible and could mask new tampering inside the historical allowance). Legacy count-only baselines fall back to count subtraction; re-run `--baseline` to upgrade. `--baseline` also refuses to re-record over an existing baseline when any count grew (ratchet guard) unless `--force` is given.

**Re-baseline policy:** if `--accept-baseline` reports adjusted counts >0, investigate and fix the new findings FIRST; re-baseline (`--baseline --force`) only after root-causing and triage — never to silence growth. Re-baselining on growth permanently normalizes a regression.

**2026-06-12 re-baseline (triaged):** monotonic violations had grown 5→9 since the 2026-06-11 count baseline; the ratchet guard refused a plain `--baseline`. Triage: all 9 (lines 1447–5344) are the same benign class — concurrent mutating API requests (pytest suite, Test-MemoryStack I3 promote/delete probes) stamp `ts` at handler start but append to the ledger in completion order, producing sub-second skew between adjacent entries. The v0.17 note "will not recur post-F.4.4" was wrong: F.4.4 added schema stamping, not append serialization, so the race recurs under any concurrent mutation burst. No tampering signature (same-actor probe/test traffic, skew <2s). Re-recorded with `--baseline --force`; the identity baseline now pins these 9 `(prev_ts, ts)` pairs and the 23 orphan ids, so any 10th violation or 24th orphan surfaces immediately.
