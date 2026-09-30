# The SessionStart Banner — What It Reads, and From Whom

`claude-config/storage-cap-check.sh` runs synchronously at every SessionStart and prints the orientation a new session starts from: recent sessions, brand context, open goals, cap warnings, review queues, one health line. It always exits 0 and is silent when nothing needs saying.

The design rule this page records: **the banner reads the box's role first, and a replica reads the authority, never its own frozen files.** Under the One-Brain Rule the nightly chain and the episodic writes live on the brain; a replica's `~/.mem0` copy of every brain artifact stopped changing at the authority cutover. Before this rule the banner showed those frozen files as current (weeks-old "recent sessions", a permanent "MEMORY.md stale" warning that blamed a script which skips by design on a replica, a "job-queue mirror STALE" alarm).

## Role gating

`~/.mem0/role` (written by the installer beside `authority-url`) is read once, at the top of the script. Absent means `brain`.

| Block | Brain | Replica / client |
|---|---|---|
| Recent sessions | local `episodic.db` | the authority (below) |
| Enrichment recency seed (`sessionstart_bundle.py`) | local `episodic.db` | the authority (below) |
| `MEMORY.md` age | yes | never |
| L10 unreviewed-flag count | yes | never |
| Brand-scope status | yes | never |
| Contradiction review queue | yes | never |
| Contradiction sweep no-op streak | yes | never |
| Job-queue mirror (`jobs-heartbeat.json`) | yes | never |
| Local morning-summary counter | yes | never |
| Retrieval-drift heartbeat | yes | never |
| Weekly `--rejudge-stamped` spawn | yes | never (the brain runs its own Sunday sweep) |
| Auto-memory lint summary, outbox and canonize lines, cap warnings for the local stores | yes | yes (valid on any box) |

## Reading the authority

Every authority read sits behind the one `MEM0_UP` probe (1 s), so a cold morning costs that probe and nothing more.

- **Health line (any role, authority up).** One `GET /health/maintenance` capped at 1.5 s, no key needed. Silent when `ok` is true and `drift.alarm` is false. Otherwise exactly one line:
  `[AMS] brain NOT OK — failed: <steps>; degraded: <steps>; pool <used_pct>% <health>; stale: <steps>; drift alarm`
  Empty parts are omitted; the pool part appears only when the pool alarm or the pool health alarm is set. A malformed or empty body prints nothing and never affects the exit code. The response shape is the `/health/maintenance` contract in [`mem0-api.md`](./mem0-api.md).
- **Recent sessions (replica / client, authority up, key present).** `GET /v1/episodes?recent=20` (1.5 s); the five newest rows with a non-empty goal print under the usual header with the suffix ` (authority)`. An empty list prints nothing. If the authority answered the probe but this read fails, one line says `recent sessions unavailable: authority unreachable`, so the gap is never silent.
- **Authority down (replica / client).** The probe fails, and the banner prints exactly one plain line, `[AMS] authority unreachable`, and none of the replica's own frozen brain artifacts. A brain whose server is still starting keeps the older "memory server still starting" message.

The heartbeat digest (`[heartbeat]`) stays file-read-only and brain-only; `/health/deep` is never called from the banner. The regression pin in `scripts/windows/tests/RegressionGuards.Tests.ps1` allows exactly one network read, the `/health/maintenance` line above, bounded at 1.5 s and gated on `MEM0_UP=1`.

## The enrichment bundle

`claude-config/sessionstart_bundle.py` (the `Recently-relevant memory` precis) posts to `/v1/context/bundle` with `hook_contract_version` stamped, like every other bundle caller. The server counts each bundle body without the field in `hook_contract.missing` (`GET /health/deep` → `checks.hook_contract`); that counter is cumulative since the last server restart, so compare it only within one uptime window.

The query is seeded by the newest episode goal ("what was I last doing"), and the seed follows the role, like the recent-sessions block above. The brain reads its own `episodic.db`. A replica or client asks the authority, `GET /v1/episodes?recent=20&brand=<brand>` (1.5 s, `X-API-Key`; `brand` only when the session has one), and takes the newest row with a non-blank goal for that brand; a brand with no episode gets no seed, never another brand's goal. When that read fails, times out or returns nothing usable there is no seed and the query falls back to the brand and initiative tokens. A replica never falls back to its own `episodic.db`, which froze at the cutover and would rank today's facts against a weeks-old goal. A fresh PreCompact marker outranks the seed, so the seed is not fetched at all then.

## Tests

- `claude-config/tests/test_storage_cap_replica_role.py` runs the real script under bash with a fixture `HOME`, a recording `curl` and `nohup` on `PATH`, and asserts each row of the table above plus the three authority reads.
- `claude-config/tests/test_storage_cap_drift_role.py` covers the drift block.
- `claude-config/tests/test_sessionstart_bundle.py` covers the bundle payload stamp and, driving `main()` against a fixture `HOME` and a fake authority, where the recency seed comes from on each role (replica, authority down, brain, brand filter, marker).

## See also

- [`episodic.md`](./episodic.md) — the episode store and `GET /v1/episodes`
- [`installer-and-deploy.md`](./installer-and-deploy.md) — the role gate
- [`continuity.md`](./continuity.md) — session continuity
