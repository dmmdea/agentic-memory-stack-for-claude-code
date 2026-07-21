# The offline write and its replay — outbox to authority

## Purpose

When the machine loses its path to the memory authority (the **brain box**), a mutation must not be lost and must not silently diverge the store. This flow is the write half of offline resilience: a mutation made while the authority is unreachable is appended to an on-disk **Outbox**, and on reconnect a client driver replays the queue to the authority — **adds first, exactly once, never dropping a record**. The defining property is that offline behavior **emerges from connectivity**: there is no mode to turn on, and the local replica *never absorbs a write*, so the store cannot diverge. That is the offline-side statement of the **One-Brain Rule**. The full subsystem (read failover, the offline-watcher, replica restore) is [`../systems/offline-travel.md`](../systems/offline-travel.md); this flow is the focused write→replay narrative.

## Trigger

Two triggers bracket the flow:

| Half | Trigger |
|---|---|
| **Queue** | any *mutating* `memory_*` MCP tool call while the authority is **connect-unreachable** (a TCP connect refusal/timeout — not a slow-but-answered request) |
| **Replay** | three triggers: **shim startup** (every new session, unconditional — fire-and-forget), the `mem0-offline-watcher`'s `go_online` transition, or a manual run to drain a stranded queue |

## Participants

- **The mem0 MCP shim** ([`../../scripts/wsl/mem0-mcp-shim.py`](../../scripts/wsl/mem0-mcp-shim.py)) — the client every `memory_*` call flows through; its `_authority_only` write path and `_queue_op` writer produce the Outbox.
- **The Outbox and its siblings** under `~/.mem0/` — the queue, the in-flight batch, the replayed-key ledger, and the conflict log.
- **The replay driver** ([`../../scripts/wsl/replay-ops.py`](../../scripts/wsl/replay-ops.py)) — the reconnect drain: reachability gate, atomic rotation, adds-first sort, key-ledger dedup, conflict logging.
- **The PowerShell wrapper** ([`../../scripts/travel/replay-outbox.ps1`](../../scripts/travel/replay-outbox.ps1)) — a thin callable that key/health-checks then runs `replay-ops.py` inside WSL.
- **The offline-watcher** — decides *when* to replay (the `go_online` transition); owned by [`../systems/offline-travel.md`](../systems/offline-travel.md).
- **The mem0 authority server** — the write authority the queue drains to, over its **standard REST routes**. There is no special replay endpoint (see below) — see [`../systems/mem0-api.md`](../systems/mem0-api.md).

## Step-by-step flow

### Authority resolution, and the authority/replica split

Both the shim and the driver resolve the authority the same way, in this precedence:

1. **`MEM0_URL`** environment variable — an ad-hoc override.
2. **`~/.mem0/authority-url`** — the durable, per-host file, written by the installer's `-AuthorityUrl`. This is the normal source.
3. **`http://127.0.0.1:18791`** — loopback fallback.

The file is the source of truth because the MCP entry launches the shim as `wsl.exe -d <distro> -e <python> <shim>`, which execs the binary directly: **no login shell and no `WSLENV` pass-through**, so a `MEM0_URL` set on the Windows side is invisible inside the shim. Resolving from disk — the same machine-local pattern already used for `~/.mem0/api-key` — makes the authority independent of the launch command, so regenerating the MCP entry cannot silently revert it.

The default is correct on the **brain box**, where the authority *is* local. A **replica box** must carry the brain's address in that file, so its writes target the real authority rather than itself. The shim additionally holds a fixed `LOCAL_URL` (`http://127.0.0.1:18791`) for the *dormant local replica* that read failover uses; writes never go there.

> Failure this prevents (observed 2026-07-20): a replica whose authority resolved to loopback found no local server, so **both** hops were dead — every operation returned `OfflineError`/`QUEUED_OFFLINE` and writes accumulated in the Outbox unnoticed.

### Queueing a write offline

A mutating tool call routes through `_authority_only`, which sends the request to the authority **only**, with a short **1.5 s connect timeout**. On a connect-level failure it raises `OfflineError`; the tool catches it and calls `_queue_op`, which appends one JSON line to `~/.mem0/outbox.jsonl` and returns `{event: "QUEUED_OFFLINE", op, key}`. This covers every mutating tool: `add`, `update`, `delete`, `promote`/`demote`, and the goal / open-question mutations. Crucially, an offline write is **never** redirected to the local replica — it queues.

Reads are the mirror image and out of scope here, with one tie-in: an offline search or recall merges any queued Outbox `add` whose text substring-matches the query, tagged `pending_sync: true`, so a fact written minutes ago offline is still findable before it has replayed.

### The Outbox entry

Each queued line is op-typed and `uuid4`-keyed:

```json
{"op": "add", "args": {"text": "...", "user_id": "...", "infer": false, "metadata": {}}, "queued_ts": "<ISO-8601 UTC>", "key": "<uuid4>"}
```

- `op` names the mutation; the replay driver's `dispatch` map turns it back into an authority call.
- `args` carries exactly the arguments needed to re-issue that call.
- `key` is a fresh `uuid4` stamped at queue time — the **idempotency handle** for replay.

### Reconnect — the adds-first, idempotent drain

On reconnect the driver (`replay-ops.py`) drains the queue:

1. **Refuses to run unless the authority is reachable** — `/health` must return `ok: true`, so a queue is never half-drained into the void.
2. **Atomically rotates** the live Outbox out of the way (`os.replace` → `outbox.rotating.jsonl` → folded into `outbox.replaying.jsonl`), so a concurrent offline writer either lands in the rotated inode already claimed for this run or in a fresh `outbox.jsonl` that survives untouched. A leftover `rotating` file from a mid-rotation crash is folded in first, never dropped.
3. **Orders adds first.** A stable sort puts every `add` (`_ADD_ORDER = 0`) ahead of every other mutation (`_MUTATION_ORDER = 1`), so an update/delete/promote that referenced a memory created offline is applied *after* that memory exists on the authority.
4. **Deduplicates by key.** Each record's `uuid4` key is checked against the replayed-key ledger (`~/.mem0/outbox.replayed.jsonl`); an already-applied key is skipped, and a key is added to the in-memory done-set immediately after a successful dispatch, so a duplicate key *within one batch* also applies exactly once. An interrupted replay can be re-run without double-applying anything.
5. **Never drops a record.** A `4xx`/`5xx` response, a torn/unparseable line, an unknown op, or an old-format record (no `op`) is written to `~/.mem0/mutation-conflicts.jsonl` for manual recovery; a *transient* error keeps the record in the replaying file for the next run. The driver prints `{"replayed": N, "conflicts": N, "kept": N}`.

### There is no server "replay" endpoint

Replay is **entirely client-side.** The driver re-issues each queued op against the *same standard authority REST routes a live call would use* — `POST /v1/memories`, `PUT`/`DELETE /v1/memories/{id}`, `PATCH /v1/memories/{id}/tier`, `/v1/goals…`, `/v1/open_questions…` — via its `dispatch` map. The server has no awareness that it is replaying; the outbox, the ordering, the dedup ledger, and the conflict log are all the client's doing. This is why the Outbox writer (`_queue_op`) and the driver's `dispatch` map are **one contract**: a field added to one and not the other sends offline mutations to the conflict log as an unknown op.

## Data and state changes

All state lives under `~/.mem0/` (WSL home) — never on the LAN.

| Path | Role |
|---|---|
| `~/.mem0/outbox.jsonl` | the Outbox — op-typed, `uuid4`-keyed mutations queued while offline |
| `~/.mem0/outbox.rotating.jsonl` | the atomic-rotation temp between `os.replace` and fold-in |
| `~/.mem0/outbox.replaying.jsonl` | the in-flight batch; also holds records kept from a transient failure |
| `~/.mem0/outbox.replayed.jsonl` | the replayed-key ledger — the idempotency record (`{key, op}` per applied op) |
| `~/.mem0/mutation-conflicts.jsonl` | records that could not be applied and must not be lost |
| the authority's Qdrant / episodic / goals stores | where the drained mutations finally land |

## Success behavior

Every mutation made offline eventually lands on the authority exactly once, in an order where no mutation precedes the add it depends on. The Outbox empties (the replaying file is unlinked when nothing is kept), the replayed-key ledger records what applied, and a re-run of the same replay is a no-op. Offline reads in the meantime stay useful — the local replica answers, and unsynced adds surface as `pending_sync`.

## Failure behavior

- **Authority still unreachable at replay** — the driver returns immediately without draining; the Outbox persists on disk and drains on the next reconnect.
- **A `4xx`/`5xx` on a replayed op** (e.g. deleting an already-gone memory) → conflict-logged with the status code; the batch continues.
- **A deterministic dispatch failure** (unknown op, old-format record with no `op`) → conflict-logged once, never retried in a loop.
- **A torn Outbox line** (crash mid-append) → preserved verbatim in the conflict log under `reason: "unparseable"`; the surrounding good lines still replay.
- **A transient error** on one op → that record is *kept* in the replaying file for the next run; the rest of the batch proceeds.
- **A crash mid-rotation** → the leftover `rotating` file is folded into the replaying file on the next run before anything else, so nothing is lost.

## External dependencies

- **The mem0 authority server** on `:18791` — the target of every replayed op; must answer `/health` `ok: true` before a drain begins.
- **`httpx`** — the shim and driver's HTTP client; the connect-vs-read timeout distinction is load-bearing (only a connect failure queues/fails over).
- **WSL2** — hosts the shim, the driver, and the `~/.mem0/` state.
- **The offline-watcher and the local replica** — provide the *when* (the `go_online` trigger) and the offline *read* path; detailed in [`../systems/offline-travel.md`](../systems/offline-travel.md).
- **The regular mem0 API key** (`~/.mem0/api-key`, mode 600, in WSL) — sent on every replayed request.

## Invariants and assumptions

The replica rules — the offline-side One-Brain Rule:

1. **The replica never absorbs a write.** Writes go to the authority or to the Outbox — never to `LOCAL_URL`. The local replica is restored read-only and torn down on reconnect, so divergence is impossible *by construction*, not by policy. This is the same invariant [`../systems/installer-and-deploy.md`](../systems/installer-and-deploy.md) enforces at install through the `brain`/`replica` role gate.
2. **A replica box must carry the brain's address in `~/.mem0/authority-url`.** The loopback default is only correct on the brain box. A replica that kept the default has no local server to reach at all, so every write queues instead of landing — and if a local replica *were* running, it would queue and replay into its own disposable store, a One-Brain violation the watcher's authority guard also exists to prevent.
3. **Replay is idempotent and adds-first.** Re-running a partial replay double-applies nothing (the key ledger), and dependent mutations never precede the add they reference.
4. **Replay only runs against a reachable, healthy authority**, so the Outbox is never half-drained or stranded.
5. **A conflicted or unparseable record is preserved, never dropped** — it goes to the conflict log.
6. **The Outbox writer and the replay `dispatch` map are one contract** — change `_queue_op`'s entry shape or add a mutating tool without adding its op to `dispatch`, and offline mutations land in the conflict log.
7. **No canonical promotion survives the offline path.** A queued `promote` replays as `actor="claude-autonomous"`, which the server refuses for `tier=canonical` — and the MCP promote tool refuses canonical outright. Canonical is online-only, requiring the live authority and the HMAC canonical key (see [`../systems/tier-policy.md`](../systems/tier-policy.md)).

## Security and privacy notes

- **No new network exposure.** The local replica binds loopback only; the Outbox drain reaches out to the already-trusted authority `MEM0_URL` and nowhere else.
- **The API key stays in WSL.** The PowerShell wrapper reads `~/.mem0/api-key` (mode 600) *inside* WSL and never copies it to the Windows side; the driver reads it directly from the same file.
- **Queued content is local at-rest data.** The Outbox holds the same memory text the server would; it lives under `~/.mem0/` with the rest of the store and is not transmitted anywhere until it replays to the authority the operator already trusts.
- **Operator-neutral at rest.** The shipped scripts carry no real host — the authority is an env/receipt value resolved locally, and the loopback default is the brain-box case.

## Observability and debugging

- **Did a replay run?** `replay-ops.py` prints `{"replayed": N, "conflicts": N, "kept": N}`; the replayed-key ledger and any `mutation-conflicts.jsonl` entries are the durable record.
- **A fact written offline isn't findable** → confirm it is in `~/.mem0/outbox.jsonl`; offline reads only merge Outbox adds whose text substring-matches the query.
- **Replay refuses to drain** → the authority `/health` is not `ok: true`; the driver is behaving correctly by not draining into an unhealthy authority.
- **How many ops are queued / where is memory pointed** → `travel-mode.ps1 status` (the legacy switch's still-useful read-out) prints authority reachability, replica state, and the Outbox count.
- **A stuck op** → look in `~/.mem0/mutation-conflicts.jsonl` for its status code or reason; conflicts are for manual recovery, not automatic retry.

## Testing notes

- [`../../mem0-server/tests/test_shim_offline.py`](../../mem0-server/tests/test_shim_offline.py) pins the shim's contract: reads fail over to the replica on a connect error; a read timeout and an HTTP error status **do not** fail over; `memory_add`/`memory_delete` queue op-typed `QUEUED_OFFLINE` records offline; and an offline search merges pending Outbox adds (`pending_sync`).
- [`../../mem0-server/tests/test_replay_ops.py`](../../mem0-server/tests/test_replay_ops.py) pins the replay contract: adds replay before mutations; the replayed-key ledger dedups across runs; in-batch duplicate keys apply exactly once; stranded `replaying` entries retry without a live Outbox; and unknown-op / old-format / unparseable / `4xx` records are conflict-logged, never dropped or retried forever.
- Both suites import the real scripts directly and skip cleanly if `fastmcp`/`httpx` are unavailable, so they run in CI without the live stack.

## Source map

- [`../../scripts/wsl/mem0-mcp-shim.py`](../../scripts/wsl/mem0-mcp-shim.py) — `_authority_only` (writes to the authority only), `_queue_op` (the Outbox writer), the offline pending-add merge, `MEM0_URL`/`LOCAL_URL`.
- [`../../scripts/wsl/replay-ops.py`](../../scripts/wsl/replay-ops.py) — the reconnect driver: reachability gate, atomic rotation, adds-first sort (`_ADD_ORDER`/`_MUTATION_ORDER`), key-ledger dedup, the `dispatch` map over standard REST routes, conflict log.
- [`../../scripts/travel/replay-outbox.ps1`](../../scripts/travel/replay-outbox.ps1) — the thin PowerShell wrapper that key/health-checks, then runs `replay-ops.py` in WSL.
- [`../../mem0-server/app.py`](../../mem0-server/app.py) — the authority endpoints the driver re-issues against (there is no dedicated replay route).

## Related docs

- [`../systems/offline-travel.md`](../systems/offline-travel.md) — the full offline subsystem: read failover, the offline-watcher, replica restore, and this write→replay path in context.
- [`../systems/installer-and-deploy.md`](../systems/installer-and-deploy.md) — the install-time `brain`/`replica` role gate: the same One-Brain Rule enforced at deploy time.
- [`../systems/mem0-api.md`](../systems/mem0-api.md) — the REST + MCP surface the shim targets and the driver replays against.
- [`../systems/memory-model.md`](../systems/memory-model.md) — the tiers and record model the queued mutations act on.
- [`./memory-capture.md`](./memory-capture.md) — the online capture path (and the separate DLQ the PowerShell hooks use offline, which is *not* the Outbox).
- [`./memory-retrieval.md`](./memory-retrieval.md) — the online retrieval path that degrades to replica reads offline.
- [`../glossary.md`](../glossary.md) — Brain, Replica, One-Brain Rule, Outbox, Travel Mode.
- [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) — the whole system this failover path protects.
