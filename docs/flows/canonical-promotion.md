# Promoting a memory to canonical — the two doors to ground truth

## Purpose

`canonical` is the only cryptographically protected tier: the facts everything else is judged against (the contradiction sweep's anchor set, the NLI write-gate's reference, overriding agent guidance). This flow is how an `evidence` fact *becomes* canonical — and why no plain write ever can. Two independent paths reach the tier, but they converge on **one signing surface** and **one audit ledger**, so promotion is cryptographically uniform whether a human or the nightly dream initiated it. For what the tiers mean, see [`../systems/memory-model.md`](../systems/memory-model.md); for the full server-side tier rule table, [`../systems/tier-policy.md`](../systems/tier-policy.md).

The design bet: **autonomy is structurally bounded.** The dream may act alone, but only through a confidence cap, a dedup, a contradiction/corroboration gate, and the same HMAC key a human uses — an attacker who can only run the consolidator still cannot forge canonical without the key.

## Trigger

Two distinct triggers, never a single one:

| Path | Trigger | Initiator |
|---|---|---|
| **A — operator HMAC canonize** | the operator explicitly decides to lock a fact | a human running [`../../scripts/wsl/mem0-canonize.sh`](../../scripts/wsl/mem0-canonize.sh) |
| **B — dream autopromote** | nightly consolidation (phase 3.5) nominates evergreen `evidence` | `dream-consolidate.ps1`, 03:00 via Task Scheduler |

## Participants

- **`mem0-canonize.sh`** — the **only** tool that reads the canonical key and signs the request; *both* paths call it (path B with `--actor dream-autopromote`). The single signing surface.
- **Canonical-key custody** — the HMAC key resolved at call time from one of three sources (runtime tmpfs / plaintext / DPAPI blob); detail in [`../systems/dpapi-canonical-key.md`](../systems/dpapi-canonical-key.md).
- **`dream-consolidate.ps1` (phase 3.5)** — the nightly orchestrator's autonomous-promotion phase; see [`../systems/dream-skill.md`](../systems/dream-skill.md).
- **`autopromote-lib.ps1`** — the pure nomination pipeline (`Invoke-AutopromoteDecision`) and the **4C promotion gate** (`Invoke-PromotionGate` / `Get-PromotionGateVerdict` / `Resolve-GateBlocked`).
- **Codex CLI** — path B uses it twice: once to *nominate* canonical-worthy evidence, and once (a separate, adversarial pass) as the contradiction judge. Never the proposing pass judging itself.
- **The mem0 server** — `PATCH /v1/memories/{id}/tier` validates the actor, the reason, and the HMAC token via `security_invariants.validate_hmac_user_direct`, then writes the tier ledger. See [`../systems/mem0-api.md`](../systems/mem0-api.md).

## Step-by-step flow

### Path A — the operator HMAC canonize

The operator runs `mem0-canonize.sh <memory_id> "<reason>"`. The CLI:

1. **Reads the canonical key** by resolution order (`resolve_canon_key`): the runtime tmpfs key at `$XDG_RUNTIME_DIR/mem0/canonical-key` (injected while `mem0.service` runs) → a plaintext `~/.mem0/canonical-key` (dev/recovery only) → an inline DPAPI decrypt of `~/.mem0/canonical-key.dpapi`. If none yields a key, it exits without touching the server.
2. **Generates a fresh `uuid4` nonce** (`uuidgen`, python fallback) and an ISO-8601 UTC timestamp.
3. **Signs a format-2 token** — `Base64(HMAC-SHA256(key, "<ts>|<nonce>|promote|<memory_id>|<reason>"))`. The action word `promote` is *inside* the signed payload, so a promote token cannot be replayed as a put/delete/patch_metadata token.
4. **Sends** `PATCH /v1/memories/<id>/tier` with `{tier: "canonical", actor: "user-direct", reason}` plus the three HMAC headers (`X-User-Direct-Token`, `X-User-Direct-Ts`, `X-User-Direct-Nonce`).

The `reason` is a required positional argument (the CLI exits `2` without it) — the audit-trail policy has no "canonize without a reason" path.

### Path B — the dream autopromote nominee → the 4C gate

After the nightly consolidation writes its insights, phase 3.5 may autonomously promote a *few* `evidence` facts. It is precision-first and structurally bounded:

1. **Nominate.** A second Codex call proposes canonical-worthy evidence — evergreen, declarative, ground-truth, cross-session, high-confidence.
2. **Decide the shortlist** (`Invoke-AutopromoteDecision`, a pure pipeline): parse the nominees → **structural filter** (`Test-ImperativeOrTask` rejects task/imperative/standing-order text) → **sort by confidence descending** → **cap at 3** (the rest deferred to the next night) → **dedup** against the existing canonical set.
3. **Gate each survivor** (`Get-PromotionGateVerdict` → `Invoke-PromotionGate`), the **4C** contradiction + source-weighted-corroboration judge:
   - **Contradiction gate (all sources):** if a *second, independent, adversarial* Codex pass judges the nominee to contradict an existing canonical fact → **BLOCK**. The verdict fails safe: an errored canonical fetch or an unparseable judge reply is treated as a contradiction.
   - **Source-weighted corroboration:** a `trusted` source (operator-asserted — `operator-decision` / `user-decision`) fast-tracks on the contradiction gate alone; **every other source class is treated as untrusted** and needs `≥ MinCorroboration` (default **2**) independent observations. Anything not on the trusted allowlist is untrusted by construction.
4. **Promote the survivors** by calling `mem0-canonize.sh --actor dream-autopromote <id> "<reason>"` — the *same* HMAC door as path A, signed with the *same* key. The actor label only distinguishes the two in the ledger.

**The gate ships in shadow mode by default — it does not block by default.** `MEM0_PROMOTION_GATE_MODE ∈ {off, shadow, enforce}`, defaulting to `shadow` when neither the env var nor the receipt's persistent `PromotionGateMode` is set (`dream-consolidate.ps1`, ~line 640–642):

| Mode | What the gate does to the promotion |
|---|---|
| `shadow` (**default**) | computes and **logs** the verdict; the promotion decision is **unchanged** (pure calibration) |
| `enforce` (opt-in) | a BLOCK verdict — **or** a gate *error* (fail-safe) — skips the canonize; the nominee stays `evidence` |
| `off` | kill switch — no verdict computed |

The single place that turns a verdict into a skipped promotion is the unit-tested `Resolve-GateBlocked`: `off`/`shadow` *never* block; only `enforce` does. So on a default install the 4C verdict is observed and recorded, but promotion behavior is identical to having no gate — enforcement is a deliberate, reversible operator flip.

### Where both paths converge — the server tier gate

Both paths land on `PATCH /v1/memories/{id}/tier` with `tier=canonical`, and the server (`app.py update_tier`) applies the same checks in order:

1. **Actor allowlist** — `actor` must be `user-direct` **or** in `CANONICAL_AUTOPROMOTE_ALLOWED` (`{"dream-autopromote"}`); anything else → `403`. This is why path B's actor label is a first-class server constant, not a free string.
2. **Non-empty reason** — else `400`.
3. **Nonce required** — a promotion without `X-User-Direct-Nonce` → `403` (the nonce-less format-1 payload was removed in v0.20).
4. **HMAC validation** — `validate_hmac_user_direct(mid, "promote", reason, token, ts, nonce)`: key present, token/ts present, ≤ 300 s clock skew, HMAC verified **before** the nonce is burned (so invalid-token spam cannot grow the replay store), nonce not already used.
5. **Imperative canary** — the record's stored text is retrieved and, if it reads as a standing order rather than a declarative fact, rejected `422`; if the store can't be read to verify, `503` (fail-safe, never fail-open on a write gate).
6. **Write + ledger** — the Qdrant payload is set (`tier`, `updated_at`, `tier_actor`), then **one** ledger line is appended with `event=tier-change` and a `transport` of `autonomous` (dream), `cli-user-direct` (operator), or `rest-api`.

## Data and state changes

| Write / derived state | When | Where |
|---|---|---|
| The record's tier flips to `canonical` | on a valid PATCH | Qdrant point payload (`tier`, `updated_at`, `tier_actor`) |
| One `tier-change` ledger line (`actor`, `reason`, `transport`) | after the payload write succeeds | the monthly tier ledger `~/.mem0/tier-ledger-YYYY-MM.jsonl` |
| The burned nonce | during HMAC validation | `~/.mem0/canonical-replay.jsonl` (replay store, GC'd past 2× skew) |
| Shadow / enforce gate verdict per nominee (path B) | phase 3.5 | the dream's GATE log + the morning summary (nominee, gate class, source class, corroboration count, contradiction flag) |
| Over-cap / deduped / structural-reject nominees | phase 3.5 | logged and deferred, not promoted |

## Success behavior

A promoted record now sits in `canonical`: it becomes part of the anchor set the reconciliation sweeps judge new facts against, it is admitted only by the `canonical` and `history` query classes (never the per-prompt hot bundle), and it carries no decay or expiry. The ledger holds a signed, attributable record of who promoted it and why. On path B, at most 3 promotions land per night, each confidence-sorted, deduped, gate-observed, and HMAC-signed; a quiet night promotes nothing, which is a normal outcome.

## Failure behavior

- **Bad or missing HMAC** — a wrong actor (`403`), an empty reason (`400`), a missing nonce (`403`), a reused nonce (`403` replay), a skewed timestamp (`403`), or an HMAC mismatch (`403`). The tier does not change and nothing is ledgered.
- **Imperative text** — a nominee/target that reads as a standing order is rejected `422` (non-fatal for the dream — expected for edge cases); a store it cannot read to check the text yields `503` rather than a silent skip.
- **Path B, shadow mode** — a BLOCK verdict changes nothing (by design); the block is only *recorded*. Enforcement requires the explicit `enforce` flip.
- **Path B, gate error** — the 4C gate is wrapped so it can never crash the consolidator; an error becomes a fail-safe BLOCK **in `enforce` only**, and is logged in shadow/off.
- **Offline** — there is **no queued path to canonical.** The MCP `memory_promote` tool refuses `tier=canonical` outright (raises before any network call), and the offline outbox replays `promote` ops as `claude-autonomous`, which the server rejects for canonical. Canonical promotion requires the live authority and the canonical key — it is online-only by construction.

## External dependencies

- **The canonical key** — resolved from tmpfs / plaintext / DPAPI blob; without it, both paths refuse (see [`../systems/dpapi-canonical-key.md`](../systems/dpapi-canonical-key.md)).
- **Codex CLI** (ChatGPT-subscription) — path B's nomination pass and the independent adversarial contradiction judge; serialized by the shared Codex mutex.
- **The mem0 REST server** on `:18791` — the PATCH /tier gate and the tier ledger.
- **Qdrant + EmbeddingGemma** — path B's corroboration and nearest-canonical queries run against the live vector store.
- **Windows Task Scheduler** — hosts the nightly dream that drives path B.

## Invariants and assumptions

1. **No plain write can create canonical.** Only the HMAC-signed CLI reaches the tier; the actor must be `user-direct` or `dream-autopromote`, and both go through the same signing surface.
2. **Autonomy is HMAC-uniform.** The dream's promotion is signed with the same key as the operator's — the actor label is auditing metadata, not a privilege bypass.
3. **The 4C gate is observed-by-default, enforced-by-choice.** Shadow computes and logs; only `enforce` blocks. Never assume "blocks by default."
4. **The contradiction judge is independent and fail-safe.** A second adversarial Codex pass, never the proposer; any unverifiable verdict counts as a contradiction.
5. **At most 3 autopromotions per night**, confidence-sorted and deduped against the existing canonical set.
6. **Canonical promotion is online-only** — no offline/queued route exists.

## Security and privacy notes

The canonical tier's defense is **absence of code, not filesystem isolation**: no MCP shim or Codex path reads the canonical key, so an agent cannot sign a promotion even though it runs as the same OS user (the stack's threat model is single-user; mode-600 defends against *other* OS users, not against a compromised same-user agent — see [`../systems/tier-policy.md`](../systems/tier-policy.md)). The HMAC + single-use nonce defend the REST surface against captured-token replay within the 300 s skew window. Autonomous promotion does not weaken any of this: it signs the identical token via `mem0-canonize.sh --actor dream-autopromote`. Logs and the morning summary carry ids, counts, gate classes, and reasons — not raw memory text where avoidable. All state (key, replay store, ledger) lives under `~/.mem0/`; nothing here opens a LAN listener.

## Observability and debugging

- **Who promoted what, and why** — the tier ledger (`~/.mem0/tier-ledger-YYYY-MM.jsonl`): each `tier-change` line carries `actor`, `reason`, and `transport` (`autonomous` vs `cli-user-direct`).
- **Why a nominee didn't promote (path B)** — the dream's GATE log records each verdict, gate class (`contradiction` / `trusted-source` / `corroborated` / `uncorroborated`), source class, corroboration count, and contradiction flag; the morning summary lists promoted / gate-blocked / deduped / over-cap.
- **Calibrating enforce** — run the dream (or `-DryRun`) in the default shadow mode and read the accumulated verdicts before flipping `MEM0_PROMOTION_GATE_MODE=enforce`; `-DryRun` exercises the full pipeline (including shadow verdicts) while writing nothing.
- **A canonize that 403s** — check actor spelling, the nonce/timestamp headers, and clock skew; the server's error body names the exact rule that failed.
- **Replay-store growth** — `~/.mem0/canonical-replay.jsonl` is GC'd past 2× the skew window; a valid token with a reused nonce is a `403` replay.

## Testing notes

- [`../../scripts/windows/tests/DreamAutopromote.Tests.ps1`](../../scripts/windows/tests/DreamAutopromote.Tests.ps1) — the nomination pipeline: structural filter, confidence sort, cap-at-3, dedup.
- [`../../scripts/windows/tests/DreamGateVerdict.Tests.ps1`](../../scripts/windows/tests/DreamGateVerdict.Tests.ps1) — the 4C gate verdict, `Resolve-GateBlocked` (off/shadow never block; enforce blocks on non-promote or gate error), and the fail-safe contradiction parse.
- The mem0-server test suite exercises the `PATCH /tier` gate (actor allowlist, non-empty reason, nonce requirement, HMAC validation) and the imperative canary.
- Validate an end-to-end change with the dream's `-DryRun`, which runs the full decision path and shadow verdicts without writing.

## Source map

- [`../../scripts/wsl/mem0-canonize.sh`](../../scripts/wsl/mem0-canonize.sh) — the single signing surface (key resolution, nonce, format-2 HMAC, PATCH /tier), used by both paths.
- [`../../scripts/windows/dream-consolidate.ps1`](../../scripts/windows/dream-consolidate.ps1) — phase 3.5 autopromotion and the gate-mode resolution (shadow default ~line 640).
- [`../../scripts/windows/autopromote-lib.ps1`](../../scripts/windows/autopromote-lib.ps1) — `Invoke-AutopromoteDecision`, `Invoke-PromotionGate`, `Get-PromotionGateVerdict`, `Resolve-GateBlocked` (the pure 4C logic).
- [`../../mem0-server/app.py`](../../mem0-server/app.py) — `update_tier` (the PATCH /tier canonical gate), `CANONICAL_AUTOPROMOTE_ALLOWED`, the tier-ledger writer.
- [`../../mem0-server/security_invariants.py`](../../mem0-server/security_invariants.py) — `validate_hmac_user_direct` (format-2 HMAC, skew, nonce/replay).

## Related docs

- [`../systems/dream-skill.md`](../systems/dream-skill.md) — the nightly consolidation that owns path B (all phases, the 4C gate in depth, the shadow-default pitfall).
- [`../systems/tier-policy.md`](../systems/tier-policy.md) — the server-enforced tier matrix, the actor rules, and the HMAC formats.
- [`../systems/memory-model.md`](../systems/memory-model.md) — what canonical *is* and the promotion lifecycle it caps.
- [`../systems/dpapi-canonical-key.md`](../systems/dpapi-canonical-key.md) — canonical-key custody, injection, and recovery.
- [`../systems/mem0-api.md`](../systems/mem0-api.md) — the `PATCH /tier` contract and the ledger.
- [`../systems/reconciliation.md`](../systems/reconciliation.md) — how canonical anchors the contradiction sweeps.
- [`./memory-capture.md`](./memory-capture.md) — the capture pipeline whose nightly consolidation drives path B.
- [`../glossary.md`](../glossary.md) · [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)
