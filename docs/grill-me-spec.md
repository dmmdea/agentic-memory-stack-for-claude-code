# `/grill-me` — Specification (SPEC-ONLY; build deferred to C1)

> **Status:** SPEC ONLY. This document authorizes **no build, no write authority, no
> auto-promote, no scheduling, no hooks.** The implementation is task **C1** in the
> [re-validated master plan](superpowers/plans/2026-06-25-revalidated-master-plan.md),
> and is itself **gated** (see Acceptance). This spec exists so that, when C1 is unblocked,
> the build cannot drift past these governance bounds.

## 1. Purpose & unique value

`/grill-me` is a **bounded, operator-pulled interview** in which Claude actively elicits
**durable, evergreen, in-head operator knowledge** and routes it — gated — into memory.

The unique value is the knowledge class it reaches: facts the operator *knows* but never *types* —
locked decisions, standing preferences, hard constraints, brand/role directives, "never do X"
rules. The passive capture path (l1a-extractor over transcripts) is structurally blind to this:
it can only extract what was said in a session. Tacit, never-stated knowledge is invisible to
it. Elicitation is the only path that reaches it.

## 2. Frontier grounding (why this is defensible, and where the line is)

- **Proactive INGESTION** — "sync everything", auto-importing all docs/history/notes
  (GBrain-style) — **IS the named over-use-cliff anti-pattern.** It pollutes context, inflates
  the store, and drives over-adoption (the model trusting injected non-ground-truth). **REJECTED.**
- **Proactive ELICITATION** — *bounded, HITL-gated, evergreen-only* questioning — is defensible
  and supported by the frontier (ProMem, arXiv 2601.04463: targeted, user-confirmed knowledge
  elicitation). **ADOPTED, under the hard bounds in §3.**

The distinction is the spine of this spec: we pull *specific evergreen facts the operator
chooses to share*, never *bulk-absorb everything*.

## 3. Hard governance invariants (non-negotiable)

1. **Operator-PULL trigger.** Runs ONLY when the operator invokes `/grill-me`. Never Claude-initiated
   unprompted, never on a timer, never from a hook or scheduled task. ("Claude-initiated" below
   means Claude *conducts* the interview — asks the questions — once the operator has pulled it.)
2. **HITL-gated writes, per fact.** Every elicited candidate is *proposed*; nothing is written
   without the operator's explicit per-candidate confirmation (accept / edit / reject).
3. **No new write authority.** `/grill-me` front-ends the **existing** write paths — `memory_add`
   for evidence/stable, and the **HMAC user-direct canonize gate** (`mem0-canonize.sh`) for
   canonical. It adds **zero** new authority; it cannot write anything the operator could not
   already write through those gated flows.
4. **No auto-promote.** `/grill-me` never promotes to canonical autonomously. Canonical stays
   behind the HMAC user-direct gate, operator-run. It may *suggest* a tier; the operator confirms
   through the existing gate.
5. **Evergreen-only.** Eligible content = durable, time-stable facts (decisions, preferences,
   constraints, brand/role context). **Ineligible:** transient state, task/branch status,
   debugging notes, operational ephemera, anything with a short shelf-life. (Mirrors the mem0
   save-policy.)
6. **No scheduling / no unattended runs.** A grill with no human present has no HITL gate, so it
   is forbidden by construction.
7. **Bounded session.** One topic, a capped number of questions per run. No open-ended "tell me
   everything."

## 4. Flow (when C1 builds it)

1. Operator: `/grill-me [topic]`.
2. Claude asks a **bounded** set of targeted questions about *durable* knowledge in that topic
   — e.g. "What's the locked decision on X?", "What brand constraint must I never violate for Y?",
   "What's your standing preference for Z?". Questions deliberately target evergreen, in-head facts
   the transcript path can't reach — not things already captured.
3. Operator answers, edits, or skips each.
4. Claude drafts candidate facts (evergreen, brand/scope-tagged at write time per the content-aware
   tagging decision) and *proposes* a tier (default evidence/stable; canonical only via the gate).
5. **HITL review:** operator accepts / edits / rejects each candidate.
6. Accepted facts are written through the **existing** path only: `memory_add` (evidence/stable),
   or — for canonical — handed to `mem0-canonize.sh` for the operator to run the HMAC gate. No
   bypass.

## 5. Anti-patterns — what `/grill-me` must NEVER become

- ❌ Auto-ingestion / "sync everything" / importing docs or history in bulk (the over-use cliff).
- ❌ Scheduled or unattended elicitation (no human ⇒ no gate).
- ❌ Auto-promotion to canonical (bypasses the HMAC user-direct gate).
- ❌ Eliciting or storing transient/ephemeral state (violates evergreen-only).
- ❌ Claude grilling the operator unprompted (violates operator-PULL; also just annoying).
- ❌ Writing any candidate without explicit per-fact confirmation.
- ❌ Granting itself write authority beyond the existing gated paths.

## 6. Acceptance (for the gated C1 build — not now)

C1 is gated on Phase-2 governance + the joint-ridge **over-adoption** eval. The build is accepted
only if it demonstrates:

- **Faithful-use ↑ without over-adoption ↑** — measured on `eval/promotion-gate/` /
  `--mode overadoption`: eliciting + storing real evergreen facts must not raise the
  `over_adoption_rate` (the model must not become more credulous of injected non-ground-truth).
- **End-to-end probe:** `/grill-me` elicits a genuine evergreen fact → operator confirms → it lands
  in memory via the gated flow → it is later retrieved in-scope (findability eval) → over-adoption
  rate does not regress.

## 7. Out of scope for this spec

No implementation, no skill file, no write path changes, no scheduling, no hooks, no auto-promote.
This document is the contract the C1 build must satisfy; it grants nothing on its own.
