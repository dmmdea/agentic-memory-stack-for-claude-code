# Offline and travel

## Purpose

This system is what keeps memory usable when the machine loses its network path to the **Brain** — on a plane, in a dead zone, or any time the authority is unreachable. Its defining property is that offline behavior **emerges from connectivity**: there is no mode to turn on. When the memory authority is connect-unreachable, reads automatically fail over to a local read-only replica and mutations queue to an on-disk **Outbox**; when the authority returns, the queued operations replay to it. The replica can never absorb a write, so the store can never diverge — the offline-side statement of the **One-Brain Rule**.

The older `travel-mode.ps1 on`/`off` switch that an operator flipped by hand is **legacy**, superseded in v1.15 by this emergent design (see [`../../CHANGELOG.md`](../../CHANGELOG.md), v1.15). Its snapshot-restore machinery is still reused under the hood, but nobody flips a switch anymore.

## Questions this doc answers

- What happens to a `memory_search` / `memory_add` when the Brain is unreachable — and does anything break?
- Where do offline writes go, what shape are the queued records, and how are they keyed?
- What is the `mem0-offline-watcher` scheduled task, what does it watch, and what does it trigger?
- How is a read-only replica stood up from a snapshot, and why via the snapshot *upload* API rather than a directory copy?
- On reconnect, in what order does the queue drain, and how is a replay made safe to re-run?
- Why is `travel-mode.ps1` marked legacy, and what still uses its `on` path?
- Why can offline operation never cause the store to diverge?

## Scope

- The MCP shim's per-call **authority → replica** read failover and its **write-to-Outbox** queueing ([`../../scripts/wsl/mem0-mcp-shim.py`](../../scripts/wsl/mem0-mcp-shim.py)).
- The Outbox file, its entry shape, and the ancillary replay/ledger/conflict files.
- The `mem0-offline-watcher` background task and its offline/online transitions ([`../../scripts/travel/offline-watcher.ps1`](../../scripts/travel/offline-watcher.ps1), [`../../scripts/travel/install-offline-watcher.ps1`](../../scripts/travel/install-offline-watcher.ps1)).
- Replica restore from a snapshot set ([`../../scripts/travel/restore-replica.ps1`](../../scripts/travel/restore-replica.ps1)).
- The reconnect replay driver — adds-first, idempotent ([`../../scripts/wsl/replay-ops.py`](../../scripts/wsl/replay-ops.py)).
- The `scripts/travel/*` set, including the legacy `travel-mode.ps1` switch.

## Non-scope

- **How memory is captured or retrieved when online.** Those are [`../flows/memory-capture.md`](../flows/memory-capture.md) and [`../flows/memory-retrieval.md`](../flows/memory-retrieval.md); this doc covers only the degraded-connectivity path.
- **Producing the snapshot set.** The nightly backup that writes the manifest + episodic + history + Qdrant artifacts this system *restores from* is a backup concern ([`../data-backup.md`](../data-backup.md)); this doc consumes those artifacts, it does not create them.
- **The install-time role gate.** How a box is fixed as `brain` or `replica` at install — the enforcement side of the same One-Brain Rule — is [`installer-and-deploy.md`](installer-and-deploy.md).
- **The server's REST/MCP surface itself.** Endpoint contracts live in [`mem0-api.md`](mem0-api.md); this doc treats them as the calls the failover and replay layers target.

## Key concepts

- **Brain** / **Replica** — the brain box holds sole write authority over the store; a replica box serves it read-only. Defined in [`../glossary.md`](../glossary.md).
- **Authority** — the brain's mem0 URL as seen by this box: loopback on the brain box itself, or the brain's remote URL on a replica box. It is whatever `MEM0_URL` resolves to; the shim reads it as `AUTHORITY_URL`.
- **Local replica** — a dormant local mem0 + Qdrant on loopback (`127.0.0.1:18791`), started only during an outage, restored read-only from the newest snapshot set.
- **Outbox** — the operation queue (`~/.mem0/outbox.jsonl`); each line is an op-typed, `uuid4`-keyed record of a mutation made while offline. Defined in [`../glossary.md`](../glossary.md).
- **Replayed-key ledger** — `~/.mem0/outbox.replayed.jsonl`; the record of which Outbox keys already applied to the authority, which is what makes replay idempotent.
- **Connect-level failure** — a TCP connect refusal/timeout, distinct from a slow-but-answered request. Only a connect-level failure triggers failover; a read timeout or an HTTP error status does not.
- **Travel Mode** — the **legacy** manual switch (`travel-mode.ps1 on`/`off`). Defined (and marked legacy) in [`../glossary.md`](../glossary.md).

## How the system works

Offline resilience is delivered by two independent layers. Neither requires an operator to flip anything.

### Layer 1 — the shim's per-call failover (no flag)

The mem0 MCP shim is the client every `memory_*` tool call flows through. It holds two URLs: `AUTHORITY_URL` (from `MEM0_URL`, default loopback `http://127.0.0.1:18791`) and a fixed `LOCAL_URL` (`http://127.0.0.1:18791`, the dormant local replica). Its behavior splits by operation kind:

- **Reads** (`_request`) try the authority first with a short **1.5s connect timeout**; on a connect-level failure they fall over to the local replica and tag the result `source: "local-replica"` with a `stale_note`. If *both* are connect-unreachable the call raises `OfflineError` and the tool returns an empty-but-valid result marked `offline`. Crucially, a **read timeout or an HTTP error status is not a failover trigger** — the authority accepted the connection and is merely slow or erroring, and masking a real answer with a stale replica read would be wrong. That answer propagates instead.
- **Writes and mutations** (`_authority_only`) go to the authority *only*. On a connect-level failure they do **not** touch the replica — they call `_queue_op`, which appends the operation to the Outbox and returns `{event: "QUEUED_OFFLINE", op, key}`. This covers every mutating tool: `add`, `update`, `delete`, `promote`/`demote`, and the goal / open-question mutations.

Offline reads additionally surface **queued-but-unsynced adds**: a search or recall merges any Outbox `add` whose text substring-matches the query, marked `pending_sync: true`, so a fact written minutes ago offline is still findable before it has replayed.

The PowerShell auto-capture hooks (the L1a extractor / C1 consolidator, in [`../../scripts/windows/memory-common.ps1`](../../scripts/windows/memory-common.ps1)) are a *separate* client and do **not** fail over to the replica. When the authority is unreachable their POST fails and the fact is dead-lettered to the DLQ (`~/.claude/state/mem0-post-failures.jsonl`) to retry on the next run. A connection-level failure (`status_code == 0`) deliberately does **not** count toward the DLQ's 5-attempt quarantine cap, so a multi-day offline stretch of Stop hooks never quarantines good writes. (The DLQ is documented in [`../flows/memory-capture.md`](../flows/memory-capture.md).)

### Layer 2 — the offline-watcher (background, automatic)

The shim can only fail reads over to a *running* local replica. Keeping that replica stopped when online (one live brain) and bringing it up during an outage is the job of the `mem0-offline-watcher` scheduled task, which ticks every **2 minutes** ([`../../scripts/travel/offline-watcher.ps1`](../../scripts/travel/offline-watcher.ps1)). Each tick:

1. Probes the authority's `/health` (short timeout).
2. Steps a small hysteresis state machine that debounces flapping links: from `online`, **3 consecutive down** ticks transition to `offline` (`go_offline`); from `offline`, **2 consecutive up** ticks transition to `online` (`go_online`). Steady ticks with no transition do nothing.
3. Acts only on a transition:
   - **`go_offline`** — if the replica snapshot is stale (>24h since the last restore), refresh and bring it up read-only by reusing `travel-mode.ps1 on`; otherwise just start the local `qdrant`/`mem0` user services.
   - **`go_online`** — run the replay driver to drain the Outbox to the authority, then stop the local `qdrant`/`mem0` services, remove the vestigial `~/.mem0/travel.json` flag, and point `MEM0_URL` back at the authority.

The watcher resolves the authority carefully: an explicit `-Authority` wins; otherwise `MEM0_URL`, but **only if it is not a local/loopback/unspecified/malformed host** (a `Test-IsLocalUrl` guard). This matters because `travel-mode.ps1 on` sets a user-scope `MEM0_URL` pointing at the *replica* — without the guard, a fresh watcher tick would inherit the replica as its "authority", probe it as healthy, and replay the Outbox *into the disposable local store*, which would be a One-Brain violation.

### The legacy switch

`travel-mode.ps1` predates both layers: an operator ran `on` before a trip to restore the replica and redirect writes, and `off` on return to replay and tear down. It is **legacy** — the shim no longer reads its `travel.json` flag, and the watcher now performs the same restore/replay automatically. Its `on` path (which calls `restore-replica.ps1`) is still *reused* by the watcher's `go_offline`, so the script is not dead code; but it is no longer an operator workflow. Its `status` verb remains a convenient human read-out of where memory is pointed and how many operations are queued.

## Important flows

### Going offline (a read during an outage)

Authority connect fails → the shim's 1.5s connect timeout trips → the read is reissued against the local replica → result returned tagged `local-replica` + `stale_note`, with any matching pending Outbox adds merged in. Independently, within ~2–6 minutes the watcher's third down-tick fires `go_offline` and ensures the replica services are up (restoring a fresh snapshot first if the last one is >24h old).

### Queueing a write while offline

A mutating tool call hits `_authority_only`, the connect fails, and `_queue_op` appends one JSON line to `~/.mem0/outbox.jsonl`:

```json
{"op": "add", "args": {"text": "...", "user_id": "...", "infer": false, "metadata": {}}, "queued_ts": "<ISO-8601 UTC>", "key": "<uuid4>"}
```

The `op` field names the mutation; `args` carries exactly the arguments needed to re-issue it; `key` is a fresh `uuid4` stamped at queue time — the idempotency handle for replay.

### Reconnect and replay (adds-first, idempotent)

On `go_online` the watcher invokes [`../../scripts/wsl/replay-ops.py`](../../scripts/wsl/replay-ops.py), which drains the Outbox to the authority. There is **no dedicated server "replay" endpoint** — the driver simply re-issues each queued op against the *same* standard authority REST routes a live call would use (`/v1/memories`, `/v1/goals`, `/v1/open_questions`, …). It:

1. **Refuses to run unless the authority is reachable** (`/health` returns `ok: true`), so a queue is never half-drained into the void.
2. **Atomically rotates** the live Outbox out of the way (`os.replace` → `outbox.rotating.jsonl` → folded into `outbox.replaying.jsonl`) so a concurrent offline writer either lands in the rotated inode already claimed for this run or in a fresh `outbox.jsonl` that survives untouched.
3. **Orders adds first.** A stable sort puts every `add` (`_ADD_ORDER = 0`) ahead of every other mutation (`_MUTATION_ORDER = 1`), so an update/delete/promote that referenced a memory created offline is applied *after* that memory exists on the authority.
4. **Deduplicates by key.** Each record's `uuid4` key is checked against the replayed-key ledger (`~/.mem0/outbox.replayed.jsonl`); an already-applied key is skipped, and a key is added to the in-memory done-set immediately after a successful dispatch so a duplicated key *within one batch* also applies exactly once. An interrupted replay can therefore be re-run without double-applying anything.
5. **Never drops a record.** A `4xx`/`5xx` response, an unparseable/torn line, an unknown op, or an old-format record (no `op`) is written to `~/.mem0/mutation-conflicts.jsonl` for manual recovery; a transient error keeps the record in the replaying file for the next run.

`replay-outbox.ps1` is a thin PowerShell wrapper (the callable name the legacy switch uses) that verifies the API key and authority health, then invokes this same `replay-ops.py` inside WSL.

### Restoring the replica

`restore-replica.ps1` restores the newest complete snapshot set into the local stack read-only. It requires `jq` and a local embedder on `:11436` (the replica needs EmbeddingGemma@768 locally or recall returns nothing), copies the `episodic.db` and `history.db` SQLite ledgers straight in while mem0 is stopped, and restores the Qdrant collection (`mem0_egemma_768`) via the **snapshot upload API** (`POST /collections/<c>/snapshots/upload?priority=snapshot`) rather than a directory copy — a raw copy is version-coupled and silently corrupts across Qdrant versions. It finishes by asserting the replica answers `/health` and reporting the restored point count.

## Data and state

All state is under `~/.mem0/` (WSL home) or `~/.claude/state/` (Windows), never on the LAN.

| Path | Role |
|---|---|
| `~/.mem0/outbox.jsonl` | the Outbox — op-typed, `uuid4`-keyed mutations queued while offline |
| `~/.mem0/outbox.replaying.jsonl` | in-flight batch during a replay; also holds records kept from a transient failure |
| `~/.mem0/outbox.rotating.jsonl` | the atomic-rotation temp between `os.replace` and fold-in |
| `~/.mem0/outbox.replayed.jsonl` | replayed-key ledger — the idempotency record |
| `~/.mem0/mutation-conflicts.jsonl` | records that could not be applied and must not be lost |
| `~/.mem0/travel.json` | **vestigial** — only feeds `travel-mode.ps1 status`; the shim never reads it |
| `~/.claude/state/offline-mode.json` | the watcher's hysteresis state (`mode`, consecutive up/down counts) |
| `~/.claude/state/replica-restored.txt` | timestamp marker of the last successful replica restore (the 24h-staleness check) |
| `~/.claude/state/mem0-post-failures.jsonl` | the PowerShell hooks' DLQ (separate from the Outbox) |
| local `episodic.db`, `history.db`, Qdrant `mem0_egemma_768` | the read-only replica's restored data |

## Interfaces and entry points

- **`mem0-offline-watcher`** — the scheduled task (registered by `install-offline-watcher.ps1`, `pwsh.exe -File offline-watcher.ps1`), ticking every 2 minutes at `RunLevel Limited`. The primary, hands-off entry point.
- **The MCP `memory_*` tools** — every read/write an agent makes passes through the shim's failover/queue logic transparently; no offline-specific tool exists.
- **`replay-ops.py [--outbox <path>] [--authority <url>]`** — the reconnect replay driver (run inside WSL; also callable manually to drain a stranded queue).
- **`restore-replica.ps1 -BackupDir <dir> -Stamp <stamp>`** — stands up the read-only replica from a snapshot set.
- **`travel-mode.ps1 status | on | off`** — the **legacy** operator switch; `status` is still a useful read-out.
- **`install-offline-watcher.ps1`** — registers the watcher task (refuses to run on the authority box, whose `go_online` would stop the live brain).

## Dependencies

- **The mem0 MCP shim and server** ([`mem0-api.md`](mem0-api.md)) — the shim is where failover/queue live; the server is the authority the queue replays to.
- **WSL2 with systemd-user services** — the local `qdrant`/`mem0` units the watcher starts/stops, and `jq`.
- **llama-swap on `:11436`** — the local EmbeddingGemma embedder the replica needs to answer recall offline.
- **A backup source** — a complete snapshot set (manifest + episodic + history + Qdrant, sharing a timestamp) produced by the nightly backup ([`../data-backup.md`](../data-backup.md)); `restore-replica.ps1` consumes it.
- **Windows Task Scheduler** — hosts the `mem0-offline-watcher` task.
- **`httpx`** — the shim and replay driver's HTTP client (connect-vs-read timeout distinction is load-bearing here).

## Downstream effects

- **Changing the Outbox entry shape** (`op`/`args`/`key`/`queued_ts`) breaks `replay-ops.py`'s `dispatch` map and its key-based dedup — the writer (`_queue_op`) and the reader (the driver) must change together.
- **Adding a new mutating MCP tool** without adding its `op` to `dispatch` means offline calls queue but then land in `mutation-conflicts.jsonl` as an unknown op on replay.
- **Changing the failover exception set** (which `httpx` errors count as connect-level) changes *when* reads go stale — widening it to read timeouts would mask real authority answers with replica reads.
- **Changing the authority-resolution guard** in the watcher risks the One-Brain violation it exists to prevent (replaying into the disposable local store).
- **Changing the Qdrant collection name or the snapshot artifact naming** breaks `restore-replica.ps1`.

## Invariants and assumptions

- **One-Brain (offline side): the replica never absorbs a write.** Writes go to the authority or to the Outbox — never to `LOCAL_URL`. The replica is restored read-only and torn down on reconnect. Divergence is therefore impossible *by construction*, not by policy. This is the same invariant [`installer-and-deploy.md`](installer-and-deploy.md) enforces at install through the `brain`/`replica` role gate.
- **Only a connect-level failure fails over.** A read timeout or HTTP error is a real (if unhappy) answer and must escape, never be replaced by a stale replica read.
- **Replay is idempotent and adds-first.** Re-running a partial replay double-applies nothing (key ledger), and dependent mutations never precede the add they reference.
- **Replay only runs against a reachable, healthy authority**, so the Outbox is never half-drained or stranded.
- **A conflicted or unparseable record is preserved, never dropped** — it goes to the conflict log.
- **The watcher must never run on the brain box** — its `go_online` stops the live `mem0`/`qdrant`; `install-offline-watcher.ps1` refuses registration there.

## Error handling

- **Both authority and replica unreachable** → reads raise `OfflineError`; tools return empty-but-valid results marked `offline` (with any pending Outbox adds merged), so a session degrades rather than crashes.
- **A torn Outbox line** (crash mid-append) → preserved verbatim in `mutation-conflicts.jsonl` under `reason: "unparseable"`; the surrounding good lines still replay.
- **A deterministic replay failure** (unknown op, old-format record with no `op`) → conflict-logged once, never retried in a loop.
- **A `4xx`/`5xx` on a replayed op** (e.g. deleting an already-gone memory) → conflict-logged with the status code; the batch continues.
- **A crash mid-rotation** → a leftover `outbox.rotating.jsonl` is folded into the replaying file on the next run before anything else, so nothing is lost.
- **A stale/partial replica restore** → the watcher stamps `replica-restored.txt` *only* when `travel-mode.ps1 on` actually succeeded, so a failed restore is retried next cycle rather than trusted as fresh.
- **A transient WSL failure resolving the repo path on reconnect** → the watcher skips that cycle's replay; the Outbox persists on disk and drains on the next offline→online transition.

## Security and privacy notes

- **No new network exposure.** The local replica binds loopback only (`127.0.0.1:18791` mem0, `127.0.0.1:6333` Qdrant); nothing here opens a LAN listener.
- **The API key stays in WSL.** `replay-outbox.ps1` reads `~/.mem0/api-key` (mode `600`) inside WSL and never copies it to the Windows side.
- **Queued content is local at-rest data.** The Outbox holds the same memory text the server would; it lives under `~/.mem0/` with the rest of the store and is not transmitted anywhere until it replays to the authority the operator already trusts.
- **Least authority on the task.** The watcher runs at `RunLevel Limited` (non-elevated) as the interactive user.
- **Operator-neutral at rest.** The shipped scripts carry no real host — the authority is an env/receipt value resolved locally, and the loopback/host guards are written to reject any local URL as the authority regardless of who the operator is.

## Observability and debugging

- **Where is memory pointed / how many ops are queued?** `travel-mode.ps1 status` prints the authority reachability, whether the replica is running, and the Outbox count.
- **Is the watcher transitioning?** Read `~/.claude/state/offline-mode.json` (`mode` + consecutive counts) and the task history for `mem0-offline-watcher` in Task Scheduler.
- **Did a replay run?** `replay-ops.py` prints `{"replayed": N, "conflicts": N, "kept": N}`; the replayed-key ledger and any `mutation-conflicts.jsonl` entries are the durable record.
- **A fact written offline isn't findable** → confirm it is in `~/.mem0/outbox.jsonl`; offline reads only merge Outbox adds whose text substring-matches the query.
- **Replay refuses to drain** → the authority `/health` is not `ok: true`; the driver is behaving correctly by not draining into an unhealthy authority.
- **Replica returns nothing offline** → the local embedder on `:11436` isn't serving, or `jq` is missing — both are hard preconditions `restore-replica.ps1` checks and names.

## Testing notes

- [`../../mem0-server/tests/test_shim_offline.py`](../../mem0-server/tests/test_shim_offline.py) pins the shim's contract: reads fail over to the replica on a connect error; a read timeout and an HTTP error status **do not** fail over; `memory_add`/`memory_delete` queue op-typed records offline; and an offline search merges pending Outbox adds.
- [`../../mem0-server/tests/test_replay_ops.py`](../../mem0-server/tests/test_replay_ops.py) pins the replay contract: adds replay before mutations; the replayed-key ledger dedups across runs; in-batch duplicate keys apply exactly once; stranded `replaying` entries are retried without a live Outbox; and unknown-op / old-format / unparseable / `4xx` records are conflict-logged, never dropped or retried forever.
- Both suites import the real scripts directly and skip cleanly if `fastmcp`/`httpx` are unavailable, so they run in CI without the live stack.

## Common pitfalls

- **Thinking there is a mode to turn on.** There is not, in the current design — offline behavior emerges from connectivity. `travel-mode.ps1 on/off` is legacy; do not build new behavior on it.
- **Assuming a slow authority fails over.** Only a *connect* failure does; a read timeout is a real answer in flight and must not be masked with a stale replica read.
- **Expecting the PowerShell hooks to fail over like the shim.** They do not — their offline writes dead-letter to the DLQ and self-heal on the next run; the shim is the only client with replica failover + Outbox.
- **Running the watcher (or `travel-mode.ps1 on/off`) on the brain box.** Its `go_online`/`off` stops the *live* brain; both refuse there by design.
- **Restoring Qdrant by copying its storage directory.** Use the snapshot upload API; a raw copy is version-coupled and silently corrupts.
- **Editing `_queue_op` or `dispatch` in isolation.** The Outbox writer and the replay reader are one contract; a field added to one and not the other sends offline mutations to the conflict log.
- **Trusting `~/.mem0/travel.json` as live state.** It only feeds the legacy `status` read-out; the shim ignores it.

## Source map

- [`../../scripts/wsl/mem0-mcp-shim.py`](../../scripts/wsl/mem0-mcp-shim.py) — the shim: `_request` read failover, `_authority_only` writes, `_queue_op` Outbox writer, offline pending-add merge.
- [`../../scripts/wsl/replay-ops.py`](../../scripts/wsl/replay-ops.py) — the reconnect replay driver (reachability gate, atomic rotation, adds-first sort, key-ledger dedup, conflict log).
- [`../../scripts/travel/offline-watcher.ps1`](../../scripts/travel/offline-watcher.ps1) — the 2-minute tick: authority probe, hysteresis state machine, offline/online transitions.
- [`../../scripts/travel/install-offline-watcher.ps1`](../../scripts/travel/install-offline-watcher.ps1) — registers the `mem0-offline-watcher` task (refuses on the brain box).
- [`../../scripts/travel/restore-replica.ps1`](../../scripts/travel/restore-replica.ps1) — read-only replica restore (SQLite copy + Qdrant snapshot upload + health assertion).
- [`../../scripts/travel/travel-mode.ps1`](../../scripts/travel/travel-mode.ps1) — the **legacy** operator switch (`status`/`on`/`off`); its `on` restore path is reused by the watcher.
- [`../../scripts/travel/replay-outbox.ps1`](../../scripts/travel/replay-outbox.ps1) — the PowerShell wrapper that key/health-checks then invokes `replay-ops.py`.
- [`../../scripts/travel/README.md`](../../scripts/travel/README.md) — the travel-scripts overview.
- [`../../scripts/windows/memory-common.ps1`](../../scripts/windows/memory-common.ps1) — the PowerShell hooks' non-failover client + DLQ (the contrast to the shim).
- [`../../mem0-server/tests/test_shim_offline.py`](../../mem0-server/tests/test_shim_offline.py), [`../../mem0-server/tests/test_replay_ops.py`](../../mem0-server/tests/test_replay_ops.py) — the failover and replay contracts.

## Related docs

- [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) — the whole system this failover path protects.
- [`installer-and-deploy.md`](installer-and-deploy.md) — the install-time `brain`/`replica` role gate: the same One-Brain Rule enforced at deploy time.
- [`mem0-api.md`](mem0-api.md) — the mem0 server and MCP shim surface the failover and replay layers target.
- [`../flows/memory-capture.md`](../flows/memory-capture.md) — the online capture path and the DLQ the PowerShell hooks use offline.
- [`../flows/memory-retrieval.md`](../flows/memory-retrieval.md) — the online retrieval path that degrades to replica reads here.
- [`../data-backup.md`](../data-backup.md) — the snapshot set this system restores the replica from.
- [`../glossary.md`](../glossary.md) — Brain, Replica, One-Brain Rule, Outbox, Travel Mode.
- [`../../CHANGELOG.md`](../../CHANGELOG.md) — v1.15, the offline-first-emergent design statement.
