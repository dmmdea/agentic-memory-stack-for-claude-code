# Migrating the stack to a new machine — with your memory intact

The runbook for moving an **existing, populated** stack to a new Windows + WSL2 machine and *continuing* there — as opposed to a fresh install (empty store) or an in-place upgrade (`UPGRADE.md`, private repo). Follow the phases in order; each ends with a verification you should not skip.

**The mental model:** the *code* is reinstalled fresh (the installer is idempotent and operator-agnostic), the *data* is restored from a backup snapshot, and the *credentials* are re-provisioned — never transported. Three different treatments for three different things.

| What | Treatment | Why |
|---|---|---|
| Code + services + hooks | fresh `install.ps1` on the new machine | installer derives every path from the new machine's users/distro |
| Memory data (Qdrant collection, episodic.db, ledgers) | **restore from a backup snapshot** | this is your accumulated memory — the point of migrating |
| Canonical HMAC key | **generate fresh** on the new machine | the DPAPI blob is bound to the old machine's Windows user and *cannot decrypt anywhere else*; nothing in the store depends on the old key (it only signs short-lived promote tokens), so a fresh key loses nothing |
| OAuth (Claude, Codex) + git auth | re-login on the new machine | device-bound tokens; not backupable |

> **One machine at a time.** After migrating, stop the scheduled jobs on the old machine (Phase 5). Two live stacks silently diverge — each machine's sessions write to its own local store, and there is no merge tool.

---

## Phase 0 — on the OLD machine: take the snapshot

```bash
# WSL — force a fresh backup snapshot right now (the nightly `stack-backup` chain step takes one daily; a Windows/WSL-hosted brain runs it from a timer at 03:30)
bash ~/apps/mem0-scripts/stack-backup.sh
ls -t ~/.mem0/backups | head    # note the newest timestamp (YYYYmmdd-HHMMSS)
```

The snapshot set includes: the Qdrant collection snapshot, `episodic.db` + `history.db` (SQLite online-backup + integrity check), the tier-ledger segments, `MEMORY.md`, the audit-flags baseline, and the Windows `settings.json` (hook registrations — for *reference*, not for copying onto the new machine). Check the `manifest-<TS>.json` and note the Qdrant **point count** — it is your end-to-end verification number in Phase 3.

Also copy (small, not in the snapshot set): `~/.mem0/contradiction-promote-review.jsonl` (pending review queue), `~/.mem0/learn-rules.jsonl` (pending corrections), and any `*-report.jsonl` you care to keep for the audit trail.

Transport the backup directory to the new machine by any private means (LAN copy, external disk). It contains your memory contents — treat it as sensitive.

## Phase 1 — on the NEW machine: prerequisites + fresh install

1. Satisfy the prerequisites (top-level `README.md` table): WSL2 distro with systemd + `mirrored` networking, Python 3.12+, Node 22+, and **llama-swap with both GGUFs** — the one prerequisite the installer can't auto-satisfy; follow [`install/llama-swap-setup.md`](../install/llama-swap-setup.md).
2. Clone the repo and run the 4-phase installer from Windows PowerShell:
   ```powershell
   git clone <your-repo-url> $HOME\agentic-memory-stack
   cd $HOME\agentic-memory-stack
   .\install.ps1        # prereqs → WSL services → Windows config → verify
   ```
3. `install\3-verify.ps1` must end **ALL VERIFY CHECKS PASSED** before you continue. At this point you have a *working, empty* stack.

## Phase 2 — credentials (fresh, never transported)

1. **Canonical key** — generate + DPAPI-wrap on the new machine, exactly the "new box" provisioning in [`systems/dpapi-canonical-key.md`](./systems/dpapi-canonical-key.md): `generate-canonical-key.sh` → `dpapi-store-canonical-key.ps1` → verify the tmpfs key + a `mem0-canonize.sh` scratch cycle → remove the plaintext. Do **not** copy the old machine's blob or plaintext key; there is no data dependency on it.
2. **LLM auth** — `claude /login` and `codex login` (pick *Sign in with ChatGPT*).
3. **Git hosting auth** — re-authenticate your git tooling on the new machine.

## Phase 3 — restore the memory data

Place the transported backup directory at `~/.mem0/backups` on the new machine, then:

```bash
# List what's restorable, then dry-run the snapshot you took in Phase 0.
bash ~/apps/mem0-scripts/stack-restore.sh                     # lists snapshots
bash ~/apps/mem0-scripts/stack-restore.sh --snapshot <TS> --dry-run
# NOTE: --dry-run validates the snapshot but does NOT run the existing-collection
# check below — a clean dry-run does not guarantee the live run proceeds.

systemctl --user stop mem0.service

# The fresh install's server start already CREATED mem0_egemma_768 (empty), and the
# restore script refuses to write into an EXISTING collection (it checks existence,
# not emptiness). Confirm it is truly empty, then delete it so the snapshot restore
# can recreate it:
curl -s http://127.0.0.1:6333/collections/mem0_egemma_768 | grep -o '"points_count":[0-9]*'
#   -> must print "points_count":0 on a fresh box. If it is NON-zero, STOP — you are
#      not on a fresh install; restore to the alternate targets instead and reconcile.
curl -X DELETE http://127.0.0.1:6333/collections/mem0_egemma_768

# Now restore DIRECTLY into the production targets:
bash ~/apps/mem0-scripts/stack-restore.sh --snapshot <TS> \
     --target-collection mem0_egemma_768 \
     --target-episodic ~/.mem0/episodic.db
```

The restore script only has production-target flags for the **collection** and **episodic.db**; the other artifacts land at fixed `-restore` paths. Promote them while mem0 is still stopped:

```bash
cp ~/.mem0/history-restore.db            ~/.mem0/history.db              # mem0's own SQLite sidecar
cp ~/.mem0/MEMORY-restore.md             ~/.mem0/MEMORY.md               # the memory index file
cp ~/.mem0/audit-flags-restore.baseline  ~/.mem0/audit-flags.baseline    # keeps L10 "NEW since baseline" honest
# The restored ledger is a CONCATENATION of the legacy file + all monthly segments.
# Keep it as the pre-migration archive (writers start fresh monthly segments here):
cp ~/.mem0/tier-ledger-restore.jsonl     ~/.mem0/tier-ledger-pre-migration.jsonl

systemctl --user start mem0.service
```

Copy the queue/ledger files from Phase 0 back into `~/.mem0/`. Then **verify the continuation** — all three, no skipping:

```bash
# 1. Point count matches the Phase-0 manifest
curl -s http://127.0.0.1:6333/collections/mem0_egemma_768 | grep -o '"points_count":[0-9]*'
# 2. Deep health is green (store + embedder + collection binding)
curl -s http://127.0.0.1:18791/health/deep
# 3. THE test: an old memory retrieves on the new machine
#    (in Claude Code: mcp__mem0__memory_search with a query only your history can answer)
# 4. The promoted artifacts exist in production paths (not only as *-restore copies):
ls ~/.mem0/history.db ~/.mem0/MEMORY.md ~/.mem0/audit-flags.baseline ~/.mem0/tier-ledger-pre-migration.jsonl
```

> **Vector compatibility:** the restored memory vectors were embedded with EmbeddingGemma-300m and stay valid as long as the new machine serves the same embedder (which `llama-swap-setup.md` installs) — no re-embedding needed. **Exception — episode embeddings:** `stack-restore` restores only the memory collection. The nightly set also carries a snapshot of the episodes collection (`qcol-episodes-<TS>.snapshot`), but it is not restored automatically, so the `episodes_egemma_768` collection (semantic episode search / the raw-trace fallback) starts empty on the new box and old episodes fall back to keyword (FTS) search until you rebuild it: `~/apps/mem0-server/.venv/bin/python ~/apps/mem0-scripts/episode-embed-backfill.py` (one-time, local, free).

## Phase 4 — full-stack verification

```powershell
& "$env:USERPROFILE\.claude\scripts\Test-MemoryStack.ps1"   # liveness + invariants
```

Then open a real Claude Code session and confirm: the SessionStart banner appears, a relevant prompt gets a `[MEMORY CONTEXT]` block containing an *old* memory, and after ~10 min of use the L1a log shows a successful extraction (`~/.claude/logs/l1a.log`). That is capture + storage + retrieval all proven on the new machine.

## Phase 5 — decommission the old machine's stack

On the old machine, stop the writers so the stores can't diverge:

```powershell
# Windows: the two scheduled tasks
schtasks /Change /TN "ClaudeCode-DreamConsolidator-3am" /DISABLE
schtasks /Change /TN "ClaudeCode-SemanticDedup-430am" /DISABLE
```
```bash
# WSL: the timers + services
systemctl --user disable --now decay-scan.timer stack-backup.timer goals-stale-sweep.timer \
  contradiction-sweep.timer episodic-reconcile.timer l10-audit.timer
# also present on machines that lived through the v0.22 embedder migration (ignore "not found"):
systemctl --user disable --now egemma-rollback-prune.timer 2>/dev/null || true
systemctl --user stop mem0.service
```

Keep the old `~/.mem0/backups` for a few weeks as the rollback anchor, then retire it. If you must use both machines during a transition window, use **one at a time** and treat the newest machine's store as authoritative — sessions on the other machine will capture into a store you'll discard.

## Troubleshooting the move

| Symptom on the new machine | Cause → fix |
|---|---|
| `3-verify.ps1` fails on `:11436` | llama-swap not built/running — [`install/llama-swap-setup.md`](../install/llama-swap-setup.md) |
| Restore refuses with `target collection ... already exists` | the fresh install's server start created the empty collection — verify `points_count:0`, delete it, rerun (exact sequence in Phase 3) |
| Restore lands in `-restore` targets | you omitted the explicit `--target-*` flags (the safe default protects populated stores) — rerun as in Phase 3 |
| `mem0.service` up but canonical promotion 503s | the key chain isn't provisioned — Phase 2 step 1; check the tmpfs key per the DPAPI doc's Recovery section |
| Old memories don't retrieve but health is green | you restored into the alternate collection — check `curl :6333/collections` for `*-restore` names and redo with `--target-collection <the set's memories collection: mem0_eg2_768, or mem0_egemma_768 for a set from before 1.35.0>` |
| Hooks never fire in Claude Code | restart VS Code after the installer (hooks + MCP load at session start) |

---

# Moving to another embedding space

A different embedding model is a different vector space even at the same width, so a model change
re-embeds every point into new collections and re-calibrates every cosine threshold
([embedder profiles](systems/embedder-profiles.md)). The stack does both through a **profile**: the
collections are named by model and the thresholds are calibrated per profile. The old collections are
never written, so they stay the rollback anchor.

**Measure first.** A newer model is not automatically better here: on 2026-10-08 EmbeddingGemma-2 lost
about a quarter of the per-prompt path's MRR on this store under mem0's additive fusion, and still about
0.02 under rank fusion. Run the house evals (`eval/embedder-ab/` in the operator repo) against a shadow
server before moving the memories. The 1.35.0 move to EmbeddingGemma-2 was an operator decision taken
with that cost measured, for media memories (runbook below).

## The wiki (a rebuild)

The wiki index is derived from the vault and rebuilt in seconds, so it moves without a migration:

```bash
# 1. serve the profile's alias on the authority (EmbeddingGemma-2: llama.cpp >= b11452; see
#    install/llama-swap-setup.md). A PC that refreshes the wiki without serving it sends its builds and
#    searches to the brain; serving it there too keeps them local.
# 2. choose the wiki's space
echo 'MEM0_WIKI_EMBED_PROFILE=egemma2' >> ~/.mem0/stack.env
# 3. rebuild into the new collection (the old one is kept; it is 4 MB)
~/apps/mem0-server/.venv/bin/python ~/apps/mem0-scripts/wiki-index-build.py
```

Re-calibrate any consumer-side wiki score cut-off for the new space.

## The memories, entities and episodes (a migration)

```bash
P=~/apps/mem0-server/.venv/bin/python; T=~/apps/mem0-scripts/embedder-migrate.py
# 1. serve the target profile's alias on llama-swap, in the group that never evicts the memory stack
#    (and add it to the harness's memory_stack)
# 2. build the target collections beside the live ones while mem0 runs (reads the source, never writes it)
$P $T --to egemma2 --dry-run
$P $T --to egemma2
$P $T --to egemma2 --verify          # counts match; a sampled re-embed reproduces every stored vector
# 3. switch with WRITES STOPPED: a write accepted after the last catch-up would land only in the old
#    collection. The PCs queue writes in their outboxes while mem0 is down and replay them after.
systemctl --user stop mem0
$P $T --to egemma2 --catch-up --dry-run    # read to_delete / to_delete_ids before anything is deleted
$P $T --to egemma2 --catch-up              # refuses more than --max-delete deletions (default 200)
install/linux-authority.sh --embed-profile egemma2   # records the profile, re-renders the unit, starts mem0
curl -s http://<authority>:18791/health/deep | jq '.collection, .embed_profile'
```

Do not edit `MEM0_EMBED_PROFILE` in stack.env by hand before the installer: the installer compares the
recorded profile with the new one to decide whether a store is being rebound, and refuses a rebind to a
space with no points. Re-anchor the retrieval-drift canaries whose stored text names the old embedder or
collection.

### The 1.35.0 move to EmbeddingGemma-2 (multimodal)

On top of the steps above:

1. **Serve it with its projector.** `install/1-wsl-services.sh` stages `embeddinggemma-2-Q8_0.gguf` and
   `mmproj-embeddinggemma-2-Q8_0.gguf` (`MEM0_STAGE_EG2=1` ahead of the move) and prints the llama-swap
   entry: `--mmproj`, `--ctx-size 4096 --batch-size 4096 --ubatch-size 2048`, `--flash-attn on`, in the
   group that never evicts the memory stack, llama.cpp b11452 or later. Budget about 1.5 GiB on the
   card while it is loaded. Check one image embeds before migrating: `/health/deep` reports
   `embed_profile.media`, and a media add answers `media_embedded: true`.
2. **Migrate, catch up, switch** exactly as above (`--to egemma2`). There are no media memories before
   the switch, so nothing needs re-embedding with media.
3. **Rebuild the wiki** into `wiki_pages_eg2_768` (if it is not there already) and set any consumer's
   wiki score cut-off for EmbeddingGemma-2 (about 0.62).
4. **Replicas and PCs** restore the new sets only while they serve `embeddinggemma2` locally
   (`restore-replica.sh` refuses a set in a space the box does not serve): add the entry there, then
   `install/linux-replica.sh --embed-profile egemma2`. A set from before the switch keeps restoring into
   the legacy space.
5. **Retire EmbeddingGemma-300m** after the new space has served real traffic and at least one backup
   set holds it: `egemma-rollback-prune.sh` for the old collections, then remove the
   `embeddinggemma`/`embeddinggemma-ams` llama-swap entries this stack added and their GGUF copies
   (other consumers of the stock `embeddinggemma` alias keep theirs).

**Rollback** (same shape, reverse direction): stop mem0, `embedder-migrate.py --from <new> --to <old>
--catch-up --dry-run`, then without `--dry-run` (it brings the old collections up to date with the writes
made since the switch), then `install/linux-authority.sh --embed-profile <old>`. The tool refuses to write
any collection the stack is using (the server's bound collections, or the active profile's while mem0 is
stopped), so a catch-up run one step early or in the wrong direction stops instead of mirroring the live
space. **Prune** the old space only after the new one has served real traffic for a while:
`scripts/wsl/egemma-rollback-prune.sh` deletes the previous space's collections only while the server
reports the new profile and collection, and never the live wiki collection; it is never armed by an
installer.
