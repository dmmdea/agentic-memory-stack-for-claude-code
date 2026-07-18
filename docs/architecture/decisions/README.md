# Architecture Decision Records

ADRs document proposed, active, and historical technical decisions, including their context,
tradeoffs, consequences, and rejected alternatives. Treat only ADRs with an `Accepted` status as
current guidance when reviewing, planning, or changing code.

Architectural decisions are human-owned. Agents can help draft ADR text from accepted decisions
and keep existing ADRs aligned with code, but they do not make the decisions.

## When to write one

Use an ADR for decisions that shape the system beyond a single implementation detail — why a
component owns a specific kind of state, why a process must be idempotent, why an integration is
isolated behind an adapter. Routine implementation details, small refactors, bug fixes, and
temporary workarounds belong in code, PRs, issues, or ordinary documentation instead.

Discuss proposed decisions in PRs, issues, planning docs, or review comments. An ADR may be
created while that discussion is in progress with a `Proposed` status, then changed to `Accepted`
once the decision is approved.

## Frontmatter

Every ADR begins with YAML frontmatter. The allowed fields are exactly:

- `status` (required): one of `Proposed`, `Accepted`, `Superseded`, `Deprecated`, `Rejected`.
- `date` (required): the date the ADR was created, formatted as `YYYY-MM-DD`.
- `superseded_by` (required only for `Superseded` ADRs): a repository-relative path to the
  replacement ADR.

Do not add other fields. The statuses mean: `Proposed` — under discussion, not current guidance;
`Accepted` — the current decision; `Superseded` — replaced by a newer ADR; `Deprecated` — discouraged
but historically relevant; `Rejected` — considered but intentionally not adopted.

## Supersession

Do not rewrite an accepted ADR to describe a different decision. When a decision changes, create
a new ADR, mark the old one `Superseded`, and point its `superseded_by` at the replacement.
Accepted ADRs may still receive small corrections or links that do not change the recorded
decision. New ADRs start from [../../templates/adr.md](../../templates/adr.md).
