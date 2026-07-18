# Surviving compaction — PreCompact capture to SessionStart restore

## Purpose

Compaction throws away conversation. When Claude Code's context window fills, it compacts — summarizing and discarding older turns — and the post-compaction session loses the working thread it just had. This flow makes that thread *survivable*: a PreCompact hook distills the live conversation into a small redacted **query marker** before compaction runs, and the SessionStart that fires immediately after consumes the marker to inject a **conversation-relevant memory bundle**, so the resumed session starts with the facts the discarded turns were about.

The flow has a second, harder job: it must **never make compaction worse than the memory it protects**. Claude Code treats a PreCompact hook that exits with code `2` as a *hard block on compaction* — so a broken capture hook can wedge a live session that has no way forward but to compact. The **fail-open `|| true` contract** on the capture command exists precisely so this capture-time helper can never hard-block the very compaction it is trying to enrich; the **skew guard** at install-verify time catches the precondition that once made it fire.

## Trigger

Two events bracket the flow, in this order:

| Half | Trigger |
|---|---|
| **Capture** | Claude Code's **PreCompact** event — fired just before the context window is compacted (auto-compaction, or the user's manual compact). The stack registers a WSL capture hook on it. |
| **Restore** | The **SessionStart** event that fires when the post-compaction session opens; its WSL banner script runs the bundle-restore helper, which looks for a *fresh* marker. |

The restore half also runs on an ordinary cold session start (no compaction just happened) — with no fresh marker it simply falls back to its cold-boot behavior, so the same helper serves both cases.

## Participants

- **Claude Code's hook runtime** — fires `PreCompact` before compacting and `SessionStart` when a session opens; injects a hook's stdout into the new session's context. PreCompact stdout is *not* injected (hook spec) — PreCompact is capture-only.
- **The PreCompact capture sidecar** — [`../../claude-config/precompact_capture.py`](../../claude-config/precompact_capture.py), a dependency-free WSL Python hook that tails the transcript, redacts it, and writes the marker.
- **The PreCompact extractor dispatcher** — `stop-extract.ps1`, the *other* hook the stack registers on PreCompact (it snapshots the transcript and dispatches the L1a fact extractor). It is a co-tenant of this event, documented in [`../systems/codex-hooks.md`](../systems/codex-hooks.md); this flow centers on the capture sidecar.
- **The query marker** — `~/.mem0/precompact-query.json`, the hand-off file between the two halves.
- **The SessionStart banner script** — [`../../claude-config/storage-cap-check.sh`](../../claude-config/storage-cap-check.sh), the WSL SessionStart hook that emits the resume banner and invokes the bundle helper.
- **The bundle-restore helper** — [`../../claude-config/sessionstart_bundle.py`](../../claude-config/sessionstart_bundle.py), which consumes the marker and asks the authority for a ranked bundle.
- **The mem0 authority** — serves `POST /v1/context/bundle` (the admission-gated ranked memory bundle); see [`../systems/mem0-api.md`](../systems/mem0-api.md).
- **The skew guard** — a check in [`../../install/3-verify.ps1`](../../install/3-verify.ps1) that asserts every settings.json-referenced deployed script exists on disk.

## Step-by-step flow

### 1 — PreCompact fires; two hooks run, both fail-open

The installer registers **two** stack hooks on `PreCompact` (see [`../../install/2-windows-config.ps1`](../../install/2-windows-config.ps1)): the extractor dispatcher `stop-extract.ps1`, and the capture sidecar. The capture sidecar is registered as a WSL command with a trailing `|| true`:

```text
wsl.exe [-d <DISTRO>] -e bash -lc "python3 /mnt/c/Users/<WIN_USER>/.claude/scripts/precompact_capture.py || true"
```

That `|| true` is the **fail-open contract** — see *Invariants* for why it is load-bearing. Both hooks are best-effort by design and neither can block the compaction that is about to happen.

### 2 — The sidecar distills a redacted resume query

`precompact_capture.py` receives the hook JSON on stdin. It:

1. Reads `transcript_path` (a **Windows** path) and translates it to its WSL mount (`C:\…` → `/mnt/c/…`).
2. **Tails only the last 256 KB** of the transcript (`_TAIL_BYTES`), never the whole file — bounding the pathological giant-transcript case.
3. Parses the JSONL and keeps the **last 6** user/assistant turns (`DEFAULT_MAX_TURNS = 6`) as `[role] text`.
4. **Redacts** credential-shaped text (OpenAI `sk-` keys, `Authorization:` bearer/basic headers, `api_key|token|password|secret` assignments, PEM private-key blocks) so nothing secret lands in the marker, even transiently.
5. Truncates the result to **800 chars** (`DEFAULT_MAX_CHARS`), keeping the most-recent tail.

If any step yields no query — empty transcript, missing file, unreadable path — it writes **no marker and exits 0**.

### 3 — The marker is published atomically

With a non-empty query, the sidecar writes `~/.mem0/precompact-query.json` via a temp file + `os.replace` (atomic publish), so a concurrent reader never sees a half-written marker:

```json
{"query": "<redacted précis of the last turns>", "session_id": "<id>", "ts": <unix-seconds>}
```

Then compaction proceeds. PreCompact cannot inject anything into the session (hook spec), so the marker is the *only* thing carried across the compaction boundary.

### 4 — SessionStart restore consumes the marker

When the post-compaction session opens, the WSL SessionStart hook `storage-cap-check.sh` runs and calls `sessionstart_bundle.py --brand <brand> --initiative <initiative>`. The helper:

1. **Reads and consumes the marker** (`load_and_consume_marker`): it opens `~/.mem0/precompact-query.json`, then **always deletes it** — fresh, stale, or corrupt — so a marker can never linger into a later session (**consume-once**). The marker's query is *used* only if it is **fresh: less than 300 s old** (`MARKER_MAX_AGE = 300`), since the post-compact SessionStart fires seconds later.
2. **Chooses the query and bundle shape** (`choose_query_and_params`):
   - **Fresh marker present** → the real conversation query → `tier="frontier"`, **K = 2** (a genuine query justifies the second slot and can rank it).
   - **No fresh marker** (cold boot) → a **recency pseudo-query** built from the most-recent episode goal for the brand → precision-first `tier="small"`, **K = 1**.
3. **Fetches the bundle** — `POST /v1/context/bundle` with `checkpoint:false` (this read must not write a synthetic resume episode), the chosen `tier`, and brand/initiative scope. `tier` scales K at the server's **calibrated 0.30 semantic gate** (small ⇒ K ≤ 1, frontier ⇒ K ≤ 2) — the server's own machinery, not a client-side score floor.
4. **Distills and emits** — takes the top-K non-blank durable/evidence facts, truncates each to **120 chars** (`DEFAULT_LIMIT` — a précis, never a raw dump), and prints an advisory block:

```text
Recently-relevant memory (verify before acting):
  - [recall] <distilled fact 1>
  - [recall] <distilled fact 2>
```

Claude Code injects that stdout into the opening context, so the resumed session sees the facts the discarded turns were about — with an explicit "verify before acting" caveat, because these are ranked recalls, not ground truth.

## Data and state changes

| Path / resource | Change | By |
|---|---|---|
| `~/.mem0/precompact-query.json` | **created** (atomic temp + `os.replace`) at PreCompact; **deleted** at the next SessionStart, always | sidecar writes; helper consumes |
| `~/.mem0/precompact-query.json.tmp` | transient staging file for the atomic publish | sidecar |
| the transcript file | **read-only tail** (last 256 KB); never modified | sidecar |
| `/v1/context/bundle` (mem0) | a **non-checkpoint read** — `checkpoint:false`, so **no** episode row is written | helper |
| the opening session context | one advisory `[recall]` block injected via SessionStart stdout | Claude Code |

No memory is written by this flow. Capture produces exactly one small local file; restore consumes it and performs one read.

## Success behavior

- **On a real compaction:** the marker is written before compaction, is under 300 s old at SessionStart, and the resumed session opens with a `frontier`/K=2 `[recall]` block ranked against the actual last-conversation query. The marker is then gone.
- **On an ordinary cold start:** no fresh marker, so the helper silently falls back to the `small`/K=1 recency pseudo-query (or abstains entirely if there is no signal). Either way the marker directory is left clean.
- **Compaction itself always completes** regardless of the capture hook's fate — the whole point of the fail-open contract.

## Failure behavior

- **Capture script missing or throws** → `python3` exits non-zero (exit **2** on a missing script), but `|| true` forces the bash command to exit 0, so compaction is **never** blocked. No marker is written; SessionStart falls back to cold-boot behavior.
- **`wsl.exe` itself fails** → it returns a code that is *not* 2, which PreCompact treats as non-blocking — again, compaction proceeds.
- **Transcript unreadable / empty / no text turns** → the sidecar writes no marker and exits 0 (fail-silent).
- **Marker stale (> 300 s) or corrupt** → the helper still deletes it (consume-once) and treats the boot as a cold start.
- **Authority unreachable / no API key / bundle empty** → `fetch_bundle` returns `[]`, `format_block` returns `""`, and the helper prints nothing and exits 0 — the rest of the SessionStart banner is unaffected.

Every failure path in both halves is fail-open/fail-silent: the worst case is *no enrichment*, never a blocked compaction and never a visible error.

## External dependencies

- **Claude Code's hook runtime** — the source of the `PreCompact` and `SessionStart` events, the stdin hook JSON, and the SessionStart-stdout injection contract. The PreCompact **exit-code-2-blocks-compaction** behavior is the specific contract the fail-open `|| true` guards against.
- **WSL2** — hosts the capture sidecar, the marker under `~/.mem0/`, and the restore helper; both scripts are stdlib-only (no third-party imports).
- **`python3` in the WSL distro** — the interpreter for both helpers.
- **The mem0 authority** on `:18791` serving `POST /v1/context/bundle`, plus its embedder via llama-swap for ranking. Only the *restore* half depends on it; capture is offline-only.
- **The API key** at `~/.mem0/api-key` — the restore helper stays silent without it.

## Invariants and assumptions

1. **PreCompact capture can never hard-block compaction.** Claude Code treats a PreCompact hook exit code `2` as a **hard block**, and `python3` exits `2` when its script file is missing. The registered command therefore ends in **`|| true`**, which makes the bash invocation exit 0 no matter what `python3` does. This is the root-cause fix for the **compaction-deadlock class** described below (CHANGELOG v1.16.0/1).
2. **The deadlock class it kills.** On 2026-07-17 a config-repo *untrack + pull* deleted a box's entire machine-local deploy layer (`~/.claude/scripts/`) while the shared `settings.json` kept referencing the now-missing `precompact_capture.py`. Every long session that reached its context limit needed to compact, the missing script made the PreCompact hook exit 2, the exit-2 hard-blocked compaction, and the session **wedged at "Prompt is too long"** with no way forward. `|| true` makes that impossible going forward; the skew guard (invariant 5) catches the *missing-script precondition* before it can bite.
3. **Capture is capture-only and fail-silent.** PreCompact cannot inject context, so the sidecar's sole job is to write the marker; any error writes no marker and exits 0.
4. **The marker is consume-once and freshness-gated.** The restore helper deletes the marker on every read regardless of validity, and only *uses* a query younger than 300 s — so a marker can neither linger into an unrelated later session nor drive a restore against a conversation that is minutes gone.
5. **The skew guard keeps the deploy layer honest.** `3-verify.ps1` asserts that *every* `~/.claude/scripts/<file>` referenced anywhere in `settings.json` exists on disk (deliberately broader than just hook commands — over-detection is the safe direction). This is the check that surfaces the deleted-script-layer condition that once triggered the deadlock class.
6. **The marker carries no secrets.** Redaction runs before the query is written, so credential-shaped text never reaches the marker file even transiently.
7. **Restore never writes memory.** The bundle read is `checkpoint:false`; it must not create a synthetic episode or otherwise mutate the store.

## Security and privacy notes

- **Redaction at the source.** The capture sidecar strips OpenAI keys, `Authorization` headers, `api_key`/`token`/`password`/`secret` assignments, and PEM private-key blocks *before* writing the marker — the same credential-shape set the server-side readers use.
- **The marker is local at-rest data.** It lives under `~/.mem0/` with the rest of the store, holds only a bounded (≤ 800-char) redacted précis, and is deleted at the next SessionStart. It is never transmitted anywhere; the restore query text is sent only to the already-trusted local authority.
- **Advisory, not authoritative.** The injected block is explicitly headed "verify before acting" and tagged `[recall]`, so ranked recalls are never mistaken for canonical facts.
- **Operator-neutral at rest.** Both shipped scripts are dependency-free and carry no real host or operator identity; `transcript_path` and the WSL mount are resolved at runtime.

## Observability and debugging

- **Did capture run?** Look for `~/.mem0/precompact-query.json` immediately after a compaction (it is deleted at the next SessionStart, so catch it in the window). Its absence after a compaction means the sidecar found no query or failed silently.
- **Did restore fire?** The `Recently-relevant memory (verify before acting):` block in the SessionStart banner is the visible signal; its absence means no fresh marker *and* no cold-boot signal, or an empty/failed bundle.
- **Session wedged at "Prompt is too long"** → the classic symptom of the deadlock class. Confirm the skew guard: re-run `3-verify.ps1` and check "No hook references a missing deployed script"; a missing `precompact_capture.py` (or any referenced script) points at a skewed deploy layer — redeploy via `2-windows-config.ps1`.
- **Marker present but no restore block** → check freshness (a marker older than 300 s is ignored) and that `~/.mem0/api-key` exists and the authority answers `/v1/context/bundle`.

## Testing notes

- The pure logic of the restore helper is unit-testable in isolation: `build_boot_query` (recency pseudo-query vs abstention), `choose_query_and_params` (fresh-marker ⇒ frontier/K=2, else small/K=1), `load_and_consume_marker` (always-delete + freshness gate), `distill`, `select_facts`, and `format_block` (silent on empty). The I/O paths (`recent_goal_for_brand`, `fetch_bundle`) are exercised by the live end-to-end, not unit-tested — matching the in-file design note.
- The capture sidecar's `win_to_wsl_path`, `redact`, `extract_turns_text`, and `build_query` are likewise pure and directly testable; the redaction set should be regression-tested against each credential shape.
- **The deadlock-class regression is a *deploy-layer* test, not a script test:** the skew guard in `3-verify.ps1` is what must stay green — verify it fails loudly when a referenced script is absent.

## Source map

- [`../../claude-config/precompact_capture.py`](../../claude-config/precompact_capture.py) — the PreCompact capture sidecar: transcript tail, turn extraction, redaction, atomic marker publish, fail-silent contract.
- [`../../claude-config/sessionstart_bundle.py`](../../claude-config/sessionstart_bundle.py) — the SessionStart restore helper: `load_and_consume_marker` (consume-once + 300 s freshness), `choose_query_and_params` (frontier/K=2 vs small/K=1), the `checkpoint:false` bundle fetch, distillation.
- [`../../claude-config/storage-cap-check.sh`](../../claude-config/storage-cap-check.sh) — the WSL SessionStart banner that invokes the restore helper.
- [`../../install/2-windows-config.ps1`](../../install/2-windows-config.ps1) — registers both PreCompact hooks; defines the `… || true` fail-open capture command and its rationale.
- [`../../install/3-verify.ps1`](../../install/3-verify.ps1) — the skew guard: every settings.json-referenced deployed script must exist on disk.
- [`../../CHANGELOG.md`](../../CHANGELOG.md) — v1.16.0/1 "deploy-layer-skew hardening": the fail-open PreCompact fix and the deadlock it closed.

## Related docs

- [`../systems/codex-hooks.md`](../systems/codex-hooks.md) — the Claude Code hook pipeline, the PreCompact extractor dispatcher (`stop-extract.ps1`) that co-tenants this event, and the fail-open hook contract in full.
- [`../systems/continuity.md`](../systems/continuity.md) — session-continuity design (episodes, recent-decisions, the SessionStart banner) that the restored bundle enriches.
- [`../systems/installer-and-deploy.md`](../systems/installer-and-deploy.md) — the install/deploy machinery, the deploy layer, and the skew/parity checks that keep it honest.
- [`../systems/model-aware-injection.md`](../systems/model-aware-injection.md) — how the injected memory block is scaled to the consuming model's tier.
- [`../systems/mem0-api.md`](../systems/mem0-api.md) — the `/v1/context/bundle` surface the restore helper reads.
- [`../glossary.md`](../glossary.md) · [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)
