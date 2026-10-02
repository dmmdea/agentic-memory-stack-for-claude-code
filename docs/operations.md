# Operations runbook

When something breaks — or a session banner asks for attention — look here first. Each entry: symptom → diagnostic queries → fix.

> Linux paths are relative to your WSL user's home (`~`); Windows paths use `$env:USERPROFILE`. The installer substitutes your real usernames into the deployed scripts.

---

## Quick health check (run this first when anything seems off)

```powershell
# In any PowerShell on Windows (WSL mirrored networking makes these reachable):
'qdrant=http://127.0.0.1:6333/healthz',
'mem0=http://127.0.0.1:18791/health',
'llama-swap=http://127.0.0.1:11436/v1/models' | ForEach-Object {
    $name,$url = $_ -split '=',2
    try { Invoke-RestMethod -Uri $url -TimeoutSec 5 | Out-Null; Write-Host "  $name OK" -f Green }
    catch { Write-Host "  $name DOWN ($($_.Exception.Message))" -f Red }
}

# Deep end-to-end (store + embedder dimension + collection binding; slower):
Invoke-RestMethod http://127.0.0.1:18791/health/deep

# Codex (the extraction/judgment LLM) still authenticated?
"Reply with exactly: ok" | codex exec --skip-git-repo-check -c model_reasoning_effort='"low"' -
```

`/health` also reports the stack release version. Anything red → matching section below. For the full scripted check, run the deployed **`Test-MemoryStack.ps1`** (`~/.claude/scripts/Test-MemoryStack.ps1`) — liveness + invariants, pass/fail per row. It is role-aware: on a replica the shared-store rows probe the authority in `~/.mem0/authority-url` (the `memory authority` INFO row shows which), mutation probes are skipped, and brain-only machinery reports "by design" (see [systems/installer-and-deploy.md](systems/installer-and-deploy.md#role-aware-health-rows-health-check-time)).

---

## The scheduled machinery (what should be running when)

| When | Job | Where to check |
|---|---|---|
| every prompt | memory injection + episode checkpoint + correction capture | `~/.claude/logs/user-prompt-extract.log` |
| session start | banners (storage cap, audit flags, review queue), resume précis, dream catch-up spawn | the banner itself; `~/.claude/logs/` |
| session end / compaction | L1a fact extraction (10-min throttle) | `~/.claude/logs/l1a.log` |
| daily 3:00 (Task Scheduler, WakeToRun) | dream consolidation | `schtasks /Query /TN "ClaudeCode-DreamConsolidator-3am" /FO LIST /V`; `~/.claude/logs/dream.log` |
| daily 3:30 | stack-backup (**last 8 daily snapshots kept ≈ an 8-day restore window**) | `systemctl --user list-timers` in WSL |
| daily 4:30 (Task Scheduler) | semantic dedup | `~/.mem0/tier-ledger-YYYY-MM.jsonl` (deletes are logged; monthly segments) |
| Sun 02:00 / 04:00 / 05:00 / 05:30 | decay-scan / goals-stale-sweep / contradiction-sweep / episodic-reconcile | `systemctl --user list-timers` in WSL |
| every 6 h | L10 heuristic audit | `~/.mem0/audit-flags.jsonl` |
| **native Linux authority:** daily 03:00 (`ams-nightly.timer`, `Persistent=`, RTC wake armed 02:45) | the one chain, 18 steps: `dream` → `semantic-dedup` and `store-judge` (with a hub configured) → `index-refresh` → `wiki-index` (when configured) beside `goal-recurrence-promote` and `episode-upkeep` (after the dream, before the backup) → the Sunday jobs `decay-scan`, `goals-stale-sweep`, `contradiction-sweep`, `episodic-reconcile`, `retrieval-pairs` → `stack-backup` → `syncoid` → `pcloud-copy` → `morning-summary` → `health-stamp` → `rtcwake` (order and conditions: [installer-and-deploy](systems/installer-and-deploy.md#linux-authority-native-installlinux-authoritysh)) | `systemctl --user list-timers ams-nightly.timer`; `tail ~/.mem0/maintenance/receipts.jsonl`; `curl -s http://<authority>:18791/health/maintenance` |

The Task Scheduler and per-job timer rows above describe a Windows/WSL-hosted brain. On the native authority, the shipped shape, those jobs are steps of the one chain in the last row, and a replica PC registers none of them: its hooks and shim talk to the authority, and its own local mem0 and Qdrant stay dormant.

```bash
# WSL: are the timers armed?
systemctl --user list-timers --all | grep -E "decay|backup|goals|contradiction|reconcile|l10"
```

**Reading the semantic-dedup receipt** (`~/.mem0/dedup-summary.jsonl`, last row; also the step's `work` counts): `scanned`, `skipped_no_vector`, `compared_pairs`, `candidates`, `deleted`, `protected_skips`, `max_deletions`, `capped`, plus `planned` (deletions the run meant to make) and `delete_failed` (refused by mem0). A run that scanned over 1000 points and compared no pair (or skipped over 1 % for want of a vector) reads `degraded:compared-0` / `degraded:skipped-no-vector`, and so does the `dedup-job` capability. A run whose every planned delete was refused reads `degraded:deletes-refused`, and one where more than half were refused `degraded:deletes-failing`; the restore record marks each refused id (`delete_failed`). Each run deletes at most `--max-deletions` (default 50, highest cosine first), so a backlog drains over a few nights; `--dry-run` writes every candidate to `~/.mem0/dedup-report.dryrun.jsonl` for review before any live run. Canonical, automemory-migrated and operator-sourced insight records are never deleted.

**Reading the episodic-reconcile receipt** (`~/.mem0/episodic-reconciliation.jsonl`): the Sunday run also abandons `in_progress` episodes untouched for 7 days (`abandoned_stale_in_progress`, with `abandoned_sample`, `abandoned_oldest_ended_at` and `in_progress_remaining`; the sweep is SQLite-only, so it runs before the Qdrant readiness check and a Qdrant outage does not stop it) and embeds up to 500 missing episode summaries per run (`embedding_backfill`, newest first; it polls `/health/embedder` for up to 120 s for a cold seat and skips the backfill only when the embedder stays down for that whole window); embedding coverage under 90 % of eligible episodes reads `degraded:embedding-coverage-<pct>` (a catching-up backlog: outcome and step status, exit 0). An
*orphaned link* is an episode→memory link whose memory is gone from Qdrant. Since 2026-08-24
the receipt splits them by deletion evidence — `orphaned_explained_count` (a DELETE row in
`history.db` **or** a `delete`/`decay-delete` event in the tier-ledger: the semantic-dedup and
decay purges, lineage debt on record) vs `orphaned_unexplained_count` (vanished with **no**
trace: possible data loss). Only unexplained orphans degrade the outcome
(`degraded:orphaned-links-unexplained:<n>`, threshold zero); explained ones stay in the
receipt with actor + reason so a suspicious burst is still visible. If neither evidence
source can be read the run abstains (`degraded:orphan-evidence-unavailable:<n>`) rather than
accusing every orphan; if exactly one source fails (or `history.db` reports zero DELETE rows
beside live orphans — a rebuilt table) the split still runs but the outcome is
`degraded:orphan-evidence-partial:<source>:unexplained=<n>` and Test-MemoryStack prefixes
the row with `EVIDENCE PARTIAL` — fix the source before reading the counts. Both sources are
probed every run, so a dead source is a standing signal, not a discovery made the week
orphans appear.

**Reading the episode-upkeep receipt** (the daily `episode-upkeep` chain step, `episodic-reconcile.py --upkeep`; one line per run in the same `~/.mem0/episodic-reconciliation.jsonl`, marked `"mode": "upkeep"`, and the step's receipt in `~/.mem0/maintenance/receipts.jsonl`). It does two things and nothing else (no orphan, drift or coverage pass): it closes `in_progress` episodes untouched for more than 7 days (`abandoned_stale_in_progress`; `abandoned_sample` lists up to `--limit-sample` of them as `{id, session_id, ended_at}`, `abandoned_oldest_ended_at` and `in_progress_remaining` say how far back it reached and what is left; rows are marked `abandoned`, never deleted, and their text is not rewritten), and it embeds up to 200 episode summaries whose search vector is missing. The vector gap is a **per-id diff** (the episodes SQLite says are indexable minus the ids in the vector collection), so a gap of 4 reads `missing: 4` with `missing_ids` naming them, where the Sunday coverage figure would say 99 %. The embedder is polled (up to 120 s) and used only when something is missing. The outcome line is `ok`, or `degraded:<reason>[,<reason>]` with exit 0 so the chain's next steps still run: `embedder-down` (it never came up inside the wait or the retry budget), `embed-errors-<n>`, `remaining-<n>` (a backlog left after the cap), `backfill-failed` (Qdrant unreachable or the ledger unreadable) and `abandon-failed`; it surfaces in `GET /health/maintenance` as a degraded step until the next clean run. Only a missing `episodic.db` fails the step. Because the daily lines share the file with the Sunday run's, anything reading it for the weekly reconcile must skip the `"mode": "upkeep"` ones (Test-MemoryStack's `episodic reconcile` row does). **To see what a run would do, by hand, writing nothing:** `~/apps/mem0-server/.venv/bin/python ~/apps/mem0-scripts/episodic-reconcile.py --upkeep --dry-run` prints the would-be receipt (`would_abandon`, the sample, `missing`, `missing_ids`) and changes nothing: no ledger write, no embed, no receipt line. Without `--dry-run` the same command is the backlog run: it closes every stale checkpoint (the first run clears the whole backlog at once; later runs find only the day's) and embeds 200 missing vectors per run (`--backfill-limit N` raises or lowers that; `0` skips the backfill). `scripts/wsl/episode-embed-backfill.py --limit N --wait-embedder 120 --retry-budget-s 120 [--dry-run]` is the vector half on its own. A summary finalized while the embedder is cold is retried in the background first (journal lines `episode embed deferred ep=<id>`, `... recovered ep=<id>` or `... gave up ep=<id>`); what that cannot recover the daily step does.

---

## "The session banner says contradictions await review"

This is the reconciliation system working as designed: a Codex verdict flagged records as stale/contradicting, and the queue-gated resolution policy routes them to you instead of hiding them. (The weekly canonical-anchored sweep is the one path that stamps directly — `--unstamp` reverses any stamp in one command.)

```bash
# WSL — see the queue (one JSON line per candidate: memory_id, canonical_id, candidate text, justification)
cat ~/.mem0/contradiction-promote-review.jsonl

# The sweep runs on the mem0 venv python; the deployed copy lives in ~/apps/mem0-scripts/
PY=~/apps/mem0-server/.venv/bin/python
# The modes that stamp or clear a stamp send the sweep's server-side job label, which the server
# accepts only with the authority's service key (1.32.5). Run them through the wrapper; see
# "A hand run is refused with service-credential-required" below.
SVC="bash $HOME/apps/mem0-scripts/ams-service-run.sh"

# Enforce a reviewed candidate (hides it from durable/operational retrieval; forensic 'history' still sees it)
$SVC contradiction-sweep.py --promote <memory_id>

# It was a false flag / you promoted the wrong one — one-command recovery
$SVC contradiction-sweep.py --unstamp <memory_id>

# Re-judge everything currently flagged (auto-CLEARS false positives; never auto-hides)
$SVC contradiction-sweep.py --rejudge-stamped --judge codex --apply

# A `kind: canonical-possibly-stale` line has no canonical_id, so --promote refuses it. Once you have refreshed or
# demoted the canonical (or decided it is fine), drop the line; the weekly sweep also drops it by itself when its
# stale_canonical_id is no longer a live canonical
$PY ~/apps/mem0-scripts/contradiction-sweep.py --dismiss <memory_id>

# A `kind: supersede` line (a sweep judged the OLDER record stale; its canonical_id is the NEWER one) is a staleness review, not a
# contradiction, so --promote refuses it. Record the supersession (dry-run first, --apply writes; it also dequeues the line),
# and undo it if you got it wrong (--scope partial|all for partial annotations):
$PY ~/apps/mem0-scripts/contradiction-sweep.py --resolve-supersede <older_id> --winner <newer_id> --apply
$PY ~/apps/mem0-scripts/contradiction-sweep.py --unsupersede <older_id> --apply
```

The Codex judge needs the Windows shim up (`:18792`; it self-starts at session start when enabled, idle-stops after 4 h). Since 2026-08-24 every judged run brings it up on demand itself — the units' `ExecStartPre` spawns it (inlining `codex-shim-spawn.ps1` + a curl health poll) and the sweep has an in-run backstop (`ensure-codex-shim.sh`, WSL interop, which invokes that same spawn ps1) — so `outcome=no-op:codex-shim-unreachable` now means the bring-up **also** failed (the receipt's `ensure_attempted` says whether it ran): check `~/.claude/logs/codex-shim.log`, then run `bash ~/apps/mem0-scripts/ensure-codex-shim.sh` by hand. It deliberately refuses to fall back to a local judge.

Two more outcomes you may see: `degraded:judge-lock-contended` — the sweep waited its full 40-min patience budget for the shared codex lock (dream/L1a hold it legitimately; a wedged holder is reclaimed after 30 min) and gave up; it self-heals next run. `degraded:aborted:judge unresponsive` — five consecutive real judge failures. And Test-MemoryStack's `codex live-judge freshness` row WARNs when **no live Codex verdict** has landed in 9 days — cache-only and fast-skip runs look benign individually, that row is the cross-run alarm.

---

## "A hand run is refused with `service-credential-required`"

Since 1.32.5 the labels the authority's own jobs write under are claims, not credentials: the contradiction sweep's and the retired-at stamper's `actor`, the ship-log reclassifier's `actor`, and the dream's `actor` and insight `source`. A label counts only when the request also carries the authority's **service key** in the `X-AMS-Service-Key` header. The ordinary API key proves nothing about a label, because every PC and every MCP session holds it. A request that names such a label without the key is answered `403 service-credential-required: <field>=<label> is a server-side job label and ...`; the rest of the message says whether the server holds no service key at all or the request did not carry it. A write that names no job label, or its own (`claude-autonomous`), runs under the ordinary rules. The nightly units load the key as the `ams-service-key` credential, so the chain is unaffected. The case here is a hand run from a plain shell, which holds at most the API key.

```bash
# On the authority (over ssh, for a native Linux authority). The wrapper takes a script name, then its own arguments verbatim.
bash ~/apps/mem0-scripts/ams-service-run.sh contradiction-sweep.py --unstamp <memory_id>
bash ~/apps/mem0-scripts/ams-service-run.sh contradiction-sweep.py --promote <memory_id>
bash ~/apps/mem0-scripts/ams-service-run.sh stamp-retired-at.py --dry-run
bash ~/apps/mem0-scripts/ams-service-run.sh ship_log_reclassify.py --live
```

`ams-service-run.sh` (deployed to `~/apps/mem0-scripts` by `install/linux-authority.sh`, source `scripts/wsl/ams-service-run.sh`) is the operator's way to run the scripts that send a job label. It runs only `contradiction-sweep.py`, `stamp-retired-at.py` and `ship_log_reclassify.py` (any other name exits 2) and refuses with exit 3 on any box whose role is not `brain`: no replica or PC holds the service key, by design. On the native authority it starts a transient user unit `ams-service-run-<UTC timestamp>` that loads `ams-api-key` and `ams-service-key` and points `MEM0_URL` at the tailnet bind (a native authority has no `~/.mem0/api-key` for a plain shell to read, so the wrapper supplies the API key too), and it exits 2 when `MEM0_SECRETS_DIR`, `MEM0_BIND` or one of the two `.cred` files is missing. On a WSL brain it runs the script directly, and the script reads `~/.mem0/service-key`. The modes that need the key are the ones that stamp, clear a stamp or retire: `--promote`, `--unstamp` and `--rejudge-stamped --apply` of the sweep, and the writing modes of the other two. `--dismiss` and the supersede modes (`--resolve-supersede`, `--unsupersede`, `--supersede-markers`) send no job label.

**Use tmux or screen for a long run.** The unit's output is piped to your terminal, so a dropped ssh session stops the unit at its next write (the same trap `ams-dream-now.sh` avoids by logging to the journal). `stamp-retired-at.py` and `ship_log_reclassify.py` scroll the whole corpus: start them inside `tmux` or `screen`. `ship_log_reclassify.py --live` refuses up front (exit 2) when it holds no service key: each live record posts its episode first, and the retire that follows would be refused, leaving an orphan that the script warns never to re-run over.

If the nightly itself shows the refusal, check whether the server loaded the key:

```bash
curl -s http://<authority>:18791/health/deep | jq '.checks.service_key'     # {present: true, source: "credential"} on the native authority, "plaintext" on a WSL brain
journalctl --user -u mem0.service -n 30                                      # a unit that cannot load its credential fails here, before the server starts
```

- **`present: false` on the authority** → the server started without the key. `checks.service_key` is informational and never flips `ok`, but the capability row `service-key` reads dead on a brain, and the dream's insights and the sweep's stamps are refused until the key is back. On the native authority, re-run `install/linux-authority.sh`: it makes a missing key and replaces one that does not decrypt on this host (the runbook below has the regenerate command). On a WSL brain the key is `~/.mem0/service-key`, read once and cached by the server: `scripts/wsl/deploy.sh` creates a missing one before its restart and asserts it is loaded, and a file created by hand needs `systemctl --user restart mem0.service`.
- **`mem0.service` will not start at all after an upgrade** → once the drop-in carries `LoadCredentialEncrypted=ams-service-key:...`, a missing or undecryptable `ams-service-key.cred` stops the unit. The same re-run of the installer fixes it: it decrypt-tests the file and, when the test fails, sets the old one aside as `ams-service-key.cred.undecryptable-<UTC stamp>` (never deleted) and writes a new one. Nothing needs restoring from a backup.
- **The dream reports `posted-0-of-<n>`, and its `insight post failed` journal line says `the server refused the dream-consolidator label: this process holds no service key`** → the dream's process held no service key. On the native authority that is a step unit not re-rendered since the upgrade (re-run the installer); the unposted insights wait in the spool and go first on the next run. On a Windows-hosted brain it is the WSL `~/.mem0/service-key` missing, and a failed insight add from PowerShell is never queued in the shared Outbox, whose replay deliberately never sends the service key: a `403` lands in `~/.claude/state/mem0-post-poison.jsonl` for a human, a transient failure in `~/.claude/state/mem0-post-failures.jsonl`, whose entries are re-posted through the same PowerShell add path, which reads the key again.
- **A replica or a PC sees the refusal on a label** → correct. They hold no service key and their servers, dormant or not, refuse every job label. Run the job on the authority.

---

## "The banner says audit flags need review"

L10's 6-hourly heuristics flagged writes (oversize / injection-shaped / credential-shaped / missing provenance). Flags are advisory — nothing is hidden.

```bash
PY=~/apps/mem0-server/.venv/bin/python
$PY ~/apps/mem0-scripts/audit-flags-triage.py --summary     # what's flagged, grouped
# --resolve --only-types <class> marks ONE flag class reviewed (2026-08-24) — the safe shape for a
# backlog burn: e.g. the advisory `oversize` class, leaving possible-credential etc. open:
$PY ~/apps/mem0-scripts/audit-flags-triage.py --resolve --only-types oversize --reason "advisory class; records intact"
# --resolve (no --only-types) marks the WHOLE current backlog reviewed (there is no per-id mode);
# hold back categories you still want visible with --keep-types
$PY ~/apps/mem0-scripts/audit-flags-triage.py --resolve --reason "reviewed: benign"
```

---

## "A memory I know exists isn't surfacing"

Retrieval is **precision-first** — records are hidden by design for several reasons. Check which one:

```bash
# 1. Was it rejected at admission? (reason per rejection: tier / superseded_by / contradicts_canonical / brand / age)
tail -20 ~/.mem0/admission-rejected.jsonl

# 2. Ask in the forensic class — history disables the hide checks:
#    mcp__mem0__memory_search query="..." query_class="history"

# 3. Brand scope: a brandless search returns ONLY brand-neutral records (fail-closed).
#    Pass brand="..." or allow_cross_brand=true deliberately.
```

- **Superseded / contradicts-canonical** → that's reconciliation: `--unstamp <id>` (through `ams-service-run.sh`, see above) if a contradiction stamp is wrong, `--unsupersede <id>` (or the `memory_unsupersede` tool) if a supersession is (section above). A record whose text says `SUPERSEDED ... by mem0 <id>` but still surfaces was never superseded: the text is not read, only the `superseded_by` field is. Retire it with `memory_supersede`, or find and convert the old hand-written markers with `contradiction-sweep.py --supersede-markers` (runbook in [`reconciliation.md`](./systems/reconciliation.md#hand-written-markers---supersede-markers)).
- **It's `insight` tier and you expected it in the per-prompt block** → insights are deliberately filtered from the hot path; use `memory_search`.
- **Nothing injected at all on a prompt** → abstention-first: nothing cleared the 0.30 gate. That's correct behavior for off-domain prompts.

---

## "L1a fires but no facts get extracted"

**Diagnose:**

```powershell
Get-Content "$env:USERPROFILE\.claude\logs\l1a.log" -Tail 30
Invoke-RestMethod http://127.0.0.1:18791/health          # mem0 up?
"Reply: ok" | codex exec --skip-git-repo-check -c model_reasoning_effort='"low"' -   # Codex auth?
```

**Common fixes:**
- **Log empty / no `=== start ===` lines** → the Stop/PreCompact hook isn't firing. Check `~/.claude/settings.json` `Stop`/`PreCompact` point at `stop-extract.ps1`; restart VS Code.
- **"codex subagent failed" / auth error** → `codex login` (ChatGPT sign-in).
- **"json parse failed"** → Codex returned non-JSON (read the raw preview in the log; usually transient).
- **"extracted N, posted 0"** → mem0 rejecting writes: `journalctl --user -u mem0.service -n 50`. A 413 means the fact exceeded the storage cap (facts must be atomic; the extractor prompt enforces this).
- **"no facts extracted" on a real session** → often correct: the inferability gate drops generic content. Verify against a session with genuinely project-specific facts.
- Failed posts self-heal from the dead-letter queue on the next run (`~/.claude/state/mem0-post-failures.jsonl`; poison quarantine after 5 attempts).

---

## "The nightly dream didn't run"

**Diagnose:**

```powershell
schtasks /Query /TN "ClaudeCode-DreamConsolidator-3am" /FO LIST /V | Select-String "Last Run|Last Result|Next Run"
Get-Content "$env:USERPROFILE\.claude\logs\dream.log" -Tail 40
```

**Fixes:**
- **Missed night (PC off, etc.)** → self-healing: at the next session start, `dream-catchup.ps1` re-runs a dream that's >48 h stale or has queued promotions. On a replica both are no-ops since 1.28.4 (the log line says `role=replica`): the brain's `ams-nightly.target` is the dream, so read its journal there. A replica's `dream-consolidate.ps1 -Force` exits on its role and consolidates nothing (its log says `skipping: role=replica`). To force a dream now, run it on the authority: `ssh <brain-alias> 'bash ~/apps/mem0-scripts/ams-dream-now.sh'` (below).
- **Task missing/broken** → re-register idempotently: rerun `install\2-windows-config.ps1`.
- **Ran but 0 insights** → often correct (no consolidation-worthy evidence). Check the log's Codex output preview. If it shows insights consolidated but none posted, the server may have refused the dream's label (1.32.5: it needs the service key; see "A hand run is refused with `service-credential-required`" above).
- The MEMORY.md index refresh is decoupled (`memory-index-refresh.ps1`, 6-h throttle) — a down dream no longer freezes the index.

**On the native Linux authority** the dream is the first step of the nightly chain and there is no catch-up script (the timer is `Persistent=`; the boot guard skips a completed night):

```bash
journalctl --user -u ams-step-dream --no-pager -n 60          # the phase log (also ~/.mem0/maintenance/logs/dream.log)
tail -20 ~/.mem0/maintenance/receipts.jsonl                    # one line per step: ok / exit / duration / note
cat ~/.mem0/maintenance/dream/gather.json | jq '.signals|length'
curl -s http://<authority>:18791/health/maintenance | jq '{ok, failed_steps, degraded_steps, stale_steps, pool, usage, judge_transport}'
~/apps/mem0-server/.venv/bin/python ~/apps/mem0-scripts/codex-usage-report.py --gate   # the 25 % reserve verdict the dream read
systemctl --user start ams-step-dream.service                 # the chain's own step: after a good night it is a receipted no-op ("guard: ..."), else the dream's 23 h throttle applies
bash ~/apps/mem0-scripts/ams-dream-now.sh                     # a real forced dream now (see below)
```

**Forcing a dream by hand.** `ams-dream-now.sh` (deployed to `~/apps/mem0-scripts` by `install/linux-authority.sh`, source `scripts/wsl/ams-dream-now.sh`) starts one dream run on the authority, outside the chain guard. It takes no arguments and reads the install record in `~/.mem0/stack.env`: it refuses (exit 3, naming the ssh form above) on any box that is not `MEM0_ROLE=brain` with `MEM0_HOST_KIND=native`, and stops with exit 2 when `MEM0_SECRETS_DIR` or one of the three `.cred` files (`ams-api-key`, `ams-canonical-key`, `ams-service-key`) is missing. It runs a transient user unit `ams-dream-now-<UTC timestamp>` with the same three `LoadCredentialEncrypted=` credentials (the service key is what lets the server accept the dream's insight writes and `touched_by_dream` stamps; without it they are refused and the dream posts nothing), the same `Environment=` lines and the same `codex-usage-report.py --probe` pre-step as `ams-step-dream.service`, and the unit's own command without `--guarded`, plus `--force`. Two consequences: the run goes through `ams-step.sh`, so it is receipted as step `dream` like any chain step (and, like any run that is not the stamping step, it never stamps `last-chain-success`); and `--force` bypasses only the dream's own 23 h throttle, so the judge lock (a run overlapping the nightly is a quiet, receipted skip) and the Codex quota reserve (`skipping: codex quota gate`) still apply. The unit is not tied to your session: its output goes to the journal as the chain's does, so a dropped ssh session, a closed laptop or a Ctrl-C leaves the dream running to completion instead of killing it at its next print (which would leave a partial cycle and a `failed` `dream` receipt). The helper prints the follow command first, `journalctl --user -u ams-dream-now-<timestamp> -f`, then waits and exits with the unit's status, printing the unit's result when it ends. That result says the run ended, not what the dream decided (a skip for the judge lock or the quota gate exits 0 too): the verdict is the dream's own `dream:` lines, in that journal or in `~/.mem0/maintenance/logs/dream.log`. A forced dream is a real one, so when it completes it marks the same 23 h throttle as the nightly dream: a hand run that finishes after 04:00 leaves the next 03:00 dream less than 23 h behind it, and that dream skips (`skipping: nightly throttle (23h) not yet elapsed`, in the journal and in `dream.log`). Run it before 04:00, or take it as standing in for the next night: re-running on unchanged evidence only adds near-duplicate insights ([ARCHITECTURE](../ARCHITECTURE.md#why-a-3am-consolidation)). There is deliberately no `--dry-run` here: a dry run would still be receipted as a `dream` step and refresh the dream's freshness in `/health/maintenance`.

A receipt with `note: "skipping: codex quota gate ..."` is the reserve rule, not a failure; `"guard: chain succeeded since the last 03:00 boundary"` is the boot re-run of a completed night; `"weekly: not Sun; no-op"` is a weekday. A step with `ok:false` names its exit code and the tail of its stderr in `note`.

**Reading `status`.** Every receipt carries `status` (`ok`, `degraded` or `failed`) and `work` (counts the step reported). `degraded` means the step exited 0 but did not do its job: the dream posted 0 of 3 insights (`posted-0-of-3`; the unposted ones wait in `~/.mem0/maintenance/dream/insight-spool.jsonl` and go first next run; a server that refuses the dream's label because no service key was loaded is one cause, and the dream's journal line says so), a queued insight could not be replayed (`replay-failed-<n>`) or was left waiting (`spool-backlog-<n>`), its drift snapshot failed, or a weekly sweep found nothing to judge (`no-op-<reason>`). The receipt is `ok:true`, so the chain still stamps its success, but `/health/maintenance` lists the step under `degraded_steps` and its `ok` is false until a later run of that step comes back `ok`. `failed_steps` lists steps whose latest run exited non-zero (or reported `failed:*`); the health stamp ends the chain red on any of those, on a pool that is not `ONLINE`, or on a failing write path (`write_path.ok: false`, named on its verdict line as `write-path <last_error>`; see "The banner says the write path is failing" below). The morning summary's Chain block prints each degraded step with its note and counts, e.g. `- dream DEGRADED 41000ms -- posted-0-of-3 [consolidated=3 posted=0]`. A `weekly:` off-day receipt does not clear a Sunday failure; only a later real run does. A `--dry-run` of the dream skips the spool replay by design, so it does not report a standing spool as `spool-backlog-<n>`.

**Pool-health acknowledgment.** A pool that is DEGRADED on purpose (a planned boot-disk swap) would keep `/health/maintenance` `ok:false`, the health stamp red and a replica's banner up for the whole window. Acknowledge it with a dated key in `~/.mem0/stack.env` on the authority:

```bash
echo 'MEM0_POOL_HEALTH_ACK=DEGRADED:2026-10-06' >> ~/.mem0/stack.env   # STATE:last day (UTC); read on every request, no restart
curl -s http://<authority>:18791/health/maintenance | jq '.pool'        # health DEGRADED, health_alarm false, health_ack.active true
```

The ack lapses by itself the day after its date (`health_ack.reason` becomes `expired` and the alarm returns), and it applies only while the pool is in exactly the named state: a pool that worsens to `FAULTED` alarms at once (`mismatch`). Clear it early by deleting the line (`sed -i '/^MEM0_POOL_HEALTH_ACK=/d' ~/.mem0/stack.env`) once the pool is `ONLINE`. A process environment value of the same name wins over the file. Installer re-runs carry the line over (`STACK_ENV_OPERATOR_KEYS`). The health stamp and the morning summary print `DEGRADED (acked until <date>)` while it is active, so an acknowledged pool is never mistaken for a healthy one.

---

## "Codex says 'Not logged in'"

```powershell
codex login status
```
- Wrong/expired auth → `codex logout` then `codex login`, pick **Sign in with ChatGPT**.
- Extraction, the dream, and the judgment shim share one Codex lock — a stuck lock shows in logs as "codex lock held"; it stale-reclaims automatically.

---

## "MCP tools not appearing in Claude Code"

```powershell
# Registered?
(Get-Content "$env:USERPROFILE\.claude.json" -Raw | ConvertFrom-Json).mcpServers.mem0

# Shim runs? (Ctrl+C to exit; an import error = Python/venv issue).
# The shim file is deployed to the WINDOWS ~/.claude/scripts and run through the WSL venv:
$shim = (wsl -e wslpath -a ($env:USERPROFILE + "\.claude\scripts\mem0-mcp-shim.py")).Trim()
wsl -e bash -lc "~/apps/mem0-server/.venv/bin/python $shim < /dev/null"
```

- **mem0 down** → all tools fail: `systemctl --user start mem0.service`.
- **Server config changed** → MCP servers spawn at session start; restart VS Code.
- **Banner corruption** (fastmcp ANSI banner on stdout) → `mcp.run(show_banner=False)` must be set (it is, in the shipped shim).

---

## "The banner says the write path is failing"

The banner line `[AMS] brain NOT OK — write path failing (503 upstream since <time>)`, or `write_path.ok: false` on `/health/maintenance`, means the last memory write the server saw (`POST /v1/memories` or `PUT /v1/memories/{id}`) ended in a `5xx` and no write has succeeded since. The signal is **passive**: the server learns it from real write traffic and never probes, so it clears only when a write succeeds (a server restart also clears it, back to "no evidence yet"), and an hour of silence does not clear it. A `2xx` that never reached the embedder is **neutral** and clears nothing: an idempotent duplicate (`deduplicated: true`, which is what an automated writer re-posting its transcript mostly gets) and an `infer: false` add that stored nothing (`results: []`). So on a quiet system the flag can outlive the outage: it clears on the next write that stores something (a new fact, a `PUT`), not on re-posted duplicates. The fields and rules are in [systems/mem0-api.md](systems/mem0-api.md#get-healthmaintenance).

```bash
curl -s http://<authority>:18791/health/maintenance | jq '.write_path'          # ok, last_error, last_error_at, errors_1h, writes_1h
journalctl --user -u mem0.service --since "1 hour ago" | grep -E "add failed|update failed" | tail -5   # what the failing writes raised
curl -s http://127.0.0.1:11436/v1/models | jq '.data[] | select(.id | test("embedding"))'                # is the embedder listed, and loaded?
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv                                 # what else holds the card?
```

- **`503 upstream` (or `503 cold-embedder`):** the embedder cannot serve right now. The write routes usually report a dead embedder as `503 upstream` (they map embedder errors through `_upstream_error`, whose body names no reason); `503 cold-embedder` is the same condition reaching the dedicated handler, so read the two alike. It is not resident and could not start, or llama-swap is down or rate-limiting. The usual cause is a co-resident process holding the GPU so the embedder has no room to load; check the embedder's residency first (the `/v1/models` listing above, and what `nvidia-smi` shows on the card). Free the card, or restart llama-swap as described under "mem0 returns 500" below.
- **`500 upstream`:** an error the server does not recognise as an outage (a coding error, a context overflow). Read the `add failed` / `update failed` traceback in the journal. **A `5xx` from any cause on a write route counts**, so a server-side bug on that route is a broken write path too, and so is one bad request that lands in the routes' catch-all (a `PUT` of an id that does not exist answers `500`): the flag stays red until the next write that stores something, so read the traceback before assuming the embedder.
- **Hand check of the embedder:** `curl -s http://<authority>:18791/health/embedder` once. It embeds one token and therefore loads the model. Use it by hand, **never as a poll** (the chain's own episode backfills are the exceptions: the daily `episode-upkeep` step polls it for up to 120 s only when episode vectors are missing, so a night with nothing missing never calls it, and the Sunday `episodic-reconcile` polls it the same way (up to 120 s) once a week before its bounded backfill, whether or not anything is missing): an uptime checker calling it (or `/health/deep`) every few minutes would keep the embedder resident and defeat the five-minute idle unload. Poll `/health/maintenance` instead, which reads only the tracker and costs nothing.
- **After the fix,** the next write from any session that stores something (a new fact, a `PUT`) clears the flag and the banner goes quiet; re-posted duplicates do not, by design, because they never touch the embedder. If the hand check above shows the embedder healthy and nothing new is being written, the flag waits for the next new fact; restarting the server also clears it (back to "no evidence yet"). `errors_1h`, `last_error` and `last_error_at` keep the record of the outage.
- **The nightly chain sees it too.** `ams-health-stamp.sh` ends the night red (exit `2`) on a failing write path, like a failed step, and its verdict line (the receipt note) and the morning summary's health line end `write-path <last_error>`, e.g. `health ok=False failed=- degraded=- pool 71.4% ONLINE write-path 503 upstream`. A healthy write path adds nothing to either line.

---

## "mem0 returns 500"

```bash
systemctl --user status mem0.service
journalctl --user -u mem0.service -n 50
curl http://127.0.0.1:6333/collections/mem0_egemma_768     # Qdrant collection healthy?
curl -s http://127.0.0.1:11436/v1/models | grep -o embeddinggemma   # embedder being served?
```

A write that failed with a `5xx` also shows on `/health/maintenance` as `write_path`: see "The banner says the write path is failing" above.

- **Qdrant refused** → `systemctl --user restart qdrant.service`.
- **Embedder refused / wrong dim** → llama-swap issue. **llama-swap is per-host: it may run as a WSL systemd-user unit OR as a Windows-native process** (mirrored networking makes `:11436` reachable either way, which masks the difference until restart time — find the owner before restarting):
  - WSL-native: `systemctl --user restart llama-swap.service` (if that unit doesn't exist on this host, llama-swap is not running in WSL — don't stop here).
  - Windows-native: find the owner (`Get-NetTCPConnection -LocalPort 11436 -State Listen | Select OwningProcess`, then `Get-Process -Id <pid>`); if it runs under a scheduled task (the common setup), restart it with `Stop-ScheduledTask -TaskName <task>; Start-ScheduledTask -TaskName <task>` — otherwise stop the process and relaunch it the way it was started.

  Either way, confirm `embeddinggemma` is in its config (see `install/llama-swap-setup.md`). First call after a cold start can be slow (model load) — `/health/deep` needs a generous timeout.
- **ImportError at startup** → venv or a missing module from `MEM0_MODULES` (a fresh-install class of bug now guarded by the import-closure test); rerun the installer.

---

## "Qdrant lost a collection / points missing"

```bash
curl http://127.0.0.1:6333/collections
curl http://127.0.0.1:6333/collections/mem0_egemma_768    # .points_count, .status
```

- **Points dropped to 0 / collection gone** → restore from the latest daily snapshot (last 8 kept ≈ an 8-day restore window): see [`data-backup.md`](./data-backup.md) and `scripts/wsl/stack-restore.sh`. Deletions by dedup/decay are individually restorable: the **full payloads** are preserved in `~/.mem0/dedup-report.jsonl` (`deleted_full_payload`) and `~/.mem0/decay-report.jsonl` (`full_payload`); the monthly tier-ledger segments (`tier-ledger-YYYY-MM.jsonl`) carry the id/reason/actor audit trail.
- **Status red** → corrupted; restore the latest Qdrant snapshot from `~/.mem0/backups/`.

---

## "The authority's only disk died"

The authority's pool holds everything in one place: the corpus, `~/.mem0` (receipts, ledgers, `stack.env`), the three `.cred` files and the System A hub's bare repository. Recovery is a rebuild from an **off-box** copy, not a restart: the cloud mirror of the newest set (`pcloud-copy`) or the ZFS replica (the previous night's set, an RPO of about 24 h; [data-backup](./data-backup.md#off-box-copies)). Whatever was written after that set is gone. Every command below runs on the rebuilt authority. Gather first what was not on the dead disk: the install flags its `stack.env` recorded (tenant, embed model, bind address, ZFS dataset, hub, wiki sources, gate mode, and the hand-set keys `MEM0_BRAIN_SSH`, `MEM0_SHARED_BRANDS`, `MEM0_BRAND_MAP`, `MEM0_NLI_GATE_ENABLED`, `MEM0_POOL_HEALTH_ACK`), a plaintext copy of the API key and of the canonical key (step 2; the service key is regenerated, not copied), and the off-box set.

1. **Rebuild the OS and the installer's prerequisites** (`install/linux-authority.sh` step `[0]` refuses without them): a non-root service user; `python3`, `curl`, `jq`, `systemctl`, `ip`, `systemd-creds`; tailscale up (the bind address is its IPv4); `~/apps/mem0-server`, `~/apps/mem0-scripts`, `~/qdrant-server` and `~/.mem0` as symlinks into the data dataset (nothing on the root disk); llama-swap on `:11436` serving the embedder GGUF the store was embedded with ([llama-swap setup](../install/llama-swap-setup.md); a different conversion of the same model is a different vector space and every search scores noise); passwordless `sudo` for the store binary and the nft unit (or do those by hand). Out-of-repo pieces come back too: `/etc/nftables.d/ams.nft` (the installer persists it as `ams-nft.service`), `~/.mem0/scripts/syncoid.sh` and the pCloud mount.
2. **Restore the secrets.** The `.cred` files in the secrets dir are encrypted to the dead box's host key and TPM, so none of them can be decrypted on a rebuilt one. Two of them, the API key and the canonical key, hold values that must survive; re-create those two from plaintext copies of the values that exist off the box: the bundle `key-backup.sh` made before the keys became credentials (verify it with `sha256sum -c MANIFEST.sha256`), or wherever the plaintext was kept when the `.cred` files were created. On the native authority the `.cred` files are the only on-box copies, and `key-backup.sh`, which reads `~/.mem0/canonical-key` and `~/.mem0/api-key`, finds neither there; no backup set carries them ([key custody](./systems/key-custody.md)). Without a plaintext copy the canonical key cannot be regenerated, because the existing canonical records were signed under it. Encrypt each from its plaintext copy: `systemd-creds --user encrypt --with-key=host+tpm2 --name=ams-api-key <api key file> <secrets dir>/ams-api-key.cred` and `systemd-creds --user encrypt --with-key=host+tpm2 --name=ams-canonical-key <canonical key file> <secrets dir>/ams-canonical-key.cred` (the ids are the ones the units load with `LoadCredentialEncrypted=`). Keep `--name=`: without it systemd embeds the output file's name (`ams-api-key.cred`), and decryption under the id `ams-api-key` fails on the name check (`systemd-creds(1)`). Re-using the old API key keeps every replica and client working. **The third credential, `ams-service-key` (the service key), is regenerated, never restored.** Only this box's server and units hold it and no stored record depends on its value, so it needs no backup, and a copy restored from elsewhere would not decrypt here anyway. Make a new one: `python3 -c 'import secrets; print(secrets.token_hex(32))' | systemd-creds --user encrypt --with-key=host+tpm2 --name=ams-service-key - <secrets dir>/ams-service-key.cred` (the plaintext crosses the pipe and is never written to disk). Re-running the installer in step 3 also makes it when it is missing, and replaces one that does not decrypt on this host, setting the old file aside as `ams-service-key.cred.undecryptable-<UTC stamp>` (never deleted). Without a usable `ams-service-key.cred` `mem0.service` does not start, because its drop-in loads it with `LoadCredentialEncrypted=ams-service-key:...`. Codex login is re-issuable: sign in again with `CODEX_HOME=<secrets dir>/codex`. Put back the hub identity `~/.ssh/id_ed25519_ams_hub` with the hub host's key in `~/.ssh/known_hosts`, and the wiki pull key (`MEM0_WIKI_PULL_KEY`), when they are configured.
3. **Re-run the authority installer with the recorded flags** (`--dry-run` first). With `stack.env` gone there is nothing to inherit, so pass them: `bash install/linux-authority.sh --bind-ip <tailscale0 ipv4> --secrets-dir <secrets dir> --user-id <tenant> --embed-model <name>`, plus whichever of `--zfs-dataset`, `--eval-root`, `--pcloud-dir`, `--ams-checkout` with `--ams-hub`, `--wiki-sources` with `--wiki-pull-key` and `--promotion-gate-mode` the old install used. `--user-id` matters most: left off, the tenant falls back to the Linux login name and every search runs as the wrong user, with the canaries reading 0/7 against a healthy store. Then re-add the hand-set keys to `~/.mem0/stack.env` (they are carried over on later re-runs). The install makes the service key's `.cred` here if step 2 did not, and fails when the server it starts does not report that key loaded. It leaves mem0 running on an empty store, and its step `[6]` arms `ams-nightly.timer`. **Switch the timer off until the read-backs pass:** `systemctl --user disable --now ams-nightly.timer` (a later installer run arms it again). A 03:00, or the `OnBootSec` re-run after a reboot, during the restore would run the chain on an empty or half-restored store: `stack-backup` would snapshot it, `pcloud-copy` would mirror that snapshot off-box as the newest set (the one step 4 restores from), and on a Sunday `goals-stale-sweep --auto-abandon` would run against it.
4. **Restore the corpus** ([MIGRATION.md, Phase 3](./MIGRATION.md#phase-3--restore-the-memory-data) has the long form). Copy the newest complete set into `~/.mem0/backups/` (the manifest and every file it lists; compare the manifest's `checksums`), then `bash ~/apps/mem0-scripts/stack-restore.sh --snapshot <TS> --dry-run`, `systemctl --user stop mem0.service`, and confirm the install's empty collection is empty (`curl -s http://127.0.0.1:6333/collections/mem0_egemma_768` shows `points_count` 0: the script refuses an existing collection) before deleting it with `curl -X DELETE http://127.0.0.1:6333/collections/mem0_egemma_768`. Restore into the production targets: `bash ~/apps/mem0-scripts/stack-restore.sh --snapshot <TS> --target-collection mem0_egemma_768 --target-episodic ~/.mem0/episodic.db`; promote the fixed `-restore` copies as MIGRATION.md lists them (`history-restore.db`, `MEMORY-restore.md`, `audit-flags-restore.baseline`, `tier-ledger-restore.jsonl`); `systemctl --user start mem0.service`. The script restores the memory collection and `episodic.db` only. The episodes and wiki collections are derived (`episode-embed-backfill.py`; the nightly `wiki-index` step or `wiki-index-build.py`); the entities collection has no rebuild path, so upload `qcol-entities-<TS>.snapshot` to `mem0_egemma_768_entities` (delete an empty one mem0 already created first) with the same snapshot-upload call the script makes.
5. **Restore the System A hub.** Its bare repository was on the dead pool too. If the replica carries it, restore it as it was (owner, mode, config). If not, rebuild it empty as the fleet ADR describes (branch `main`, `receive.denyNonFastForwards` and `receive.denyDeletes` on, a `git-shell` user with per-machine keys), add the authority's and every PC's public key back, and seed it from one PC: the first `ams-store sync --once --hub-host <hub>` against an empty hub fetches nothing and pushes that PC's history (pick the PC that synced last), and the other PCs converge at their next session boundary. The authority's own checkout (`--ams-checkout`, step 3) needs the hub identity key and the hub host's key in `known_hosts`; the installer refuses and names whichever is missing. The nightly `store-judge` step carries on from the hub.
6. **Read it back.** `curl -s http://<bind>:18791/health` (the stack version), `/health/deep` (`ok`, `checks.canonical_key.source` = `credential`, `checks.service_key.present` = true, `checks.embedder.dim` = 768, `collection` = `mem0_egemma_768`, `checks.qdrant.points` against the manifest's `counts.qdrant_points`) and `/health/maintenance` (`ok`, `failed_steps`, `degraded_steps`) all answer without a key. Then the canaries: `checks.retrieval_drift` in `/health/deep` (the dream's before/after snapshots; it needs `--eval-root`) and one known-answer `memory_search` that only your history can answer. Only when those pass, arm the chain again: `systemctl --user enable --now ams-nightly.timer`. Prove the chain last: start `ams-nightly.target` once by hand or wait for 03:00, then read `/health/maintenance` again: a new `stack-backup` set and its `pcloud-copy` mirror are the proof that backups work again. If the rebuilt box has a different tailnet address, re-point every replica and client (`install.ps1 -AuthorityUrl ...`, `linux-replica.sh --authority ...`); their queued Outbox writes replay when it answers.

---

## "Disk is filling up"

```bash
du -sh ~/.mem0 ~/qdrant-server/storage 2>/dev/null; du -sh $(wslpath "$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')")/.claude/logs 2>/dev/null
```

- `~/.mem0/backups/` keeps the last 8 of each artifact (daily snapshots → ≈ an 8-day window) — prune older manually if needed.
- Logs rotate automatically (1 MB, 5 archives); ledgers segment monthly.
- The SessionStart storage-cap banner warns at growth boundaries; it never auto-prunes.

---

## "I see weird memories I didn't write"

1. **L1a extracted junk** → find by source (`memory_search`, metadata `source=l1a-extractor`), delete via `memory_delete` (ledgered). Persistent junk = tighten a genuinely noisy transcript pattern, but remember the inferability gate already drops most.
2. **Poisoning via tool output** → the layered defenses (redaction, delimiter-boxed judge prompts, L10 injection-shaped flags, NLI write-gate if enabled) exist for this; check `audit-flags.jsonl` and triage. Canonical cannot be forged regardless (HMAC-gated).
3. **An `insight` or a stamp you did not expect** → since 1.32.5 neither can come from a caller that holds only the shared API key. The dream's insight labels and the sweep's and stamper's job labels count only with the authority's service key (`X-AMS-Service-Key`), which no PC, replica or MCP session holds (an MCP `memory_add` with `tier=insight` is always stored as `evidence`), and moving a record out of `insight` needs the operator's signed token, like leaving canonical (`mem0-canonize.sh --action demote`; the MCP `memory_demote` and `memory_promote` tools no longer move an insight in either direction). So an unexpected insight or stamp came from a process on the authority, or from someone who got at the key there. The key does not resist a shell on the authority as the service user, an ssh session to the brain, or (WSL brain) a Windows-side process of the same user. One key serves every job, so it is not per-job least privilege; it is separate from the canonical key, so a job that holds it cannot mint canonical tokens.
4. **Known gaps 1.32.5 does not close** (none needs a forged label, and none is worse than the `DELETE` every key holder already has on `evidence`, `stable` and `temporal` records): `PATCH /tier` to `temporal` hides a record from every query class, and the `_canonical_intent` metadata key, the supersede door on unprotected tiers and `retired_at` on non-canonical records are likewise open to an API-key holder. Caller-chosen `source` labels that server-side jobs read have no credential behind them: the autopromote corroboration fast-track (`user-decision` / `operator-decision`, whose legitimate sender is an unprivileged PC hook, so no server credential can tell it from a forger) and semantic-dedup's `automemory:` protection. The ledger's `transport` field is self-declared from header presence.

---

## Full-chain smoke test

```powershell
& "$env:USERPROFILE\.claude\scripts\Test-MemoryStack.ps1"
```

Liveness (services, health, MCP registration, hooks SHA-match) + invariants (search behavior, injection gates), pass/fail per row. The install-time equivalent is `install\3-verify.ps1`.

---

## Known issues / past bugs (so you don't re-debug them)

| Symptom | Root cause | Status |
|---|---|---|
| `claude --print` from hooks: "Not logged in" | Max OAuth single-session enforcement | BY DESIGN — the stack uses Codex |
| Per-prompt injection silently dead under VS Code | stdout instead of the `hookSpecificOutput` envelope + a Windows concurrent-spawn race + a short daemon timeout | FIXED v1.11.0 (exec-form hook, envelope, 8 s timeout) |
| Fresh installs crash-loop `mem0.service` | `redact.py` imported but not deployed by the installer | FIXED v1.11.1 (+ import-closure gate in CI) |
| Hook POSTs fail with 400 on non-ASCII | UTF-8 bytes not declared on `-Body` | FIXED (encoding sweep) |
| MCP tools time out at handshake | fastmcp ANSI banner on stdout | FIXED — `show_banner=False` |
| A correct fact vanished from retrieval | early auto-enforce hid records on a single Codex YES | FIXED — never-auto-hide + review queue + `--unstamp` |
| Sweep flags valid historical ship-logs as stale | contradiction prompt reused for the supersession question | FIXED — dedicated STALE/KEEP judge (precision 35→67%) |
| Embedder first-call timeout after idle | llama-swap cold model load | EXPECTED — retry / generous deep-health timeout |
| An unfinished session's summary in `episodic_recent` / the recent-sessions view was task notifications and relayed agent messages | the in-progress episode's running summary appended every UserPromptSubmit prompt, machine turns included, and the readers returned it verbatim | FIXED v1.32.4 — the running summary records only what a person typed (the checkpoint still lands), the read path scrubs unfinished rows (which also cleans the existing backlog without a database write), and `MEMORY.md`'s "Recent episodes", the dream and the `episodic.db` health row look at finished episodes ([`systems/continuity.md`](./systems/continuity.md)) |
| A fact marked `SUPERSEDED ... by mem0 <id>` in its text still appears in searches | sessions had no door for retiring a fact and appended text with `memory_update`; the admission gate reads the `superseded_by` field, never text | FIXED 1.32.4 — `memory_supersede` is the one door (server-enforced, ledgered, reversible); `contradiction-sweep.py --supersede-markers` reports the existing markers and `--apply` converts the full ones |
