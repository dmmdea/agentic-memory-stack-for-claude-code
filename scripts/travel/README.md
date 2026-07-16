# Travel mode — offline memory on the laptop

The memory authority lives on **your-machine** (WSL: Qdrant + mem0 `:18791`). Over Tailscale it is reachable
from anywhere with internet, so travel mode is the **offline-only** fallback: planes, cabins, dead
zones — not "away from home".

| Verb | What it does |
|---|---|
| `travel-mode.ps1 status` | Where memory is pointed, is the authority reachable, how many memories are queued |
| `travel-mode.ps1 on -DryRun` | **Pre-flight.** Resolves + seeds the snapshot, prints whether you are offline-safe. Touches nothing. Run this before a trip. |
| `travel-mode.ps1 on` | Restores the newest snapshot into a **read-only** local replica; new memories queue to `~/.mem0/outbox.jsonl` |
| `travel-mode.ps1 off` | Replays the outbox to the authority, stops + disables the replica |

The **one-brain rule** holds by construction: when the authority is connect-unreachable, the mem0 MCP
shim fails over to the replica automatically for reads and queues **all** mutations (op-typed records) to
`~/.mem0/outbox.jsonl`. The replica never absorbs a write, so it can never diverge. (`~/.mem0/travel.json`
now only pins travel-mode's own on/off status display — the shim doesn't read it.)

## The trap this design exists to avoid

`P:\memory-backups\your-machine` lives on **pCloud's virtual drive**, which *streams* files from the cloud on
demand. Restoring from it works fine at your desk and **fails on the plane** — the one scenario travel
mode is for. (Found 2026-07-14: the first end-to-end test passed only because the laptop still had
internet.)

So the snapshot source is resolved **local-first**:

1. a **complete** set (manifest + episodic + history + qdrant, sharing a timestamp) on local disk
   (`D:\memory-backups\your-machine`) wins;
2. otherwise the pCloud copy, with a loud warning that it cannot work offline;
3. while online, `on` seeds/refreshes the local cache from the newest complete cloud set
   (atomic `.part` + rename, keeping the newest 3 sets).

Completeness is judged per **snapshot set**, never by "does the directory exist" or "how fresh are the
files" — an empty local dir must not shadow a good cloud set, and a stale local set must not shadow a
newer cloud one.

## Guards

- `on` **and** `off` refuse to run on the authority machine (`-AuthorityHost`, default `your-machine`): `on`
  would overwrite the live brain with a stale snapshot, and `off` would stop and disable the live
  mem0/qdrant (both share the same WSL calls).
- `off` refuses to flip while the authority is unreachable, so the outbox is never stranded (`-Force`
  overrides, leaving the queue intact for a later replay).
- Replay is idempotent: each entry carries a uuid4 key (stamped by the shim's `_queue_op`), and
  replayed keys are recorded in `~/.mem0/outbox.replayed.jsonl` — an interrupted replay can be
  re-run without duplicating.

## Laptop convenience

Three Desktop wrappers (`travel-mode-ON/OFF/status.cmd`) call these scripts with the local snapshot dir,
so travel mode is a double-click, not a command to remember.
