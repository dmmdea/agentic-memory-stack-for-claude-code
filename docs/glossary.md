# Glossary

Domain terms used across this repository's documentation and code. See [STYLE.md](STYLE.md) for the naming rule.

## Brain

The **brain box**: the single machine that hosts the memory authority (Qdrant + the mem0 server on `:18791`) and is the sole write authority for the store. The `brain`/`replica` role is chosen at install time by the installer's role gate. See [installer-and-deploy.md](systems/installer-and-deploy.md).

## Replica

A **replica box**: a machine that mirrors or consumes the memory read-only. A replica can never absorb a write, so it can never diverge from the Brain. Installed with `-Role replica`, it never registers the nightly canonical-mutation tasks. See [installer-and-deploy.md](systems/installer-and-deploy.md).

## One-Brain Rule

The invariant that exactly one machine — the Brain — holds write authority while replicas stay read-only; mutations made while the authority is unreachable queue to the Outbox and replay on reconnect, so divergence is impossible by construction. Enforced at install by a `brain`/`replica` role gate. See [installer-and-deploy.md](systems/installer-and-deploy.md).

## Tier

A memory's trust level — the axis `canonical` > `stable` > `insight` > `evidence` > `temporal` that controls who may write a record, who may promote it, and how much an agent should believe it. Tier enforcement is server-side: a disallowed write returns `403`. See [tier-policy.md](systems/tier-policy.md).

## Canonical

The locked-ground-truth tier: the facts every other record is judged against. No plain write can create it — only the operator's HMAC-signed CLI or the nightly Dream's gated autopromotion (≤ 3/night, through the 4C Promotion Gate). It never decays and is admitted only by the `canonical` and `history` Query Classes. See [tier-policy.md](systems/tier-policy.md).

## Stable

The settled-durable tier between machine-captured Evidence and operator-locked Canonical: durable and non-decaying, but not cryptographically protected. Written only by promotion (`PATCH /tier`), never by a direct add. See [tier-policy.md](systems/tier-policy.md).

## Evidence

The default tier — every auto-captured fact lands here first. It is deliberately mid-trust: retrievable and useful but never authoritative, so an agent should verify an Evidence fact before consequential action. It is a candidate for promotion or for env-gated Weibull decay on the durable path. See [memory-model.md](systems/memory-model.md).

## Insight

The consolidated-knowledge tier: higher-order patterns distilled *across* sessions by the nightly Dream and written only by the consolidator actor. `insight` is both a trust tier and a memory type; it is admitted on durable/operational reads but filtered out of the per-prompt hot bundle. See [memory-model.md](systems/memory-model.md).

## Temporal

The explicitly-perishable tier for facts with a shelf life. In the current admission policies it is write-side parking — stored and ledgered but admitted by no Query Class — and it is deleted by the weekly decay-scan once its expiry passes. See [memory-model.md](systems/memory-model.md).

## Receipt

The install-time file the installer writes recording the operator's deploy choices — the chosen repository path and the box's `brain`/`replica` role — which R9-tracked deployed scripts read to resolve operator-specific values at runtime. See [installer-and-deploy.md](systems/installer-and-deploy.md).

## Sentinel

An operator-specific placeholder token (for example `__WSL_USER__`, `__WIN_USER__`, `__WSL_DISTRO__`, `__MEM0_BIND__`) embedded in the repository's scripts and systemd units so the public source ships free of real values; the installer and `deploy.sh` substitute the real values at deploy time. See [installer-and-deploy.md](systems/installer-and-deploy.md).

## R9 Parity

The `Test-MemoryStack.ps1` check (code **R9**) that SHA256-compares repository source against the deployed copy of every hot-path hook script and config — and checks the compiled hook client against an install-time `.sha256` sidecar — WARNing on any repo-vs-deployed drift. See [installer-and-deploy.md](systems/installer-and-deploy.md).

## Dream

The nightly consolidator (Windows Task Scheduler, 3am, 24h-throttled): a four-phase orient → gather → consolidate → prune cycle that synthesizes 1–3 lineage-tracked Insights and may autonomously promote at most 3 facts/night to Canonical through the 4C Promotion Gate. See [dream-skill.md](systems/dream-skill.md).

## Drift Canary

The retrieval-drift canary run in the Dream's prune phase — a zero-Codex before/after snapshot of canary-fact retrievability that verifies a benign consolidation did not change what is findable. See [memory-capture.md](flows/memory-capture.md).

## Outbox

The operation-outbox (`~/.mem0/outbox.jsonl`) that queues mutations made while the Brain authority is unreachable; entries are op-typed and uuid4-keyed for idempotent replay to the authority on reconnect. See [CHANGELOG.md](../CHANGELOG.md) (v1.15).

## Travel Mode

A **legacy**, switch-based offline approach (`travel-mode.ps1 on`/`off`) that restored a read-only replica snapshot and queued writes to the Outbox. It was superseded in v1.15 by offline-first behavior that *emerges* from connectivity — reads fail over to a replica and writes queue automatically, with no explicit mode switch. See [CHANGELOG.md](../CHANGELOG.md) (v1.15).

## Brand

The primary isolation scope stamped on a memory. The Admission Gate enforces it fail-closed: a brandless search returns only brand-neutral records and a branded search never leaks another brand's records (opt out only with an explicit `allow_cross_brand`). See [admission-gate.md](systems/admission-gate.md).

## Campaign

A finer-grained "funnel" sub-scope beneath Brand, inferred client-side from the session's working directory. It isolates funnel-specific canonical rules so a session sees shared (no-campaign) facts plus only its own funnel's rules, never another funnel's. See [storage-cap-check.sh](../claude-config/storage-cap-check.sh).

## Admission Gate

The server-side retrieval-admission policy applied to every `POST /v1/memories/search`. It is a relevance/hygiene filter (not an authorization layer) that rejects hits by tier allowlist, Brand mismatch, operational recency cap, supersession, contradiction-of-canonical, and an optional relevance floor, logging each rejection. See [admission-gate.md](systems/admission-gate.md).

## Promotion Gate

The 4C contradiction/corroboration gate that scores every autonomous Canonical promotion. It ships in shadow mode by default (computes and logs a verdict without changing the decision); in enforce mode, the Dream's confidence-sorted, ≤ 3/night, deduped nominees are blocked and left as Evidence if they fail it. See [memory-model.md](systems/memory-model.md).

## Query Class

The mode in which memory is asked for — `durable` (default), `operational`, `canonical`, or `history` — each with its own Admission Gate policy governing which tiers are admitted, the recency cap, and whether superseded/contradicted records are hidden. "Durable" is a query class, not a tier. See [memory-model.md](systems/memory-model.md).

## L1a Extractor

The session fact extractor (`scripts/windows/l1a-extract.ps1`): on a Stop/PreCompact hook a Codex subagent reads the last ~24 turns under an inferability-gate prompt, keeping only genuinely project-specific facts (max 5/run, one success per 10 min) and posting each to mem0 as `tier=evidence`. See [memory-capture.md](flows/memory-capture.md).

## Ship-Log

Release-note narrative ("shipped X, fixed Y, merged Z") — the largest class of junk a coding agent generates. The capture pipeline splits it out of durable facts and folds it into the session's episode summary instead of polluting semantic memory. See [memory-capture.md](flows/memory-capture.md).

## Dead-Letter Queue (DLQ)

The retry queue (`~/.claude/state/mem0-post-failures.jsonl`) that a failed mem0 write dead-letters to; it retries on the next extractor run, quarantines poison codes (413/401/422) immediately, and gives up after 5 attempts. See [memory-capture.md](flows/memory-capture.md).

## Episodic Ledger

The `episodic.db` SQLite + FTS5 sidecar that records one episode per session (goal, summary, state) linked to the mem0 facts it produced — the temporal/narrative layer that answers questions vector search cannot ("what was I working on last Tuesday?"). See [episodic.md](systems/episodic.md).

## Open Question

A declarative uncertainty raised but left unanswered in a session, tracked cross-session in the open_questions registry (FTS5 search + open/resolved/abandoned/duplicate lifecycle). It operationalizes Epistemic Reachability — knowing what you don't know. See [open-questions.md](systems/open-questions.md).
