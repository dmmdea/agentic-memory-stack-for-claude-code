# V2 milestone — fresh-session resume (V1.0 SHIPPED → explore new literature)

Paste the **PROMPT** block below into a new Claude Code session (cwd = `C:\path\to\agentic-memory-stack-for-claude-code`) to continue. Everything above it is orientation.

## State (2026-06-16) — V1.0 COMPLETE + PUBLISHED
- **V1.0 milestone DONE.** R1–R6 faithfulness roadmap (v0.24–v0.29.6) + Phase 7 operator-agnostic self-contained installer overhaul (v0.30 + v0.31), each audited 0 CRIT/0 HIGH.
- **PRIVATE repo** (source of truth): `youruser/agentic-memory-stack`, main `1d7b9fa`, tag `v1.0`.
- **PUBLIC repo** (published): `https://github.com/youruser/agentic-memory-stack-for-claude-code`, tag `v1.0.0`, Apache-2.0, secret scanning + push protection on, 0 alerts.
- **Two-repo model:** private = source of truth; public = clean scrubbed mirror **regenerated on demand** by `scripts/publish/build-public-mirror.ps1` (HARD safety gate; never hand-edit the public repo). Workflow: `scripts/publish/PUBLISHING.md`.
- **Dreaming + overnight features VERIFIED working (2026-06-16):** the 3am dream consolidator ran successfully (LastTaskResult 0; 06-16 consolidated 3 insights / posted 2 with R5 lineage governance dropping 1; MEMORY.md regenerated); all systemd overnight timers (decay-scan, stack-backup, goals/contradiction sweeps, episodic-reconcile, l10-audit) Result=success; Task Scheduler 3am Dream = Ready; backup manifest 3h old + restore-drill 06-13 ok; L1a extraction current; deployed Test-MemoryStack R9 **14/14 GREEN** (32 PASS / 1 WARN = the documented v0.29.4 contradiction-sweep non-authoritative-local-rejudge state — clear it with a Codex rejudge while the shim is up).
- **FIXED this session (overnight diagnostic):** Test-MemoryStack's nightly-consolidator check pointed at the dead legacy `c1.log` (it showed a stale 06-09 "mem0 unreachable" error while the dream was actually healthy) — now reads `dream.log` and correctly reports the run. Repo `scripts/windows/Test-MemoryStack.ps1:992` + deployed copy redeployed byte-identical (R9 14/14 still GREEN).
- **One BENIGN item deferred (opportunistic):** the dream's `touched_by_dream` stamp 403s on canonical-tier records (the HMAC gate working as designed — just log noise). Fix = in `dream-consolidate.ps1`, detect canonical tier and skip the stamp; needs the dream-consolidate change + R9 redeploy + mirror republish.
- **Open security follow-up (the operator):** rotate the live secrets that were in the now-untracked `claude-config/settings.json` (proxy secret, vault credential, Vercel team ID, PubMed token) — never in the public mirror/history.

## Standing disciplines (uphold every step)
- **Frontier research step-discipline (min-maxer):** complementary LIVE web research BEFORE every build step — find how the people who mastered it do it; don't reinvent from internal docs. Not just internal audits.
- **Per-phase gate:** research-ground (LIVE WEB) → build (TDD; **Codex for ALL LLM judgment via `codex_shim_client`, never local**) → adversarial audit (Workflow, multiple lenses + default-reject verifiers) → fix to 0 CRIT/0 HIGH → verify (pytest + `Run-PesterTests.ps1` + `Test-MemoryStack`) → deploy live → ship increment (CHANGELOG/VERSIONS/progress/session_summary, commit on main, push origin with the **youruser** gh token [active often pepsdubai → `gh auth switch --user youruser`, push, switch back], mem0 checkpoint) → **regenerate + re-publish the public mirror** (`build-public-mirror.ps1`, gate must pass, then push a fresh clean snapshot).
- **Don't force fit:** evaluate new techniques for genuine system fit; only adopt what measurably improves the stack (R1 faithfulness harness is the measuring stick). A documented "not a fit, here's why" is a valid outcome.
- Reminders: calibrate relevance on the SEMANTIC scale; any new app.py-imported module → add to `MEM0_MODULES` + the import-closure test; deployed PS scripts are R9 SHA-tracked (redeploy after a commit, EOL drift); warm WSL before mem0/local calls; **Grep brace-globs `{a,b}` give false negatives — use single-pattern greps.**

---

```PROMPT
Continue perfecting the agentic-memory-stack. cwd is C:\path\to\agentic-memory-stack-for-claude-code. V1.0 is COMPLETE + PUBLISHED — read docs/v2-resume.md (this state) first, and recall mem0 (workspace=ai-ecosystem project=ecosystem) for the "V1.0 MILESTONE COMPLETE + PUBLISHED 2026-06-16" checkpoint + the two-repo/publish model.

State: PRIVATE repo youruser/agentic-memory-stack (source of truth, main at v1.0 + a post-v1.0 diagnostic patch — `git log` for the head); PUBLIC mirror https://github.com/youruser/agentic-memory-stack-for-claude-code (tag v1.0.0, Apache-2.0; one commit behind on the diagnostic patch — fold it into the next republish). Dreaming + overnight features are VERIFIED working (3am dream consolidates insights nightly; all overnight timers succeed; Test-MemoryStack R9 14/14 GREEN). A diagnostic bug was FIXED this session (Test-MemoryStack now reads dream.log not the dead c1.log); ONE benign item remains opportunistic (dream touched_by_dream 403 noise on canonical-tier records) — see docs/v2-resume.md.

UPHOLD frontier research step-discipline: do complementary LIVE WEB research BEFORE every build step (min-maxer rule), not just internal audits.

TASK — explore + evaluate NEW literature/tools for SYSTEM FIT (do NOT force anything that doesn't make sense; a documented "not a fit + why" is a valid outcome). For each: LIVE-WEB research it deeply, map it against what the stack already does (R1-R6 + the live architecture), and write a fit-analysis to docs/research/ with a clear ADOPT / ADAPT / REJECT recommendation + rationale grounded in cited sources + the R1 faithfulness harness as the measuring stick. Candidates:
  1. AtomMem: Learnable Dynamic Agentic Memory with Atomic Memory Operation — https://share.google/qO58g2BgnxfCJ8Kds (find the arXiv/source).
  2. arXiv 2601.22436 (PDF: https://arxiv.org/pdf/2601.22436) — NOTE: this appears to be the paper that SEEDED the R1-R6 roadmap (per the 2026-06-14 DESIGN INSIGHT memory); re-mine it for techniques NOT yet implemented.
  3. arXiv 2605.26302 — https://arxiv.org/abs/2605.26302.
  4. Microsoft fastcontext — https://github.com/microsoft/fastcontext.
  5. Cameron R. Wolfe — "Agent Evals" — https://cameronrwolfe.substack.com/p/agent-evals (agent evaluation methodology; map directly against the R1 faithfulness harness — it may sharpen HOW we measure whether any adopted technique actually improves the stack).

Use a Workflow (parallel research lenses → adversarial fit-verification) to evaluate all four, then synthesize. For any candidate judged ADOPT/ADAPT, follow the full per-phase gate (research-ground LIVE WEB → TDD with Codex for all LLM judgment → Workflow adversarial audit to 0 CRIT/0 HIGH → verify pytest + Run-PesterTests.ps1 + Test-MemoryStack → deploy live → ship increment to the PRIVATE repo → regenerate + re-publish the public mirror via scripts/publish/build-public-mirror.ps1 [safety gate must pass]). PLAN-FIRST for any multi-file build (superpowers:writing-plans → sign-off).

Rules: Codex for ALL LLM judgment, never local. Two-repo maintenance — never hand-edit the public repo; regenerate it via the pipeline + re-run the safety gate before any public push (use the youruser gh token: gh auth switch --user youruser, push, switch back to pepsdubai). Verify before claiming done; show evidence. Grep with single patterns (brace-globs false-negative). Warm WSL before mem0/local calls.
```
