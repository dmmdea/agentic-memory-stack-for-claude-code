# Documentation Style

Documentation in this repository is written for both humans and AI agents.

## Writing rules

- Use clear, direct Markdown and prefer stable headings.
- Link to related docs and source files with Markdown relative links from the current file.
- Explain behavior, responsibilities, flows, invariants, and pitfalls.
- Avoid restating every implementation detail and avoid unsupported guesses.
- Mark uncertainty explicitly with a `> UNVERIFIED:` blockquote when behavior is hard to infer.
- Include a source map in every system and flow doc.
- Stay concise enough to read before making changes.

Good docs are navigational, not exhaustive: what matters, where the details live, what can break.

## Operator-neutral vocabulary

This repository is public and must stay free of any single operator's machine names, hostnames,
LAN addresses, brand names, and local filesystem paths. Use these terms instead:

| Instead of | Write |
| --- | --- |
| The machine that hosts the memory service | the brain box |
| A machine that mirrors or consumes it | a replica box |
| The person running the stack | the operator |
| A concrete local path or hostname | `<your-machine>`, `<repo-root>`, `<your-host>` |
| A concrete private IP or endpoint | the documented default port, or `<host>:<port>` |

Examples use placeholder values only. `scripts/ci/check-docs.py` enforces this mechanically.

## Title Case for glossary terms

Glossary entries in [glossary.md](glossary.md) use Title Case headings. In finished docs, prefer
the same Title Case form when referring to a glossary-defined concept, especially when it
distinguishes an application-specific concept from an ordinary word. Title Case is a clarity aid
for finished docs — a concept may still appear in lowercase in drafts, comments, source
identifiers, issues, or informal notes.

## Granularity

Create or expand documentation when behavior is important enough that a reader would otherwise
need to inspect several files to understand it. Keep behavior inside a system doc when it is
local and easy to explain there. Promote it to a flow doc when it crosses multiple systems, has
several steps or states, is frequently changed or debugged, has important error handling,
involves external services, or has security, data integrity, or user-visible implications. Do not
document every file: prioritize what is central, risky, frequently changed, difficult to infer, or
important to user-visible behavior.

## Source maps

Every system and flow doc ends with a source map linking the most important files that implement
the behavior, so a reader can inspect details without rediscovering where the implementation lives.
Do not list every file unless the system or flow is small. Prefer entry points, state definitions,
handlers, services, jobs, tests, and integration files.

## Code comments

Docs explain how a system works; code comments explain why a specific implementation detail
exists. Good comments cover non-obvious branches, ordering constraints, side effects, invariants,
security checks, external API quirks, retries, caching, concurrency, and cross-system effects.
Avoid comments that restate the code, explain obvious names, duplicate system documentation,
embed long architectural explanations, or leave stale history that belongs in an ADR.

## Keeping docs updated

Every pull request that changes behavior, interfaces, security, data, or operational procedures
must update the affected documentation as part of the same change.

Reviewers treat documentation as part of the code change and verify that it is accurate before
approving. Update docs when a change affects system responsibilities, runtime or user-visible
behavior, internal workflows, data models, external integrations, public APIs, configuration,
error handling, security or auth behavior, invariants, testing or debugging expectations, or
glossary-defined concepts. If docs and code disagree, update the docs to match the code, update
the code to match the documented intent, or explicitly call out the mismatch for review.

## Templates

New docs start from [templates/system.md](templates/system.md), [templates/flow.md](templates/flow.md),
or [templates/adr.md](templates/adr.md). Do not rename or reorder the template headings.
