# Changelog

This repo is the PRIMARY source for the agentic-memory-stack product; this file is the
product's version authority as of v1.17.0 (the earlier private-side history is summarized
in the first entries below — full pre-inversion history lives in the maintainer archive).

## 1.34.0 — rank fusion for every search, capture liveness, urgent chain pages and maintenance observability (2026-10-08)

### Changed
- **Every memory search ranks by rank fusion** (`mem0-server/fusion.py`, [docs/systems/fusion.md](docs/systems/fusion.md),
  Proposed ADR `hybrid-fusion-rank.md`; audit EVAL-01). mem0 added the raw cosine, a sigmoid BM25 score and an
  entity boost, so on candidates whose cosines sit within 0.1-0.2 of each other the keyword and entity terms
  outvoted the meaning of the query, and durable freshness (0.77-0.90 for evidence-tier memories) then
  outvoted relevance too. The server now replaces mem0's `score_and_rank` (the module global its search calls)
  with weighted reciprocal rank fusion over the dense pool (k 2, keyword 0.4, entity 0.25), keeping the
  raw-cosine gate and the candidate pool. Measured on a restored copy of the store through the authority's
  mem0 2.0.4, EmbeddingGemma-300m, freshness on: paraphrased questions 0.758 -> 0.972 MRR (EN), identifier
  questions 0.490 -> 0.652, what the per-prompt hook injects 0.622 -> 0.838; a tie with dense search on
  paraphrases and +0.04-0.08 on identifiers. A lab server built from this change returned exactly the
  replayed top 10 for all 1,080 queries.
- Search results' `score` is now the fused score: in (0, 1], not a cosine. `threshold` still compares the
  raw cosine, which each result now carries as `cosine`; the admission gate's optional brand-coherence
  floor (off everywhere) reads `cosine` instead of `score`.
- The reranker's skip cut (`rerank_skip`) is 1.0 in every space: the head is ranked first by every leg
  (0.0-0.1% of the lab's searches, as inert as the old 0.92 / 0.89 on mem0's scale).
- `POST /v1/memories/diagnose` reports the target's raw `cosine` beside its fused `score`, and its threshold
  verdict compares the cosine (it compared the fused score with a cosine threshold).

### Added
- `GET /health/deep` `checks.fusion` (`{ok, mode, bound, callers_bound, searches: {reached, bypassed}}`):
  `ok` is false when mem0's search no longer loads the module global, when mem0's `score_and_rank` takes a
  parameter the fusion does not, or when a search returned results without reaching the fusion. That fails
  the native authority installer's new post-condition, `deploy.sh`'s gate and Test-MemoryStack's L2 row
  (which now names it) instead of the stack silently ranking the old way. `MEM0_FUSION=mem0` restores
  mem0's own formula (the rollback, and how to run a mem0 the fusion cannot bind to), set in a
  `mem0.service` drop-in of your own: the unit does not read `stack.env`
  ([docs/systems/fusion.md](docs/systems/fusion.md)). A search with `explain` gains a `fusion` trace stage.
- Capability manifest row `search-fusion` (`checks.fusion`): `dead` when unbound or bypassed, `degraded` in the
  deliberate mem0 mode.
- **`capture` on `GET /health/maintenance`** (audit CRIT-01): is the PC-side L1a extractor still finishing
  runs? Read per request from `episodic.db` (index reads over a read-only connection): `activity_at` (the newest
  episode touched, which every prompt does) and `success_at` (the newest complete episode: L1a's finished runs).
  `state` is `ok` (a run within 48 h), `stalled` (no run for more than 96 h while sessions were active within
  48 h and have been going for at least an hour, so the first prompt after a trip does not convict), `quiet` (any
  other silence: PCs off, or inside the grace window) or `unknown` (no run on record, or the store could not be
  read). It never turns `ok` false; the health stamp and the morning summary name a stalled capture, and an
  external monitor can read `capture.stalled`.
- **`critical_failed_steps` on `GET /health/maintenance`** (audit CRIT-02): `failed_steps` minus the steps whose
  failure is not actionable at night because they depend on a PC being on (today `wiki-index`). An external
  monitor can page urgently on it, through quiet hours, while `failed_steps` keeps paging normally.

### Fixed
- **Boot-guard and weekly no-op receipts no longer overwrite a step's real run** on `/health/maintenance`
  (audit CDD-02): after a reboot every step read "ran at boot in 0 ms". `steps.<name>` headline fields and
  `last_success` come from the latest real run; the latest no-op rides in `last_noop`. A guard no-op no longer
  keeps a failing daily step out of `stale_steps`, nor one whose real runs a day-long reboot loop pushed out of
  the 2,000-receipt window.
- **The drift alarm names its canary** (audit F-03): `drift` gains `missing` and `below_hwm_nights`, and the
  dream heartbeat (Python and Windows) says which canary stands the alarm and for how many nights.
- **The morning summary's health line is tonight's** (audit CM-01): `health-stamp` now runs before
  `morning-summary`, which quotes it (`ams-step-*.service` `After=` order); a failure in `morning-summary` or
  `rtcwake` now reaches `/health/maintenance` the next night.
- **A refused autopromotion is visible** (audit WG-01): the dream receipt counts `nominated`,
  `structural_rejected`, `promoted`, `promote_failed`, `gate_blocked`; a refused promotion reads
  `degraded:autopromote-failed-<n>`, the failure log keeps the cause, and the dream's nominee filter rejects
  what the server's imperative canary would refuse (Python and the Windows twin, whose multi-nominee case
  rejected nothing under PowerShell 5.1), so a canary-bound memory is never gated or sent again.
- **Goal links are unique per (episode, link type, goal)** (audit DC-03): linking is ensure-exists (a repeat
  returns the existing id, through `add_link` too); the first start logs and removes the duplicates the nightly
  promoter wrote (404 on the authority on 2026-10-08, keeping each first link's time) and adds the unique index
  `uq_episode_links_goal`; a goal merge moves each link once and reports the ones the target already had as
  `dropped_duplicates` (response and ledger). Goals those duplicates kept looking fresh can read stale on the next
  Sunday's sweep. To roll back past 1.34.0, drop the index first
  (`sqlite3 ~/.mem0/episodic.db 'DROP INDEX IF EXISTS uq_episode_links_goal'`): an older build inserts plainly.
- **A short memory id is a 404, not a 500**, on `GET /v1/memories/{id}` and `POST /v1/memories/diagnose` (seven
  such GETs answered 500 on the authority from 2026-09-19 to 10-08), and every UUID spelling reaches Qdrant in its
  canonical form (a padded or `uuid:`-prefixed one answered 400 there).
- **The dream's canonical fetch works on mem0 2.1.0 and says when it is cut**: it sent an empty query, which
  mem0 2.1.0 rejects (HTTP 500, read as "no canonicals", so the canonical-dedup guard ran empty), and the request
  default threshold 0.1 could drop canonicals by their cosine to that query. It now sends a fixed query with
  threshold 0 and asks for the server's cap of 500; a full page reads `degraded:canonical-fetch-truncated`
  (37 canonicals today). Python and PowerShell.
- CI gates `test_autopromote_lib.py` and `test_ams_chain.py`, which it never collected.
- `restore-replica.sh` step 4 gates on the top-level `ok` of `/health/deep`: it grepped for any `"ok": true`,
  which every healthy sub-check also matched, so a red endpoint restored green.

### Measured (no change)
- Revisited the EmbeddingGemma-2 question with the fusion fixed: EmbeddingGemma-2 recovers most of what
  mem0's formula cost it (paraphrase 0.944 EN, was 0.542) but still trails EmbeddingGemma-300m in every cell
  by 0.01-0.04 (four of nine significant). The memories stay on EmbeddingGemma-300m.

## 1.33.0 — embedder profiles: one definition per embedding space, a measured migration path, the wiki in its own space (2026-10-08)

### Added
- **Embedder profiles** (`mem0-server/embedder_profile.py`, [docs/systems/embedder-profiles.md](docs/systems/embedder-profiles.md)).
  A profile names one embedding space: the llama-swap alias, the served context and token budget, the task
  prefixes and a template version, the collections built in it (named by model, never reused across spaces)
  and every cosine threshold calibrated on it. Every embedder, collection name and threshold in the server,
  the chain steps, the installers, backup/restore and the Windows scripts now resolves through it. Shipped:
  `egemma-300m` (EmbeddingGemma-300m, the default and the space every existing store is in) and `egemma2`
  (EmbeddingGemma-2: 768-d, Gemma 4 backbone, needs llama.cpp b11452+). `MEM0_EMBED_PROFILE` selects the
  memories' space; `MEM0_WIKI_EMBED_PROFILE` gives the LLM Wiki index its own. With nothing set, behaviour is
  unchanged: the wiki index records each point's embed recipe from now on, and a point written before that
  counts as EmbeddingGemma-300m's recipe, so an upgrade re-embeds nothing.
- **`scripts/wsl/embedder-migrate.py`**: builds a space's memories, entities and episodes collections beside
  the live ones (ids, payloads and the BM25 sparse vectors copied, only the dense vector re-embedded with the
  shim's own prefix and truncation), `--verify` (counts, and a sampled re-embed must reproduce each stored
  vector), `--catch-up` (edits and deletions since the build; run in reverse it is the rollback), and an
  embed-identity record (`~/.mem0/embed-identity.json`). It never writes a collection the stack is using (the
  server's bound collections, or the active profile's while mem0 is stopped) unless `--force`; a catch-up
  deletes at most `--max-delete` (200) points and `--catch-up --dry-run` lists them first; episodes are
  embedded from `episodic.db`'s full summary (the payload holds only 800 characters). Exercised end to end on
  a restored copy of the store (16,946 memories), UUID and integer ids, and by offline tests.
- `install/linux-authority.sh` / `linux-replica.sh --embed-profile` (recorded in stack.env and the unit; an
  existing store is refused a profile change until the new space is built); `MEM0_WIKI_EMBED_PROFILE` is an
  operator-owned stack.env key carried across re-runs.
- `/health` reports `embed_profile`; `/health/deep` reports `embed_profile` (profile, model, template version,
  collections, the wiki's space) and the embedder probe's model, not only its width.
- `wiki-index.sh build|search` follow the AUTHORITY's wiki space: a replica that does not serve that space's
  model streams the snapshot (`build-here`) or the query (`search-here`) to the brain, so a PC can never
  write the wiki in another space.

### Changed
- The thresholds that compare cosines (context-bundle gate, raw-trace episode floor, NLI pre-filter,
  evidence-sweep floor, autopromote sibling, semantic-dedup tiers, reranker skip) come from the active
  profile. The EmbeddingGemma-300m values are unchanged; EmbeddingGemma-2's were calibrated on a restored
  copy of the store (its cosine scale sits far higher: off-topic top-1 0.61-0.69 vs 0.17-0.29).
- The prefix shim reads its prefixes and its token budget from the profile (EmbeddingGemma-2: a 3,900-token budget, ~4,300 characters of English prose by the shim's conservative estimate;
  at ctx 4096, twice the old window at the old VRAM).
- `stack-backup.sh` snapshots the active space's collections and, while they exist, the other generation's
  (the rollback anchor); the manifest records the profile, model, template version and which collection each
  file holds. Restores refuse a set from another space than the box serves.
- The rollback prune (`egemma-rollback-prune.sh`) is now a profile-gated prune of the previous space; it never
  deletes a collection the server reports in use (the live wiki may sit in the "old" space) and is never armed
  by an installer.
- `semantic-dedup.py` deletes only when the collection it scanned is the one the server is bound to (it deletes
  through the server); otherwise it skips with `degraded:collection-mismatch`. A dry run still plans.
- `wiki-index.sh`: an authority whose wiki space cannot be read (a timed-out probe) sends the work to the
  brain instead of building in this box's space; a remote search sends its arguments on stdin, not through
  the brain's login shell.
- `install/1-wsl-services.sh` stages the EmbeddingGemma-2 GGUF only when a space uses it (or
  `MEM0_STAGE_EG2=1`). The collection overrides and the three threshold knobs are carried across installer
  re-runs like the other operator keys; the server now reads `MEM0_QDRANT_COLLECTION` / `MEM0_COLLECTION`
  from stack.env too (it used to ignore them), and reports any active threshold knob on `/health/deep`
  (warning at start when the space is not the default).
- The installer refuses to bind an existing store to an empty space whatever stack.env records.

### Fixed
- **Chain steps could embed with the wrong GGUF** (audit F-01): `MEM0_EMBED_MODEL` reached only the server unit,
  so `episode-upkeep`'s backfill fell back to the stock `embeddinggemma` alias, a different conversion than the
  store's file. Every embedder now resolves the alias from the same profile, reading stack.env.
- **Health checks verified only `dim == 768`** (F-02), which cannot tell two 768-d models apart.
- **Hard-coded collection names in the weekly jobs** (WG-05), and `ledger-audit.py`, `ship_log_reclassify.py`
  and `stamp-retired-at.py` still pointed at the dead pre-EmbeddingGemma `memories` collection. Behaviour
  change: `ledger-audit.py`'s orphan scan now runs against the live store (its findings and baseline change),
  and the `--live` paths of the other two now act on the live store where they used to fail with a 404.
- `cp437-repair.py` re-embedded repaired text with a private copy of the prefix and the 2,048-token budget; it
  now takes both from the profile.
- The backup manifest's fixed per-kind keys went to whichever collection sorted first.

### Measured, not migrated
EmbeddingGemma-2 was evaluated on a restored copy of the store before anything moved (240 memories × EN/ES
questions, 45 wiki pages, house probe sets; details in the system doc). On short memory facts it was slightly
worse dense-only (MRR 0.931 vs 0.953) and much worse through the per-prompt hybrid path (0.369 vs 0.504 EN),
so the memories stay on EmbeddingGemma-300m; on whole wiki pages it answered detail questions better
(+0.10 MRR), so the wiki index can move to it (`MEM0_WIKI_EMBED_PROFILE=egemma2`).

## 1.32.6 — the MCP shim and the replay script run on the native authority: the key and the tenant resolve at runtime (2026-10-07)

### Fixed
- **`mem0-mcp-shim.py` and `replay-ops.py` could not start on a native Linux authority.** Both read the API key
  only from `~/.mem0/api-key`, and the shim exited at import when that file was missing. On the native authority the
  key exists only as a systemd encrypted credential, which a unit sees as a file whose path is in `MEM0_API_KEY_FILE`.
  Both now resolve the key as `MEM0_API_KEY_FILE` (when the file reads non-empty), then `~/.mem0/api-key`, then
  the same `FAIL: mem0 API key not found` exit as before. The resolver is inlined in each file, because
  `ams_env.py` is not deployed in the Windows and Linux-client layouts, and it adds no plaintext environment
  fallback: a process that wants the credential loads it and names the file, nothing else.
- **A local MCP client on the native authority wrote and searched under a placeholder tenant.** The five shim tools
  (`memory_add`, `memory_search`, `memory_recall`, `memory_list`, `memory_diagnose`) and the `add` replay in
  `replay-ops.py` default `user_id` to an operator placeholder that the Windows installer and
  `install/linux-client.sh` substitute at deploy time. `install/linux-authority.sh` copies the scripts raw, so on
  the authority the placeholder reached the server as a literal tenant (the failure class of the unresolved-sentinel
  entries further down). A `user_id` with the placeholder shape (double underscore, upper case, double
  underscore) is now resolved at call time: `MEM0_DEFAULT_USER_ID`, then `MEM0_WSL_USER` in `~/.mem0/stack.env`,
  else left as given. An explicit tenant is never touched, and the signature defaults are unchanged, so the
  installers' substitution and their unresolved-sentinel check behave exactly as before.
- **The plugin manifests carried 1.28.5.** `.claude-plugin/plugin.json` and `marketplace.json` are back in step
  with `VERSION`.

## 1.32.5 — a server-side job label needs the authority's service key, and an insight leaves insight only by a signed demote (2026-10-01)

### Security
- **Any holder of the ordinary API key could claim a server-side job's label.** The server trusted free-text labels
  in a request body or query string: the sweep's `contradiction-sweep-v019` and the backfill's `stamp-retired-v013`
  (PATCH /metadata, skipping the canonical and insight HMAC gate), the legacy `backfill-apply-v013`, `decay-scan`
  and `system`, and the dream's `dream-consolidator` family (insight PUT/DELETE/PATCH without a token, PATCH /tier
  into insight, and POST /v1/memories tier=insight through `metadata.source`). Every PC and every MCP session
  holds that key, so any of them could hide a non-canonical record (`contradicts_canonical`, `retrievable=false`),
  schedule its deletion (`expires_at`), mint, rewrite or delete a high-trust insight, or stamp `retired_at` on a
  canonical. 1.32.4 closed the canonical hide keys and named this as the open follow-up. Now such a label counts
  only when the request also carries the authority-only **service key** in `X-AMS-Service-Key`; without it the
  server answers `403 service-credential-required` and names the label. The check runs in every write handler
  (POST add, PUT, DELETE, PATCH /tier, PATCH /metadata) before the policy, and the policy functions themselves
  (`assert_writable`, `validate_insight_actor`, `authorize_metadata_patch`) now take `service_verified` and honour a
  label only when told it was proven, so a handler that forgets the check is refused rather than trusted.
- **An insight could be moved out of insight with no token**, after which PUT and DELETE were ungated: the insight
  gate fell to two plain requests, the hole session 12 closed for canonical. A move out of insight now signs
  `demote` like a move out of canonical (`mem0-canonize.sh --action demote`), no job label exempts it (nothing in
  the stack demotes an insight), and PATCH /tier re-reads the tier under the record lock: a record that became
  canonical or insight while an unsigned change was in flight is a 409.
- **The codex judge no longer inherits the credential pointers.** It reads memory text any API-key holder can
  write; `codex_shim_client.judge_env()` drops `CREDENTIALS_DIRECTORY`, `MEM0_API_KEY_FILE`, `MEM0_KEY` and
  `MEM0_API_KEY` from its environment. That removes the pointer, not the files: the sandbox is the boundary.
- Boundary, stated plainly: the service key separates the authority's own jobs from every caller that holds only
  the shared API key (MCP sessions, hooks, PCs, replicas). It does not resist a shell on the authority as the
  service user. It is one key for every job. No replica or PC holds it, and it is not the canonical key, so a job
  that holds it still cannot mint a canonical token. Known and unchanged: caller-chosen `source` labels that
  server-side jobs read (the autopromote corroboration fast-track's `user-decision`, semantic-dedup's
  `automemory:` protection) need a different design, because their legitimate senders are unprivileged PC hooks.

### Added
- **`ams-service-key`**, an authority-only, regenerable secret. Native authority: the systemd credential
  `LoadCredentialEncrypted=ams-service-key:<secrets-dir>/ams-service-key.cred` on mem0.service, the dream and the
  sweep steps, and the hand-run wrappers; `install/linux-authority.sh` makes it when it is missing or does not
  decrypt on this host (the old one is set aside, never deleted) and fails the install unless mem0 reports it
  loaded. WSL authority: `~/.mem0/service-key` (mode 600) from `install/1-wsl-services.sh` and `scripts/wsl/deploy.sh`,
  which asserts it after its restart. Replicas never hold it and refuse every job label, by design.
- `/health/deep` `checks.service_key: {present, source}` (informational) and the capability row `service-key`
  (required on the brain: an absent key there is dead).
- `scripts/wsl/ams-service-run.sh <script> [args]`: the operator's hand run with the service key on the authority,
  for `contradiction-sweep.py` (`--unstamp`, `--promote`), `stamp-retired-at.py` and `ship_log_reclassify.py`.
- `ams_env.service_key()` and `ams_env.mem0_headers()`: the dream, the sweep and the backfill scripts build their
  mem0 headers in one place and send the key when they hold it.

### Changed
- The MCP shim downgrades every `memory_add` with tier=insight to evidence (no session can hold the key); before, a
  consolidator source passed straight through.
- MCP `memory_demote`/`memory_promote` (actor `claude-autonomous`, no token) can no longer move an insight in
  either direction: a wrong insight is demoted with the signed `mem0-canonize.sh --action demote` (or a signed delete).
- PowerShell: a failed insight add from the Windows-hosted dream never enters the shared Outbox (`replay-ops.py`
  drains it for every session and never sends the service key): a 403 goes to the poison file, a transient failure
  to the dead-letter file, which re-posts through `Add-Mem0Memory` with the key.
- `ship_log_reclassify.py` reads the key and URL through `ams_env` and refuses `--live` without the service key
  (it would otherwise write each episode and then be refused the retire).
- `app.py` keeps no copy of the insight allowlist; it imports `security_invariants.INSIGHT_ALLOWED_ACTORS`.

### Upgrade notes
- **Authority first, from a release tree of the tag.** `install/linux-authority.sh --dry-run` then the real run: it
  generates `ams-service-key.cred`, renders the units with the new credential line, restarts mem0 and checks the key
  is loaded. Run it after the v1.32.5 release assets exist (its step [4b] fetches the store binary for the tag), or
  pass `--ams-store-binary/--ams-store-sums`. Once mem0.service carries the line, a missing or undecryptable
  `ams-service-key.cred` stops it from starting; re-running the installer regenerates the key.
- **WSL brain:** `scripts/wsl/deploy.sh` makes `~/.mem0/service-key` before its restart and asserts it after; also
  re-run `install/2-windows-config.ps1` so the Windows dream's `memory-common.ps1`/`dream-consolidate.ps1` send it.
- **PCs:** nothing is required for the security change (no PC sends a job label). Re-run the installer to get the
  shim's insight downgrade and the PowerShell Outbox rule; the new MCP behaviour appears after a session restart.
- **Insight adds queued in the shared Outbox before the upgrade** (a Windows-hosted dream's failed add, or an MCP
  session that named a consolidator `source`) replay without the key and land once in `mutation-conflicts.jsonl`
  as `403 service-credential-required`. Drain the Outbox before upgrading, or re-post a genuine one from the
  authority (the nightly dream also re-derives insights from the same evidence).
- **Operators:** hand runs that send a job label (`contradiction-sweep.py --unstamp/--promote`, `stamp-retired-at.py`,
  `ship_log_reclassify.py`) go through `bash ~/apps/mem0-scripts/ams-service-run.sh <script> <args>` on the authority.
- Live suites that seed insights or send job labels need the key in the run's environment (they run on the
  authority only); the headless suite does not.

## 1.32.4 — unfinished sessions read clean and close daily, missed episode vectors come back, and a real supersede door (2026-10-01)

### Fixed
- **Unfinished sessions showed machine text.** An unfinished episode keeps a running summary of the
  prompts typed so far, and every UserPromptSubmit prompt went into it, machine turns included: background
  task notifications and messages relayed from another agent session. The recent-sessions view
  (`GET /v1/episodes`, MCP `episodic_recent`) and episode search returned that text verbatim. A machine
  turn or a relayed message now appends nothing (`hook_contract.is_relayed_agent_message`, an exact port of
  `Test-RelayedAgentMessage`, pinned to the shared corpus with the PowerShell function; `ended_at` still
  moves, so the checkpoint contract and the stale clock hold), and both views scrub such segments from any
  row that is not complete, which also cleans the existing backlog without rewriting it. The running
  summary no longer freezes on the first prompts once it reaches its cap (it keeps the opening ask and the
  newest prompts) and no longer eats leading or trailing pipes.
- **Unfinished sessions crowded the readers that want finished ones.** They carry no goal and always the
  newest `ended_at`, so they filled the windows read by the MEMORY.md index, both dreams, the session-start
  seed and the replica's recent-sessions banner. Those readers now ask for `state=complete`
  (`GET /v1/episodes` takes an optional `state`; an older server ignores it and the old blank-goal skips
  still apply), `episodic_search` results carry `state`, and the Test-MemoryStack staleness row reads the
  new `last_complete_ended_at`.
- **Stale unfinished sessions closed only on Sundays.** The closer added in 1.32.0 (`in_progress` rows idle
  more than 7 days become `abandoned`) ran only in the weekly `episodic-reconcile` step, had no dry run,
  recorded only a count, and sat behind the Qdrant readiness gate although it touches only SQLite. It now
  also runs nightly in the new chain step `episode-upkeep` (`episodic-reconcile.py --upkeep`; 18 steps),
  runs before the Qdrant gate, takes `--dry-run`, guards against a checkpoint racing in, and receipts a
  sample, the oldest `ended_at` and the remaining count. Rows are never deleted or rewritten.
- **A session finalized while the embedder was down never got its search vector** until the weekly
  reconcile or a hand backfill. A cold-shaped embed failure (connection refused, 503, or a 500 "upstream
  command exited prematurely") now schedules a bounded background retry after the response, and
  `episode-upkeep` embeds whatever is still missing every night (diff first: no embedder call when nothing
  is missing).
- **The vector backfill gave up on a cold start.** `episode-embed-backfill.py` retries a cold-shaped
  failure per row inside a run budget, can wait for the embedder first, writes the ams-step outcome line
  and reports the exact number still missing; the Sunday reconcile polls a cold embedder instead of
  skipping its whole backfill on one 503. A context-overflow 500 is still never retried.
- **Facts "superseded" by appended text stayed in default searches.** With no way to retire a stale fact,
  sessions appended `SUPERSEDED <date> by mem0 <id>: ...` to the text; the admission gate reads the
  `superseded_by` field, never the text. New door: `POST /v1/memories/{id}/supersede` (scope `full` hides
  the record outside the `history` class; scope `partial` with a `detail` annotates one stale claim and never
  hides) and `DELETE` to undo, with the MCP tools `memory_supersede` / `memory_unsupersede` (queued offline
  like the other writes; a refusal names its code and is never queued). `PUT` answers a hand-written marker
  with a `supersede_note`. `contradiction-sweep.py --resolve-supersede` goes through the door and dequeues
  its review line; `--unsupersede` undoes; `--supersede-markers` lists every hand-written marker
  (dry run; `--apply` converts only FULL markers with a valid winner, `--apply-partial` annotates partial
  ones). FULL is the narrow case: anything scoped or ambiguous is partial and never hides. The MEMORY.md
  index and the nightly dream also skip superseded records now (partial ones stay). The memory protocol
  snippet gains "Correcting a fact".

### Security
- **Any holder of the ordinary API key could hide any record, canonical included.** The only guard on
  `superseded_by` was the request body's free-text `actor` (`supersession-resolve-v030`), and the
  trusted-actor path in `assert_writable` skips the canonical HMAC check; the sweep's actor string could
  hide a canonical the same way through `contradicts_canonical`. `superseded_by` now has no metadata-PATCH
  writer at all (the supersede door is its only writer and enforces its refusal matrix whoever calls:
  canonical, insight or tier-less records, retired or superseded winners, another user, another brand, or a
  branded winner over a brand-neutral record are refused), and no actor may put a hide key on a canonical
  record. The PATCH key policy moved unchanged into `security_invariants.authorize_metadata_patch` so the
  whole decision is tested headless. Scope, stated plainly: the trusted and legacy actor strings remain
  unauthenticated for non-canonical records (for example the sweep's `contradicts_canonical` on an insight);
  closing that needs a credential for server-side actors.
- **A supersession link could reach a canonical record through a cascade delete.** Because any API-key
  holder can now create a `superseded_by` link, `DELETE ...?cascade=true` never deletes a canonical, insight,
  tier-less or unreadable chain member through one (it returns them as `cascade_skipped_protected`), and
  `PATCH /tier` refuses to move a superseded record into canonical or insight (`409 superseded-record`). A
  plain delete names the records it leaves superseded by a missing winner (`orphaned_supersessions`).
- **PyJWT >= 2.15.0** (CVE-2026-101918, RecursionError in PyJWKClient; pulled in by fastmcp through mcp).
  The floor is on both server pip lines, asserted by the installer's post-condition, and on the Linux
  thin-client venv; a raw-line test keeps every floor single-quoted (an unquoted `>=` is a shell redirect
  that installs an unpinned package).

### Upgrade notes
- **Fleet-wide, and it needs a package index.** Server code, a chain unit and a dependency floor change:
  deploy the authority with `install/linux-authority.sh` from a release tree of the tag (it raises PyJWT,
  installs and enables the new `ams-step-episode-upkeep` unit, and restarts mem0; snapshot `pip freeze`
  first). On each PC run the full `install.ps1` (phase 1 raises PyJWT in the WSL venv the MCP shim runs on;
  phase 2 deploys the shim, the banner and the hooks); `deploy.sh` alone does not run pip. Re-run
  `install/linux-client.sh` on a Linux thin client. Check `pip show pyjwt` afterwards: 3-verify does not.
- **The new MCP tools appear after a Claude Code session restart** (the shim is loaded at session start).
- **The first `episode-upkeep` night closes the whole stale backlog at once** (every `in_progress` row idle
  more than 7 days). To see it first: `episodic-reconcile.py --upkeep --dry-run` on the authority.
- **Hand-written markers already in the store stay until converted.** Run
  `contradiction-sweep.py --supersede-markers` (dry run; read `~/.mem0/supersede-markers.json`), then
  `--apply --only <ids>` for the FULL rows you accept; `--apply-partial` annotates partial ones.
- **The memory protocol snippet in an existing CLAUDE.md is not rewritten by the installer**; the MCP tool
  docstrings carry the same rule.

## 1.32.3 — a message from another agent session is never an operator correction (2026-09-30)

### Fixed
- **Messages relayed from another agent session were captured and posted as operator corrections.**
  1.32.2 stopped task notifications, but its first drain still posted 14 peer-session messages: the
  harness delivers a message from another agent session as the user turn (opening with the
  `<cross-session-message` wrapper, or with its "Another Claude session sent a message:" line), and a
  peer message that quotes "revert that" or "that's wrong" matched the correction patterns. The new
  `Test-RelayedAgentMessage` (the wrapper at the start, or after the announcement line; the real tag,
  followed by whitespace or `>`) makes `Test-CorrectionLikePrompt` return false, and
  `learn-rules-drain.ps1` drops such a queued line like a machine turn. The C10 machine-turn rule is
  unchanged: it keeps a peer message human-shaped for the memory block.

### Upgrade notes
- **PC-only.** The capture and the drain run on each PC: re-run the installer there (on Windows,
  `install/2-windows-config.ps1`, then the WSL `deploy.sh` and `3-verify.ps1`). The authority does not
  run them and needs no redeploy; it can take this release at its next installer run.
- **Peer messages already posted stay in the store** until removed: `source=learn-rules`, text
  starting with the wrapper or the announcement line.

## 1.32.2 — a task notification is never an operator correction, and the brand backfill proposes only what the resolver would route (2026-09-30)

### Fixed
- **Background task notifications were captured and posted as operator corrections.** The correction
  capture ran `Test-CorrectionLikePrompt` on every UserPromptSubmit prompt, machine turns included, and
  a task notification whose tool output says "revert that" or "you forgot" matched its patterns. The
  first 1.32 drain posted 100 queued "corrections"; 66 were task-notification blobs, stored as branded
  evidence memories. `Test-CorrectionLikePrompt` now returns false for a machine turn (the C10 rule,
  `Test-MachineTurnPrompt`), so neither the daemon path nor the inline path queues one, and
  `learn-rules-drain.ps1` stamps a queued correction whose text is a machine turn `dropped` instead of
  posting it. Dropping needs no network, so it no longer waits for the per-run budget. The drain's copy
  of the rule is pinned to the shared machine-turn corpus.
- **`brand-backfill.py` proposed business labels the C3 resolver would never give.** `propose()` ran the
  content rules on every record whose path did not route, and read the workspace OR the project, so a
  project rule never ran when a workspace was set. The contract (`brand_routing`) runs the content rules
  only in a content-rule workspace; a non-routing path elsewhere gets no brand. The first live dry run
  proposed content labels for records from unrelated workspaces and missed path rules carried by the
  project. `propose()` now tries a path rule on the workspace, then on the project; runs the content
  rules only for a content-rule workspace or a record with no path at all; and otherwise proposes
  nothing. `brand_routing` gains `in_content_rule_workspace()`, which `resolve()` now uses (same
  semantics; the shared corpus passes unchanged).
- **The printed signing command did not work on a native authority.** For canonical and insight rows,
  `--apply` printed `mem0-canonize.sh --action patch_metadata ...`, which finds no key from a shell on a
  native authority. It now prints `ams-canonize.sh` there (`MEM0_HOST_KIND=native`).

### Upgrade notes
- **Deploy the authority, then re-run the installer on every PC.** The backfill and the resolver helper
  run on the authority; the capture and the drain run on each PC, so a PC keeps queueing task
  notifications until its own installer re-runs (on Windows, `install/2-windows-config.ps1`). Lines a PC
  already queued are dropped by its next drain once it runs the new script.
- **Notifications already posted stay in the store** until someone removes them: they carry
  `source=learn-rules` and text starting `<task-notification>`, so they are easy to find and delete by id.
- **Review content rows closely.** A content rule names one business, so a fact about several
  businesses' accounts or about shared infrastructure can match exactly one of them, and a wrong label
  hides that fact from every other workspace. Leaving such a row out keeps the record visible everywhere.

## 1.32.1 — a failing write path turns health red, and a junction no longer captures a session twice (2026-09-30)

### Fixed
- **`/health/maintenance` answered `ok: true` while every memory write failed.** When the embedder could
  not start (a co-resident GPU process held the card), `POST /v1/memories` answered 500 or 503 for
  hours, and the banner and uptime checks stayed green. The endpoints that touch the embedder load the
  model, and polling them would keep it resident against the five-minute idle unload, so the new signal
  is passive. `mem0-server/write_path.py` records the final status of every `POST /v1/memories` and
  `PUT /v1/memories/{id}` through an HTTP middleware that sees the response after the exception handlers
  ran: a 2xx is a success, a 5xx or an unhandled exception is a failure, a 4xx is not recorded.
  `/health/maintenance` gains `write_path` = `{ok, last_ok_at, last_error_at, last_error, errors_1h,
  writes_1h}` and folds `write_path.ok` into `ok`. `ok` turns false on a failure and clears only on the
  next successful write that reached the embedder. A content-hash duplicate answer and an `infer: false`
  add that stored nothing are neutral (they neither count nor clear), because writers re-post whole
  transcripts and those answers would flip the signal back to green inside the outage. There is no I/O,
  no model load and nothing on disk, so a restart forgets the state. The response is never changed; the
  middleware adds about 0.4 ms per request.
- **The SessionStart NOT OK line, the nightly health stamp and the morning summary now name a failing
  write path.** `claude-config/storage-cap-check.sh` prints `write path failing (<last_error> since
  <last_error_at>)`, for example `(503 upstream since ...)`. `scripts/wsl/ams-health-stamp.sh` and `ams-morning-summary.sh` end the health line
  with ` write-path <last_error>`, and the stamp exits 2 on it, like a failed step. A healthy, missing or
  unreadable write path changes no byte of either line.
- **A transcript reachable through a directory junction under `~/.claude/projects` could be captured
  twice.** `scripts/windows/sessionstart-capture.ps1` globs `projects\*\*.jsonl`, which lists every
  transcript behind a junction a second time at the same mtime. The tie was unordered and the watermark
  was `<full path>|<ticks>`, so each flip of the winning path spawned the extractor again, and when the
  alias won, the alias path reached the session row's workspace label and brand. The pick now orders by
  mtime, then a file under a real directory before one behind a reparse-point parent (read once per
  directory), then path. The watermark is `<file name>|<ticks>` (the file name is the session id), and a
  watermark written by an earlier release is still honoured by its tail after a separator, so the upgrade
  costs no extra capture. A transcript that exists only behind a junction is still captured.
- **`install/3-verify.ps1` told a replica that its first nightly dream fires at 3:00 AM.** A replica
  never dreams; on any role but `brain` the next steps now say the nightly dream and dedup run on the
  memory authority.
- **`install/linux-authority.sh` rendered the nft belt's system unit into the user unit dir.** Its
  `systemd/ams-*` glob picked up `ams-nft.service`, the root oneshot it also installs into
  `/etc/systemd/system`, so every install left a disabled copy under `~/.config/systemd/user` that could
  never load a firewall table. The unit set now leaves it out, and a real install removes the copy an
  earlier install left there.

### Upgrade notes
- **Deploy the authority, then re-run the installer on every replica PC.** The write-path signal exists
  only after the authority's server restarts on the new `app.py`, `maintenance_health.py` and
  `write_path.py` (`install/linux-authority.sh`, or `deploy.sh` on a WSL authority, which also ships the
  two nightly scripts). The capture fix, the banner text and the verify text reach a PC when its own
  installer re-runs (on Windows, `install/2-windows-config.ps1`).
- **Uptime checks that read `/health/maintenance`** go red on a failing write path, which is the point.
  The field is absent before the upgrade, so add a condition on `write_path.ok` only after the authority
  reports it. After an outage, `ok` stays false until the next write that stores something; a hand check
  of `/health/embedder` shows the embedder itself (never poll it: it loads the model). A 5xx from any
  cause on a write route counts, a server-side bug included.
- **The capture watermark format changes.** An older release reading a new-format watermark spawns the
  extractor once more, and the extractor's throttle or its transcript cursor drops the repeat. Sessions
  already stored under an alias path keep their label; no data is migrated.

## 1.32.0 — a hollow night reads red, a canonical demotion needs the operator's token, and jobs that only looked healthy now do their work (2026-09-30)

### Fixed
- **A chain step that exited 0 was receipted `ok` whatever it had done.** The dream could post 0 of 3
  insights, or a sweep could do nothing, and the night still read green. `scripts/wsl/ams-step.sh` now
  gives each job `$AMS_OUTCOME_FILE`, where it may write one line, `<status>[:<reason>] <json counts>`,
  and the receipt carries `status` (`ok`, `degraded` or `failed`) and `work`. Exit 0 with `failed:*` is
  receipted `ok:false`, exit 0 with `degraded:*` is `ok:true` with `status:degraded`, and text that does
  not parse is `degraded` with `outcome-unparsable: ...`. Guard and weekly no-ops stay `ok`, and a job
  that writes no outcome line is unchanged. When a non-ok receipt would have an empty note, the job's
  last stdout line becomes the note, because the journal drops unit-less stdout lines.
  `scripts/wsl/contradiction-sweep.py` writes `degraded:no-op-<reason>` for a `no-op:<reason>` run and
  `ok {canonicals_checked, canonicals_total, pairs, yes}` for a normal one; its exit codes are
  unchanged.
- **`/health/maintenance` stayed green over a failed step, a degraded step or a degraded pool.** It
  folded in only pool capacity and a 48 h staleness rule, so a failed latest run or a DEGRADED pool
  mirror never turned it red. `mem0-server/maintenance_health.py` now adds `failed_steps` and
  `degraded_steps` (each step's latest real run; `weekly:` and `guard:` no-op receipts are not runs, and
  `health-stamp` is excluded), `pool.health` and `pool.health_alarm` (from `zpool list -H -o health`; an
  unreadable pool reads `unknown` and raises no alarm), and `drift` and `wiki` blocks. `ok` now includes
  failed steps, degraded steps and pool health. A `--weekly` step is judged against 8 days and daily
  steps keep the 48 h rule; a weekly step that comes back `degraded` or `failed` stays listed for the
  week, and a later `ok` run clears it. None of this applies until the authority's server restarts with
  the new `maintenance_health.py` and `app.py`.
- **A bad night did not end in a red step.** `scripts/wsl/ams-health-stamp.sh` now prints
  `health ok= failed= degraded= pool <pct>% <health>` as its last line and exits 2 on `failed_steps` or
  `pool.health_alarm`. `scripts/wsl/ams-morning-summary.sh` labels DEGRADED rows with their notes and
  work counts and adds failed, degraded and pool health to its health line. A DEGRADED pool therefore
  turns `ok` false and ends the health-stamp step red every night until the pool is repaired or
  acknowledged with `MEM0_POOL_HEALTH_ACK` (below).
- **A dream that could not post its insights lost them, and the night still read green.**
  `scripts/wsl/dream-consolidate.py` now polls `/health/embedder` for up to 10 minutes before the
  embed-dependent phase, then proceeds. An insight whose POST fails is spooled to
  `~/.mem0/maintenance/dream/insight-spool.jsonl` and replayed first on the next run. The step now writes
  a `degraded` outcome, with counts, when a night falls short; the reasons are `posted-<p>-of-<c>`,
  `replay-failed-<n>`, `spool-backlog-<n>`, `drift-snapshot-failed` and `canonical-fetch-failed`. A dry
  run does not report a standing spool as a backlog.
- **The Codex usage ledger recorded 0 tokens.** `mem0-server/codex_shim_client.py` and
  `scripts/wsl/ams_env.py` now parse usage telemetry (tokens, resolved model and effort) from stdout and
  from stderr. A value the parser cannot find is `null`, never `0`.
- **The SessionStart banner said only `brain NOT OK` for a pool whose capacity it could not read.** On a
  faulted pool `zfs list` fails, so there was no capacity figure and no reason. The banner
  (`claude-config/storage-cap-check.sh`) now names the pool's health (`pool <health>`), and
  `claude-config/tests/test_banner_maintenance_contract.py` builds the payload with the real builder and
  runs the real banner on it, so the field names both sides use stay pinned.
- **Two callers sent no hook contract version.** The dream's canonical search and the SessionStart
  bundle call now send `hook_contract_version`. The retrieval-drift guard's search calls live in a
  separate evaluation repo and still need the same one-line stamp, so `hook_contract.missing` stays a
  floor, not an alarm, until then (`docs/systems/codex-hooks.md`).
- **A replica PC's SessionStart banner reported files that stop changing once the box is a replica.**
  Its storage-cap figures, dream summary and episodic store froze on the day the authority moved, so
  the banner kept showing old numbers as current, and the enrichment query's recency seed read the same
  frozen `episodic.db` and seeded every session with a weeks-old goal. On a replica or client,
  `claude-config/storage-cap-check.sh` now reads the authority's `GET /health/maintenance` (bounded),
  the morning summary and local pool figures print only on the authority, and an unreachable authority
  is stated as that, never backfilled from local files. `claude-config/sessionstart_bundle.py` seeds
  the query from the authority's `GET /v1/episodes?recent=20[&brand=...]` with a 1.5 s bound and never
  falls back to the local copy, so a failed fetch means no seed; a fresh PreCompact marker skips the
  fetch, and the authority's own path is unchanged. On a replica SessionStart can take up to 1.5 s
  longer when the authority is slow (a dead authority is gated earlier by the 1 s probe). Both fixes
  reach a PC when its installer redeploys `claude-config`.
- **The wiki index step went red after three nights with every source PC powered off at 03:00, and a
  skipped night read green (register P6-8).** The authority only ever tried to pull at about 03:00, a
  skip night was receipted `ok` with an empty note (so the index aged unseen until it turned red at
  exactly 72 h), the 72 h limit measured the age of the last pull rather than the freshness of the
  index, and each source's ssh failure reason was discarded. `scripts/wsl/wiki-index-nightly.sh` now
  measures freshness as the age of `max(last-pull, last-build)`, and every successful build, nightly or
  session-side (`scripts/wsl/wiki-index.sh`), writes `~/wiki-index/last-build`. The 72 h failure
  measures that freshness. A skip night writes a `degraded` outcome only when the index is older than
  24 h (`ok` otherwise), with each source's ssh exit status and stderr in the outcome JSON.
- **An embedder that could not start dropped memory writes instead of queueing them (register P6-7).**
  On 2026-09-24 the embedder could not start beside a large resident model on the authority's shared
  GPU for about 80 minutes, which left 167 embed 500s and 39 memory-write 500s that were not queued,
  and a hollow nightly. `retry_later` in `mem0-server/embedder_503.py` now answers 503 with Retry-After
  for a refused or timed-out connection, a 502, 503 or 504, and llama-swap's
  `500 upstream command exited prematurely` (the model could not start), so the shim queues the write to
  the outbox. Every other 500, such as a context overflow or a coding error, stays a 500.
- **A cold reranker load outlived the client timeout, and the search silently fell back to dense
  order.** The first attempt times out at 8 s and a cold load takes longer. `mem0-server/reranker.py` now
  retries a first-attempt `ReadTimeout` once, inside a whole-stage budget of 20 s that a test pins
  against every caller's timeout (the MCP shim's read timeout is 30 s). `rerank_status` reports
  `ok-after-cold-retry` when the retry worked, and `warm()` does a one-document rerank.
  `/health/embedder` now reads llama-swap v256 and later (`status` is an object) as well as the flat
  schema, `?warm=rerank` warms the reranker, and the SessionStart pre-warm calls it.
  `docs/systems/reranker.md` is rewritten to the behavior that ships, and `install/llama-swap-setup.md`
  has a corrected `ttl` paragraph.
- **Re-running the Linux client or replica installer without `--ams-hub` dropped the fleet store and
  blanked the recorded hub (register P6-13).** `install/linux-client.sh` and `install/linux-replica.sh`
  now inherit `--ams-hub` from `~/.mem0/client-receipt.json` when the flag is omitted; `--ams-hub ""`
  clears it, and with no hub given or recorded the fleet store is skipped with a `WARN`. A receipt whose
  recorded hub cannot be read back fails the run instead of being rewritten without it. The replica
  forwards the flag only when it was given, so the client does the inheriting. Nothing to do on deploy
  unless a box should leave the fleet store: pass `--ams-hub ""` once.
- **Every installer re-run deleted the operator's promotion-gate mode and brand switches.** Each
  installer that writes `~/.mem0/stack.env` rewrites the whole file, and the operator-owned keys it
  carries over (`STACK_ENV_OPERATOR_KEYS`, added in 1.31.3) did not include them, so a dream promotion
  gate set to `enforce` silently went back to the code default, `shadow`. `MEM0_PROMOTION_GATE_MODE`,
  `MEM0_SHARED_BRANDS`, `MEM0_BRAND_MAP` and `MEM0_NLI_GATE_ENABLED` are now carried
  (`install/stack-env.sh`; `stack_env_carry` takes a skip list for a key that also has a flag).
  `install/linux-authority.sh` gains `--promotion-gate-mode shadow|enforce`: omitted, a recorded value
  is carried; an explicit empty value drops the line; anything else is refused before any change.
  Nothing is written on a first install, because the operator picks `enforce` after calibrating. If an
  earlier re-run already deleted the line, set it once with the flag.
- **A deployed tree could not say which commit it runs.** The backup manifest's `git_sha` read `unknown`
  on every set, because the deployed tree has no `.git`. The new `install/deploy-stamp.sh` writes the
  release sha (or `unknown`) to a `DEPLOYED_SHA` file beside the app's `VERSION` and the deployed
  scripts, and the authority and replica installers call it.
- **Test runs wrote into the production authority (register P6-10).** The 2026-09-29 audit counted about
  80 canonical test points (the cleanup signed the nonce-less format the server no longer accepts and
  swallowed the refusal), about 80 test points under the operator's own tenant, 42 permanent
  `3-verify` smoke points and about 900 test sessions in the episodic DB. Tests that redirected only
  `HOME` also wrote into the real profile, because on Windows Python resolves `~` from `USERPROFILE`.
  `mem0-server/tests/conftest.py` now has a session-scoped guard: the live suites refuse a non-loopback
  `MEM0_URL` unless `AMS_ALLOW_LIVE_PROD_TESTS=1`, refuse the stack's own tenant, and use `test-*`
  tenants. A shared fixture redirects `HOME`, `USERPROFILE` and `Path.home()` together across the test
  tree, and canonical and insight cleanup signs format 2 with a nonce through the HMAC path and asserts
  the delete. `install/3-verify.ps1` deletes its smoke point by id and fails the check if it cannot; the
  rules are in `docs/DEVELOPMENT.md`. This release does not remove debris already in a store, which is
  done separately after an operator-reviewed dry run.
- **PreCompact minted a phantom session on every compaction.** It keyed the episode on the name of the
  temporary snapshot (`precompact-snap-<PID>`), not on the session. `scripts/windows/stop-extract.ps1`
  now passes the hook's real `session_id` to the extractor, so no `precompact-snap-*` sessions are
  created.
- **`scripts/upgrade-check.sh` was blind on two legs, and one of them read like a clean result (register
  P6-9).** With no `pip-audit` installed, the security scan printed an empty `REVIEW:` that read like
  "nothing found", and the llama-swap version regex never matched `version: v256`. A scan that did not
  run now says `security scan UNAVAILABLE` and the script exits 2, and the llama-swap version is read
  from `version: v<N>`. `scripts/tests/test_upgrade_check.py` drives both legs with stubbed `pip-audit`
  and `llama-swap` output.
- **One read-only probe of a backup could make the next night's prune delete the real databases.** The
  per-kind prune in `scripts/wsl/stack-backup.sh` counted SQLite `-wal` and `-shm` sidecars as backups.
  `prune_kind <kind> <ext>` now counts only `<kind>-<digits...>.<ext>` for one explicit extension per
  kind and orders by the timestamp in the name, so sidecars, `.tmp` partials and strays never count. An
  empty WAL with its `-shm`, and a `-shm` with no WAL, are swept; a non-empty WAL is left alone. Backup
  databases are opened with `mode=ro&immutable=1`, which leaves no sidecars, and manifests age out with
  their sets (newest 8). Test: `scripts/wsl/tests/test_backup_retention.py` (in the CI headless list).
- **Qdrant's own copy of every nightly snapshot was never deleted, about 150 MB a night.** Once the copy
  in the set is verified (byte size and sha256 equal to Qdrant's `.checksum`, else `cmp`),
  `scripts/wsl/stack-backup.sh` keeps the newest 2 snapshots per collection server-side and deletes
  older ones through the API. Hand-made `qdrant-*.snapshot` one-offs older than 14 days are swept. A
  copy that fails verification is removed and nothing server-side is deleted, so a mismatch fails safe
  and the night reads red. The first night after deploy trims the whole backlog in one go (see Upgrade
  notes).
- **The episodes, entities and wiki-page collections were in no backup set, and a night that lost one
  still read `ok`.** `scripts/wsl/stack-backup.sh` now snapshots every `episodes_*`, `*_entities` and
  `wiki_pages_*` collection into the set as `qcol-<kind>-<TS>.snapshot` (a further one of the same kind
  as `qcol-<kind>+<collection>-<TS>.snapshot`), each kind with its own newest-8 window. The collection
  list is parsed strictly: a response that is not a collection list reads `degraded`, not clean. A
  failed secondary snapshot, or a failed server-side list, DELETE or sweep, does not turn the night red,
  because a red night makes the cloud copy refuse the primary set. It writes a `degraded` outcome to
  `AMS_OUTCOME_FILE`, with the reasons `secondary-snapshot-failed` and `server-prune-failed` and the
  counts `secondary_snapshot_failed` and `server_prune_failed`, and exits 0; the receipt reads
  `status: degraded`, `ok: true`, and `/health/maintenance` turns `ok` false with `stack-backup` under
  `degraded_steps`. The episodes snapshot is in the set but is not restored automatically
  (`docs/MIGRATION.md`).
- **The backup manifest hard-coded its version, could not name a commit, carried no checksums and
  sourced `stack.env`.** `scripts/wsl/stack-backup-manifest.sh` now takes `app_version` from the
  deployed `VERSION` and `git_sha` from the `DEPLOYED_SHA` stamp (a 40-hex sha or `unknown`; a present
  stamp is authoritative, and only a tree with no stamp file asks its checkout). Every file gets
  `checksums` (`size`, `sha256`), further collections of a kind are listed under
  `qdrant_extra_collections`, and `deliberately_excluded` says how each excluded collection is rebuilt.
  `stack.env` is read by key and never sourced; sourcing it is what turned the nights of 2026-09-21 and
  2026-09-22 red (`<user>@<host>: command not found`). `mem0-server/tests/test_stack_env_writers.py` no
  longer lists the manifest writer among the scripts that source it.
- **On a night that failed, the cloud copy re-copied the newest existing set, and it wrote the manifest
  first, verified nothing and never pruned.** `scripts/wsl/ams-pcloud-copy.sh` now refuses with exit 5
  and an outcome line (`failed:stale-set` or `failed:stack-backup-not-ok`, counts `{"age_h":N}`) when
  the newest manifest is 26 h old or older (`AMS_PCLOUD_MAX_AGE_H`) or `stack-backup`'s latest receipt
  is not ok. It copies only real artifacts, data files first and the manifest last, and verifies sizes
  before the manifest travels (exit 6, `failed:copy-size-mismatch`). After a verified copy it keeps the
  newest 7 complete sets (`AMS_PCLOUD_KEEP_SETS`, floor 1) and deletes dead partials older than the
  newest complete set; it never deletes the newest complete set. A degraded `stack-backup` night still
  lets the copy run, and a refusal is a failed step (`pcloud-copy` under `failed_steps`) until the next
  good night. `docs/data-backup.md` now states that the cloud copy is not client-side encrypted and that
  the ZFS replica lags one backup set (an RPO of about 24 h).
- **Index pointers written with a leading marker were invisible to `ams-store`, so lint, derive and the
  size floor never saw the busiest store.** A pointer with an emoji or a `Word:` token after `- ` did
  not parse. `ams-store/internal/index/parse.go`, `line.go` and `render.go` (and the floor, hygiene and
  judge callers that rewrite an entry) now keep one marker token in `Record.Prefix` and re-emit it byte
  for byte through `index.RecordLine`. A bullet with a `.md` link that still does not parse (two marker
  words, a `*` bullet, or a decorated line with a second `.md` link) stays opaque and becomes the
  actionable lint kind `unparsed-pointer`, counted in `counts.unparsed_pointer`. A fenced line is not a
  finding, and canonical lines are unchanged.
- **A changed fact forked into a second record, because the documented update-by-id was stamped and
  never used.** MIGRATE now updates by id (`ams-store/internal/judge/apply.go`, `migrate.go`). A fact
  whose frontmatter carries `migrated: <id>` is read with `Get`, and only a record whose source is this
  slug's own tag is overwritten with `PUT /v1/memories/{id}`, then read back and counted `updated`. Any
  other case (id missing, another source) is a plain Add. A refused update, an unreadable record or a
  read-back mismatch keeps the line and never falls back to Add, and undo never deletes an updated
  record. The server's PUT takes text only, so `meta` is accepted by the client and not sent, and a
  brand tag is not applied on the update path.
- **Scratch and temp workspaces were enrolled as stores, judged and migrated into the corpus.** A slug
  under the encoded OS temp dir, or containing `-AppData-Local-Temp-` or `-scratchpad-`, is now skipped
  by enumeration and by `judge-apply` (`ams-store/internal/store/exclude.go`, `enumerate.go`,
  `cli/judgeapply.go`; the list is `store_exclude` in `<projects root>/.ams/policy.json`).
  `--candidates` prints an empty offer set, and an apply answers `excluded-scratch` with exit 0, takes
  no lock and writes no receipt and no corpus record. A PC still on the old binary keeps the old
  behavior, including pushing a scratch store.
- **A store emptied by migration could not finish its derive.** In
  `ams-store/internal/derive/derive.go`, an emptied store whose every indexed slug is explained by a
  `Migrated:` trailer in history now drops its dangling lines and reports `applied`, where it used to
  abort with `aborted-no-fact-files`. One unexplained slug still aborts.
- **A first sync could push back facts the hub had deleted.** On a first join (no merge base while the
  hub branch exists), every local-only path the hub's history deleted is now quarantined. It is copied
  to `<state root>/quarantine/`, reported `resurrected`, left out of the merged tree and not pushed
  (`ams-store/internal/merge/mergetree.go`, `internal/gitx/plumbing.go`, `sync/sync.go`).
- **Store receipts grew without bound, lint findings never aged out and a queued deletion left no
  outcome.** `sync-receipts.jsonl` and `compact-receipts.jsonl` now rotate at 2 MB and keep 3
  generations (`ams-store/internal/receiptlog`, new); lint, judge and sync read the live file plus the
  newest generation, and consecutive identical `aborted-*` rows for one store collapse into one row with
  `repeat` and `first_ts`. Every queued deletion leaves exactly one outcome in the receipt of the pass
  that resolves it: `deferred_applied`, `deferred_still_queued`, `resurrected` or `deferred_gone`
  (`merge/deferred.go`, `sync/receipts.go`). In `ams-store/internal/lint/history.go`, `rules.go` and
  `summary.go`, `MergeFindings` now takes a 7 day window, drops a resurrected finding whose path is gone
  from the store (kept while a quarantine copy exists) and counts a repeated row once, and `last_status`
  is cleared when the store's newest receipt is older than 7 days.
- **The busiest store never converged, because the over-trigger clock counted bytes only and the
  migration cap was five (register P5-6).** `RecordOverTrigger` now trips on bytes >= 20000 OR lines >=
  160 (`store.OverTrigger`, in `ams-store/internal/store/constants.go` and `cli/seams.go`).
  `scripts/wsl/ams-store-judge-apply.sh` passes `--max-migrations 15` for a store over the compaction
  trigger and 5 for every other store; the decision is recorded in `docs/systems/ams-store.md`.
- **A night of failed corpus writes read green, and the hub checkout was never linted.**
  `judge-apply --json` now reports `offered`, `updated` and `add_failed`, where `add_failed` counts
  every write that failed or failed its read-back. `scripts/wsl/ams-store-judge-apply.sh` sums the
  counts of every `judge-apply --json`, lints the hub checkout after the apply and the closing sync, and
  writes the step outcome with the counts `stores`, `offered`, `migrated`, `add_failed`, `updated`,
  `actionable` and `unparsed_pointer`. The outcome is `degraded:add-failed-<n>` when offered > 0,
  migrated == 0 and add_failed > 0; the run still exits 0. The receipt then reads `status: degraded`,
  and `/health/maintenance` turns `ok` false with `store-judge` under `degraded_steps` until the next
  good night. Test: `mem0-server/tests/test_ams_store_judge_wrapper.py` (in the CI headless list).
- **Almost no fact carried a brand: the audit found brand isolation covering about 3 % of the store.**
  The L1a worker stamped a brand on its episode but never on its facts (12,354 of 12,355 extractor facts
  were brandless). `scripts/windows/l1a-extract.ps1` now resolves the brand before the facts loop and
  routes each fact with its own text; an unrouted path posts no `brand` key. When the worker analyses a
  PreCompact snapshot it routes by the real transcript (`-OriginTranscriptPath`), for facts and episode
  alike, while the cursor stays keyed on the file it reads. `scripts/windows/user-prompt-extract.ps1`
  and `scripts/windows/mem0-hook-daemon.ps1` no longer stamp an unrouted session with a hard-coded brand
  on the user-decision write, and the daemon records the transcript directory as the workspace instead
  of a constant.
- **Dream insights never carried a brand.** In `scripts/wsl/dream-consolidate.py` an insight now takes
  the brand held by more than half of the memories it cites (neutral sources count in the denominator),
  never a shared label, otherwise none. The brand is part of the metadata that is posted and, when the
  POST fails, spooled, so a spooled insight replays the next night with it. A spool line written before
  this release has no brand and replays brandless.
- **24 % of L1a extractions were skipped on the Codex lock with no retry, and a Codex failure kept only
  the first line of its error.** `Acquire-CodexLockWithWait` (`scripts/windows/memory-common.ps1`, used
  by `scripts/windows/l1a-extract.ps1`) now polls every 2 s for up to 20 s (`AMS_L1A_LOCK_WAIT_SECONDS`
  overrides) instead of skipping when the lock is held. A waiter re-checks the throttle and the cursor
  once it holds the lock and exits without calling Codex when another run finished meanwhile.
  `scripts/windows/sessionstart-capture.ps1` drops a same-second duplicate SessionStart with an atomic
  per-session marker. A Codex failure now logs the last three lines of its output (`Get-OutputTail`).
- **The L1a worker's Codex call answered 401 when an API key outranked the ChatGPT login, and a session
  with no extracted facts left no episode.** `Invoke-CodexSubagent`
  (`scripts/windows/memory-common.ps1`) now clears `OPENAI_API_KEY` and `CODEX_API_KEY` from the child
  process only and logs the auth mode, never a credential. The extraction prompt keeps the episode
  whenever the session had substantive turns; before, no facts meant no episode.
- **The nightly semantic dedup compared zero pairs for weeks, and its receipt still read `ok`.** Once
  the collection gained a named sparse vector, every point's vector became a dict, and the job skipped
  anything that was not a bare list. `dense_vector(point)` in `scripts/wsl/ams_env.py` is now the one
  extractor: a bare list as is, a dict gives the unnamed `""` entry, else the first list value, anything
  else `None`, which callers count. `scripts/wsl/semantic-dedup.py` compares the real dense vectors with
  blocked numpy products per (tier, partition) group, counts `scanned`, `skipped_no_vector`,
  `compared_pairs`, `candidates`, `planned`, `deleted` and `delete_failed`, and deletes at most
  `--max-deletions` (default 50) per run, highest cosine first. Canonical, automemory-migrated and
  operator-sourced insight records are still never deleted, and the restore record is written before
  each delete. The step outcome reads `degraded:compared-0` (more than 1,000 scanned, nothing compared),
  `degraded:skipped-no-vector` (over 1 %), `degraded:deletes-refused` or `degraded:deletes-failing` (the
  API refused what the run planned), and the counts ride the receipt's `work`. `numpy` joins
  `mem0-server/requirements.txt`, the installer's fresh-venv pip line and the CI pip line.
- **The health gate read the dedup job as alive from the mtime of a report the job rewrites every run.**
  In `mem0-server/capabilities.py` and `mem0-server/job_liveness.py`, the `dedup-job` capability now
  reads the job's own summary. It is degraded on `compared_pairs == 0` with `scanned > 1000`, on a
  degraded or no-op outcome, or with no summary within 36 h, and dead past 96 h.
- **A contradiction stamp kept hiding records long after its target stopped being canonical.** In
  `mem0-server/admission_gate.py` and `mem0-server/app.py`, a `contradicts_canonical` stamp now hides a
  record only while its target is still tier canonical (one batched retrieve per search, a 10-minute
  cache). A lookup error admits the record and counts `stamp_target_unresolved`, and the diagnose
  endpoint resolves targets the same way. In the audit 98 % of the rejections were of the dead-stamp
  kind, and they stop once the server restarts on the new code.
- **The weekly contradiction sweep re-swept the same first 50 ids and always hid the newer fact.**
  `scripts/wsl/contradiction-sweep.py` now defaults `--user-id` to the corpus tenant and sweeps
  canonicals never-checked first, then longest-unchecked, writing the marker back per canonical, so
  `--limit` is a rotating budget (`canonicals_checked/total`, `weeks_for_full_pass`). A canonical whose
  marker write fails is not counted as checked and degrades the run. A YES whose candidate is newer than
  the canonical goes to the review queue as `canonical-possibly-stale` (no `canonical_id`, so
  `--promote` cannot hide it) instead of being stamped. A canonical with no dense vector is counted (all
  skipped reads `degraded:no-vectors`), and zero canonicals under the defaulted tenant reads
  `degraded:zero-canonicals-defaulted-tenant`.
- **Review-queue lines and stamps that pointed at a demoted canonical could not be cleared.**
  `--dismiss <memory_id>` and an apply-mode prune now drop `canonical-possibly-stale` lines once the
  canonical is gone or demoted. `--then-rejudge-stamped`, added to the `ExecStart` of
  `systemd/ams-step-contradiction-sweep.service`, runs the stamped re-judge after the sweep and now also
  clears stamps whose target was demoted or retired. The sweep writes one outcome line per run, with the
  counts `weeks_for_full_pass`, `marker_written`, `marker_failed`, `skipped_no_vector`,
  `stale_canonical_routed` and `stale_review_pruned` beside `canonicals_checked`, `canonicals_total`,
  `pairs` and `yes`. The chained re-judge adds its counts with a `rejudge_` prefix and can neither
  replace the sweep pass's reason nor turn a degraded sweep `ok`, and a non-zero exit also says why on
  stderr, which is the failed receipt's note.
- **`contradiction-sweep.py --dismiss` did nothing when the Codex judge was unavailable.** With the
  judge unreachable, `--dismiss <memory_id>` answered a no-op with exit 0 and left the queue line,
  although it judges nothing. It now runs without the judge and drops every review-queue line for that
  memory: exit 0 when something was removed, 1 when nothing matched.
- **Only 14 % of episodes had an embedding, and 1,352 checkpoints were never finalized.** In
  `scripts/wsl/episodic-reconcile.py`, `scripts/wsl/episode-embed-backfill.py` and
  `mem0-server/episodic.py`, `in_progress` episodes untouched for 7 days now become `abandoned` (counted
  in the receipt), and up to 500 missing episode embeddings are backfilled per run (newest first,
  skipped when the embedder is down, stopped after 5 consecutive failures). The outcome reads
  `degraded:embedding-coverage-<pct>` below 90 % and still exits 0. The descriptions of
  `systemd/episodic-reconcile.service` and its timer no longer say read-only.
- **The context bundle echoed a session's own open questions back at it.** `/v1/context/bundle`
  (`mem0-server/app.py`, `mem0-server/episodic.py`) now leaves out goals and open questions first seen
  in the requesting `session_id`. It ranks the rest by recency of episode link, then priority.
- **A legitimate note kept the mojibake tripwire permanently degraded.** `mem0-server/mojibake_check.py`
  now skips a point that carries `mojibake_ok: true`, so a note that legitimately looks like mojibake is
  exempted by carrying that flag. Only a point with the flag is skipped; every other record is still
  checked.
- **A plain `install.ps1` re-run turned a replica PC into a brain.** `install.ps1` and
  `install/2-windows-config.ps1` now keep the recorded role when `-Role` is omitted
  (`install/role-lib.ps1`); an explicit `-Role` still wins, and first installs are unchanged. A role
  record that exists but cannot be read (a garbage or empty role file, a receipt with a bad or empty
  `Role`, an unparseable receipt) stops the installer before it writes anything and asks for an
  explicit `-Role`, instead of silently resolving to `brain`. Test:
  `scripts/windows/tests/InstallRole.Tests.ps1` (Windows PowerShell 5.1 and PowerShell 7).
- **A WSL deploy could leave `DEPLOYED_SHA` empty.** `scripts/wsl/deploy.sh` now stamps it through the
  shared contract in `install/deploy-stamp.sh` (a 40-hex sha or `unknown`), where
  `git rev-parse HEAD > DEPLOYED_SHA || true` left an empty file when git failed, and it reads the stamp
  back as a rollback ref only when it is a sha. `install/1-wsl-services.sh` sources the same contract
  and stamps `DEPLOYED_SHA` beside `VERSION` at its fresh-install and refresh sites. Until an installer
  run or `deploy.sh` has stamped the tree, the backup manifest's `git_sha` reads `unknown`.
- **The HOME-redirect guard did not scan every directory CI collects tests from.** It now scans every
  directory CI's pytest step collects from (derived from `.github/workflows/ci.yml`). The one offender
  it finds, `scripts/tests/test_upgrade_check.py`, is fixed.
- **A tool description told every session that the per-prompt hook was dead.** The MCP shim's tool
  descriptions (`scripts/wsl/mem0-mcp-shim.py`) now say the hook is alive: `memory_recall` is an
  explicit, deeper recall in addition to the injection, and `memory_search` is a GPU reranker with a
  cold load after idle. The dead-hook comments elsewhere are gone (comments and docstrings only).

### Security
- **A canonical record could be demoted with the ordinary API key, then edited or deleted with no
  token.** `PATCH /v1/memories/{id}/tier` gated only promotions into `canonical`, and the canonical write
  gate protects a record only while it is still canonical. Any holder of the ordinary API key (every
  session's MCP shim sends one, and `memory_demote` sends `actor=claude-autonomous`) could move a
  canonical record to `evidence` and then `PUT` or `DELETE` it, a two-step bypass of the whole tier that
  was found while preparing corrections to canonical records, not by the audit. A move out of
  `canonical` now needs the operator's HMAC token for the new action word `demote`
  (`<ts>|<nonce>|demote|<mid>|<reason>`) and a reason, and a promote token cannot be replayed as a
  demotion (`security_invariants.tier_change_hmac_action(current, target)` holds the policy). The
  current tier is read before any change and fails closed: a store error returns 503, a record with no
  `tier` field reads as canonical, and a missing record returns 404. Under the per-record write lock,
  which is keyed on the canonical UUID spelling, the handler re-reads the tier and answers 409 when a
  record it saw as non-canonical became canonical in the meantime, or 404 when the record was deleted.
  The write-ahead intent line is appended after that re-check, so a refused change leaves no unpaired
  intent.
- **Moving a record out of `canonical` is now an operator command.**
  `bash mem0-canonize.sh --action demote <id> "<reason>" [--tier evidence|stable|temporal]`
  (`scripts/wsl/mem0-canonize.sh`) signs the stripped reason (the server verifies `reason.strip()`) and
  defaults to `evidence`. Over MCP, `memory_demote` cannot move a canonical record: it returns 400
  without a reason and 403 with one. Any automation that demoted canonicals through the plain API now
  gets 403; none exists in this repo, but
  `scripts/wsl/replay-ops.py` replays a queued demote as an unsigned PATCH, which lands in
  `mutation-conflicts.jsonl` as a non-retryable 403. A record with no `tier` field now needs a signed
  demotion too, and `scripts/wsl/tier-backfill.py` stays the operator's route to unlock one. After the
  server restarts, an unsigned demote should return 403 and a signed one 200. Test:
  `mem0-server/tests/test_canonical_demotion_gate.py` (headless, in the CI list).
- **The shared redaction rules missed several provider token shapes and prose forms (register P6-11).**
  The rule set now covers the key and token shapes of hosting, database, CDN, payment, email and VPN
  providers, webhook-signing secrets, cloud API keys and chat-bot tokens, plus the prose forms
  `API key <tok>` and `<label> (is|value is) <tok>` and login/password pairs. All three copies of the
  rules (`mem0-server/redact.py`, `scripts/windows/memory-common.ps1`,
  `claude-config/precompact_capture.py`) run against the shared `tests/fixtures/redaction-cases.jsonl`,
  positive and negative rows, so a rule added to one copy without the others turns a case red. Nothing
  here changes what the store keeps: the server's `add()` stays count-only and no stored record is
  scrubbed.
- **The nightly audit's credential flag was a six-keyword tripwire that skipped retired points
  (register P6-11).** It flagged 1 of the 11 credential-bearing points found in the store.
  `scripts/wsl/l10-audit.py` now builds `possible-credential` on the shared redaction rules, provider
  prefixes and a high-entropy signal for 32+ character random-looking tokens (a seeded sample flags 94
  to 98% of random 32 to 64 character tokens, so it is a screen, not a guarantee). It scans retired
  points for this flag only, because a retired point is still a readable row in the vector store and in
  every backup, and the `oversize` flag uses the migration cap for `automemory:` sources. The first L10
  run after deploy flags retired credential-bearing points once, which is intended; review them by hand.
- **The authority ran `cryptography` 48.0.1 with three open advisories, held there by the repo's own
  `<49` cap (register P6-9).** GHSA-jwv3-5hgf-82ww and GHSA-m2h6-j472-rp4c are fixed in 49.0.0 and
  GHSA-g6cj-pr64-35w5 in 50.0.0. The installer's pip lines (`install/1-wsl-services.sh`) and
  `mem0-server/requirements.txt` now state floors instead of caps, `cryptography>=50.0.1` and
  `mem0ai[nlp]>=2.0.4`, and agree with each other (they disagreed about both, and `mem0ai` was an exact
  `==2.0.4` pin that had aged two minors). The installer also installs `pip-audit`; its post-condition
  proves the floors, `pip check` and the audit tool and names the remedy when one fails, and CI runs
  it. Re-running the authority installer applies the floors to an existing venv, so treat it as the
  upgrade: snapshot `pip freeze` first, and run the full suite, `/health/deep` and the canaries before
  keeping the result.
- **A credential pasted into an operator correction sat on disk unredacted.** Capture stored the raw
  prompt in the correction queue (`~/.mem0/learn-rules.jsonl`) with no redaction, and nothing consumed
  it. `Add-LearnRuleCapture` (`scripts/windows/user-prompt-lib.ps1`) now runs the text through
  `Redact-Secrets` before the 2000-character cap, and a redactor failure drops the capture rather than
  writing the raw text. The per-prompt hook and the daemon load that lib alone, so it carries a
  comment-stripped copy of `Redact-Secrets` with all 26 rules; `AuthorityResolution.Tests.ps1` pins it
  identical to the `memory-common.ps1` function, and `UserPromptExtract.Tests.ps1` runs it through the
  shared fixture. The drain redacts again what it posts, because older lines predate capture-time
  redaction. Corrections captured before this release stay unredacted in the queue and its `.bak` until
  pruned.

### Added
- **A planned DEGRADED pool can be acknowledged with a dated key.**
  `MEM0_POOL_HEALTH_ACK=<STATE>:<YYYY-MM-DD>` in the authority's `~/.mem0/stack.env` (a process
  environment value of the same name wins) names the pool state you expect and the last day (UTC) you
  expect it, so `/health/maintenance` and the health stamp do not go red for a pool that is degraded on
  purpose. It is read on every call, so adding or removing it needs no restart
  (`mem0-server/maintenance_health.py`, `mem0-server/app.py`). It counts only while the pool is in
  exactly the named state and today is on or before the date; an expired, mismatched or malformed value
  leaves the alarm on, and `pool.health_ack.reason` says why. While it is active the health stamp and
  the morning summary print `DEGRADED (acked until <date>)`, and installer re-runs carry the key
  (`install/stack-env.sh`).
- **A replica PC now refreshes the wiki index itself when it is due.**
  `scripts/windows/memory-maintenance-spawn.ps1` starts a detached
  `scripts/windows/wiki-index-catchup.ps1` at SessionStart when the authority reports the index older
  than 20 h (`wiki.fresh_age_h`) or a vault page is newer than the PC's own refresh stamp, at most once
  per 6 h and with no new scheduled task. An authority that does not report `wiki` yet falls through to
  the page-time rule, and the authority itself never runs the catch-up. The vault directory is operator
  configuration, `WIKI_VAULT` or the first line of `~/.mem0/wiki-vault`; with neither, the catch-up
  does nothing. The installer deploys the operator-neutral driver `claude-config/wiki-index-refresh.sh`
  and never overwrites an operator's own copy (a hand-written one is left as it is, with a NOTICE);
  `docs/systems/wiki-index.md` covers the stamps, the degraded skip nights and the catch-up.
- **A fact migrated into the corpus can now carry the brand of its workspace.**
  `judge-apply --brand-map <path>` (`ams-store/internal/brand`, new; `cli/judgeapply.go`) adds `brand`
  to MIGRATE metadata: the first `rules` match on the workspace slug or, in a content-ruled workspace,
  exactly one distinct brand across the `content_rules` over the fact body; a shared label is returned
  as it is. Path separators (backslash, slash, space) count as one character to the matcher, as in the
  other resolvers, and a missing, empty or malformed map is brand-neutral.
  `scripts/wsl/ams-store-judge-apply.sh` passes `--brand-map` from `MEM0_BRAND_MAP` (environment, else
  `stack.env`) only when it is set; unset means brand-neutral, as before.
- **The Python, PowerShell and Go brand resolvers now read one brand map the same way (contract C3).**
  `scripts/wsl/brand_routing.py` (new) implements it: `rules` (first match on the path or slug),
  `content_rule_workspaces` with `content_rules` (in such a workspace each fact is classified by its own
  text: exactly one distinct brand, else none) and `shared_brands`. Separators in paths match one
  another; a missing map is silently brand-neutral, and malformed JSON is neutral with one stderr
  warning per problem. `Get-BrandFromTranscriptPath` in `scripts/windows/memory-common.ps1` now runs the
  same resolution and returns brand, workspace and project, and `scripts/windows/user-prompt-lib.ps1`
  carries a byte-identical copy (the two files do not dot-source each other on the hook hot path, and a
  Pester test fails if they drift). With no usable rules the long-standing default rule for this stack's
  own workspace still applies. `tests/fixtures/brand-routing-cases.jsonl` (34 cases) is the one contract
  all three resolvers run; `claude-config/brands.example.json` holds neutral examples and
  `docs/systems/brands.md` is new.
- **Existing records can be tagged with a brand from a reviewed report, never automatically.**
  `scripts/wsl/brand-backfill.py` (new) has two modes: `--dry-run --out report.jsonl` writes one row per
  record it would tag and changes nothing, and `--apply --from report.jsonl` applies exactly the
  reviewed rows through `PATCH /v1/memories/<id>/metadata` with actor `brand-backfill`. It refuses a row
  whose record changed since the report (a fingerprint), is already tagged, is gone, or proposes a brand
  the map cannot route. It never patches canonical or insight records; for those it prints the
  `mem0-canonize.sh` HMAC command instead. With no map it exits 3.
- **`/health/deep` now says which mode the promotion gate runs in.** `mem0-server/app.py` and
  `mem0-server/capabilities.py` report `promotion_gate_mode` (environment, then `stack.env`, then
  `shadow`, as the dream resolves it) and `checks.promotion_gate`, and an authority whose gate only
  shadows shows `promotion-gate: degraded`. The value itself stays an installer and operator setting
  (see Promotion-gate mode in the upgrade notes).
- **Operator corrections now reach the authority instead of piling up in a local queue.** The queue
  (`~/.mem0/learn-rules.jsonl`) was a write-only sink: the per-prompt hook appended every
  correction-shaped prompt and nothing read it back. `scripts/windows/learn-rules-drain.ps1` (new)
  drains it under an exclusive lock and a one-hour throttle: it stamps `test-failure` lines `dropped`
  without posting them, and posts each pending `correction` to `/v1/memories` with `infer` off and
  metadata `tier=evidence`, `source=learn-rules`, `kind=correction`, `captured_at`, `session_id` and
  `brand`, then stamps it `drained` with the returned `mem0_id`. A 400, 413 or 422 stamps the line
  `rejected`, so one poison line cannot block the queue; a connect failure, a 401, 403 or 429, or a 5xx
  stops the run and leaves every unposted line `pending`. At most 50 corrections go out per run,
  finished lines are pruned after 30 days (pending lines never), and the brand comes from the C3 map
  over the transcript path and the correction's own text. `scripts/windows/memory-maintenance-spawn.ps1`
  starts the drain at SessionStart on every role (a replica writes corrections too), and
  `install/2-windows-config.ps1` and `scripts/windows/build-hook-client.ps1` ship it.
- **The corrections drain cannot post an accepted correction twice, and it carries forward one typed
  while it runs.** It rewrites the queue with a temp file and `File.Replace`, keeps one `.bak`, and
  re-reads the queue at the commit; the status is patched textually so the original `ts` and escaping
  survive, and a malformed line is preserved byte for byte. If the swap fails after POSTs were accepted
  (capture's append handle is open without delete-share, or an AV/ACL lock), the commit is retried with
  a fresh read each time, every accepted POST is journaled at once to `<queue>.pending-commit`, and the
  next run applies that journal before it posts anything. The run then reports `aborted`
  (`commit-failed`) and leaves the throttle open until a commit succeeds. One residual is accepted:
  capture appends without the lock, so a line appended in the microseconds between the drain's last
  re-read and the swap is missing from the live file (it remains in the `.bak`).
- **A dream can be started on demand on the authority.** `/dream-now` on a replica was a silent no-op
  with no working replacement. `scripts/wsl/ams-dream-now.sh` runs on the authority and starts one dream
  outside the chain guard, as a transient user unit with the dream step's own credentials, environment
  and pre-step, receipted like a chain step; it refuses anywhere but the native authority. It passes
  `--force`, because the dream's own 23 h throttle would otherwise make it the same silent no-op, and it
  exports the API-key path inside the command from `$CREDENTIALS_DIRECTORY`, because
  `systemd-run -p Environment=` does not expand `%d`. Test: `scripts/wsl/tests/test_ams_dream_now.py`.

### Changed
- **The docs and the installer's llama-swap warning no longer recommend resident or CPU-only setups
  (register P6-9).** The warning block and several docs still recommended `ttl: 0`, `always_loaded` and
  `--n-gpu-layers 0`, which the fleet rules forbid (every model unloads at `ttl: 300` and runs on the
  GPU). `install/1-wsl-services.sh` now shows a non-exclusive support group with `ttl: 300` and
  `--n-gpu-layers 999`, the guidance is fixed where it appeared, and the docs gate
  (`scripts/ci/check-docs.py`) refuses those three forms as guidance anywhere outside this changelog.
- **Every sync commit now says which `ams-store` made it.** Every sync commit and merge-engine commit
  carries `Ams-Store-Version: <version>`, and the receipt records `kind: once|watch`
  (`ams-store/internal/sync/history.go`, `merger.go`, `ams-store/internal/merge/engine.go`,
  `cli/root.go`). A watcher pass and a lagging client are therefore attributable on the hub.
- **The admission gate admits a shared brand label like a brand-neutral record.**
  `mem0-server/admission_gate.py` admits a label listed in the brand map's `shared_brands` or in
  `MEM0_SHARED_BRANDS` (process environment first, then `stack.env`, since the server unit does not load
  it), for a brandless search and for another brand's search, as it does a brand-neutral record. The
  gate reads the map file itself (path from `MEM0_BRAND_MAP`, cached on mtime and size), so one
  `shared_brands` list works on the server and on a PC. An unlisted brand stays fail-closed.
- **The brand-scope audit covers every tier and counts untagged brand mentions.**
  `scripts/wsl/brand-scope-audit.py` audits every tier (the earlier audit looked at canonical records
  only) and writes two new metrics to the status file: `untagged_brand_mentions`, by brand plus how many
  match several, and `unroutable_brands`. Its exit code is unchanged.
- **The dream catch-up no longer counts operator corrections as dream debt, and the self-test watches
  the drain instead.** `scripts/windows/dream-catchup.ps1` now counts as debt a queued promotion or a
  gap over 48 h; correction lines no longer count, because the drain owns that queue.
  `scripts/windows/Test-MemoryStack.ps1` gains a RECOVERY row, `corrections drain`, that WARNs when a
  pending correction is older than 48 h, and drops the old debt probe on a state path nothing writes.
  `docs/systems/memory-model.md`, `docs/flows/memory-capture.md`, `docs/systems/brands.md`,
  `docs/systems/dream-skill.md`, `docs/operations.md` and `ARCHITECTURE.md` describe the drain and no
  longer call the queue dream debt or an unimplemented loop.
- **The front-door docs describe the stack that ships.** `ARCHITECTURE.md`, `README.md` and `CLAUDE.md`
  still described a Windows Task Scheduler, loopback-only, CPU-inference stack. They now describe one
  authority (native Linux, mem0 on the authority's private-network address, Qdrant on loopback, embedder
  and reranker on the GPU with a 300 s idle unload), replica PCs and clients whose local stores are
  dormant, and the one 17-step nightly chain with the store judge in it. The chain lists in
  `docs/systems/installer-and-deploy.md` and `docs/operations.md` name all 17 steps and say how many,
  `docs/api-contracts.md` names all five keyless endpoints, and
  `mem0-server/tests/test_docs_match_code.py` holds both lists to the code.
- **`docs/operations.md` gains the manual dream and a disk-loss runbook, and `docs/DEVELOPMENT.md` the
  deploy steps.** The runbook, "The authority's only disk died", needs in its step 2 a plaintext copy of
  the authority's two keys stored off the box, which this repository cannot supply.
  `docs/DEVELOPMENT.md` now gives the three-step replica deploy and the authority deploy from a release
  tree.
- **The tier, consolidator and `stack.env` docs now match the code.** `docs/systems/tier-policy.md` and
  the memory protocol snippet say that `temporal` has no `valid_until` (the one expiry field is
  `expires_at`, stripped at add) and that the consolidator is the nightly dream (`dream-consolidator`,
  `dream-autopromote`). `install/stack-env.sh` and the installer doc say who reads each carried
  `stack.env` key, and that `MEM0_NLI_GATE_ENABLED` is carried but never read from that file (a test
  pins it). The fleet ADR's phase block is closed out, and the stale CPU wording in the reranker and
  embedder probes is dropped.

### Upgrade notes
- **Deploy the authority, then re-run the installer on every replica PC.** `install/linux-authority.sh`
  (`deploy.sh` on a WSL authority) restarts the server on the new code and ships the chain scripts; the
  server-side changes here (`maintenance_health.py`, `app.py`, `embedder_503.py`, `reranker.py`,
  `redact.py`, the canonical demotion gate) apply only after that restart. A replica PC or client gets
  the banner and enrichment-seed fixes, the PreCompact session id, the SessionStart pre-warm, the wiki
  catch-up and the redaction rules in `memory-common.ps1` and `precompact_capture.py` only when its own
  installer re-runs (on Windows, `install/2-windows-config.ps1`).
- **Pool health.** A DEGRADED pool now turns `/health/maintenance` `ok` false and makes the health-stamp
  step exit 2 every night. For a planned window add `MEM0_POOL_HEALTH_ACK=DEGRADED:<YYYY-MM-DD>` (the
  state and the last day, UTC) to `~/.mem0/stack.env` on the authority: no restart, it holds only while
  the pool is in exactly that state, and it lapses the day after its date. A weekly step that comes back
  `degraded` or `failed` stays listed for the week.
- **Promotion-gate mode.** `install/linux-authority.sh --promotion-gate-mode shadow|enforce` writes
  `MEM0_PROMOTION_GATE_MODE` (the code default is `shadow`); omitted, a recorded value is carried, and
  an empty value drops it. An earlier re-run may have deleted a hand-set line, so on an authority that
  should enforce, check `~/.mem0/stack.env` and set the flag once if the line is missing.
- **Fleet store.** A Linux client or replica re-run without `--ams-hub` now inherits the recorded hub;
  pass `--ams-hub ""` to leave the fleet store.
- **Wiki catch-up.** On each replica PC that mounts the vault, set `WIKI_VAULT` or write the vault
  directory on the first line of `~/.mem0/wiki-vault`; with neither, the catch-up does nothing. A
  hand-written `wiki-index-refresh.sh` is kept as it is.
- **Canonical demotion.** After the restart, an unsigned demote of a canonical record should return 403
  and a signed one 200; use `mem0-canonize.sh --action demote`. Automation that demoted through the
  plain API now gets 403.
- **Dependencies.** Re-running the authority installer (`install/linux-authority.sh`, or
  `install/1-wsl-services.sh` on a WSL authority) raises `cryptography` to `>=50.0.1` and installs
  `pip-audit`, so it needs a package index. Snapshot `pip freeze` first, and run the full suite,
  `/health/deep` and the canaries before keeping the result; `deploy.sh` does not run pip.
- **The first L10 run after deploy** flags retired credential-bearing points once; review them by hand.
- **Live test suites** refuse a non-loopback `MEM0_URL` unless `AMS_ALLOW_LIVE_PROD_TESTS=1` is set.
- **Brand map.** Set `MEM0_BRAND_MAP` on the authority (in its `~/.mem0/stack.env`, which installer
  re-runs carry) to the path of your brand map; the map is operator data and is not in this repository.
  `scripts/wsl/ams-store-judge-apply.sh` passes `--brand-map` only when the variable is set
  (environment, else `stack.env`), and the Python resolvers and the admission gate fall back to
  `~/.claude/scripts/brands.json`; with no map anywhere, everything stays brand-neutral, as before. List
  every label that must stay visible everywhere under `shared_brands` in the map, which works on the
  server and on a PC: once PCs run the new hooks, new facts from this stack's own workspace carry that
  workspace's brand and drop out of brandless recall unless the label is shared. `MEM0_SHARED_BRANDS` in
  `stack.env` reaches the authority only, because the Windows hooks read shared labels from the map and
  their own environment. Existing untagged records change only through a reviewed
  `scripts/wsl/brand-backfill.py --dry-run --out report.jsonl`, then `--apply --from report.jsonl`;
  nothing tags them automatically, and a spool line written before this release replays brandless.
- **Semantic dedup.** It compares pairs again after weeks of comparing nothing, so it starts deleting
  near-duplicates again: up to 50 a night (`--max-deletions`), highest cosine first, with a restore
  record in `~/.mem0/dedup-report.jsonl` written before each delete. The audit measured a backlog of
  about 112 distinct ids, mostly install-verify and extractor near-duplicates. Run
  `semantic-dedup.py --dry-run` once on the authority and read `~/.mem0/dedup-report.dryrun.jsonl`
  before its first live night, or set a lower `--max-deletions` in the step's unit until you have
  reviewed it. `numpy` is a new dependency (`mem0-server/requirements.txt`, the installer's pip line).
  Re-running `install/linux-authority.sh` installs it into an existing venv, because that installer runs
  the fresh-install pip line every time; the WSL installer's refresh branch and `deploy.sh` do not, and
  the installer notes that numpy arrives with `fastembed`, so on a WSL authority check that
  `import numpy` works in the venv before the first night.
- **Backup.** The first nightly backup after deploy prunes in one go: the server-side Qdrant snapshots
  to 2 per collection (about 26 files on the audited authority), the cloud mirror to 7 complete sets
  (about 10 sets deleted) and the local manifests to 8 (about 17 deleted). That is intended and happens
  once. The set now also carries the episodes, entities and wiki-page collections, and a night that
  loses a secondary snapshot or a server-side delete reads `degraded`, which turns `/health/maintenance`
  `ok` false until the next good night; a stale or failed set makes the cloud copy refuse with exit 5 or
  6, a failed step until a good night. Watch the first live night: the Qdrant checks (the first token of
  `.checksum` being the sha256, the snapshot list returning `name` and `creation_time`, DELETE also
  removing the `.checksum`), the coreutils calls the scripts rely on (`stat -c`, `sha256sum`, `cmp`,
  `xargs -d`, `find -mtime -delete`) and the deletion latency on the cloud mount have only run against
  fakes, and a mismatch fails safe: the copy is removed, the night reads red and nothing is deleted
  server-side. The manifest's `git_sha` reads `unknown` until an installer run, or `deploy.sh` on a WSL
  host, stamps the tree.
- **Episode embeddings.** `episodic-reconcile` reads `degraded` (`embedding-coverage-<pct>`, measured at
  14 % before) until embedding coverage passes 90 %. It still exits 0, but by the health contract that
  keeps `/health/maintenance` `ok` false and a replica PC's banner on `NOT OK` for as long. The weekly
  run backfills 500 embeddings, so the gap closes on its own in about six Sunday runs; a one-off run of
  `scripts/wsl/episode-embed-backfill.py` with no limit, which embeds every complete episode that is
  missing one, closes it at once. The first run also marks `in_progress` episodes untouched for 7 days
  `abandoned`.
- **Curation.** The server-side changes (`admission_gate.py`, `capabilities.py`, `job_liveness.py`,
  `episodic.py`, `mojibake_check.py`, `app.py`) apply after the authority's restart, and the chain
  scripts and units ship with the installer. Stamps whose target is no longer canonical stop hiding
  records at once, and a failed target lookup admits the record and is counted. The weekly sweep now
  also runs the stamped re-judge, so it clears stamps against demoted targets and writes one combined
  receipt. `/health/deep` shows `promotion-gate: degraded` while the gate only shadows (see
  Promotion-gate mode above). This release does not demote the stale canonicals, unstamp later operator
  decisions or remove the test canonicals; that is data work.
- **Store judge.** The new `ams-store` binary has to reach the authority and every PC.
  `install/linux-authority.sh` installs it from this version's release asset (`--ams-store-binary` and
  `--ams-store-sums` take an offline drop), and a PC still on the old binary keeps the old behavior,
  including pushing a scratch store. The wrapper ships with the authority installer. The first derive on
  a store with decorated pointers parses them: dangling lines are dropped, over-cap lines shortened or
  floored and `hook:` text harvested into fact files, so a large index reorders and syncs a batch of
  edits. Lint reports `unparsed_pointer` findings for decorated lines with a second link until they are
  split. A night in which every corpus write fails turns `/health/maintenance` `ok` false (`store-judge`
  under `degraded_steps`) until the next good night.
- **Corrections drain.** It exists on a PC only after that PC's installer re-runs (or
  `scripts/windows/build-hook-client.ps1` for the hot list). It then runs at every SessionStart on every
  role and posts at most 50 evidence-tier memories per hour per PC to the authority. The existing
  backlog drains at 50 per hour, the old `test-failure` lines are stamped `dropped` and pruned 30 days
  later, and hooks outside this repository keep appending test-failure lines and writing the subagent
  queue until their own change is deployed. Corrections captured before this release stay unredacted in
  the local queue and its `.bak` until pruned; the drain redacts what it posts. The self-test row
  `corrections drain` WARNs when a pending correction is older than 48 h.
- **Replica PCs keep their role.** A plain `install.ps1` re-run used to turn a replica into a brain.
  With `-Role` omitted, `install.ps1` and `install/2-windows-config.ps1` now keep the recorded role, an
  explicit `-Role` still wins, and first installs are unchanged; an unreadable role record stops the
  installer and asks for `-Role` (pass it explicitly then). Re-run the installer on each replica
  PC: the PC-side changes in this release (brand resolution in the hooks and the L1a worker, the L1a
  lock wait, capture-time redaction of corrections and the corrections drain) reach a PC only then.
- **Manual dream.** `scripts/wsl/ams-dream-now.sh` has run only against a fake `systemd-run`, and its
  shape was checked against a real transient unit; it has not run against the real credentials. After
  the deploy, run it once on the authority
  (`ssh <authority-alias> 'bash ~/apps/mem0-scripts/ams-dream-now.sh'`) and check the dream's receipt.
- **Disk-loss runbook.** Step 2 of "The authority's only disk died" (`docs/operations.md`) needs a
  plaintext copy of the authority's two keys stored off the box. This repository cannot supply or check
  it, so confirm that one exists.

## 1.31.4 — the installer stops reporting a phantom pid 0 (2026-09-23)

### Fixed
- **Installer: no phantom "pid 0" process.** With no `ams-store.exe` running, `Get-AmsStoreProcesses` returns an empty array that PowerShell hands to `-Processes` as `$null`, and `@($null)` is one element, so the 1.31.3 installer printed "1 ams-store.exe process(es) seen but could not be identified (pid 0)" on every clean install. `Select-AmsStoreProcessesForStore` now skips null entries. Test: `reports nothing when no ams-store.exe runs`.

## 1.31.3 — the self-test stops failing replicas for things the installer does on purpose; operator keys survive a re-run; a test HOME no longer holds the live store lock

**The compactor row contradicted the installer.** Since 1.25.0, `install/2-windows-config.ps1`
step 1d removes the nightly `ClaudeCode-MemoryCompactor-5am` task on a box whose store hub path is
proven, and keeps it where that path is not proven. `Test-MemoryStack.ps1` still reported every
absent task as `FAIL not registered`, so a replica with `HubHost` set got that FAIL right after a
clean install (measured 2026-09-22).

- The hub-path predicate now lives in one place, `Get-AmHubPathGaps` in `memory-store-lib.ps1`.
  "Proven" means a hub host is configured, the hub identity key is present, and the hub's host key
  is in the user's `known_hosts`. The installer's step 1c computes its refusal list from it, and its
  known_hosts seeding reads the same `Get-AmHubHostKeyLines`. The self-test row calls it through
  `Get-AmCompactorTaskVerdict`: absent with the hub path proven is OK (retired), absent without it
  is FAIL and names the gap, and a present task gets the same action-shape checks as before.

**A replica reported the drift guard dead from a file the brain no longer writes.** A replica
still carries the `~/.mem0/consolidation-drift.jsonl` it wrote while it was the brain. That file's
last `guard-dead` record made the `consolidation drift` row FAIL with "consolidations are running
UNGUARDED", while the brain's guard compared 7/7 canaries every night with zero snapshot
failures. The row now reads the local log on the brain only, the same rule as `drift guard
liveness` and as the SessionStart banner since 1.28.4. On a replica it reports the brain's guard
state from the authority's `/health/deep` for information; the capability manifest row is the one
that FAILs a dead `drift-guard`.

**`MEM0_BRAIN_SSH` was deleted by every installer re-run.** `wiki-index.sh` reads the brain alias
from `~/.mem0/stack.env`. No flag sets it, so the operator adds it by hand, and each of the three
writers rewrites the whole file. `install/stack-env.sh` now lists the operator-owned keys once
(`STACK_ENV_OPERATOR_KEYS`), and `stack_env_carry` carries them over from the existing file. It
takes the first occurrence, drops a CR from a hand edit, and the value is still checked as a plain
token. `1-wsl-services.sh` (a Windows replica's WSL, where the wrapper runs), `linux-replica.sh`
(a native replica) and `linux-authority.sh` all pass the carried keys to their write. Nothing on the
brain reads the key today, but re-running that installer is the brain's only deploy path, and a
deploy must not drop a line the operator wrote.

**A test run held the live store's lock.** The Windows named mutexes `Local\ams-store`,
`Local\ams-memory-compact` and `Local\ams-store-watch` are global to the logon session,
whatever the state root. A Pester run's sandbox compactor, running with a temp `USERPROFILE`,
held `Local\ams-memory-compact`, so the real `ams-store sync --once` exited 4 ("the per-PC lock
is held") and `install/3-verify.ps1` failed (measured 2026-09-23). Every production mutex name is
now scoped to its store: the base name, `-`, and the first 16 hex digits of SHA-256 over the
canonical state root (full path, backslashes, no trailing separator, lower case). The Go side
(`internal/lock/scope.go`, derived from the lock file's directory) and the compactor's GUARD 0
(`Get-AmStoreMutexName` in `memory-store-lib.ps1`) derive the same name, and both suites pin one
golden vector. One store still admits one holder, and two stores no longer exclude each other.
GUARD 0 now also counts a mutex that already exists as held. `ams-store` opens the mutex without
owning it (Go moves goroutines between threads), so `WaitOne` alone had let a compaction start
beside a Go pass on the same store. The binary and the PowerShell scripts must be on the same
release for the two to exclude each other; the installer puts both on the release `VERSION`
names in one run.

**Closing the upgrade window.** Moving to per-store names leaves a gap during the upgrade itself.
The installer renames the running exe aside, and a pre-1.31.3 `sync --watch` kept running from
`ams-store.exe.prev` on the bare names. On Windows the watcher singleton is a mutex only, so the
next SessionStart would have started a second watcher on the scoped name, and a scoped compactor
would have run beside the old one. Three changes close the gap, none of which takes the bare
names:

- Before the swap, the installer stops every `ams-store.exe` that serves this user's store. A
  process qualifies when its image is the deployed exe or its `.prev`, its owner is this identity,
  and its `--state-root` (or the default root) is this store. The resident watcher is stopped at
  once. A derive or sync pass gets 20 s to finish, then is stopped. Each stop is logged. The
  watcher is then restarted from the new image the way SessionStart starts it. If the hub path is
  not proven, the log says the watcher starts at the next SessionStart. Another user's process,
  another store's process and test binaries are left alone.
- For this one release, a new watcher on the operator's default store refuses to start while a
  bare-named `Local\ams-store-watch` is open, which means an old watcher is still alive. It
  checks with `OpenMutex` and never creates or takes the name. The refusal goes to stderr and to
  `watch-refused.log` in the state root, and the watcher exits non-zero. Scratch and test roots
  never check the bare name.
- The PowerShell compactor now also takes the Go file lock `ams-store.lock` in its state root.
  It uses the same JSON holder (pid, process start time, host, `acquired_at`, reason), the same
  10-minute staleness rule, the same `.breaking` guard for a dead holder, and releases the lock
  only if it is still the holder. Exclusion between Go and PowerShell therefore no longer depends
  on mutex names. Checked against the real binary: `ams-store lock status` reads a
  PowerShell-held lock as live, `lock acquire` exits 4 against it, and PowerShell sees the lock
  as held while Go holds it.

**Stopping a process no longer risks orphaning git.** `TerminateProcess` does not run the
tree kill in `gitx` (the one that ends git child processes), so a `git.exe` killed mid-commit or
mid-gc on `history.git` could be left holding `index.lock` or a ref lock, and every later sync
would fail on it. The stop step now works in this order:

- **The watcher is asked first.** The installer writes `watch.stop` in the state root. A
  1.31.3+ watcher sees it through the directory watch it already has and exits between passes,
  never in the middle of one. A leftover stop file is cleared when a watcher starts. An older
  image ignores the file.
- **Anything still alive after its grace is killed as a tree.** The grace is 30 s for a watcher
  and 20 s for a pass. The kill is `taskkill /PID <pid> /T /F`. Right before it, the pid's start
  time is re-checked against the WMI snapshot. A pid that now names another process, or whose
  start time cannot be read, is not killed.
- **After a forced stop, stale git locks are checked, conservatively.**
  - **Which dirs:** a git dir must be a direct child of the state root with `HEAD`, `objects\`
    and `refs\`, and must not be a reparse point.
  - **Which files:** only git's own lock names are candidates: `index.lock`, `HEAD.lock`,
    `config.lock`, `packed-refs.lock` and `shallow.lock` at the top level, and `*.lock` under
    `refs\`. The search never enters a junction or symlink. Every file deleted must resolve
    inside the canonical state root with no reparse point on the way.
  - **When:** a lock younger than 2 minutes is left alone. The installer waits that grace out
    once after a forced stop.
  - **Only with no git running:** a lock is removed only while no `git.exe` of the current user
    is alive at all, and that is re-checked before each delete. Matching on the command line
    would miss cwd-only, relative, `--work-tree`-only and `GIT_DIR` invocations and git's own
    children, and Windows cannot read another process's working directory reliably.
  - **Reporting:** the result goes to `git-lock-recovery.json`. The new Test-MemoryStack row
    `store history git locks` FAILs on a lock older than 10 minutes when no `git.exe` is alive.
    It WARNs, naming the pids, when an old lock coexists with a running git, since a long gc can
    hold one. It also WARNs for 14 days after a recovery removed or kept locks.

Three smaller fixes in the same step:

- A process with no WMI image path is identified by `argv[0]` from its command line. One that
  still cannot be identified gets its own line: "N ams-store.exe process(es) seen but could not
  be identified (pid ...)".
- A respawned watcher is checked 3 s later. A quiet exit 0 is reported as "no live session here,
  it starts at the next SessionStart". A non-zero exit is reported with its code and the last
  `watch-refused.log` line. The installer no longer says "watcher restarted" without checking.
- Skipped nights now run noon to noon, local time. A catch-up retry across local midnight is no
  longer counted as a second night.

**A wedged lock holder is now loud.** GUARD 0 exits 0 without a receipt when the store lock is
held, because a second instance is the normal case. A holder that is wedged rather than dead
keeps its handle, though, and would silence every nightly run. Each skip is now recorded in
`compact-lock-skips.json`: the distinct nights skipped since the last run that took the lock,
plus the holder (the lock file's pid and reason, or this user's running `ams-store.exe`
processes). A run that takes the lock clears the file. Test-MemoryStack's new
`auto-memory compactor skipped nights` row WARNs at 2 nights and FAILs at 4, and names the
holder. While the task is retired, the row does not judge the counter.

- Tests: `AmsStoreInstall.Tests.ps1` (the predicate on real `ssh-keygen` known_hosts, and the
  verdict table), `internal/lock/scope_test.go` (two roots hold at once with the production
  names, one root still excludes by the file and by the mutex alone, a compactor-held scoped
  legacy mutex stops a Go pass on its own root only, one watcher per root, the golden vector),
  `MemoryStoreLib.Tests.ps1` (the same golden vector from PowerShell), `MemoryCompact.Tests.ps1`
  (GUARD 0 on the sandbox's scoped name, an unowned handle stops it, another store's mutex does
  not, a live Go holder of `ams-store.lock` stops it and a recycled-pid holder is broken, skips
  are counted), `CompactorLock.Tests.ps1` (the PowerShell file lock and the skipped-night
  thresholds), `internal/sync/watch_legacy_test.go` (a fake old watcher holding the bare-name
  stand-in makes the new watcher refuse and log, and the check never creates the name),
  `cli/legacywatch_internal_test.go` (only the default root checks the bare name),
  `AmsStoreInstall.Tests.ps1` (the stop step selects this store's processes from a fake process
  list and never another user's or another store's, a pass is given time to finish, and the stop
  runs before the old image is renamed; mutants without the stop step or without its wiring go
  red), `InstallerParity.Tests.ps1` (the installer and the self-test both call the shared
  predicate), `RegressionGuards.Tests.ps1` (the replica branch comes before the local drift log is
  read), and `test_stack_env_writers.py` (a pre-existing `MEM0_BRAIN_SSH` survives a
  `--render-only` re-run as a fixed point, `stack_env_carry` on a CRLF file, and every writer's wiring).

## 1.31.2 — `deploy.sh` refuses a native authority instead of breaking it

**`deploy.sh` is the WSL deploy path, and it ran on the native brain.** On a box whose
`~/.mem0/stack.env` says `MEM0_HOST_KIND=native`, it rendered the `ams-*` units with only the
WSL sentinels, so `__SECRETS_DIR__` stayed literal in every step unit's
`LoadCredentialEncrypted=` and `CODEX_HOME=`. It also put the WSL DPAPI `ExecStartPre` back into
`mem0.service` and wrote the WSL per-job timers onto a box that runs one chain
(`ams-nightly.timer`). Its `--dry-run` exited 0 and listed the damage as 41 `unit CHANGED` lines,
so an operator who followed "run deploy.sh" broke the brain without any error.

- `deploy.sh` now checks the host kind right after it sources `stack.env`. On a native host it
  stops before any write, with `--dry-run` too, and exits 5. The message gives the command that
  does deploy there: `bash <checkout>/install/linux-authority.sh --bind-ip <MEM0_BIND>
  --secrets-dir <MEM0_SECRETS_DIR>`, with both values read from `stack.env`. The installer
  inherits every other flag on a re-run. The value is compared as the other `MEM0_HOST_KIND`
  readers compare it (`.strip().lower()`): carriage returns are dropped and surrounding whitespace
  is trimmed before a case-insensitive match. A CRLF receipt, which sources as `native` plus a CR,
  is refused like any other native receipt.
- The unit loop now skips `ams-*` units on every host. Its old native-only branch could no
  longer run.
- The docs that tell an operator to run `deploy.sh` now say which hosts it applies to
  (`DEVELOPMENT.md`, `flows/install-and-cutover.md`, `systems/installer-and-deploy.md`).
- `test_deploy_host_kind.py` runs `deploy.sh` against a temp `HOME` with recorder stubs for
  `systemctl`, `curl`, `cmd.exe` and `wslpath`. It checks that a native receipt exits non-zero
  and leaves `HOME` byte-for-byte as it was, with and without `--dry-run`, and that a WSL receipt
  (or one without `MEM0_HOST_KIND`, or no receipt at all) gets past the check. It also runs CRLF,
  padded and tab/CR variants of the native receipt. With the check removed, 4 of its tests fail;
  with only the strip removed, the 8 variant tests fail.

WSL brains and replicas behave as before.

## 1.31.1 — stack.env is a file every reader parses the same way

**`deploy.sh` died on the brain's receipt.** `install/linux-authority.sh` wrote
`MEM0_WIKI_SOURCES` unquoted and space-separated. `deploy.sh` and `storage-cap-check.sh`
SOURCE `~/.mem0/stack.env` as bash, so the second host ran as a command:
`stack.env: line 16: <user>@<host>: command not found`, before `deploy.sh --dry-run` did anything.
Quoting would not fix it, because the other readers keep the raw text after `=` (the sed
`stack_val` readers, the installers' inherit, `ams_env.py`, `job_liveness.py`).

- Lists are stored comma-separated. `--wiki-sources` accepts commas, spaces or both, and an
  inherited old-form value is rewritten, so re-runs converge. `wiki-index-nightly.sh` splits on
  commas and whitespace, so a box whose receipt still has the space form keeps pulling.
- A new `install/stack-env.sh` is the one writer, used by all three writers of the file
  (`1-wsl-services.sh`, `linux-authority.sh`, `linux-replica.sh`). It refuses the whole file
  when any value is not a plain token (whitespace, `$`, backtick, `~`, `;`, `|`, `&`, quotes ...).
  The native installer runs that check before it touches anything, including on `--dry-run`.
- `--render-only` now also renders the receipt. `test_stack_env_writers.py` checks that
  `bash -c 'set -e; . stack.env'` succeeds and that bash, sed, `ams_env` and `job_liveness` agree
  on every key, and it pins the list of writers.

Behavior change: a Windows user name or repo path containing a space now stops phase 1 with a
named error. Before, it wrote a receipt that `deploy.sh` could not source.

## 1.31.0 — the per-prompt block is for people, and it does not repeat itself (C10)

**The block rode machine turns.** Claude Code raises `UserPromptSubmit` for background task
notifications as well as for prompts a person types, and the `[MEMORY CONTEXT]` block was
injected on about 91% of those notification turns (and on 81% of human prompts). In long
sessions 60–75% of the events are machine turns. Every injection stays in context and is
re-read on each later call, which added up to 30–90k chars per long session. A prompt that
starts with `<task-notification>` (after leading whitespace) is now a **machine turn**: it keeps
the 0.A episode checkpoint through the checkpoint-only POST, the same path a trivial prompt
takes, and gets no block. A notification queued behind a running turn reaches the hook with the
same wrapper. In the transcripts, 3,932 queued and 3,727 own-turn notifications all start with
it, and the sampled hook inputs match the queued prompt byte-for-byte. The verdict is applied
at every point that can emit or serve the block, and all of them are tested against one shared
corpus (`scripts/windows/tests/fixtures/machine-turn-prompts.json`):
- the lib's `Test-MachineTurnPrompt`, which serves the daemon's `bundle_raw` and `bundle` ops
  and the inline fallback;
- the compiled client, which scans the stdin prompt and withholds any block it would relay.
  It is the last emitter, so a stale daemon or fallback cannot leak one;
- the server: `/v1/context/bundle` still checkpoints a machine turn but runs no search, and
  returns empty sections with `machine_turn: true`.

Peer messages from another session are not task notifications and keep the human path.

**The block repeated itself.** 56% of the memory lines injected in a session had already been
shown earlier in the same session. Both prompt paths now share one per-session state file,
`~/.claude/state/mem0-injected-<session_id>.json`, which holds 16-hex hashes only, never memory
text:
- A memory line the session was already shown is dropped.
- The goals and frontier-questions sections render only when their content differs from what
  the session was last shown.
- When nothing novel is left, R2 abstention applies and no block renders.

A compaction discards the blocks, so it resets the state. `stop-extract.ps1` clears it on
`PreCompact`, which is synchronous, so no prompt runs in between. `mem0-hook-daemon-spawn.ps1`
clears it again on `SessionStart` with source `compact` or `clear`, as a backstop. `resume`
keeps the state, because a resumed context still holds its blocks. An unreadable state fails
open to the full block. This replaces HK-5, the daemon-only goals/questions blanking with a
fixed 25-prompt re-inject; the inline path used to repeat those sections on every prompt.

**No silent failure, and no failure that suppresses content** (review round). A reset that
cannot delete the state overwrites it with an empty state. If that fails too, it writes a
compaction marker (`mem0-injected-<session_id>.compacted`), and the reader ignores any state
saved before it. Every reset outcome (`absent`, `removed`, `truncated`, `invalidated`, `FAILED`)
is logged. So are an unreadable state, a failed save (with its path and exception), a missing
lib at PreCompact and a classifier that throws. Lines start with `C10 injection-state:` and go
to `~/.claude/logs/user-prompt-extract.log`, or `~/.mem0/hook-daemon.log` inside the daemon.
Other changes in this round:
- The daemon keeps each session's state in memory and reloads it when the file or the marker
  changes, so a warm prompt pays no file read, as with HK-5.
- Every reset writes the marker, and each save is stamped with its request's start time
  (captured before the bundle POST). A save that was in flight across a reset is therefore
  stale, and the daemon's cache drops it.
- The 7-day sweep keeps a marker while its stale state survives, and logs each marker it
  removes.
- When the logs directory is unwritable, log lines fall back to stderr.
- One helper, `Get-TranscriptSessionId`, keys the state on every path.
- The injection-state sweep reuses `Invoke-RateLimitStateSweep -Filter`.
- Both daemon render sites pass the same `-StateDir` and `-Cache` (pinned).
- The compiled client's scan is now depth-aware: only the top-level `prompt` counts. It skips
  leading whitespace with no length cap and reads truncated stdin as human.
- The client gates only the daemon-served path. It logs any block it withholds, with its byte
  count, and relays the inline fallback's output unchanged, because that child already decided
  with a real JSON parse.

**Unchanged on purpose:** `memory_cap`, `goal_cap`, `oq_cap`, the 0.30 relevance threshold,
R2 abstention, the R6 placement, and the render itself. Dedupe only removes lines before the
unchanged line builders run, so an emitted block is byte-identical to the pre-change render of
the reduced bundle. `MachineTurnDedupe.Tests.ps1` pins that parity, including the small tier.
`UserPromptExtract.Tests.ps1`, which pins the render, passes unmodified.

## 1.30.1 — the replica wrapper closes its tunnel

**`wiki-index.sh` left its tunnel open.** The EXIT trap that closes the SSH control socket
read the brain alias from a `local` of the function that opened it; when the trap fired after
that function had returned, `set -u` killed it with "unbound variable" and the `ssh -f -N`
outlived the run (found on the first post-release refresh). The alias is now a global, and
`test_wiki_index_wrapper.py` — the wrapper had no test — pins the contract: snapshot from a
tar (an empty one refused), tunnel opened → builder run → tunnel closed, in that order, the
search passing its query and `--k`, and the alias resolution order (`WIKI_BRAIN_SSH`,
`MEM0_BRAIN_SSH`, the SSH-config Host naming the authority, the host itself).

## 1.30.0 — the operator's wiki gets a searchable index on the brain, kept fresh two ways

**The wiki's semantic index existed nowhere.** The operator's LLM Wiki (a curated markdown
vault on a cloud-synced folder) had a Qdrant index built by maintainer-side scripts into the
PC that was the brain at the time; the v2 cutover made every PC's Qdrant a dormant replica and
nobody moved the collection, so `wiki-search` answered "collection missing" for six days while
its scripts still pointed at a drive letter that had moved. The scripts are now in this repo,
scrubbed and configurable, and the index lives on the brain box
([docs/systems/wiki-index.md](docs/systems/wiki-index.md)).

**Two refresh paths.** A replica's `wiki-index.sh` snapshots `wiki/` from a tar on stdin and
builds or searches through an SSH tunnel to the brain's loopback Qdrant (alias from
`WIKI_BRAIN_SSH`, `MEM0_BRAIN_SSH`, or the SSH-config Host that names the authority). On the
brain, a new chain step `wiki-index` (`--guarded`, after `index-refresh`, before the stamping
backup) pulls `wiki/` from the first reachable PC in `--wiki-sources` over a dedicated key the
operator pins to a forced tar command, and builds locally; with no PC reachable it keeps the
index while the last pull is under 72 h old and fails after. The installer renders the step
only when `--wiki-sources` is set — the store-judge rule, applied again. The index is outside
the backup set on purpose: the vault is the record, a rebuild is the restore.

## 1.29.0 — a decision is applied to today's files, and no two passes hold the lock (register P5-10, P5-14)

**The judge decided on a day-stale checkout (P5-10 a).** The hub's checkout is a PC like any
other: it moves only when something syncs it, and nothing did between one night's apply and the
next night's plan. The judge read copies a session had edited hours earlier, migrated two of
those facts, and the chain's own post-apply merge then met modify-vs-delete and kept the edited
files — `resurrected`, the deletion table working exactly as designed on a wrong-input decision.
Phase 3.7 now runs `ams-store sync --once` on the checkout before it reads any candidate, and
`ams-store-judge-apply.sh` runs the same sync before its apply loop. An exit that is not 0 or 6
(a conflict recorded in history: the work tree IS the merge result) means the files are of
unknown age, so no plan is written and nothing is applied — the trailing deterministic sync
still runs, so the floor lands whatever happened.

**Two passes could hold the per-PC lock, and a stale stage re-added queued deletions (P5-10 b).**
Breaking a dead holder's lock file was a bare `os.Remove`, which two contenders could both win:
each read the same dead holder, the first removed it and created its own lock, and the second's
remove deleted THAT fresh lock before creating another. The break now happens under an O_EXCL
sibling guard, re-reads the holder under it and leaves a live one alone; a guard older than a
minute belongs to a breaker that died mid-break and is cleared without granting the lock.
Separately, `Commit` now puts HEAD's entry back in the index for every path any workspace's
deferred queue holds: `Stage` already excludes them, but a stage taken BEFORE a concurrent merge
wrote the queue carried the on-disk bytes that merge had withheld, and the commit that followed
re-added three hub deletions on top of the merge. The queue is honoured at the moment of commit,
whatever admitted the concurrency. The watcher also takes the per-PC lock through the same seam
every other verb uses and retries a refused pass shortly after, instead of leaving the dirty
marker until the next remote check.

**The weekly contradiction sweep had never once run (P5-13, new).** `codex login status`
exits 0 and prints "Logged in using ChatGPT" on STDERR, leaving stdout empty. The native
health check read stdout alone, so every native host reported `logged_in=False` on a Codex
that was logged in. `contradiction-sweep --judge codex` preflights that health, so it
refused to judge, fell back to `ensure-codex-shim.sh` hunting a Windows shim a native brain
does not have, failed on the empty `MEM0_WIN_USER`, and exited 0 with
`outcome=no-op:codex-shim-unreachable`. The chain's health stamp then recorded the step
`"ok": true` with a fresh `last_success`, so a weekly memory-integrity sweep that had never
judged anything reported green every Sunday since the authority went native - both recorded
Sundays (2026-09-13, 2026-09-20) are the same no-op. The health check now reads both
streams. The test that covered this passed throughout because its fixture put the message on
stdout, the one stream the real binary does not use; it now also asserts the stderr shape
measured on the live authority. `test_codex_native_transport.py` was absent from CI's
headless list entirely and has been added - the whole native-transport suite (11 tests) was
never running there.

**The replica installer runs on aarch64 (P5-14).** Qdrant publishes a glibc build for x86_64 and
a musl one for aarch64; the installer picks by `uname -m` and refuses any other architecture by
name. Both Linux builds link jemalloc, which aborts at startup on anything but 4 KiB pages
(`<jemalloc>: Unsupported system page size`) — the Raspberry Pi 5's default kernel is 16 KiB. The
page size is now a prerequisite check, before anything is installed, and names the remedy
(`kernel=kernel8.img` under `[pi5]`, the 4 KiB `linux-image-rpi-v8` kernel). The thin client's
pwsh hint also covers boxes with no snap and arm64.

## 1.28.5 — the native brain's liveness probe sees its own dream (register P5-12)

`job_liveness` read the dream throttle mark, the `prune.json` / `gather.json` phase receipts
and the morning summary only from a Windows profile under `/mnt/c/Users/<MEM0_WIN_USER>`. A
native Linux brain has no such profile: its chain writes those under `~/.mem0/maintenance`
(`ams_env.state_dir()`), so the four fields stayed null, the health note read
"MEM0_WIN_USER unset", and `capabilities` reported dream-cycle, memory-index, sweep-job and
codex-auth as `unknown` on a brain whose chain had run 16/16 the same night. The collector now
skips the profile lookup when `MEM0_HOST_KIND=native` and reads the native paths for any of
those fields the profile did not fill (a profile's markers still win where both exist); its
"missing" notes are raised only on a native host, so a Windows box collects nothing new.

Same class, same box: the canonical-key row read `degraded` for the strongest posture the
brain has. Every shipped unit loads the key with `LoadCredentialEncrypted` (systemd-creds),
which the provider reports as source `credential`; that source now counts as `alive` beside
`runtime` and `dpapi`, and plaintext stays `degraded`.

## 1.28.4 — a replica never dreams (register P5-11)

Since the authority moved to the native Linux brain, the nightly chain (dream, store judge, index,
backups) runs there and a replica's `~/.claude/state/last-dream` marker never advances again. The
Windows-side catch-up read that as a permanent "long gap" and, every 6 h throttle window, ran a
full consolidation FROM the replica: insights posted to the authority on top of the brain's own
night, the drift snapshot failing against the dormant loopback server (a standing "DRIFT GUARD
DEAD" in every session banner), the index build failing the same way and never marking its
throttle, ~28k Codex tokens a run. Measured on the reference workstation 2026-09-19: two runs a day since the
cutover.

The catch-up, the standalone index refresh and the consolidator itself now read the installer's
`~/.mem0/role` (absent = brain) and exit with a logged `role=<r>` on anything but the brain; the
consolidator's gate holds for `-Force` too. The session banner reads the drift state only on the
brain. Nothing changes on the brain: an explicit `role=brain` still runs every path.

## 1.28.3 — the blast cap also exempts pointers to files the history deleted on purpose

1.28.2 exempted dangling pointers whose slug carries a `Migrated:` trailer. The same shape
arises without a trailer: a hand re-home of a store's doctrine into topic files deletes a
hundred single-fact files in one commit, and every PC then holds a hundred dangling pointers
over its 20 % cap — the clean-up hygiene would refuse on every pass, on every PC, forever. A
file that a commit in the shared history deleted and that is absent at HEAD is a decision
already made and synced, not a wipe in progress, so those pointers no longer count either
(receipt field `dedangled_history_deleted`; an abort's note reports the exempt total). The
lookup fails closed: a file still present at HEAD, a missing history, or any error counts. The
wipe-protection the cap exists for is unchanged, because an unreadable directory has no
deletion commits.

## 1.28.2 — the blast cap no longer refuses the judge's own deletions (register P4-4, night 1)

The second thing night 1 found on the PCs. The hub judge may delete up to 20 % of a store's entries
in one night — its own blast cap. A PC receives those deletions as files vanishing under an index
that still points at them, and its hygiene pass drops the dangling pointers, bounded by the same
20 % — of the *smaller* entry count that is left. Whenever the night removed more than about a
sixth of the store, the PC's count exceeded its cap on every pass, and hygiene refused forever:
16 pointers over a 14-line cap on one store, 2 over a 1-line cap on a five-line store, both
`aborted-blast-cap` at every sync.

A dangling pointer whose slug carries a `Migrated:` trailer in the history is a decision the judge
already made and verified (write-then-verify into the corpus), not evidence of a store being gutted.
Hygiene now looks each dangling slug up through the same history lookup that stamps `migrated:`,
and those pointers no longer count against the cap; the receipt reports them as `dedangled_migrated`
and an abort's note says how many were exempt. The lookup fails closed: no trailer, or an unreadable
history, still counts, and the pre-existing abort on a mass-dangling index is unchanged.

## 1.28.1 — a harvest stamp no longer resurrects a queued deletion (register P4-4, night 1)

The first night of the P4-4 metric found the judge's migrations being undone by the PC that
received them. The hub judge migrated nineteen facts from one store and deleted their files; a PC
synced under a live session, so every deletion was queued rather than materialized (§5.3(7)); then
that PC's own post-merge derive wrote each queued file — it stamped `migrated: <id>` from the
`Migrated:` trailer that had just arrived in the same merge, and re-harvested the hook. The drain
compared bytes, read those machine writes as a session's later edit, and abandoned every deletion
as `resurrected`; the next push re-added the files to the hub. Measured on the second store the
same night: three migrated facts back on the hub, each carrying its own `migrated:` stamp.

The drain's re-check now uses the deletion table's normalized comparison instead of byte
equality, and `migrated:` joins `hook:` and `modified:` as a line that comparison ignores: harvest
output is never a person's edit. A real body edit after the merge still resurrects the deletion —
that case is pinned alongside the new one. No format changes, no migration; the fix takes effect
on each PC's next sync.

## 1.28.0 — the capture path runs on a native Linux client (register P4-3)

A Linux box could read the corpus but never contribute to it: the L1a capture path — a Stop /
PreCompact / SessionStart hook spawning a worker that reads the finished transcript, asks Codex for
durable facts and posts them to the authority — was written in PowerShell against Windows. That left
the register's own invariant *"first-class on every OS"* unmet and it was the last thing between the
the ultrabook node and its row's gate.

The scripts now branch on host kind rather than assuming Windows, using the same test the Python
side already uses (`$PSVersionTable.Platform`, absent on PowerShell 5.1, so its absence reads as
Windows). What differs by platform: the home directory, the corpus key (a WSL UNC share vs the
per-host file), the Codex CLI (a pinned npm `.cmd` vs PATH resolution), the child shell the subagent
runs in, and the timeout kill (`taskkill /T /F` vs `.NET Kill($true)` — both kill the whole tree).
Every path is built with `Join-Path` per segment, because a backslash is an ordinary filename
character on Unix.

`Get-AmsHomeDir` resolves per call and sits first in the file. Both were found by failures rather
than reasoning: a cached value made the sandboxed-HOME suites read the operator's real profile, and
a helper defined after `$script:Mem0Url`'s load-time assignment was invisible to the function that
needed it, silently yielding the loopback fallback instead of the authority.

`install/linux-client.sh` gains `[5c]`: it deploys the four capture scripts **with the tenant
sentinel resolved** and registers the three capture hooks, skipping loudly without `pwsh`, `codex`
or Codex auth. The substitution is load-bearing — an unresolved sentinel posts every fact under a
literal `__WSL_USER__` tenant that the authority accepts, so nothing fails and the facts are simply
not where anyone looks. The store hooks and the capture hooks are independent.

Proven on the box: pwsh 7.6.5 on Ubuntu 26.04 with codex-cli 0.154.0 extracted four facts from a
real transcript and posted them; the authority holds them with `source=l1a-extractor`. Four
mutations seen red, one of which initially survived a weaker assertion that has been tightened.

## 1.27.1 — the replica installer forwards the fleet-store flags

1.27.0 put the store block in `linux-client.sh`. `linux-replica.sh` — the installer the replica
boxes actually run — calls that script with an EXPLICITLY built argument list, and `--ams-hub` was
not in it. A replica install would have printed "no --ams-hub: this client does not join the fleet
store (skipped)", reported success, and left the box outside the fleet with nothing failing
anywhere. Found by reading the replica's own flag list against the client's, one commit after
shipping the block, and before any box was installed with it.

`--ams-hub`, `--ams-store-binary` and `--ams-store-sums` are now accepted and forwarded verbatim.
The test runs the replica installer's dry run end to end and asserts the hub reaches the client's
plan; removing the forward makes it fail with the exact symptom.

## 1.27.0 — a Linux client can join the fleet store (register P4-3)

`linux-client.sh` installed the MCP shim and the outbox and nothing else, so a native Linux box
could not join the fleet store at all: no binary, no hub transport, no hooks. The ultrabook row had
nothing to deploy. With `--ams-hub` the client now installs all three; without it the block is
skipped and a thin client installs exactly as before. `linux-replica.sh` builds on this file, so
both roles inherit it.

- The binary goes to `~/.local/bin/ams-store`, fetched from the release of the tag in `VERSION`,
  verified against its `SHA256SUMS`, with a `.sha256` sidecar, and `uname -m` picking amd64 or
  arm64. No `sudo`: nothing on a client runs the binary but the user's own session.
- The hub transport is the authority's, minus what only a hub needs: the ssh `Match` block,
  `known_hosts` seeded from the user's, the history repo, exactly one remote named `hub`.
- **The client is roleless by construction** and refuses to run where a `role` file exists. That
  file is what makes `judge-apply` willing to decide; a PC carrying it would start applying the
  nightly's plans to the whole fleet's memory.
- Hook registration is a new Python helper, `claude-config/register-ams-hooks.py`, rather than
  hand-rolled JSON in bash. It mirrors the PowerShell merge exactly, including the rule from the
  2026-06-08 audit: identify our entries by command substring marker, remove only those, append
  fresh, preserve everything else, and write only when the result differs so a re-run is
  byte-identical.

Rehearsed against the live authority in a sandboxed HOME, not only tested: the binary installed and
verified, the store came up roleless, the three hooks registered with the right matcher and async
fields, a second run reported "already current" with an identical `settings.json`, and an
`ls-remote` through the config the installer wrote reached the hub. Twelve tests, three mutations
seen red, the suite registered in CI.

## 1.26.2 — the corpus partition never reached the applier, so every migration failed on its own (register P4-1b follow-up)

Found by a live run, not by a test. Against a purpose-made store and a hand-written plan, `judge-apply`
reported `dry-run: migrated 1` and then, applying for real:
`mem0 add: HTTP 500: {"detail":"Invalid user_id: cannot be empty or whitespace-only"}` — the fact was
kept, the receipt named the orphan, and the verb exited 0. The authority partitions the corpus by
`user_id`; the applier reads that partition from `MEM0_USER_ID`; **nothing in the deployed chain ever
set it.** `ams-step.sh` resolves the authority URL for every step (`ams_env.mem0_url`) but not the
partition, and the step unit carries the credential only. Every MIGRATE decision the nightly judge
made would have failed one request at a time, been receipted `line kept`, and the night would have
exited 0 — forever, with every test green.

Two halves, because the wiring and the refusal are different defects:

- **`ams-step.sh` exports `MEM0_USER_ID`** beside `MEM0_URL`, from `MEM0_DEFAULT_USER_ID` and then
  `MEM0_WSL_USER` in `~/.mem0/stack.env` — the precedence `ams_env.user_id()` already uses — with an
  explicit environment value still winning.
- **`judge-apply` refuses to build a corpus client without a partition**, exactly as it already refuses
  without an authority: one line on stderr, migrations reported as not performed, nothing posted. A
  client built with an empty partition turns one misconfiguration into one failure per fact, which is
  precisely how this stayed invisible.

Three tests, each seen red against the shipped code: a fake authority proves that nothing is attempted
when no partition is configured and that the fact survives; a second proves the configured partition
reaches the wire as `user_id` and the migration completes; the chain test proves the export and its
precedence.

## 1.26.1 — two reasons the nightly judge would never have written a plan (register P4-1b follow-up)

Both found by reading the DEPLOYED authority minutes after 1.26.0 installed, not by any test, and both
silent: the phase would have logged a skip and the applier would have run the deterministic path
forever, nightly, with every test green.

**1. The producer could not see the checkout.** The plan is written by `ams-step-dream.service`, whose
unit carries the credentials and the judge transport but **not** the store variables — those were added
to `ams-step-store-judge.service`, the applier. `systemctl --user show ams-step-dream.service -p
Environment | grep -i ams` returned nothing. `_ams_checkout_root()` and `_ams_store_bin()` now read the
environment first and then `~/.mem0/stack.env`, the precedence every other install value uses
(`ams_env.eval_root`), and the installer records `MEM0_AMS_STORE_BIN` beside `MEM0_AMS_CHECKOUT`.

**2. The schema it validates against was not deployed.** `validate_plan` refuses to write a plan it
cannot validate, and the schema lives under `docs/` — it is the published contract, generated from the
Go types — so the installer's `scripts/wsl/*` glob never carried it. On the authority:
`validate_plan says: 'the judge-plan schema is not deployed beside this script'`. `linux-authority.sh`
now copies `docs/schemas/judge-plan.schema.json` into the scripts directory beside the consolidator.

Each fix has a test that fails against the shipped code: one drives the phase with **nothing** in the
environment and only the installer's `stack.env` on disk; the other asserts the copy and that the
generated schema is checked in at all.

## 1.26.0 — the hub decides: the store-judge step, the hub checkout, and a generated plan contract (register P4-1b)

The nightly judge the fleet-store design promised is wired. `dream-consolidate.py` gains a **store
judge** phase between the autonomous promotion and the prune: for every store in the hub's checkout it
asks the binary for the offer set (`judge-apply --candidates --json`, which already excludes doctrine
and sealed lines), calls the judge once per store that has something to decide, and writes a plan.
A store with nothing to decide is `outcome: ok` with no decisions and no call - a judge that kept
everything is a successful plan, and the applier receipts it `no-op`. (`empty` is reserved for a call
that answered with whitespace, which on an over-trigger store the applier records as
`skipped-judge-unavailable`; using it for "nothing was offered" would have reported healthy stores as
failing judges to lint's `compactor-unproductive` watchdog - found by rehearsing the wrapper against
the real binary before shipping.) A failed call is `unavailable`; unparseable output is `parse_fail`. Decisions naming a slug that was never offered, repeating a slug,
or carrying the wrong fields for their verb are dropped by the producer, so one bad line cannot make
the applier refuse the whole file. The plan is validated against the schema **before** it is written
and a plan that does not validate is not written at all - a missing plan is a deterministic-only
night, which is strictly better than a malformed one.

**The contract is generated, not documented.** `docs/schemas/judge-plan.schema.json` is produced by
`go run ./scripts/planschema` from `internal/judge/plan.go` - verbs, outcomes, slug rule and version
read from the package, never retyped. Three tests hold it: the generator's file must be current, the
Go decoder's verdict on a shared 26-document corpus must match, and the schema's verdict on the same
corpus must match AND stay a subset of the decoder's (a schema stricter than the decoder makes the
producer refuse to write a plan the consumer would have applied - the nightly then stops deciding
with nothing failing anywhere).

**The chain step.** `ams-step-store-judge.service` runs after `ams-step-dream` and before
`ams-step-index-refresh`, through `ams-step.sh --guarded` like every step: it applies the plan store
by store with `ams-store judge-apply`, then syncs once. A missing plan skips the apply and still
syncs, so the deterministic floor lands on a night the judge never spoke.

**The authority installer** gains `--ams-checkout` and `--ams-hub` (both inherited from `stack.env`
on a re-run): it installs `/usr/local/bin/ams-store` from the linux/amd64 release asset of the tag in
`VERSION`, checksum-verified (`--ams-store-binary`/`--ams-store-sums` for an offline drop), and
prepares the checkout - `role` = `hub`, an ssh `Match` block, a seeded `known_hosts`, a history repo
on `main` with `hub` as its one remote, reached by the same `user@<magicdns>:repo.git` form every PC
uses. A box that configures neither flag gets no binary, no checkout, and the step is dropped from
the rendered unit set rather than enabled with nothing to judge.

Also: `main()` in the consolidator threaded every injected collaborator except `now`, so a test that
pinned the clock silently got the real one - the exit-code scenarios had been passing for a reason
unrelated to exit codes since their fixture aged out of the 36 h window. Fixed, and the dream suite
is now in CI (it never was, which is why that rotted unnoticed), along with the new schema suite and
`jsonschema` in the CI dependency line.

## 1.25.2 — a fresh checkout derives its index instead of failing (register P4-1b/P4-2 prerequisite)

`MEMORY.md` is derived and never tracked, so a checkout that has just materialized its stores from the hub holds
fact files and no index. `derive` read that index fail-closed, so the FIRST sync of any fresh checkout - a new PC,
or the hub's own checkout on the authority - materialized every store and then died with
`merge failed: read index ...: The system cannot find the file specified`, exit 5, leaving the stores on disk with
no index at all. Found by rehearsing the hub checkout against the live hub before wiring it (five stores, 350 fact
files materialized, zero indexes). A missing index is now an empty one: `derive` renders it from the fact files
(nothing to harvest, every file re-indexed from its frontmatter hook) and the compare-and-swap treats "still
absent" as unchanged while an index that appeared mid-run still aborts; `harvest` does the same. Any other read
error stays fail-closed. Two tests, both seen red against the old read: the derive engine renders an index for a
store that has none and the second pass is a no-op, and a CLI seam test drives a fresh checkout's first sync
against a bare hub end to end.

## 1.25.1 — the session-start line reports the G7 clock; the store lint runs at session start (register P4-1c)

The design's Phase 4 induced test asks for "the session-start line reporting the metric". `ams-store sync --once`
is silent on stdout by contract and the SessionStart hook runs it asynchronously, so the line is the SessionStart
banner's: `claude-config/storage-cap-check.sh` now prints, per store, the hours over trigger without an applied
decision (`stores[].over_trigger_hours` from `lint-summary.json`) - `auto-memory G7: over trigger <ws> <h>h` below
24 h, `AUTO-MEMORY G7 ALARM: …` at or above - and its stale-summary wording no longer names the PowerShell lint.
The maintenance spawner runs the binary's lint (`ams-store lint --summary-out <state>/lint-summary.json
--hub-host <hub>`) instead of `memory-lint.ps1`, which stays as the fallback only while the binary is absent:
both write the same summary, but only the binary's lint fills the G7 field (the PowerShell lint writes `null`).
Tests: a new banner suite runs the real script with a fixture summary (quiet line, alarm line listing the worst
store first, silent when nothing is over trigger, staleness instead of a stale clock, bash syntax);
InstallerParity and RegressionGuards pin the spawner's lint child and its fallback branch. The isolated induced
G2 test (a 32,646 B index written past the gate, floored to 19,896 B by the session-start pass, 130 hooks
harvested first) is recorded in the workspace receipt for P4-1c.

## 1.25.0 — ams-store into the Windows install (register P4-1a)

The Windows installer now installs the store binary and cuts the PC over to it. `2-windows-config.ps1`
downloads `ams-store-windows-amd64.exe` from the GitHub release of the tag `VERSION` names (the new
`release-assets` job in `ci.yml` cross-compiles windows/amd64, linux/amd64 and linux/arm64 on a pushed
`v*` tag, refuses a tag that disagrees with `VERSION`, and attaches the three binaries with `SHA256SUMS`;
every other CI job skips tags), verifies the asset against `SHA256SUMS`, installs it beside the hooks
with a `.sha256` sidecar, and aborts before the receipt and before hook registration when it cannot
(`-BinaryPath` + `-BinarySums` is the offline drop; an offline re-run keeps an installed binary that is
already the tag and matches its sidecar). It then writes the hub transport the seed did by hand: the
`Match host <hub> user ams-hub` ssh block (idempotent, between marker lines), the hub's host key into the
binary's own `<state>/known_hosts`, a history repo on `main` with `hub` as its one remote in the
user@MagicDNS form. It registers `ams-store gate` on PostToolUse over the two legacy markers (the
PowerShell gate is replaced, never duplicated), `ams-store sync --once` at SessionStart (async) and at
SessionEnd, and the maintenance spawner, which now launches the resident watcher (`sync --watch`, one
per PC) instead of the compactor catch-up; and it removes the 5am `ClaudeCode-MemoryCompactor-5am`
task. The sync hooks and the task removal happen only behind a proven hub path (`-HubHost`, inherited
from the receipt; the identity key present; the host key seeded) - a box that cannot prove it keeps
its legacy nightly and the installer says so in red. The receipt records `HubHost`, `AmsStoreTag`,
`AmsStoreSha256` and `AmsStoreSource`; `0-prereqs.ps1` requires git >= 2.38 (parsed, not merely
present); `3-verify.ps1` compares `--version` to the tag, the binary to its sidecar, the hook table to
the binary, runs `sync --once --json` and asserts the compactor task is gone. Pester: the installer
suites are extended (InstallerParity, RegressionGuards, AuthorityResolution) and a new
`AmsStoreInstall.Tests.ps1` runs the checksum parser, the ssh block writer, the known_hosts seeder and
the history-remote initialiser for real against a TestDrive, plus the Q-F scenario that the PowerShell
receipt reader tolerates the Go binary's rows. The PowerShell originals stay deployed until the Phase 5
gate deletes them.

### ams-store: the known_hosts path survives the shell git runs ssh through

The first live push to the hub (the P3-4 seed, minutes after 1.24.0 merged) failed with "No ED25519
host key is known" although the state-root known_hosts held the right key: `gitx.SSHCommand` quoted the
`UserKnownHostsFile` path only when it contained a space, git hands `GIT_SSH_COMMAND` to `sh -c`, and the
shell ate every backslash of the Windows path, so ssh read a file that does not exist. The path is now
always single-quoted (an embedded quote is closed, escaped and reopened), pinned by a test that runs the
emitted option through `sh -c` and asserts the shell hands ssh the exact path - with backslashes, spaces
and a quote. The fleet tests never saw it because their remotes are local paths and ssh never runs. No
runtime or version change; the binary is rebuilt from this commit.

## 1.24.0 — ams-store engines (System A store client, register P3-1/P3-2)

The Go rewrite of the auto-memory store library, write gate and nightly compactor begins
here. This change adds the `ams-store/` module: store enumeration (fail-closed, reparse-point
dedup, OS-gated case folding), the atomic writer, the `gitx` git wrapper (with the
`git >= 2.38` check for `merge-tree --write-tree`), frontmatter + hook harvest, the doctrine
rule, and the index parse/render core with the design's derived order (fixed heading, doctrine
first, commit-time descending, slug tiebreak, always LF). Every verb is a stub (`not
implemented`, exit 64); the engines land in the rows that follow. The 1:1 Pester-counterpart
table is seeded (94 of 95 scenarios named; the removed catch-up-spawn scenario is the one
exemption). Two Go CI jobs added (linux with `-race`, windows build+test). Measurements
(receipt in the workspace): the Go gate spawns ~26x faster than the PS 5.1 gate (~15 ms vs
~397 ms p50); the SessionStart hook order is not a fixed before/after. New system doc
`docs/systems/ams-store.md`. No runtime/version change to the mem0 stack — `VERSION` is
unchanged.

**The engines (this change).** No verb is a stub any more. `derive` and `harvest` (harvest,
the four hygiene passes, planned-ghost abort, blast cap, derived render with the injection
stop, convergence floor, compare-and-swap write); the merge engine (out-of-tree three-way
merge, deletion table, field-aware frontmatter merge, commit-time winner with the machine-id
tiebreak, CRLF normalization, materialize order, deferred queue, liveness); `sync` /
`sync --watch` / `lock` / `gate` / `lint`; and the hub-only `judge-apply` with every
apply-guard and the `Migrated:` trailer.

- **The seams are connected and tested as pairs.** Each engine was built against a one-method
  interface and a fake, which is what keeps the packages independent and also what makes a
  disconnected seam invisible: `gate.Options{Floor: nil}` compiles, ships and passes every
  unit test while the write gate silently becomes an advisory printer. `cli/seams.go` is the
  one place they meet and each adapter carries an end-to-end test driving the real pair.
- **Three duplications collapsed, each of which decided behaviour.** `internal/sync` and
  `internal/merge` both wrote the history repo's `info/exclude` and config, and disagreed
  about whether the shared over-trigger stamp was trackable — whichever ran last won.
  `internal/derive` carried a second copy of the anchor rule. `cli` had two global-flag
  structs, one of which left the state root empty when the home could not be resolved. One
  floor, one doctrine rule, one anchor rule, one repo shape.
- **The empty merge base must be a commit, not a tree.** The first sync between two PCs that
  each ran `git init` before either had pushed works on git 2.55 and fails outright on git
  2.43 (`object ... is a tree, not a commit`). The design's floor is 2.38, so the version
  that refuses is inside the supported range; found by the mandatory Linux `-race` run, which
  is the only place the other git version is exercised.
- **Staging is narrowed to fact files.** The forced pathspec that gets fact files past the
  blanket exclude also tracked whatever else was in the store directory, and the PowerShell
  compactor leaves `.bak-<date>-<kind>` files there; a live sync put several into history on
  their way to the hub and from there into every agent's glob. Nothing is untracked or
  deleted — that is a decision for a human.
- **A store whose whole workspace directory is gone now has its deletion staged**, which is
  the one way a store leaves the fleet; and the history repo pins a repo-local empty
  `core.hooksPath` so a global hooks path aimed at GitHub pushes does not refuse the hub push.
- **The G7 over-trigger clock is written.** `derive` and `sync` stamp
  `.ams/over-trigger.json`; nothing wrote it before, so `over_trigger_hours` was null on every
  PC and the 24 h alarm was inert.
- **The parity gate is a test, not a table in a plan.** It reads the four Pester files,
  extracts all 95 `It` blocks and asserts a Go test of the mapped name exists, with exactly
  one exemption carrying its reason inline. A companion check refuses any remaining
  placeholder skip, because a skipped test is still a test to `go test -list`. Two more repo
  gates: every package that can resolve the operator's home runs its tests with the home
  moved (two live incidents on 2026-09-15 came from tests that did not), and Go source stays
  ASCII.
- **The mutation gate is armed on every rule.** Ten rules carried a name but no mutation and
  were reported rather than checked. Arming them caught one mutation that did not compile (a
  build failure reads as a red test while proving nothing) and one that SURVIVED (it added a
  fetch before the local commit but ignored the error, so the rule was never actually broken).
  The measured figure is in the repair round below; the figure first written here (red 25)
  was never true at that commit.

**The repair round (this change).** A three-lens review of the engines found two severe
defects at the merge/sync seam, four moderate ones and a stale claim in these notes. Each fix
landed with a test seen RED against the unfixed code first.

- **The deferred queue is a pending merge result, not a note.** A queued path was re-staged by
  the next blanket add and re-committed, resurrecting a withheld deletion fleet-wide and
  re-committing a live session's older bytes over a merged blob. Staging now excludes every
  queued path, and materialize reconciles the repository index with `read-tree` first, because
  the merge is computed out of tree and `git commit` was committing the stale index entry for
  exactly the paths the add excluded.
- **The queue is drained.** `ApplyDeferred` had no production caller at all: the queue was
  written and never read. Every `sync --once` pass (and every watcher pass) drains it first,
  for every enumerated workspace, and a pass with queued changes and no drain wired refuses.
  The drain is a RE-CHECK against the ours-side blob recorded at defer time, not a blind
  write: an unchanged file takes the merged result, an edited one keeps the later edit (a
  replace is merged three-way with the disk side winning a real conflict, a deletion is
  abandoned and reported `resurrected`). A corrupt queue is a refusal, never an empty queue.
  The receipt names each withheld change's OP - every one of them used to be reported as
  `replace` - and lists what a drain applied.
- **Staging is two passes over one exclusion list.** The narrowed forced add still may add only
  `*.md`, and a second `git add -u` stages the removal of TRACKED paths of any extension, so a
  `.bak-<date>-<kind>` moved out of a store stops being tracked with stale bytes forever. A
  store whose memory directory is gone is now recognised as gone: the sweep stats the STORE
  directory, since a workspace whose store was deleted is never enumerated and the workspace
  stat kept succeeding.
- **A cleared over-trigger stamp stays cleared.** The G7 clock's reducer was a union over keys
  and a clear is an absence, so a converged store's cleared stamp came straight back from any
  PC that had not re-derived and the alarm could never reset. The file carries a `cleared_at`
  tombstone per workspace; the reducer maxes the tombstones, then mins only the stamps newer
  than their clear. A garbled time keeps the stamp ALIVE, so a bad tombstone cannot silence a
  starvation alarm. One renderer owns the file's bytes now - the producer indented and the
  reducer compacted the same tracked file, so every stamp change cost an extra commit.
- **A network git call that is not hardened does not dial.** The merge engine's `Fetch`/`Push`
  built their commands without the `GIT_SSH_COMMAND` hardening every other call site applies.
  One helper owns that environment and `gitx.Run` now REFUSES any network subcommand whose
  environment lacks it, before the process starts - the single source of truth is structural
  rather than a convention a new call site can forget.
- **`--engage-at <bytes>`** makes decision Q2's engage threshold a flag on `derive` and `gate`,
  defaulting to today's value, so the Phase 4 flip is a change of a default rather than an edit
  to the floor. The gate's own duplicate copy of the threshold reads it too, or a lowered flag
  would have been accepted and then silently skipped.
- **Four dark rows of the deletion table are executed and mutated.** Both-deleted,
  added-on-ours-only, added-on-theirs-only and the identical-bytes short circuit had zero
  executed statements; they now have end-to-end fleet fixtures and their own mutations.
- **Four tests that could not fail, fixed.** The gate's "never touches the network" claim was a
  wall-clock proxy that a real `git fetch` passed; it is a recording ssh stub with a control
  test now. The lock contender test never reached the file-lock branch on Windows (the named
  mutex short-circuited first), so its mutation SURVIVED. Every `cli` verb took the PRODUCTION
  Windows mutex names, so a real compaction - or a sibling test process - turned sixteen tests
  red and others vacuously green; the verbs take an isolatable lock. The placeholder-skip check
  scanned line by line and a gofmt-wrapped `t.Skip` evaded it; it parses the file now.
- **The mutation table is anchored by a test.** A hunk whose anchor text moved made the gate
  report the row broken and nothing automated noticed. A test asserts each hunk's `Old` occurs
  exactly once in its file and that every named test exists; it caught the re-anchoring this
  round itself needed.

Measured mutation gate at `29fbd5a` (windows/amd64, git 2.55.0, the whole table, exit 0):
**red 29, survived 0, broken 0, pending 0** - four rows more than the table had, since the
deletion-table repair added its own.

Two lead findings from the seed recon, after the repair round: history repos on Windows now pin
`core.longpaths=true` (a live projects root holds four workspace directories over MAX_PATH, and a
store under one would have been unstageable), and `derive --dry-run` no longer promises to
"write nothing" - it never touched the store or the dirty marker, and its receipt row, flagged
`dry_run`, is the compactor's contract (lint skips such rows); the cli test pins that shape.

`VERSION` moves to 1.24.0 for the engines. Phase 4 wires the binary into the installer.

## v1.23.5 (2026-09-15) — an explicit empty flag clears an inherited value; prerequisites read correctly over ssh

- **`linux-replica.sh` / `linux-authority.sh`: `--flag ""` clears.** Inherit-on-re-run (v1.23.2–v1.23.4)
  had no way to UNSET a value: re-pointing a replica from a WSL-hosted brain to a native one needed
  `BRAIN_WSL` emptied, and `--brain-wsl ""` inherited the old hop instead. An explicit empty value now
  clears the inherited value and says so; a flag not given at all still inherits.
- **`0-prereqs.ps1` under a non-console session.** `wsl.exe` prints UTF-16, which the default decoder
  renders as NUL-interleaved text that never matches "WSL" (the first remote install read "WSL2
  installed MISSING" on a box with WSL2); the check now decodes it and accepts a clean exit code. The
  Claude CLI check accepts the native installer's `~\.local\bin\claude.exe` and anything on PATH.

## v1.23.4 (2026-09-15) — the last members of both classes, found by an independent seat audit

A clean-context audit of the whole repo (the local 27B seat, two contracts) after v1.23.2/v1.23.3:

- **`linux-replica.sh` inherits `--brain-backup-dir` and `--brain-wsl` from `~/.mem0/replica.env`.**
  The backup dir had a non-empty default persisted into the receipt and never read back, so a
  re-run without the flag rewrote a custom remote backup dir to `~/.mem0/backups`; an omitted
  `--brain-wsl` blanked the WSL hop of a Windows-hosted brain.
- **`stamp-retired-at.py` resolves the authority through `ams_env`** (`MEM0_URL` >
  `~/.mem0/authority-url` > loopback; credential > key file) instead of a bare loopback literal.
- **`1-wsl-services.sh`'s post-install health probe follows `MEM0_BIND`**, and the Windows dream's
  brain-side `/health/deep` line goes through `Get-Mem0AuthorityUrl`.
- Static pins for all three in `test_loopback_probe_pins.py`; replica-installer test for the inherit.
  Left as-is by design: `3-verify.ps1`'s "brain, local authority" check and `restore-replica.*`,
  which probe a WSL brain's / a replica's own local store.

## v1.23.3 (2026-09-14) — the deploy path on a dormant replica; the authority re-run restarts its server

Both found by the v1.23.2 live deploy, both members of the same brain-assumption class.

- **`deploy.sh` on a dormant replica byte-compiles and stops before the import smoke.** `import app`
  opens the Qdrant connection at import time, so the smoke can never pass while a replica's stack
  is dormant; v1.23.2 placed the role gate after it and both replicas stopped there with their
  files already synced. The gate now runs first: a dormant replica gets `py_compile` of the synced
  modules and exits (its real smoke is the `/health/deep` gate `restore-replica` runs when the
  watcher brings it up); a live travel-mode replica still goes through the smoke and restart.
- **`linux-authority.sh` restarts `mem0.service` on every run.** `enable --now` leaves an
  already-running server on the old code: the v1.23.2 re-run stamped `VERSION` 1.23.2 and `/health`
  kept reporting 1.23.1. `restart` also starts an inactive unit, so a first install is unchanged.

## v1.23.2 (2026-09-14) — re-runs inherit every flag; no probe hard-codes loopback

The v1.23.1 tenant fix was one instance of two classes; this release closes both classes.

- **`linux-authority.sh` inherits every optional flag on a re-run**, not only `--user-id`: an
  omitted `--embed-model`, `--eval-root`, `--pcloud-dir` or `--zfs-dataset` keeps the value in
  `~/.mem0/stack.env` (the dataset is now recorded there too; a pre-v1.23.2 box inherits it from
  the installed drop-in). A re-run without `--embed-model` used to revert the embed model to the
  stock name — the exact wrong-conversion defect of Session 3 (searches score noise while
  `/health/deep` stays green) re-created by the installer itself; an omitted `--eval-root` silently
  dropped the drift canary, an omitted `--zfs-dataset` the pool-usage check.
- **`linux-replica.sh` and `linux-client.sh` inherit the tenant** (`stack.env`, else the client
  receipt); only a first install falls back to the login name, which differs from the tenant on
  every native box in this stack.
- **`memory-compact.ps1` posts, reads back and deletes through `Get-Mem0AuthorityUrl`.** Its three
  mem0 calls were the last hard-coded loopback probes under `scripts/windows`: on a replica they
  hit the dormant local store, and during an outage would have migrated facts INTO the disposable
  replica.
- **`deploy.sh` honours the role:** on a replica whose local mem0 is dormant it syncs the files and
  stops — the v1.23.1 deploy on the first demoted box restarted (started) that dormant mem0 and
  health-gated a store nobody reads; a live travel-mode replica is restarted on the new code and
  skips the retrieval-families gate, which judges the authority's store.
- **`deploy.sh`'s health gate and retrieval gate follow `MEM0_BIND`** (same rule as
  `stack-promote.sh` since v1.23.1); **`mem0-canonize.sh`** resolves `MEM0_URL` >
  `~/.mem0/authority-url` > loopback like every chain job, so a hand run on the native authority
  reaches the server.
- **`2-windows-config.ps1` removes a stale loopback user-scope `MEM0_URL` on a replica** (the
  residue of the pre-v1.23 offline watcher, and the second fallback of every hook resolver).
  A remote value is an operator's choice and stays.

## v1.23.1 (2026-09-14) — five defects found live during the first workstation cutover

- **`linux-authority.sh` inherits the tenant on a re-run.** An omitted `--user-id` now takes the
  tenant already in `~/.mem0/stack.env`; only a first install falls back to the login name. The
  cutover re-run without the flag rewrote the tenant to the Linux login and every search ran as
  the wrong user (canaries 0/7 against a healthy store).
- **`3-verify.ps1` timer checks are role-aware.** The WSL installer disables `decay-scan.timer` /
  `stack-backup.timer` on a replica by design; verify now expects that instead of reporting MISSING.
- **`restore-replica.ps1` fails loudly.** Every artifact must be readable from WSL (a streaming
  drive such as pCloud's `P:` passes `Test-Path` but is not mounted in WSL — the script had
  announced the old collection's count as "restored" with nothing restored), and the restored
  point count must equal the set's manifest.
- **The Windows receipt records `AuthoritySsh`** and an omitted flag inherits it, like `AuthorityUrl`.
- **`stack-promote.sh`'s post-promote health check follows `MEM0_BIND`** (the native authority does
  not listen on loopback; the check read "inconclusive" on every rehearsal).

## v1.23.0 (2026-09-14) — Phase 2 code: hooks resolve the per-host authority, queue to the Outbox, canonize on the authority

The workstation half of the System B cutover (register P2-3, P2-7, P2-8; spec §7). Nothing here
moves the authority by itself — the installer run that re-points a box does — but after this
every hook on every box follows that file.

- **Every Windows hook resolves its authority from `~\.mem0\authority-url`** (`Get-Mem0AuthorityUrl`
  in both libraries: file > `MEM0_URL` > loopback, whitelisted), and `2-windows-config.ps1` writes
  that file plus `~\.mem0\role` on the Windows side as mirrors of the WSL files. The SessionStart
  bundle and the session banner follow the same precedence — the banner probed loopback and read
  "still starting" forever on a replica, and the hooks read an env var nothing ever set.
- **A failed hook post is queued, never dead-lettered:** connection failures and retryable
  statuses append an `add` op to the WSL Outbox (the shim's record shape; `replay-ops.py` delivers
  it); deterministic 4xx go to `mem0-post-poison.jsonl`. `mem0-post-failures.jsonl` remains only
  as the fallback for the moment the Outbox itself is unreachable (WSL asleep) and is drained on the
  next run as before.
- **Replica reads fail over to the dormant local store and say so:** the `[MEMORY CONTEXT …]`
  header carries `source=authority:<host:port>` or `source=local-replica` (daemon and inline
  path alike); the SessionStart banner block names its source too.
- **`install.ps1` forwards `-AuthorityUrl` / `-AuthoritySsh` to phase 2 and an explicit `-Role` to the
  WSL phase** (as `MEM0_ROLE`; `wsl.exe -e` passes no environment), so one `install.ps1 -Role replica
  -AuthorityUrl … -AuthoritySsh …` demotes a box on both sides. Without `-Role` the WSL side keeps its
  inherit-never-revert rule.
- **`travel-mode.ps1` / `offline-watcher.ps1` no longer rewrite the user-scope `MEM0_URL`.** The
  hooks' authority file stays pointed at the authority in travel mode, so no hook can post into
  the disposable store.
- **Canonization runs only on the authority (Y7).** `mem0-canonize.sh` refuses to mint a token
  unless `role=brain`; from a replica it forwards its argv over SSH (`BRAIN_SSH` in
  `~/.mem0/replica.env`, written by `install.ps1 -AuthoritySsh` / `linux-replica.sh --brain-ssh`)
  to the new authority-side `ams-canonize.sh` (a transient unit loading both `systemd-creds`
  credentials on a native box); unreachable → a `canonize` Outbox op, executed over SSH at replay
  time with a token minted on the authority and confirmed per fact in
  `canonize-confirmations.jsonl`; the session banner reports queued/drained counts.

## v1.22.3 (2026-09-11) — deploy.sh keeps the native chain units off WSL hosts

`scripts/wsl/deploy.sh` copied every `systemd/*.service|*.timer` — including the `ams-*` units of the
native authority, whose `LoadCredentialEncrypted` lines carry a `__SECRETS_DIR__` sentinel only
`install/linux-authority.sh` resolves — onto whatever host ran it. They are now skipped unless
`MEM0_HOST_KIND=native`; the first workstation deploy of v1.22 would otherwise have left inert,
unresolved units on the WSL brain and the replica.

## v1.22.2 (2026-09-11) — the steps after the backup run unguarded

The first v1.22.1 chain run receipted `syncoid`, `pcloud-copy` and `morning-summary` as guard no-ops: they
carried `--guarded` and their predecessor, `stack-backup`, had just stamped the night. Every step after the
stamping step now runs unguarded (all three are idempotent).
The native Codex transport also parses the single-line `tokens used N` that codex 0.154 prints (the
first live native dream had recorded 0 tokens for three real calls).

**`MEM0_EMBED_MODEL` / `--embed-model`.** The staging authority's canaries scored noise (0.05, against
0.65–0.84 on the workstation) because its stock `embeddinggemma` GGUF is a different conversion of
the model than the one the store was embedded with; the two builds' vectors have a cross-box cosine of
0.01–0.06. The design's "reuse the offload stack's copy" assumption was wrong: the authority keeps the
exact GGUF in its dataset, llama-swap serves it under its own name, and mem0 asks for that name
(`config.EMBEDDER_CONFIG["model"]` from `MEM0_EMBED_MODEL`; `/health/embedder` and `/health/deep` follow it).
The installer refuses when llama-swap does not list the model.

## v1.22.1 (2026-09-11) — the first v1.22 deploy on the authority: l10-audit's key, the pool figure

Two findings from the live re-install. (1) `l10-audit.service` runs on its own timer outside the
chain and had no key credential of its own, so on a native box it exited 1 with "no mem0 API key";
the installer now renders `l10-audit.service.d/native.conf` (`LoadCredentialEncrypted` +
`MEM0_API_KEY_FILE`). (2) `/health/maintenance` read the pool as the root dataset's used/avail,
which subtracts slop space and reservations and reported 85.9 % (alarm) against a `zpool` capacity
of 76 %; the pool figure is now `zpool list -Hp -o allocated,size`, the number every receipt quotes,
and the dataset block keeps the `zfs` view.

## v1.22.0 (2026-09-11) — Phase 1 second half: the nightly jobs in Python on the authority, the whole chain, `cold-embedder`, and the first-nights fixes

The native authority now runs every nightly job itself. Nothing is removed: the PowerShell
originals and the workstation tasks stay until the Phase 5 gate; the staging copy is still discarded
at the end of Phase 1.

- **Python ports (register P1-3).** `scripts/wsl/dream-consolidate.py` (orient → gather →
  consolidate → autopromote → prune → drift canary), `autopromote_lib.py` (the 4C promotion gate and
  the nomination pipeline, with 1:1 twins of the three Pester files), `memory-index-refresh.py`
  (the decoupled index refresh) and `codex_usage.py` + `codex-usage-report.py` (usage report,
  plan-window probe, 25 % reserve gate). Every Codex call goes through the native transport and its
  single-flight lock. On the authority the dream's gather input is the store — the last 36 h of
  evidence plus the recent episodes — because workstation transcripts never reach it by design;
  transcripts that exist locally are appended as before. No catch-up script: the timer's
  `Persistent=` and the boot guard cover a missed night.
- **The whole chain (P1-4).** Fifteen `ams-step-*.service` units in spec order (dream → semantic
  dedup → index refresh → goal recurrence → the five Sunday jobs → stack backup → syncoid → pCloud
  copy → morning summary → health stamp → rtcwake). Each Python step loads the API key as its own
  systemd credential; the codex steps pin `CODEX_HOME` to the secrets dataset; the dream also loads
  the canonical credential so autopromotion signs natively. `ams-step.sh --weekly <Day>` gates the
  weekly jobs; `--guarded` (check-only) sits on every step between the first and the stamping
  stack-backup step, so a boot re-run of a completed night is a chain of receipted no-ops.
  `GET /health/morning-summary` serves the chain's summary to session starts.
- **`cold-embedder` on the workstation side (P1-6).** The bundle daemon names a 503 carrying
  `reason: cold-embedder`, waits the server's `Retry-After` (capped) and retries once; the
  SessionStart hook pre-warms the embedder through the new `GET /health/embedder`.
- **First-nights fixes.** Units render every home-relative path as `%h` (the Linux user and the
  tenant differ on a native box: `l10-audit.service` had failed 203/EXEC); the nftables bind belt
  persists through a root oneshot (`ams-nft.service`); `/health/maintenance` reports the POOL
  (the dataset's quota headroom read 2 % while the pool stood at 78 %) plus a `dataset` block and
  the Codex `usage` window; the boot guard is calendar-aware (an evening hand run no longer voids
  the 03:00 night); `mem0.service` gets `CODEX_HOME`; every chain job resolves the authority URL and
  the key through `ams_env.py` (no `~/.mem0/api-key` exists on the authority);
  `mem0-canonize.sh` signs with the systemd credential first. Installer flags `--eval-root`
  (drift canaries) and `--pcloud-dir`.

## v1.21.2 (2026-09-10) — first live chain run: steps enabled, receipt clock, restore WAL hygiene

Three findings from the first chain run on the native authority. (1) `systemctl start
ams-nightly.target` pulled in no step: `WantedBy=` binds a step only once it is enabled, and the
installer enabled only the timer; it now enables every `ams-step-*.service`. (2) Receipts carried
`duration_ms` in nanoseconds: the uutils `date` on Ubuntu 26.04 ignores `%3N`'s width; `ams-step.sh`
now uses bash's `$EPOCHREALTIME`. (3) `stack-restore.sh` restored `episodic.db` beside a foreign
`-wal`/`-shm` pair left by the already-started server, and SQLite reported the file malformed; the
stale pair is removed before the atomic rename.

## v1.21.1 (2026-09-10) — the key guard accepts a symlinked `~/.mem0`

The first native install refused its own `~/.mem0/canonical-key.dpapi`: the path-traversal guard
resolved the symlink into the data dataset and saw a path outside `$HOME`. The guard now also
judges the lexical (normalised, symlink-preserving) path, so the defaults placed under `$HOME`
by the owner pass while `..` traversals are still collapsed and refused. Test pins a symlinked
home directory. Stamps only otherwise.

## v1.21.0 (2026-09-10) — native Linux authority: installer, judge transport, health, nightly chain (Phase 1, staging)

The memory authority can now be installed natively on an always-on Linux box (no WSL anywhere),
ahead of the cutover described in the ADR `fleet-store-sync-and-linux-authority`. Nothing is
removed and no workstation changes role: this release is proven on a staging copy first.

- **`install/linux-authority.sh`** — mirrors the Linux replica installer (same module / pip /
  Qdrant lists read from the WSL installer, uv-managed Python 3.12), binds the server to the
  tailnet address only (`--bind-ip`, never `0.0.0.0`; `wait-for-bind.sh` as `ExecStartPre`),
  loads both secrets through `systemd-creds` (`LoadCredentialEncrypted` in a native drop-in
  `mem0.service.d/native.conf`, `MEM0_API_KEY_FILE=%d/ams-api-key`), accepts ZFS for Qdrant
  storage, enables only `l10-audit.timer` and `ams-nightly.timer`, and turns every per-job timer
  off. `--render-only <dir>` writes the resolved unit set for inspection; a test greps it for
  `/mnt/c`, `cmd.exe`, `powershell.exe` and the DPAPI fetch.
- **`canonical_key_provider`** gains the `credential` source (`$CREDENTIALS_DIRECTORY`, first in
  the chain) and `api_key_path()`; `app.py` reads the API key through it.
- **Native Codex judge transport** — `MEM0_CODEX_TRANSPORT = shim | native | auto`. The native
  path runs `codex exec` as a subprocess behind the shim client's fail-soft dict and retry loop,
  with a file lock as the single-flight mutex; `usage_limit`, `client_timeout`, `exit_nonzero`,
  `lock_contended` and `no_codex` are its error types. `/health/deep` reports
  `checks.judge_transport`. Every judge consumer inherits it unchanged.
- **One nightly chain** — `ams-nightly.timer` (03:00, `Persistent`, `OnBootSec=15min`) starts
  `ams-nightly.target`; steps attach with `WantedBy=`/`After=` (never `Requires=`) through
  `ams-step.sh`, which receipts every run (`{ts, step, ok, exit, duration_ms, receipt_id, note}`)
  and carries the 20 h boot guard. Steps in this release: stack backup, health stamp, RTC re-arm
  (`ams-rtcwake-arm.sh`; the dream, dedup and index steps follow with their Python ports).
- **`GET /health/maintenance`** — per-step last success / duration / receipt id, stale steps
  (48 h), `judge_transport`, pool usage (`zfs list` when `MEM0_ZFS_DATASET` is set) with the
  85 % alarm, boot ids of the last 7 days. Readers fail soft.
- **Embedder outages are 503 + `Retry-After: 10`** with `reason: cold-embedder` (`embedder_503`),
  so the shim queues the write instead of failing it; 4xx from the embedder is left alone.

Tests: installer (bash in a scratch HOME), key provider, native transport (injected runner), chain
step/guard/rtcwake, maintenance health, 503 classifier and handler — each red first, each with a
mutation proven red. Windows-side scripts are untouched.

## v1.20.21 (2026-09-10) — one compactor per PC, one judge attempt per store per night

The SessionStart catch-up added in v1.20.20 ran once per session start with no cross-instance
lock: four instances hit one store in the same second, `history.git/index.lock` failed, 243
receipts landed in nine hours, and the judge was called 32 times on one store with every result
rejected. Interim relief ahead of the AMS v2 design (ADR fleet-store-sync-and-linux-authority).

- **GUARD 0 — one compactor instance per PC.** A session-local named mutex
  (`Local\ams-memory-compact`) makes every concurrent instance exit at once with a log line and
  no receipt; the survivor does the whole run. The OS releases it if the holder dies.
- **One judge attempt per store per 20 h.** Every receipt now records `judge_called`;
  `Get-AmStoreRunHistory` exposes `LastJudgeUtc`. A store whose judge was called in the last
  20 h gets deterministic hygiene and the floors as before, but the judge call is withheld and a
  store with nothing else to do receipts `skipped-judge-attempted-today` (not productive, does
  not extend `skip_streak`). `-Force` bypasses the window for a hand run. The `-CatchUp` starved
  check applies the same window, so a session start no longer re-runs a store the judge already
  decided today — the sequential half of the storm. `memory-lint` treats the new status as neutral
  (excluded from the `compactor-unproductive` window, never counted as good), so a store that is
  waiting for tomorrow's attempt is not reported as stuck.
- **Receipt ages under pwsh 7 were skewed by the UTC offset.** `ConvertFrom-Json` in pwsh 7
  already yields a `[DateTime]` for `ts`; the `[string]` re-parse dropped the `Z` and read it as
  local time, so `LastProductiveUtc` was 5 h young on this fleet whenever the lib ran under pwsh 7
  (tests, installer). PS 5.1 — the scheduled task — was unaffected. `ConvertTo-AmUtc` handles both.

Tests: GUARD 0 held/free, judge withheld inside the window and called outside it, the catch-up
exclusion, `LastJudgeUtc`/`LastProductiveUtc` under pwsh 7 typing. The seal test ages its
first-run receipt past the window so the second run still calls the judge.

## v1.20.20 (2026-09-08) — a live session can no longer starve a store past the sync limit

Root cause of "MEMORY.md over its load limit" in a live session. The compactor gets one shot a
day at 05:00; its liveness guard skipped one store two nights running (legitimately — a session
wrote memories 33 minutes before the run); the store grew ~3,700 B/day against ~8,000 B of
headroom and crossed the 25,000 B sync limit, at which point the harness stopped syncing it and
every new session loaded a partial index. Three watchdogs stayed quiet, each for its own reason.

- **The throttle stamp is per run, so a skipped store was never retried.** A run that skipped
  this store still marked the stamp because other stores reached a decision, and the SessionStart
  catch-up — the only other chance in the day — exited at every session start. The catch-up is
  now **per store** (`Get-AmStoreRunHistory`): a fresh stamp no longer ends it when a store above
  trigger has reached no decision in 24 h, and the 12 h run throttle no longer re-silences that.
- **The liveness guard escalates at the sync limit.** A skip protects against a lost update,
  which is recoverable in one night; an index the harness refuses to load is broken for everyone.
  At/over the limit the quiet window drops from 30 to 5 minutes, and after two consecutive skips
  the run proceeds and says so (`liveness_override` in the receipt). Under the limit: unchanged.
- **Starvation is reported.** Every receipt carries `skip_streak`. `compactor-silent` keys on
  the receipts *file's* age — and a skip writes a receipt, so a store skipped nightly looked
  alive; `compactor-unproductive` needs three bad receipts in a row, and this one had `applied,
  skipped, skipped`. Lint now raises **`compactor-starved`** (actionable; the heartbeat renders it)
  on two consecutive skips above trigger or one skip at the limit.
- Found, documented, not changed: the write-time gate never fires on this index because its
  matcher is `Write|Edit` and the index is written through Bash/python. Probed directly it works
  (29,630 → 24,906 B, receipted). Widening it costs a `powershell.exe` spawn per shell call.
- Eight tests, boundaries included: under the limit a live session still skips; at the limit a
  1-minute-old write still skips while a 10-minute-old one proceeds; the catch-up runs a starved
  store on a fresh stamp and stays silent when the stamp is backed by a recent decision.

## v1.20.19 (2026-09-07) — the WSL installer now enforces the One-Brain rule too

- **`install/1-wsl-services.sh` no longer enables canonical-mutation units on a replica.** It
  enabled every unit unconditionally, so running it on a replica silently created a SECOND write
  authority: a local `mem0` + `qdrant`, plus the `l10-audit`, `decay-scan` (its `ExecStartPost`
  runs semantic-dedup), `stack-backup`, `goals-stale-sweep`, `contradiction-sweep`,
  `retrieval-pairs`, `episodic-reconcile` and `goal-recurrence-promote` timers — all mutating
  canonical state in a store that box does not own.
  `install/2-windows-config.ps1` has gated its half since v1.16, and the health check already
  reported WSL timers on a replica as brain-only machinery "by design", so the installer
  contradicted both. Every brain unit now goes through `enable_brain_unit`, which installs the
  unit either way (promoting a replica stays a one-liner), and on a replica enables nothing and
  disables whatever an earlier ungated run turned on — the same skip-and-remove the Windows
  installer performs.
- **The installer could not finish on a replica either.** Its service-status readout pipes
  `systemctl is-active` through `sed`, and `is-active` exits non-zero for an inactive unit —
  under the script's `set -eo pipefail` that aborts the run. On a replica every unit in that
  readout is inactive by design, so the gate above would have been followed immediately by a
  failed install: units written, health probes and completion message never reached. The readout
  is now non-fatal, and the probes report `dormant by design` on a replica instead of sending an
  operator to `systemctl status` for a unit that is off on purpose.
- Three guards, all mutation-proven: a static one asserting no brain unit is enabled by an ungated
  `systemctl` call; a **behavioural** one that extracts the helper and runs it against a stub
  `systemctl`, asserting a replica emits `disable --now` and never `enable --now` while a brain
  still enables; and one that runs the real status-readout line against a stub reporting an
  inactive unit, asserting the script survives it. Wording alone would not have caught a gate that
  never matches — that is one of the mutations proven red.

## v1.20.18 (2026-09-07) — a deployed runtime must be able to say which release it is

- **Every installer that deploys the server modules now stamps `VERSION` beside `app.py`.**
  `_resolve_stack_version()` reads that file at import and falls back to the string
  `"unknown"`, so an installer that copied the modules without the stamp produced a runtime
  whose `/health` could not answer the one question a deploy exists to settle. The Linux
  replica shipped exactly that way — both candidate paths absent, `stack: "unknown"` — and the
  WSL installer's fresh-install AND refresh paths had the same gap (only `deploy.sh`, the
  Brain's normal path, stamped it).
  This is not cosmetic. During the v1.20.17 deploy a STALE stamp was the only signal that a
  step had been missed: the server reported 1.20.16 while the repo said 1.20.17, which is what
  led to finding that the installer had never copied the file at all. A runtime that reports
  "unknown" cannot even lie usefully — it just removes the check.
- A guard test asserts the invariant per installer: every module-deploy site must be matched by
  a `VERSION` stamp, so a future deploy path cannot reintroduce an unstamped runtime.

## v1.20.17 (2026-09-07) — provenance in the store, and a cost meter for the decision

Close-out of the model-routing work.

- **`judge_model` on the tier ledger (schema v18).** `actor` is a role label
  ("dream-autopromote", "user-direct") and never said WHAT judged a promotion. Both the
  write-ahead intent row and the completion row now record the model. Additive: the field is
  OPTIONAL, so every pre-v18 row stays valid. It sits deliberately OUTSIDE the signed
  material — the canonical HMAC covers `<ts>|<nonce>|promote|<mid>|<reason>` — so it is an
  audit convenience, never an authorisation input. `mem0-canonize.sh` passes it via
  `JUDGE_MODEL`.
- **`codex exec -o <file>` at the three JSON-parsing call sites.** Scraping the answer out of
  stdout depends on a `codex` marker that is absent when a run produces no assistant message;
  the scrape then returned the metadata header and the caller parsed it as the answer (5 of
  330 live extractor calls). The `-o` file is Codex's own copy of the final message. Stdout
  scraping stays as the fallback and `$null` remains the honest outcome when both are absent.
- **`codex-usage-report.ps1`** — per-job calls, tokens, latency, failures and requested-vs-
  resolved model DRIFT, plus the plan's 7-day window. Built because the NLI write-gate is
  pinned but OFF and the decision to enable it needs measured cost, not a guess.
- **Silent-failure review fixes (folded in before merge).** The reviewer found that the new
  code repeated, in four new places, the very anti-pattern this release exists to remove.
  - The plan-window read cast an unvalidated field to `[int]`. This endpoint is UNOFFICIAL, so
    a renamed field lets the CALL succeed; `[int]$null` is `0`; the report would have stated
    "0% used" - maximum headroom - from a response that carried nothing, and the only
    downstream guard is a `$null` test that a genuine `0` passes. The shape check now lives in
    `Get-CodexPlanWindow`, where it is directly testable, and an unknown reads as unknown.
  - `unparsed` rows got their own column. They are excluded from DRIFT on purpose (an unknown
    is not a mismatch), but folding them into "not drift" meant a codex header-format change
    would turn every row unparsed while drift reported a clean `0` - invisible in the one
    report built to catch silent model change.
  - The compactor wrote NO ledger row when the judge succeeded and returned nothing: `''` is
    falsy, so `if ($raw)` skipped the write entirely. Fixed the same way `l1a-extract.ps1`
    already did it, with `outcome='empty'`.
  - The R-offload producer check could only see literal command/args text, so a wrapper script
    that reaches a producer - the real exposure - downgraded to a WARN. It now follows one hop
    into the script the hook names, says INFERRED rather than claiming proof, and names any
    matcher or file it could not evaluate.
  - A malformed `duration_ms` is now COUNTED (`bad_duration`) rather than dropped. The review
    said such a row would kill the whole report; measuring it showed otherwise — the cast is
    only statement-terminating, so the row is skipped and the run continues. The real defect was
    quieter and worse for being quiet: that row left the latency sample while still counting in
    `calls`, so p50/max described a smaller population than the column beside them claimed.
  - Also: `-o` temp files are cleared on every exit path and swept after 24h (a KILLED task can
    run no cleanup at all, so caller discipline alone cannot bound that directory);
    `New-CodexLastMessagePath` moved inside the compactor's try, so a failure there degrades to
    deterministic hygiene instead of failing the whole store; and the `-o` unreadable-file
    fallback now logs instead of silently reverting every call to the stdout scrape it was built
    to replace.
- **R-offload invariant narrowed to the real exposure.** It used to FAIL on ANY PreToolUse
  matcher that fires for the offload harness, which conflates "a hook fires" with "the harness
  receives the [MEMORY CONTEXT] block". Only a matcher bound to a memory-context PRODUCER can
  route that block; a third-party deny-only guard cannot. Held as a hard FAIL, the old rule
  reported the stack UNHEALTHY for days over another session's delegate guard — which is how a
  standing red light stops being read. A firing foreign matcher is now a WARN that names the
  offender; a producer on a firing matcher still FAILs (proven by mutation). The same change
  fixes a blind spot in the hook lookup: a hook is routinely
  `{command: "node.exe", args: ["…guard.js"]}`, and reading only `command` saw "node.exe".

## v1.20.16 (2026-09-07) — the judge model is pinned per job, and recorded

Every Codex call inherited whatever `~/.codex/config.toml` named. A config edit on 2026-09-07
moved the whole stack onto `gpt-6-astra` and nothing recorded it: no receipt, log line or ledger
row could say which model had judged a memory.

- **Per-job model routing.** `Invoke-CodexSubagent -Model` (and a `model` field on the shim's
  `/judge` request, allowlisted, shim `0.27.1` → `0.28.0`). Synthesis and consequence run on
  `gpt-6-astra` at **medium** effort (operator directive); bounded extraction, classification and
  routing run on `gpt-5.6-terra`. A guard test fails the build if a call site forgets `-Model`.
  Routing table and rationale: `docs/systems/codex-hooks.md`.
- **Provenance.** New `Parse-CodexHeader` reads the RESOLVED model and effort out of Codex's own
  stdout header (verified against codex-cli 0.153.4), and the usage ledger gained
  `model_requested`, `effort_requested`, `model_resolved`, `effort_resolved` and a closed
  `outcome` enum. `memory-compact.ps1` and `autopromote-lib.ps1` called the usage logger ZERO
  times and are now instrumented, as are the dream's abort paths and L1a's parse-failure exit.
- **Fixed: the promotion phase had been nominating nothing.** `Extract-JsonFromText` discarded a
  bare top-level array, and `'[]' | ConvertFrom-Json` yields nothing in PowerShell, so the empty
  list was invisible. `dream.log` recorded "autopromote: bad Codex JSON (promoting nothing): []"
  on 2026-09-03 and 09-07.
- **Fixed: header-only replies were parsed as answers.** `Get-CodexResponseText` returned the raw
  metadata header when Codex emitted no assistant message; it now returns `$null` so the caller
  records `outcome='parse_fail'` (5 of 330 live L1a calls).
- **Fixed: a live lock holder could be robbed.** `Acquire-CodexLock` reclaimed on age alone even
  with the holder alive; age now only applies when no PID can be read.
- Timeouts and ceilings sized to the work: L1a 60→90s (observed max 62.4s), Astra phases →240s,
  gate 90→180s, sweep 45→60s, dream lock 30→45 min, dream task limit 15→40 min, compactor task
  20→30 min (its own lock window was already 30).
- `CODEX_JUDGE_IDENTITY` bumped (`…effort-low:v1` → `codex-cli:terra:effort-low:v2`): the 30-day
  verdict cache was not bumped when the model changed, so stale verdicts would have survived.
- Stale `gpt-5.5` references replaced across docs and comments with the job's role.

## v1.20.15 (2026-09-06) — the compactor runs every night, and converges on lines

- Compactor throttle 23h → 12h: a daytime hand run marked the throttle and the next 05:00 run
  skipped itself (one silent night, 2026-09-04).
- `memory-compact.ps1 -CatchUp`, launched by the SessionStart spawner: runs the nightly only
  when the newest receipt is older than 24h. The box was off at 05:00 on 2026-09-05 and the
  scheduler's missed-start retry refused ("user not logged on"); nothing re-ran the job.
- Line floor: when an index is over its line trigger, the oldest pullable facts the judge did
  not migrate are migrated deterministically (write-then-verify, blast cap, doctrine excluded)
  until the store is back at its line target; receipts carry `line_floored`. A store had
  climbed to 174 lines while every nightly migrated 0.
- Orphan re-index synthesizes the hook from a frontmatter-less file's first line of prose
  instead of "recovered orphan; no description".
- Doctrine now includes attributed statements (`Owner: …`): the first live line-floor run
  migrated an operator rule typed `project` with no imperative verb. Migration failures log the
  server's answer instead of a bare "returned no id".

## v1.20.14 (2026-09-03) — Linux replica role

- New `install/linux-replica.sh`: a native-Linux box becomes a replica — thin client plus a
  dormant local Qdrant + mem0 (user units installed, disabled) and a 2-minute
  `offline-watcher.timer`. Server module list, Qdrant version and pip line are read from
  `install/1-wsl-services.sh` at run time (one owner). Ends with a first restore as the proof.
- Qdrant storage is backed by a loop-mounted ext4 image when the home filesystem is not
  ext4/xfs/btrfs/tmpfs: Qdrant 1.18.2's snapshot restore fails on f2fs (verified live; tmpfs
  and ext4 restore the same snapshot).
- New `scripts/travel/restore-replica.sh`: pulls the Brain's newest complete snapshot set over
  SSH (through `wsl.exe` for a Windows+WSL Brain), size-verified and cached, restores it via
  the Qdrant snapshot upload API, replaces the ledgers, requires `/health/deep` through the
  local embedder, stamps `~/.mem0/replica-restored`. Carries the One-Brain guard (role must be
  `replica`, authority must be remote).
- New `scripts/travel/offline-watcher.py`: the PowerShell watcher's state machine and
  transitions on Linux, plus one fix — the replica is refreshed while ONLINE (last restore
  >24 h), not at `go_offline` when the Brain is unreachable by definition.

## v1.20.13 (2026-09-03) — Linux thin client

- New `install/linux-client.sh`: a native-Linux box with no WSL and no local store can now use a
  remote Brain. It deploys the MCP shim and its sibling replay driver into a small venv, writes
  the per-host authority/role/key files (`role=client`), registers the `mem0` MCP server in
  Claude Code, appends the CLAUDE.md tier protocol, and proves the install with a real MCP
  session calling `memory_health` against the authority. A loopback authority is refused;
  the `__WSL_USER__` tenant sentinel is resolved to `--user-id` as on Windows.
- `replay-ops.py`'s One-Brain refusal (never replay an Outbox into loopback) now covers the
  `client` role as well as `replica`.

## v1.20.12 (2026-09-03) — hygiene on every store; the liveness row measures live

- Deterministic hygiene (orphan re-index, dangling/duplicate-slug removal) now runs nightly on
  EVERY populated store; only stores over the size trigger go on to the judge, migrations and the
  floor. A small store had carried 7 orphaned facts for days while the lint reported them every
  session and nothing ever fixed them. Clean below-trigger stores write no receipt.
- `Test-MemoryStack`'s maintenance-liveness row measures the live stores instead of the
  SessionStart lint snapshot, which kept a stale size for hours after a remediation.

## v1.20.11 (2026-09-03) — auto-memory: converge under the sync limit, or say so

Live failure: the AI-Ecosystem index reached 27.4 KB with 126 of 180 lines over the cap and the
harness loaded only part of it in another session. The compactor had run nightly and stamped
`applied` while leaving the store at 25,219 B (judge-driven shortening converges ~13 lines a
night, "KEEP is the safe default"), skipped the wake-up catch-up night entirely because dream and
dedup held the codex lock, and retried three 413-oversized migrations forever; the write-time
lint was advisory and ignored.

- Shared deterministic **convergence floor** (`Invoke-AmConvergenceFloor`): at/over the sync
  limit, the longest non-doctrine hooks are truncated to the line cap until the index is under the
  trigger. Doctrine is never touched.
- Compactor: floor runs after the judge (with or without it); a held codex lock skips only the
  judge; bodies over the server cap are never migration candidates; `applied-unconverged` /
  `unconverged` statuses with **exit 1**; receipt gains `floored`.
- Write-time gate: `memory-index-write-gate.ps1` (PS 5.1, Windows-native) replaces the bash
  advisory on PostToolUse — same advisory, plus in-place normalization at the sync limit behind a
  content-hash CAS, receipted.
- `Test-MemoryStack`: the maintenance-liveness row is red when any store is at/over the sync limit
  now or the latest receipt is unconverged; R9 tracks the gate.

## v1.20.10 (2026-09-02) — SessionStart banner fires on open, not on every resume

A context audit over 76 transcripts found the `[agentic-memory-stack]` / `[heartbeat]` /
`[storage-cap]` orientation banner re-emitted on every session *resume* (308 resume fires vs
225 startups, ~1.3 KB each) — the single largest routine SessionStart repetition, because the
installer registered the hook with no matcher. It now registers `startup|clear|compact`:
a fresh or cleared session gets its orientation, a compaction re-reads it into the rebuilt
context, a resume already has it. Pinned by a regression guard so a hand edit to
settings.json is never the only copy.

Same audit, second-largest class: the resident daemon's HK-5 dedupe re-injected unchanged open
goals/questions every 12th prompt; the cadence is now every 25th (the re-inject exists only
as a post-compaction guard, which 25 still serves). Pinned by a regression guard.

## v1.20.9 (2026-09-01) — installer: a WSL path that isn't one is refused, not recorded

The drift guard died silently for 4 nights: an operator ran the Windows config phase from
Git Bash, whose MSYS path conversion rewrote `-EvalRootWsl /mnt/...` into
`C:/Program Files/Git/mnt/...` before pwsh ever saw it. The installer recorded it unchecked;
the dream's drift snapshot became `python C:/Program Files/...` (bash split at the space,
exit 2 every night, "no false alarm" skip every night) while the liveness row kept reading
the stale state sidecar as "guard alive" — only the capability manifest's age check
eventually surfaced it. Two defenses, both keyed on the same form check:

- `2-windows-config.ps1` refuses a resolved `EvalRootWsl` (explicit or inherited) that is
  not an absolute POSIX path, with an error naming MSYS conversion and the
  `MSYS_NO_PATHCONV=1` fix — a poisoned receipt can neither be written nor survive a re-run.
- `Test-MemoryStack.ps1`'s drift-guard liveness row FAILs on a malformed receipt value
  before consulting the state sidecar, so an already-poisoned box alarms on the next health
  run instead of after four quiet nights.

## v1.20.8 (2026-09-01) — outbox: a stopped drain must resume, and never rewind a record

Found live: during an embedder-contention window (llama-swap 429 → server 503) a session's
writes queued to the outbox; the drain stopped on the retryable 503 — correctly keeping the
op — but kept it in `outbox.replaying.jsonl`, which the shim's session-start drain trigger
never looked at. The op sat stranded 15 hours across many session starts while
`outbox_depth` read None. Worse, the op was an update whose target the session had already
re-updated directly: a blind replay would have regressed the record to the older draft.

- shim `_drain_outbox_async`: triggers on a non-empty `outbox.replaying.jsonl` too
  (`replay-ops.py` has always resumed it; only the trigger was blind).
- `replay-ops.py`: superseded-update guard — before dispatching an update, compare the
  record's `updated_at` to the op's `queued_ts`; ops the world moved past go to
  `mutation-conflicts.jsonl` with reason `superseded-by-newer-write` (preserved, never
  dispatched, never dropped). Fail-open for legacy ops without `queued_ts` and on GET
  failures — fail-closed would recreate the stranded class.
- `job_liveness`: `outbox_depth` counts both queue files, so a stranded backlog is visible
  to the offline-outbox capability row instead of reading "unknown".

## v1.20.7 (2026-09-01) — server: no record is born without a tier

A record added without `metadata.tier` (a path the add endpoint's own 403 guidance
recommended) was stored tier-less — and `fetch_current_tier` fail-closes an absent tier to
`canonical` (the H1-race shield), so every mutation of that record demanded the user-direct
HMAC. An agent could create a memory it could never correct or delete; 127 such points
existed live, including a malformed add whose metadata parameter block leaked into the
memory text. `POST /v1/memories` now defaults the tier to `evidence` at birth (after the
canonical/insight gates, before hash-dedup), a live test pins born-tier + deletability, and
`scripts/wsl/tier-backfill.py` stamps the existing stock — skipping and reporting any id the
tier ledgers ever named with canonical/insight history rather than silently demoting it.

## v1.20.6 (2026-08-31) — installer: four fresh-install gaps, all silent

Four bugs filed against a fresh install, each invisible on a long-lived box because the
missing piece had been hand-installed or the failure exited 0:

- **fastmcp was never declared.** The MCP shim runs on the server venv's python and imports
  `fastmcp`, but neither `requirements.txt` nor either installer pip line carried it — a
  fresh install produced an MCP that only ever said "Failed to connect". Now in the floors
  file, both pip branches, and the installer's import-gating post-condition; a regression
  guard pins all three.
- **jq was required but not a prerequisite, and the backup swallowed its absence.** The
  nightly backup parses the Qdrant snapshot name with `jq`; without it the parse was empty
  and the block printed a WARN and exited 0 — the vector collection silently absent from
  every backup while the run reported success. jq is now a phase-0 prerequisite check, and
  every skip/failure path in the Qdrant block sets rc=1 like the local-file blocks always did.
- **3s health probes raced.** `3-verify.ps1`'s Qdrant/mem0/authority liveness probes used a
  single `-TimeoutSec 3` attempt and reported false MISSING right after wsl.exe activity
  while the round-trip check passed in the same run (the search leg was hardened 2026-07-25;
  these were the same defect one section up). Probes now retry once with a 10s timeout.
- **PowerShell platform truth.** `3-verify.ps1` was BOM-less UTF-8 with em-dashes and no
  `#Requires` while the docs promised "PowerShell 5.1+" — under 5.1 it parse-dies mid-file.
  Decision: the installer standardizes on pwsh 7. Phases 2–3 now carry a UTF-8 BOM (so 5.1
  parses them) plus `#Requires -Version 7` (so 5.1 refuses cleanly), phase 0 checks pwsh is
  present, and README/skill docs state the real contract: pwsh 7 for the installer, the
  built-in 5.1 for the deployed hooks.

## v1.20.5 (2026-08-28) — health: a replica is checked against the brain it uses

`Test-MemoryStack.ps1` probed loopback for every mem0/Qdrant row, so the first replica it ran
on reported 14 permanent FAILs for services a replica deliberately keeps dormant. It now
resolves the memory authority the way `3-verify.ps1` does (`~/.mem0/authority-url`, then the
receipt, then loopback) and every shared-store row targets it; on a replica the mutation
probes are skipped (server invariants the brain proves daily; they would also need a
canonical key the replica does not serve), brain-only machinery reports "by design", the
dream/dedup task rows flip polarity (present on a replica = FAIL), and a new `memory
authority (one-brain)` row FAILs a replica pointed at itself. The brain path is unchanged.
Regression guards pin the single loopback literal and the role gates. Proof: the laptop-node
replica went 14 FAIL → 0 FAIL (46 PASS, 2 genuine WARNs); the reference-workstation brain run is unchanged.

## v1.20.4 (2026-08-28) — installer: the replica fix, fixed for replicas

v1.20.3 defined the shared `$taskUserId` *inside* the brain-role branch. A replica skips that
branch, so the compactor registration (every role, after the gate) received a null `UserId`
and the replica deploy failed again — while the brain deploy passed, and the pre-merge live
probe had exercised the principal expression rather than the installer's control flow. The
definition now precedes the role gate, and the parity test asserts that ordering. The proof
this time is the installer itself completing on the replica.

## v1.20.3 (2026-08-28) — installer: task principals resolve on workgroup boxes

Deploying v1.20.2 to a replica box failed at the compactor task: `Register-ScheduledTask`
returned "No mapping between account names and security IDs". The installer built every
task principal as `$env:USERDOMAIN\$env:USERNAME`, and on a workgroup machine USERDOMAIN is
the literal `WORKGROUP`, which has no SID. The brain-only dream/dedup registrations carried
the same latent bug; the one brain box happened to have a matching USERDOMAIN. All three now
use `WindowsIdentity.GetCurrent().Name`, which resolves on domain, workgroup and
Microsoft-account boxes alike. A parity test fails if `USERDOMAIN` reappears in a principal.

## v1.20.2 (2026-08-26) — auto-memory: a migration is never a no-op

Found by a receipt-fidelity test written after the v1.20.1 live run showed a blank
"original line" for a re-indexed orphan. The test exposed something worse than a blank
field: an orphan that hygiene re-indexes and the judge then migrates leaves the index
text byte-identical to before, so the run reported `no-op` — while the migration write
had been made and verified, the orphan file stayed on disk, and no receipt row named the
corpus id (`migrated=1` beside `status=no-op`). The same unnamed-record class the v1.20.1
review closed on the abort paths, one exit path further along. A run with verified
migrations pending now always proceeds through write → verify → delete → receipt, and a
constructed index line carries itself into the receipt.

## v1.20.1 (2026-08-26) — auto-memory: the fix round reviewed

The operator asked for an adversarial review of the v1.20.0 fix round itself, and it found
what this stack's own notes predict: a fix applied literally recreated the bug class.

**Critical.** The "delete fact files only after the index write" fix placed the delete
between the write and the post-write invariant check. An invariant failure then restored
the pre-run index — which still listed the just-deleted facts — while the receipt reported
`lost=0`. The new reachability rule ("linked from ANY line") simultaneously created ghosts
that hygiene could not repair (a file name mentioned in a heading or a prose note), so the
invariant failed every night. Reproduced end-to-end: up to five files deleted per night,
index restored onto them, forever. Fixed by ordering — write, verify invariants, THEN
delete — and by deciding ghosts from entry links only, checked *before* the judge runs, so
an unrepairable store aborts with nothing written and nothing posted.

**High.** A compare-and-swap abort left verified migration records in the corpus that no
receipt named (now undone, unless the server reported the id as a pre-existing dedup hit —
those are never deleted, which closes the second finding: the shared write helper discarded
the `deduplicated` flag, so an unverifiable write could have deleted an L1a fact or an
earlier migration). The compactor now performs its own migration POST and reads the flag.
An enumeration failure *after* the write now reports `applied-unverified` and deletes
nothing instead of collapsing into a generic error.

**Medium.** The banner staleness guard was inert on Python 3.10 (seven fractional digits);
the unproductive-compactor finding was fleet-size gated by a 40-line tail; the widened
entry regex accepted a checkbox line as a pointer and fenced examples as entries (now:
fenced lines are text, and ambiguous lines are never removed and never ghosts); a
dead-extra-link repair rejected any line that also carried a live extra link; byte
truncation could split a surrogate pair; an empty seal file read as "no seals"; a locked
temp file was swallowed; `-Workspace` with a typo silently rehearsed nothing.

Tests: +7 library, +7 compactor scenarios, boundary assertion on the blast cap. Live
verification at the deployed config: real scheduled-task start (`LastTaskResult 0`, live
store correctly skipped), hook through its registered `wsl.exe` command with stdin, lint
through its real PS 5.1 spawn, banner rendered, exit codes propagate through `run-hidden.vbs`.

## v1.20.0 (2026-08-26) — auto-memory maintenance

The coding-agent harness keeps its own per-workspace file memory — an index of one-line
pointers, injected in full at every session start, plus one fact file per pointer. Nothing
in this stack maintained it. A live store was found at 96% of its hard per-file limit, with
an unindexed fact file no session had ever loaded and an index line pointing at a deleted
file; no job existed that would ever have noticed. This release makes those stores
self-maintaining, in three pillars.

**Lint** (`memory-lint.ps1`, spawned at session start, read-only, 6h throttle): enumerates
every populated store — deduplicating alias directories by canonical path — and recomputes
findings from disk: orphan, dangling link, duplicate slug, over-long line, oversized fact
file, missing frontmatter, near or over a budget. Stateless by design: the finding set is a
handful of items recomputable in milliseconds, and a monotone watermark would have silently
suppressed a defect that was fixed and later recurred. Two findings watch the maintainer
itself — a store above trigger with no run receipt in 48h, and a history repo that has
gained a remote.

**Write-time lint** (`memory-index-write-lint.sh`, PostToolUse on Write/Edit): the harness
warns on its *line* cap, but nothing checked *bytes per line* — which is what fills the byte
budget first (the store above was at 64% of the line cap and 96% of the byte cap). The hook
reports an over-long index line to the agent that just wrote it, in the same turn, so the
bloat is fixed at the source instead of being compacted forever. Advisory; always exits 0.

**Compaction** (`memory-compact.ps1`, new 5:00am task, every role — these stores are
machine-local, unlike the shared corpus): fires at 20,000 B or 160 lines, targets below
17,000 B and 140 lines. Deterministic hygiene first (dangling and duplicate lines removed,
orphans re-indexed from their own frontmatter), then one judge call over the *delta only* —
long lines and migration candidates, never the whole index.

Five guards, one behavioural test each, written so that removing the guard fails the test:

- **Liveness gate + compare-and-swap.** No process locks the index, and a box that sleeps
  runs its catch-up at the next logon — exactly when sessions start. Observed during the
  build: a store grew three entries mid-flight. The job skips a workspace with recent session
  activity, and re-reads the index hash and file set immediately before the swap, aborting on
  any drift. Abort, never roll back: a directory-level revert would clobber the live write.
- **Doctrine is untouchable.** `metadata.type: feedback` is *nested* — a top-level match finds
  nothing, which would have made the rule inert and every standing order eligible for deletion.
  Doctrine is classified deterministically and never even offered to the judge.
- **Strict decrease, seal, blast cap.** A judge edit applies only if it strictly shrinks the
  index past the hygiene baseline (hygiene is correctness and is exempt); each line may be
  rewritten by the judge at most once, ever; no run removes more than a fifth of the lines.
  A rewritten hook must retain an anchor token, so a line cannot be reduced to a label that
  no longer says when to open the file.
- **Write-then-verify migration.** A migrated fact is posted verbatim, tagged
  `source: automemory:<workspace>/<file>`, and read back **by id** with byte equality before
  its line and file are removed. A write returning no id counts as unverifiable and the line
  stays. Verification by semantic search was rejected: ranking top for its own text can be
  satisfied by a pre-existing near-duplicate.
- **Feasibility.** If doctrine alone exceeds the target budget, the job stops and reports
  rather than loosening the hard rule.

Supporting changes: `semantic-dedup.py` now protects auto-memory migrations — it deletes the
newer of a near-duplicate pair, and a migration is always the newer side, so an unguarded run
would have evicted a just-verified fact the next morning; two migrations delete neither, and
canonical still wins. History is a local git repository with its git-dir outside the tree and
no remote, replacing a hand-rolled archive: commits are the audit trail, per-file checkout is
the undo, and lint fails if a remote ever appears. All maintainer state lives outside the
store directory — an in-store archive would have resurfaced removed facts in every agent
search and re-exposed the credential-bearing file that started this work. Two health-check
rows added: store budgets and structural cleanliness (invariants), and maintainer liveness
(recovery) — a registered task proves nothing if it never fires.

## v1.19.0 (2026-08-08) — the hardening-program waves

Waves W1–W5 of the audit-driven hardening program (55 adjudicated findings; see the
audit register). W1: the verification spine — launch-path parity gates, deploy
pre-flight, behaviour-verified fixes on the LAUNCH PATH rather than the repo. W2:
PUT payload carry-over (atomic pre-merge, per-record locks), CP437 mojibake repair
across four stores, the BM25 sparse leg revived with a gating /health/deep canary.
W3: alarm delivery legs — capability manifest, job-liveness surface, drift-guard
cross-run legs, SessionStart heartbeat digest. W4: revive-or-bury — redaction rule
set fixed and widened under one three-runtime fixture, the Codex judgment leg
live-proven after two dead dependencies, the one-brain guard made real, DPAPI docs
truth-pass. W5: retrieval observability (`explain`, `POST /v1/memories/diagnose` +
`memory_diagnose`, `rerank_status`), gap annotations (withheld-family counters +
recall age summary + conditional staleness line), the keyword-recall union leg
(AMS-56: fail-closed on rerank, deliberate path only), per-pair judge cache +
retrieval-pair dry-run, real-query replay harness + deploy-gated retrieval
families, count-only entrance-redaction telemetry, and sparse-leg reboot survival
(durable fastembed cache + bounded sentinel self-heal + pre-reboot cache gate).
Note: the redaction rule set has a fourth copy in SkillOpt on the offline replica —
it adopts the shared fixture on that box's next return.

## v1.18.0 (2026-07-25) — the silent-failure week

Twenty PRs repairing a family of defects that shared one trait: **something stopped working and
nothing said so.** Every one was found by hand or by audit, never by an alarm, because each failed
into a shape indistinguishable from "nothing to do".

### Outages fixed

- **Memory injection was dead on every prompt** (~1000 recorded failures). Claude Code passes a
  hook command with no `args` array to Git Bash, where an unquoted backslash is an escape
  character, so the client's absolute path was shredded and the hook exited 127 — silently. Hooks
  registered *with* an `args` array are exec'd directly and kept working, so the event looked
  healthy throughout.
- **Episodic capture was dead for 9 days**, from two independent causes at once: the hook launcher
  was pinned to a version-stamped WindowsApps PowerShell path that Windows deletes on update, and
  the command strings carried the backslash bug above. Fixing either alone left it dead.
- **The weekly contradiction sweep had never judged anything** in the deployed layout — its
  `sys.path` resolved correctly in the repo but to a non-existent directory once deployed, so the
  Codex bridge import failed and the run exited 0 every week.
- **A replica silently queued every write to the Outbox.** The MCP shim read its authority from an
  environment variable, but `wsl.exe -e` execs directly (no login shell, no `WSLENV`
  pass-through), so the value never arrived and the shim fell back to a dead loopback.
- **The offsite backup was deleting archives.** `robocopy /MIR` mirrors, so every source-side
  retention prune destroyed the offsite copy too; 688 MB existed only offsite and was hours from
  being purged.

### Systemic fixes

- Authority resolution is a per-host file (`~/.mem0/authority-url`), read identically by the shim,
  `replay-ops`, the SessionStart bundle and the offline watcher. The Outbox drains at shim
  startup, and a replica refuses to replay into its own disposable store (One-Brain Rule).
- Throttle arithmetic uses a shell-independent epoch helper: PowerShell 5.1's
  `Get-Date -UFormat %s` is offset by the machine's UTC offset while pwsh 7 is correct, and both
  editions write the same state files.
- The nightly dream throttle is 23h, not 24h — the stamp is written at cycle completion, so a
  strict 24h window against a fixed 03:00 trigger made the dream run every *other* night.
- Installer values **inherit** rather than silently revert: omitting `-AuthorityUrl` or
  `-EvalRootWsl` on a re-run keeps what the box already had.
- Scheduled tasks run windowless through a `wscript` shim and register `Hidden`.
- Verifiers stopped crying wolf — role-aware checks (a replica's local store is *designed* to be
  down while online), a retry-hardened round-trip, and probe timeouts that report a slow CPU model
  as WARN rather than FAIL.

### Guards, so these classes cannot recur silently

- `3-verify` fails when any hook command carries an unquoted backslash path — the check that would
  have caught both hook outages on day one.
- `RegressionGuards.Tests.ps1` pins the throttle constant *and* its behaviour, the epoch helper,
  receipt inheritance, and the bash-safe hook builders. Mutation-tested: reverting each fix turns
  it red.
- The missing-bridge failure is receipt-gated — quiet on a fresh or partial deploy, loud on a box
  that completed an install.
- `check-docs.py` enumerates via `git ls-files`, so local scratch files no longer trip the gate
  while CI behaviour is unchanged.

### Restored from the carve

`_debris_patterns.py` + `conftest.py` (89 live-stack tests could not even be collected) and
`Run-PesterTests.ps1` (documented but never published; two defects fixed in the port — unquoted
`Start-Process -ArgumentList` elements broke on any path containing a space, and a locale-specific
module path).

## v1.17.0 (2026-07-18) — repo-local documentation system

A durable, repo-local documentation system for humans and AI agents, reviewed alongside code.

- **Taxonomy** under `docs/`: `systems/` (per-component deep-dives, renamed from `modular/`),
  `flows/` (cross-system pipeline walkthroughs), `architecture/` (long-lived constraints +
  `decisions/` ADRs), `glossary.md`, and `templates/`. `CLAUDE.md` gains a Documentation map
  and the agent workflow; `AGENTS.md` stays a one-line import shim so the guidance can't drift.
- **Six system docs** and **six flow docs** brought to a shared template with verified source
  maps; a **26-term glossary**; **nine seeded ADRs** recording the load-bearing decisions
  (one-brain rule, fail-open hooks, EmbeddingGemma on llama-swap, Codex as judge/extractor,
  the tier trust model, operator-agnostic sentinels, the offline-first supersession of travel
  mode, and public-repo-primary).
- **Docs gate** (`scripts/ci/check-docs.py`, a new 7th CI job): every relative doc link
  resolves to a real file, no operator-specific value leaks into docs, and every ADR carries
  valid frontmatter (`status`/`date`; `superseded_by` iff `Superseded`).
- The **docs-and-code-must-agree** rule is now explicit: every pull request that changes
  behavior, interfaces, security, data, or operational procedures updates the affected
  documentation in the same change.
- The `.claude-plugin/*` manifests are realigned to the release version (they had drifted
  to 1.15.0).

## v1.16.2 (2026-07-17) — operator-neutral test fixtures + suite repairs

- 25 test files neutralized for the public ship (fixtures self-referential; behavior
  preserved). The PII leak-guard tests now read operator-specific patterns from gitignored
  `scripts/windows/tests/pii-patterns.local.txt` (`.example` ships).
- 4 silently-broken tests repaired: Qdrant byte-body mock discriminators (broken since
  v1.12's UTF-8-bytes fix), the offload-invariant test brought to the 2026-07-14 audited
  semantics, and cwd/hostname-dependent fixtures made hermetic. Full Windows suite 459/0.
- Unit-drift commit-back: `decay-scan.service` ships with the destructive dedup
  `ExecStartPost` DISABLED (2026-07-14 audit), `stack-backup.timer` is DAILY (feeds the
  offline-first replica snapshot), and `mem0.service`'s bind address is operator config
  (`__MEM0_BIND__` ← `MEM0_BIND` in `~/.mem0/stack.env`, default loopback).

## v1.16.0/1 (2026-07-17) — deploy-layer-skew hardening

- **Fail-open PreCompact**: the capture hook command is `python3 … || true` — a missing or
  erroring capture script can never hard-block compaction (exit 2 deadlocked live sessions
  when a config-repo untrack+pull deleted a box's deployed script layer).
- **Distro-agnostic hook emission**: no `-d <distro>` when the stack's distro is the WSL
  default, so a machine-synced `settings.json` stays portable.
- **One-brain role gate**: `-Role brain|replica` (receipt-recorded); replicas never register
  the nightly dream/dedup canonical-mutation tasks and remove stale ones. Role-aware verify.
- **Skew guard**: `3-verify.ps1` asserts every hook-referenced deployed script exists.
- Installer is pwsh-only (loud pre-flight); brands.json privacy split
  (`brands.example.json` template + installer fallback).

## v1.15.0 (2026-07-16) — offline-first memory client

Offline behavior EMERGES from connectivity: reads fail over to a local read-only replica,
mutations queue to an operation-outbox replayed to the authority on reconnect. The replica
can never absorb a write; divergence is impossible by construction.

## Earlier

v0.12 → v1.14: the memory stack's build-out (mem0 + Qdrant + EmbeddingGemma on llama-swap,
hook pipeline, dream consolidator, tier governance, promotion gate, travel mode). See the
docs/ runbooks for the operational history.
