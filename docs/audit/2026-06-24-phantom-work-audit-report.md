# Phantom-Work Audit — Measured Report (2026-06-24)

**Method:** read-only, evidence-on-output-not-wiring. 33 components across 8 groups, each verified against its live artifact (the `~/.mem0/*.jsonl` ledgers, Qdrant/mem0 HTTP, dream state, hook logs), then **every WORKS/PARTIAL verdict independently re-verified by a second skeptic agent with default-downgrade** so the audit could not itself mistake wiring for output. 37 agents, ~3.3M tokens, ~55 min. Plan: `docs/superpowers/plans/2026-06-24-phantom-work-audit.md`.

## Headline

| Class | Count | |
|---|---|---|
| **WORKS** | 19 | produces its artifact (or is correctly idle with no input) |
| **PARTIAL** | 9 | runs but degraded/stale, or an instrument nothing consumes |
| **WIRED-ONLY (phantom)** | 5 | machinery fires/logs, the product step never executes |
| **DEAD** | 0 | |
| **TOTAL** | 33 | **phantom ratio = 5/33 = 15.2%** (1 verdict downgraded on re-verify) |

**15.2% hard-phantom — well under the "90%" estimate.** But the audit found something more important and more actionable than the ratio:

> ### 🔴 THE CRITICAL FINDING: the capture/ingest path has been DEAD since 2026-06-16.
> The system has stored **no new memory in 8 days**. Every retrieval/storage/dream/governance layer below is healthy — but they have all been operating on a **frozen corpus**. This is why everything you experience is stale (the SessionStart resume surfacing Jun-16 decisions, no new captures), and it is a concrete dated regression, not "low leverage."

The broken-capture cluster (all stopped ~Jun 16, despite active sessions on Jun 22 and Jun 24):
- **A1 — L1a extractor: WIRED-ONLY.** Newest `source=l1a-extractor` record in Qdrant = `2026-06-16T13:52Z`; **zero** in the last 7 days. `l1a.log` last extraction `2026-06-16 08:52`; `last-l1a` marker frozen at `2026-06-16`; Stop-hook fixtures stop at `Stop-20260616`. Producer `l1a-extract.ps1` was edited 2026-06-22 (`.bak-2026-06-22-d1`). **The extractor stopped emitting facts 8 days ago.**
- **A2 — decision auto-capture: PARTIAL.** `recent-decisions.jsonl` last entry `2026-06-16T03:48`; nothing appended since despite active sessions → this is *why* the resume is stale.
- **B3 — episodic capture: PARTIAL.** Store + query work (296 episodes, queryable), but organic capture stalled (`last_ended_at 2026-06-20`).
- **H2 — Stop/PreCompact hook: PARTIAL.** Fires, but its extraction product (A1) isn't produced.
- **C4 — SessionStart resume hint: WIRED-ONLY (downgraded).** Fire-marker `last-resume-fire.txt` is 17 days old; the feed it surfaces is 8-9 days stale.

## The other phantom components (WIRED-ONLY)

- **D3 — autonomous canonical promotion (4C enforce): WIRED-ONLY.** `dream.log` shows `autopromote done: promoted=0` on **every** real nightly run (06-19/21/22/23). The gate returns PROMOTE but the actual promotion is a no-op (the sole nominee is already canonical). **This is the limb hardened across the entire 2026-06-22 session — confirmed to promote nothing.**
- **G1 — skillopt-sleep: WIRED-ONLY (doubly inert).** `~/.skillopt-sleep/` had only `session-end.log` (229 entries), no `staging/`/`state.json`; never scheduled (0 cron, 0 task). A free `--backend mock` cycle run during this audit returned **"night 1: 0 sessions → 0 tasks"** — so even when invoked, its harvest→digest link is broken (0 sessions consumed despite 229 logged). Never ran in 13 days, and produces nothing when run.
- **H3 — PreToolUse: WIRED-ONLY.** `pre-tool-warnings.jsonl` last write predates real canonical-hit input logged on 06-21/22/23 → the warning product stopped being written despite live input.

## What actually WORKS (19 — the system is not all trash)

- **Store/serve:** mem0 server `:18791` (B1, live, search returns ranked results), Qdrant `mem0_egemma_768` (B2, 3060 pts green), EmbeddingGemma `:11436` (C1), bge reranker (C2, reorders top-2), context-bundle endpoint (C3), admission gate (C5, 4977 entries through today).
- **The nightly dream (D1) WORKS end-to-end** — all 5 phases share the last cycle 2026-06-23 03:00-03:01 (the v1.6.1 Phase-4 fix held). C1 consolidator (D2, 3 fresh insight writes 06-23), L10 audit (D4), brand-scope-audit (D7, 0 mis-scoped), memory-index-build (D8, MEMORY.md rebuilt 06-23). decay-scan (D5) **correctly idle** (zero temporal records to expire — empty-by-no-input, not phantom).
- **Governance:** 4C gate (E1), HMAC canonical gate (E2, replay-protection active), tier-ledger (E3, 18.6k lines).
- **local-offload (G2) WORKS** — `ledger.jsonl` has 1227 real offload executions; it's an MCP server (`.exe`, rebuilt today), not a PATH binary. **`offload-dream` (G2b) is the ONE working nightly self-optimizer** — produces a fresh `DREAM-PROPOSAL.md` + adopts stats (last run 06-24 03:40, success). (This is local-offload's own loop — distinct from the dead skillopt-sleep.)
- **Hooks:** UserPromptSubmit bundle path (H1, via the compiled `mem0-hook-client.exe` → daemon, fallback ps1), SessionStart banner (H4). **Note H4 sub-finding:** the banner works, but the resident-daemon *prewarm* product (named pipe / `hook-daemon.log`) is **absent** — the prewarm spawn doesn't leave a running daemon.

## Instruments with no standing consumer (PARTIAL, the eval layer)

Not empty, not broken — they produce real measurements when manually run, but nothing on a schedule re-runs them or reads their numbers: faithfulness/joint-ridge gate (F1), injection-gating, promotion-gate calibration, intensification (F2). **Exception:** retrieval-drift HAS a standing consumer — `dream-consolidate.ps1` invokes it nightly (the absent alarm file is correct: zero drift found). semantic-dedup (D6) real deletes are 2 weeks old + its latest report line was a non-executed candidate.

## Honest read

- The phantom ratio is **15.2%**, not 90%. My repeated "low leverage / modest / near ceiling" framing was an excuse where it masked the real defect: **a dated ingest regression that froze the corpus on 2026-06-16.** That is not low-leverage — it is the load-bearing write path, broken, and I never reported it across many "audit passes" because those passes checked wiring, not output. This audit was built specifically to not repeat that, and it caught it.
- The system's **infrastructure** (store, retrieve, dream, govern, local-offload) is genuinely solid. The system's **ingest** is dead, and a few celebrated features (autonomous canonical promotion, skillopt-sleep) produce nothing.
- **To ship a usable, complete version, the order is forced by the evidence:** (1) fix the 2026-06-16 capture regression (A1/A2/B3/H2) — without it, nothing else matters because the corpus can't grow; (2) decide the fate of the 5 phantom components (fix or cut: D3 autonomous-canonize, G1 skillopt, H3 pre-tool-warnings, C4 resume-feed, H4 daemon-prewarm); (3) wire the eval instruments to a standing consumer or accept them as on-demand tools. Step 1 is the only true emergency.

## Appendix — verdict ledger (33)

WIRED-ONLY: A1, C4, D3, G1, H3. PARTIAL: A2, B3, D6, F1, F2(retrieval-drift / injection-gating / promotion-cal / intensification), H2. WORKS: B1, B2, C1, C2, C3, C5, D1, D2, D4, D5, D7, D8, E1, E2, E3, G2, G2b, H1, H4. Full per-component evidence in the workflow transcript (`wf_fd588436-7ad`); raw verdicts JSON in the run output.
