# Admission Gate — Phase-1 (v0.18 Phase C) + Phase-2 (v0.19 Phase I)

Server-side retrieval admission policy for `POST /v1/memories/search`.
Module: `mem0-server/admission_gate.py`. Wired into the search handler in
`mem0-server/app.py`. Tests: `mem0-server/tests/test_admission_gate.py` (31)
plus `tests/test_contradiction_sweep.py` (26) for the offline sweep.

## Why Phase-1 exists

Two findings drove this work:

1. **Codex HIGH-5 (partial closure).** The 2026-06-08 Codex audit flagged that
   retrieval had no admission control: any caller of the search endpoint got
   back whatever the vector store ranked highest, including `tier=canonical`
   and `tier=insight` records, regardless of brand scope or staleness. The
   tier system gated *writes* (promotion requires HMAC) but not *reads*.
2. **Phase 0.D prompt-injection proximate fix.** v0.18 Phase B added a
   client-side 3-layer filter in the UserPromptSubmit hook (tier allowlist,
   brand-match guard, char/result caps) so proactively injected memory cannot
   exfiltrate canonical content into prompts. But that filter only protects
   one caller — the hook. Any other consumer (MCP tools, scripts, curl, a
   compromised agent) still hit the raw endpoint. Phase C is the
   belt-and-suspenders: the same policy enforced at the server boundary, so
   ANY caller benefits.

Defense in depth: client hook (Phase B, layer `phase-0d-client`) + server gate
(Phase C, layer `server-search`). Either can fail without fully opening the
channel.

## What Phase-1 enforces (three dimensions)

`AdmissionPolicy.evaluate(result, scope, query_class)` returns an
`AdmissionDecision(admit: bool, reason: str)` checking, in order:

1. **Tier allowlist** — `metadata.tier` must be in `policy.allowed_tiers`.
   A record with a tier outside the allowlist is rejected with
   `tier_disallowed:<tier>`. Records with **no tier** (legacy data) pass —
   ~186/200 live memories are evidence-tier and most legacy rows carry no
   tier at all; rejecting them would be a mass regression.
2. **Brand match (fail-closed since v0.19 M4)** — two branches:
   - Request scope carries a `brand`: a record whose `metadata.brand` is set
     and differs (case-insensitive since v0.19 M14, matching the client
     layer's PowerShell `-eq`) is rejected with
     `brand_mismatch:<record>_vs_<requested>`.
   - Request scope carries NO `brand`: ONLY null-brand (brand-neutral)
     records are admitted; brand-scoped records are rejected with
     `brand_scope_required:<record>` unless the caller explicitly opts in
     with `filters.allow_cross_brand=true` (stripped before Qdrant; plumbed
     into the gate scope). Before v0.19 this branch was fail-open — a
     brandless `query_class=canonical` search returned every brand's
     canonical+stable records, so the boundary enforced nothing unless the
     caller voluntarily passed a brand filter.

   Null/empty-string-brand records (legacy) are always admitted. With the
   v0.19 fail-closed default, the cross-brand leak is closed at the server
   boundary for brandless queries too; `allow_cross_brand` is the explicit,
   audited opt-out (multi-brand isolation rule).
3. **Recency cap** — only for `query_class="operational"` and only when the
   policy sets `max_age_days`: records older than the cap (per
   `metadata.created_at`, ISO-8601, `Z` tolerated) are rejected with
   `recency:<age>d_exceeds_<cap>d_in_class_operational`. Unparseable or
   missing timestamps pass (fail-open on recency, fail-closed on tier).

Tier and recency apply regardless of scope. The brand check applies on EVERY
request since v0.19: with `scope["brand"]` it rejects mismatches; without it
it admits only null-brand records (see `allow_cross_brand` above). The gate
enforces brand only — `user_id` is filtered upstream by Qdrant, and
workspace/project enforcement is still deferred.

## What Phase-2 adds (v0.19 Phase I — three more dimensions)

4. **Supersession-aware filtering (I.1)** — a record stamped
   `metadata.superseded_by=<newer_mid>` (same payload convention the
   cascade-delete chain in `app.py` walks) is rejected with
   `superseded_by:<mid>` in the durable and operational classes, so the newer
   record surfaces instead. Null/absent pointers are falsy → admitted (legacy
   data carries none). Runs right after the tier check; skipped when the
   policy is `forensic` (history class, below).
5. **Contradiction filtering (I.3)** — a record stamped
   `metadata.contradicts_canonical=<canonical_mid>` by the offline
   contradiction sweep (below) is rejected with `contradicts_canonical:<mid>`
   in durable/operational. No LLM runs at retrieval time — the gate only reads
   the stamp. `contradiction_checked_at` alone (the sweep's NO verdict /
   idempotency marker) never rejects. Skipped for `forensic` policies.
6. **Task-relevance floor (I.2)** — operational class ONLY, and only when the
   result carries a `rerank_score` (the raw bge-reranker-v2-m3 cross-encoder
   logit attached by `reranker.rerank()` when the caller passed
   `rerank=true`): a score below the configured floor rejects with
   `relevance_floor:<score>_below_<floor>`. Absent score → fail-open.
   **Disabled by default** — see the `MEM0_RELEVANCE_FLOOR_OPERATIONAL` knob
   below for the observed score distribution and the recommended enabling
   value.

### The `history` (forensic) query_class

`query_class="history"` gets the durable allowlist **plus canonical** (stable,
evidence, insight, canonical — v0.20 Phase F M13), no recency cap, and
`forensic=True` — which disables the supersession AND contradiction checks
(dimensions 4–5). It exists for forensic/audit queries: "what did we believe
before?", "show me the record the sweep flagged". Brand and tier checks still
apply in full (a cross-brand record stays rejected; a tier outside the
allowlist, e.g. `temporal`, stays rejected). canonical was added in v0.20
because a superseded/contradiction-stamped canonical record was otherwise
unreachable in EVERY class — the canonical class rejects the stamp, every
other class rejected the tier. This is not a trust-boundary change: the same
API key already reads canonical via `query_class="canonical"`; the gate is a
relevance/hygiene layer, not an authorization system (see "Still NOT closed"
below). Pass it like any other class (`query_class: "history"` in the search
body; the MCP shim's `query_class` parameter forwards it).

## Default policy mapping (`default_policy_for_class`)

| query_class  | allowed_tiers        | max_age_days | forensic | relevance_floor | rationale                                  |
|--------------|----------------------|--------------|----------|-----------------|--------------------------------------------|
| durable (default) | stable, evidence, insight | none | no | — | knowledge facts age well; canonical never surfaces by default. insight added in the v0.18 fix-pass — it is consolidator-distilled durable knowledge, and before the fix NO class admitted it (the tier was unreachable read-side) |
| operational  | stable, evidence, insight | 180     | no | from env (default disabled) | operational notes go stale; insight added in v0.19 M2-residual (consolidator insights are durable knowledge; the 180d recency cap still applies) |
| canonical    | stable, canonical    | none         | no | — | explicit canonical query; works WITH the v0.17 F.4.1 tier filter |
| history (v0.19 I.1) | stable, evidence, insight, canonical | none | **yes** | — | forensic escape hatch: admits superseded + contradiction-stamped records; canonical added in v0.20 Phase F (M13) so stamped canonical records stay forensically reachable |

Unknown / missing query_class falls back to durable. The mapping is the
single configuration point today — change the tuple/cap here, not at call
sites.

`query_class` is **case/whitespace-normalized once** at the `apply_admission`
entry point (v0.19 L4/L8): `'Operational'` and `' operational '` behave
exactly like `'operational'`. Before v0.19 a case variant selected the 180d
operational policy but silently skipped the recency rejection (evaluate
compared the raw string), admitting stale records contrary to the documented
cap.

### Explicit `filters.tier="canonical"` escape (v0.18 fix-pass)

The search handler derives the admission class BEFORE calling
`apply_admission`:

```python
_adm_qc = "canonical" if (b.filters or {}).get("tier") == "canonical" else (b.query_class or "durable")
```

An explicit `filters.tier="canonical"` is the same trust posture as
`query_class="canonical"` — both require the API key and both are a
deliberate ask for ground-truth records — so the gate honors it with the
(stable, canonical) allowlist. Before this fix the gate ran with the durable
class on every search, which silently stripped every hit a caller had
explicitly filtered for (330 `tier_disallowed:canonical` rejections in <1 day
of live logs), making the canonical tier unreachable through every shipped
search consumer.

### query_class plumbing in shipped consumers (v0.18 fix-pass)

- **MCP shim** (`scripts/wsl/mem0-mcp-shim.py`): `memory_search` accepts
  `query_class: str = "durable"` and forwards it in the search payload. Pass
  `query_class="canonical"` to retrieve tier=canonical ground-truth records —
  the default class excludes them. (New tool schema takes effect on the next
  Claude session start.) v0.19 M4 adds `brand` (scope the search to one brand
  — pass it when working in a brand context) and `allow_cross_brand` (explicit
  opt-in for a brandless search to return brand-scoped records; without either,
  the fail-closed default returns null-brand records only).
- **Pre-tool hook** (`scripts/windows/pre-tool-check.ps1`, Phase 0.F): the
  canonical-contradiction search now sends `query_class = 'canonical'`. Without
  it the server stripped canonical hits before the hook's PS post-filter ran —
  the contradiction check was dead code from Phase C onward.
- **UserPromptSubmit hook layer-1** (`user-prompt-lib.ps1`) is deliberately
  NOT changed: proactive injection stays on the stable+evidence allowlist.
  The fix only restores deliberate, explicit searches.

## Integration points

1. **Server search handler** (`app.py`, `POST /v1/memories/search`):
   `apply_admission(results, scope, query_class, layer="server-search")`
   runs AFTER the v0.13 retired filter, the v0.17 F.1.2 `_canonical_intent`
   filter, the rerank block, and the v0.17 F.4.1 query_class block — and
   BEFORE the F.2.3 retrieval log, so `returned_count` in
   `retrieval-log.jsonl` reflects post-gate reality. Scope is built from the
   request filters (`user_id`, `brand`).
2. **Client hook** (Phase B, `UserPromptSubmit`): same policy shape applied
   client-side before injection, logging with layer `phase-0d-client`. The two
   layers share the rejected-log file format (unified schema, v0.19 L6).

## Rejected-candidate audit trail

Every rejection appends one JSON line to `~/.mem0/admission-rejected.jsonl`
(server-side that is the WSL home; client-side the Windows home). Both
writers emit the **same five fields** (unified in v0.19 L6 — the client
previously omitted `schema_version`):

```json
{"ts": "...", "memory_id": "...", "reason": "tier_disallowed:canonical",
 "layer": "server-search", "schema_version": "v18"}
```

- `layer` is the provenance label — exactly two values exist:
  - `server-search` — Python gate in `admission_gate.apply_admission`
    (every `POST /v1/memories/search` caller).
  - `phase-0d-client` — PowerShell writer in
    `user-prompt-lib.ps1` / `Select-AdmittedMemoryResults` (UserPromptSubmit
    proactive-injection hook). The label matches the build plan and live log
    history; renaming it would orphan historical entries.
- `schema_version: "v18"` — bump when the entry shape changes (adding the
  field client-side in v0.19 did not change the v18 shape, it completed it).
- **Audit logging is advisory, never fatal (v0.19 M6/M11):** the server wraps
  both `log_rejected` internally and the call site in `apply_admission` —
  an unwritable audit file logs a python-logging WARN and the search still
  returns its admitted results (previously every rejecting search would have
  500'd). The client writer has had the equivalent try/catch since Phase B.
- **Rotation (v0.19 L5):** server-side, rotated at 10MB with the same
  `.jsonl.1`–`.jsonl.5` scheme as `retrieval-log.jsonl` (app.py); client-side,
  rotated at 10MB to a single `.jsonl.1` backup. Previously unbounded.
- File is `chmod 600` after every append (closes the v0.17 SEC MED on
  world-readable audit logs); failures to chmod are swallowed (Windows).
- No memory TEXT is logged — only the id and the reason — so the audit
  trail itself cannot become an exfiltration channel.

## Fail-open / fail-closed reference (pinned by tests, v0.19 L11)

| Input condition | Behavior | Why |
|---|---|---|
| `metadata.tier` missing / `None` / empty (any class, incl. canonical) | **fail-open** — admitted | legacy data carries no tier; rejecting would be a mass regression |
| tier present but outside allowlist | **fail-closed** — `tier_disallowed:<tier>` | the core read-side control |
| `created_at` unparseable (garbage string → `ValueError`) | **fail-open** — admitted | recency is advisory; tier/brand still enforced |
| `created_at` naive (no timezone → `TypeError` on aware-minus-naive) | **fail-open** — admitted | same swallow path as unparseable |
| `created_at` missing | **fail-open** — admitted (recency check skipped) | nothing to compare |
| unknown / missing / case-variant `query_class` | normalized then falls back to **durable** policy | single normalization at `apply_admission` (v0.19 L4/L8) |
| brandless scope + brand-tagged record | **fail-closed** — `brand_scope_required` (v0.19 M4) | `allow_cross_brand` is the explicit opt-in |
| audit log unwritable | **fail-open for availability** — WARN, search proceeds (v0.19 M6/M11) | audit must never break retrieval |
| `superseded_by` truthy (durable/operational) | **fail-closed** — `superseded_by:<mid>` (v0.19 I.1) | the newer record should surface instead; `history` class admits |
| `superseded_by` null/absent | **fail-open** — admitted | legacy data carries no supersession pointer |
| `contradicts_canonical` truthy (durable/operational) | **fail-closed** — `contradicts_canonical:<mid>` (v0.19 I.3) | contradiction of locked ground truth; `history` class admits |
| `contradiction_checked_at` alone (sweep NO verdict) | **fail-open** — admitted | idempotency marker, not a verdict |
| `rerank_score` absent (operational, floor set) | **fail-open** — admitted (v0.19 I.2) | rerank off / reranker down must not empty results |
| `rerank_score` below floor (operational, floor set) | **fail-closed** — `relevance_floor:<score>_below_<floor>` | the only relevance-aware rejection; disabled by default |

## Configuration knobs

- `AdmissionPolicy(allowed_tiers, max_age_days, forensic=False,
  relevance_floor=None)` — construct directly for custom policies (tests do
  this). `forensic=True` disables the supersession + contradiction checks;
  `relevance_floor` is the operational-only rerank-score floor.
- `default_policy_for_class(query_class)` — the mapping above; the only place
  the server reads policy from.
- **`MEM0_RELEVANCE_FLOOR_OPERATIONAL`** (env, float — v0.19 I.2): the
  operational relevance floor. Absent / empty / `0` sentinel / unparseable →
  **disabled** (no floor; the knob exists for tuning, never for surprise
  rejections). Observed live `rerank_score` distribution (2026-06-12,
  bge-reranker-v2-m3 raw logits, 4 varied queries against the production
  corpus): strong on-topic hits +0.69..+4.24; weak/tangential −1.21..−4.89;
  off-topic vector matches −6.77..−8.02; fully irrelevant query hits
  −10.23..−11.01 (observed minimum). **Recommended enabling value: `-15.0`** —
  well below the observed minimum, rejects nothing in today's distribution.
  Set it in the mem0 systemd unit (`Environment=`) and restart.
- `log_rejected(..., target_path=...)` — injectable path for tests; defaults
  to `~/.mem0/admission-rejected.jsonl`.
- `apply_admission(..., layer=...)` — provenance label per call site.

## Threat model

Closed by Phase-1:
- Cross-brand canonical/insight leak through the raw search endpoint (any
  caller, not just the hook).
- Default-class searches returning `tier=canonical` records that downstream
  consumers might inject into prompts (the Phase 0.D exfiltration channel,
  now blocked at both layers). `tier=insight` is admitted in the durable
  class since the v0.18 fix-pass (distilled knowledge, not a secret); the
  client-side hook layer-1 allowlist still excludes it from proactive
  injection.
- Stale operational records resurfacing in `operational` queries past 180d.

Closed by Phase-2 (v0.19 Phase I):
- **Supersession-aware filtering** (I.1) — records stamped `superseded_by`
  are suppressed in default retrieval; `history` is the audited escape hatch.
- **Task-relevance floor** (I.2) — operational-class admission can reject
  catastrophically-irrelevant reranked results (knob shipped disabled).
- **Contradiction detection** (I.3) — offline, not at retrieval time: the
  weekly contradiction sweep stamps candidates that a local LLM judged to
  contradict canonical ground truth, and the gate suppresses them.

Still NOT closed:
- Rich rejected-candidate provenance (full scope snapshot, query hash link
  to `retrieval-log.jsonl`).
- workspace/project scope enforcement (brand only today).
- Callers with the API key can still pass `query_class="canonical"` (or
  `"history"`) to read canonical-tier / suppressed records — by design
  (explicit ask), the gate is not an authorization system; HMAC promotion
  gates remain the write-side control.

## Observability

- `~/.mem0/admission-rejected.jsonl` — who was rejected, why, by which layer.
- `~/.mem0/retrieval-log.jsonl` (v0.17 F.2.3) — per-search record; its
  `returned_count` is post-gate, so `(raw candidates) - (returned_count)`
  spikes show the gate working.
- Live verification (2026-06-11): default-class search for a known canonical
  memory returned evidence-tier only, and the log gained fresh
  `tier_disallowed:canonical` entries with `layer=server-search`. Brand
  mismatches rarely appear live because the upstream Qdrant brand filter
  already scopes most searches — the unit tests cover that path. Since v0.19,
  brandless searches additionally produce `brand_scope_required:<brand>`
  rejections for brand-scoped candidates (fail-closed default; integration
  test in `tests/test_brand_isolation.py`).
- Client-side gate coverage lives in the Pester suite
  (`scripts/windows/tests/`), which is a **release gate** (v0.19 M13): run
  `Invoke-Pester scripts/windows/tests/` with 0 failures before shipping any
  change to `scripts/windows/*.ps1` — no other automated check exercises
  `Select-AdmittedMemoryResults` / `Test-DecisionLikePrompt`. Deployed-copy
  drift for the hook files themselves is caught by Test-MemoryStack's
  "deployed hooks freshness" row (SHA256 repo vs `~\.claude\scripts\`).

## Offline contradiction sweep (v0.19 I.3 runbook)

`scripts/wsl/contradiction-sweep.py` — for each canonical-tier memory, finds
the top-K semantically similar non-canonical, non-retired, non-superseded
records (same `user_id`; same brand or null-brand) via direct Qdrant
`/points/query` with the canonical's stored dense vector, then asks a local
instruct LLM via llama-swap (`:11436/v1/chat/completions`) whether the
candidate contradicts the canonical statement.

- **Judge model:** `offload-e4b` by default (v0.21.1). This is the
  local-offload harness's model — a STABLE ALIAS, so improvements made to it on
  the harness side transparently upgrade this judge with no change here. It
  fits the 8 GB RTX 3070 (~4–4.5 GB, ~6.7/8.2 GB with the model loaded) and runs
  CONCURRENTLY with the persistent `bge-reranker-v2-m3`. **Why not `ministral-14b`
  (the v0.19→v0.21 default):** the v0.19 bake-off picked the 14B on an 8–9-pair
  verdict-quality sample alone, ignoring the binding hardware constraint — the
  14B GGUF is 8.24 GB on an 8.19 GB card and with `--n-gpu-layers 999` it
  overflowed the free VRAM after the persistent group, spilled to RAM, and
  thrashed the reranker off the GPU on every weekly run. A binary contradiction
  YES/NO is short-context classification — exactly what the small offload model
  is for. The discarded alternative `llama-3.2-3b` answered YES 9/9 (uncalibrated
  prompt). Override: `--model`. Re-validate on a labelled set if the harness
  model regresses.
- **Verdict prompt (injection-resistant since v0.20 Phase C, M5):** strict
  YES/NO-first-word with one-line justification, biased conservative ("if
  uncertain, answer NO"); parsed by YES/NO prefix, anything else → pair
  skipped. The memory texts are attacker-influenceable stored content, so they
  are treated as untrusted DATA, never instructions: the system prompt carries
  an explicit data-marking clause, the user message puts the instruction FIRST
  and wraps the texts in `<statement_a>`/`<statement_b>` delimiter blocks, and
  closing-tag collisions inside the texts are neutralized before interpolation
  (no block breakout). The prompt STRUCTURE is pinned by unit tests
  (`test_judge_prompt_*` — model behavior can't be asserted; the contract is).
- **Stamping (only with `--apply`):** YES → candidate gets
  `contradicts_canonical=<canonical_mid>` + `contradiction_checked_at=<iso>`;
  NO → only `contradiction_checked_at` (candidates checked within
  `--recheck-days`, default 7, are skipped — idempotent). Stamps go through
  the mem0 API trusted-actor PATCH path: actor `contradiction-sweep-v019` is
  key-allowlisted in `security_invariants.TRUSTED_PATCH_ACTORS` to write
  EXACTLY those two keys (per-actor mapping since v0.19 I.3 — mirrors
  `stamp-retired-v013`; it cannot write `retired_at` and vice versa). NEVER
  direct Qdrant `set_payload` (H8 lesson: bypasses gate + ledger).
- **Self-healing YES stamps (v0.19 fix-pass):** a YES verdict is no longer
  permanent. Stamped candidates whose `contradiction_checked_at` is older
  than `--recheck-stamped-days` (default 30; `0` = always) are RE-JUDGED;
  a NO re-verdict clears the stamp (`contradicts_canonical=None` via the same
  trusted-actor PATCH — the null shallow-merge makes the gate's `meta.get()`
  falsy, so the record is admitted again). Skip reason while inside the
  window: `stamped-checked-within-<N>d`. A false-positive YES therefore
  self-corrects within one window instead of silently hiding a memory forever.
- **YES-stamp visibility (v0.19 fix-pass):** every run summary carries
  `stamped_ids` (`[{memory_id, canonical_id, justification}]`) and
  `cleared_ids`; Test-MemoryStack's RECOVERY "contradiction sweep" row WARNs
  whenever the last APPLY-mode run has `yes_count > 0`, listing the stamped
  ids — every retrieval suppression gets a human review within a week.
- **Unstamp (false-positive recovery — one command since v0.20 Phase C, M8
  residual):** when the Test-MemoryStack WARN row lists a wrongly-stamped id,
  clear it immediately (don't wait for the recheck window):

  ```bash
  /home/youruser/apps/mem0-server/.venv/bin/python \
    /path/to/agentic-memory-stack-for-claude-code/scripts/wsl/contradiction-sweep.py --unstamp <MEMORY_ID>
  ```

  This prints the BEFORE/AFTER metadata and clears `contradicts_canonical`
  via the SAME trusted-actor PATCH the sweep's clear-on-NO uses (actor
  `contradiction-sweep-v019`; null shallow-merge makes the gate's `meta.get()`
  falsy → the record is admitted again). It runs no sweep and appends nothing
  to the JSONL run log. Exit 0 = cleared (or nothing to clear); nonzero =
  read/PATCH failure. Equivalent raw PATCH (fallback if the script is
  unavailable):

  ```bash
  curl -s -X PATCH http://127.0.0.1:18791/v1/memories/<MEMORY_ID>/metadata \
    -H "X-API-Key: $(cat ~/.mem0/api-key)" -H "Content-Type: application/json" \
    -d '{"metadata": {"contradicts_canonical": null, "contradiction_checked_at": "'"$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"'"}, "actor": "contradiction-sweep-v019", "reason": "manual unstamp - false-positive YES verdict"}'
  ```

  The fresh `contradiction_checked_at` defers re-judging by `--recheck-days`
  (7d). If the temperature-0 judge still answers YES on the re-judge it will
  re-stamp — but now VISIBLY (the Test-MemoryStack WARN row lists the ids), so
  a persistent false positive is a signal to fix the pair at the source:
  reword the candidate/canonical text, or override `--model`.
- **Flags:** `--dry-run` (DEFAULT — prints pairs + verdicts, stamps nothing),
  `--apply`, `--limit N` (canonicals processed, 0 = all; truncation is
  surfaced in the summary + console WARNING), `--top-k` (default 8),
  `--recheck-days` (default 7), `--recheck-stamped-days` (default 30),
  `--user-id`, `--model`, `--unstamp MEMORY_ID` (remediation mode, see above).
- **Resilience + visible failure (v0.20 Phase C, M7/M16):** the sweep never
  crashes mid-pair, but it no longer hides failure behind exit 0. Preflight
  failures (Qdrant/llama-swap/mem0 unreachable, or `--model` not served by
  llama-swap — checked against `GET /v1/models` up front) record
  `outcome=degraded:*` and EXIT NONZERO, so the systemd oneshot shows failed
  in `journalctl --user -u contradiction-sweep`. Per-pair LLM failure/timeout
  → pair skipped (30s per pair; the 120s cold-load budget persists until the
  judge answers once); 5 CONSECUTIVE judge llm-errors abort the run
  (`outcome=degraded:aborted:...`, exit nonzero) instead of N silent skips.
  Mid-run backend failure → abort with partial counts, exit nonzero. The gate
  is unaffected either way.
- **Run log:** every run (incl. dry-run) appends one JSONL summary to
  `~/.mem0/contradiction-sweep.jsonl` (`ts`, `pairs_checked`, `yes_count`,
  `no_count`, `skipped_pairs`, `stamped_count`, `dry_run`, `model`,
  `canonical_total` (pre-`--limit`), `canonical_count` (processed),
  `outcome`...). `outcome` (v0.20) is `ok` | `degraded:<reason>` |
  `no-op:<reason>` — all-pairs-skipped and zero-canonical runs are
  `no-op:*` (exit 0 but WARN-visible). Read by Test-MemoryStack's RECOVERY
  "contradiction sweep" row: WARN on any non-ok outcome, WARN >14d stale,
  `N/M canonicals processed` shown when `--limit` truncated coverage (L6);
  pre-v0.20 entries without `outcome` keep the freshness-only check
  (back-compat). Timer-enabled-but-no-run-yet shows OK with a note.
- **Weekly timer** (systemd-user, mirrors decay-scan; units in `systemd/`):
  `contradiction-sweep.service` (oneshot: `--apply --limit 50`) +
  `contradiction-sweep.timer` (Sun 05:00, Persistent, 15m jitter). Install:

  ```bash
  cp /path/to/agentic-memory-stack-for-claude-code/systemd/contradiction-sweep.{service,timer} ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now contradiction-sweep.timer   # the TIMER only — never enable the service
  systemctl --user list-timers | grep contradiction          # verify
  ```

- **Manual runs:**

  ```bash
  /home/youruser/apps/mem0-server/.venv/bin/python \
    /path/to/agentic-memory-stack-for-claude-code/scripts/wsl/contradiction-sweep.py --limit 3 --top-k 4 --dry-run
  ```

- **Unit tests:** `mem0-server/tests/test_contradiction_sweep.py` (mocked-LLM
  verdict parsing, stamping payloads, eligibility/brand scoping, per-actor key
  allowlist pin; v0.20 Phase C: judge-prompt structure/injection contract,
  4xx degradation, model-availability preflight helper, outcome/exit-code
  classification, `--unstamp` before/PATCH/after via mocked HTTP).
