# Brand Routing

## Purpose

Brand isolation keeps one business's memories out of another's sessions. This system decides which brand a session, a fact or an insight belongs to (the brand map), makes the read side treat a few labels as visible to everyone (shared brands), and audits and repairs the store when records carry no brand or a brand nothing can produce.

## Questions this doc answers

- How does a session, a stored fact or a dream insight get its brand?
- What does `brands.json` accept, and what happens when it is missing or malformed?
- Which labels are shared, and how does the admission gate treat them?
- How do I find the records that mention a brand but carry none, and fix them?
- Why do L1a facts now carry a brand when they did not before?

## Scope

- The brand map (`brands.json`) and its resolvers: PowerShell (`memory-common.ps1`, `user-prompt-lib.ps1`), Python (`scripts/wsl/brand_routing.py`) and the Go judge, all run over one shared corpus.
- Shared brands (`MEM0_SHARED_BRANDS`, `shared_brands`) in the admission gate and the hook's client-side backstop.
- Where a brand is stamped: L1a episodes and facts, the user-decision write, dream insights.
- The audit (`brand-scope-audit.py`) and the reviewed backfill (`brand-backfill.py`).

## Non-scope

- The fail-closed brand check itself (a brandless request admits only brand-neutral records; a branded request rejects other brands): see [admission-gate.md](./admission-gate.md).
- Which brands exist and what they are called: operator data, never part of this repository.
- Goals and open questions: the server serves a brandless session only their brand-neutral rows, and shared brands do not change that.

## Key concepts

**Brand map.** An optional JSON file with four keys. A file without them, or no file, routes nothing and everything stays brand-neutral.

```json
{
  "rules": [{"pattern": "<regex over cwd / transcript path / workspace slug>", "brand": "brand-a"}],
  "shared_brands": ["<label visible to every scope>"],
  "content_rule_workspaces": ["<regex over path/slug whose facts are classified by content>"],
  "content_rules": [{"pattern": "<regex over fact text>", "brand": "brand-b"}]
}
```

**Resolution for one fact.**

1. The first `rules` entry whose pattern matches the path or slug wins: that brand.
2. Otherwise, if the path matches any `content_rule_workspaces` pattern, every `content_rules` pattern runs over the fact text. Exactly one distinct brand matched gives that brand; none, or several, gives no brand.
3. Otherwise no brand.

Patterns are case-insensitive. Path separators (backslash, slash, space) are one character to the matcher: the path is normalized so each becomes `-`, and the same three literals inside a pattern are normalized the same way. So `Projects/ClientA` matches `G:\My Drive\Projects\ClientA`, the Claude Code slug `g--My-Drive-Projects-ClientA` and `/home/u/projects/clienta`. An invalid regex or a malformed entry is skipped; the resolver never throws.

**Shared brand.** A label written as-is on the record but treated as brand-neutral by the admission gate: visible to a brandless search and to every other brand's search. A search scoped to the shared label itself still rejects other brands' records.

**Content-rule workspace.** A workspace that mixes businesses, so its facts cannot be branded from the path alone. Each fact is classified by its own text; a fact that names two brands, or none, stays brand-neutral.

**Unroutable brand.** A brand on a record that no rule, content rule or shared label of the map can produce: a typo, a test leftover, a brand nobody mapped.

## How the system works

**Where the map lives.** On a PC it is `~/.claude/scripts/brands.json` (deployed once by the installer, never overwritten). On the brain, `MEM0_BRAND_MAP` in `~/.mem0/stack.env` names the path, else the same default under the brain's home. `MEM0_SHARED_BRANDS` (comma-separated) adds shared labels on top of the map's `shared_brands`. Both stack.env keys are operator-owned: an installer re-run carries them over.

**Missing or malformed.** The Python resolver returns no brand and prints one stderr warning per problem (a missing file is the normal unconfigured state and stays silent). The PowerShell resolver keeps its long-standing behavior: with no usable `rules` it falls back to a single rule that brands this stack's own workspace, and nothing else.

**Sessions.** The UserPromptSubmit hook, the resident daemon and the L1a extractor all call `Get-BrandFromTranscriptPath`, one pinned copy in `memory-common.ps1` and one in `user-prompt-lib.ps1` (they do not dot-source each other, so a Pester test pins the two byte-identical). The workspace recorded with a session is the transcript's directory name, no longer a constant, and an unrouted session is no longer stamped with a hard-coded brand on the user-decision write.

**L1a facts.** The extractor resolves the brand before the facts loop and routes each fact with its own text, so a fact posted from a routed workspace carries that brand, a fact from a content-rule workspace carries the brand its text names, and an unrouted path posts no `brand` key at all. The episode keeps its path-only brand.

**Dream insights.** An insight takes the brand held by more than half of the memories it cites (neutral sources count in the denominator). A tie, no brand, or a shared label leaves it brand-neutral.

**Admission gate.** The shared set is the UNION of the brand map's `shared_brands` and `MEM0_SHARED_BRANDS`, on the server as everywhere else. The gate reads the map file itself (path from `MEM0_BRAND_MAP`, environment first, then `stack.env`, else `~/.claude/scripts/brands.json`; cached on the file's mtime and size, no import from `scripts/wsl`) and `MEM0_SHARED_BRANDS` (environment first, then `stack.env`) when a policy is built; a record carrying a listed label is admitted like a null-brand record. An unlisted brand stays fail-closed. The hook's client-side backstop (`Select-AdmittedMemoryResults`) reads the PC's map and the `MEM0_SHARED_BRANDS` environment variable, so it does not drop what the server just admitted. Because the PC has no `stack.env`, the map's `shared_brands` is the one place that works on both sides: a label set only in the brain's `stack.env` is admitted by the server but dropped by a PC whose environment lacks it.

## Important flows

**Audit.** `brand-scope-audit.py` (nightly, after the index build) scrolls every tier. It keeps its canonical check and exit code, and adds metrics that never change the exit code: `untagged_brand_mentions` (live records with no brand whose text matches a content rule, by brand, plus how many match several) and `unroutable_brands` (`{brand: count}`). Retired and non-retrievable records are ignored. All of it lands in `~/.mem0/brand-scope-status.json` beside the existing `n_canonical` / `n_misscoped`.

**Backfill.** `brand-backfill.py` fixes what the audit finds, with a person between the two steps:

1. `brand-backfill.py --dry-run --out report.jsonl` writes one row per record it would tag (`id`, `tier`, `current`, `proposed`, `rule`, `text_head`, `fp`) and changes nothing. `rule` is `path` or `content`.
2. Review the file: delete a row to skip it, set `proposed` to null to skip it, or correct the brand.
3. `brand-backfill.py --apply --from report.jsonl` applies exactly those rows through `PATCH /v1/memories/<id>/metadata` with the actor `brand-backfill` and only `{"brand": ...}`.

A row is refused, not applied, when its record changed since the report (the `fp` fingerprint covers the text, brand, tier and `updated_at`), when someone tagged the record meanwhile, when the record is gone, or when the proposed brand is not one the map can route. Canonical and insight records are never patched by this tool: those tiers need the operator's HMAC signature, so `--apply` prints the exact `mem0-canonize.sh --action patch_metadata` command for each. A run without a brand map exits 3 rather than proposing nothing.

## Data and state

- `brands.json` (operator config, not in the repository; `claude-config/brands.example.json` is the neutral template).
- `~/.mem0/stack.env` keys `MEM0_BRAND_MAP`, `MEM0_SHARED_BRANDS`.
- `~/.mem0/brand-scope-status.json` (overwritten each audit run).
- The dry-run report file (the reviewer's working copy).
- Corpus: `tests/fixtures/brand-routing-cases.jsonl`, one JSON object per line: `{"map", "path", "text", "expect"}`.

## Interfaces and entry points

- `Get-BrandFromTranscriptPath -Path <p> [-Text <t>] [-Map <m>]` returns `@{brand; workspace; project}`; `Resolve-BrandFromMap`, `Get-BrandMap`, `Get-SharedBrands` (PowerShell).
- `brand_routing.resolve(map, path, text)`, `resolve_by_content`, `content_brands`, `load_brand_map`, `shared_brands`, `routable_brands` (Python).
- `brand-scope-audit.py`, `brand-backfill.py` (authority, `~/apps/mem0-scripts/`).

## Dependencies

The brand map file; the mem0 API and Qdrant on the authority for the audit and backfill; `ams_env.py` for the authority URL and key.

## Downstream effects

A brand written on a fact or an insight changes who can recall it. A record that used to be brand-neutral and is now branded disappears from brandless and other-brand sessions, which is the point; run the audit's report and a dry-run review before a backfill `--apply`, because some facts that mention a brand are genuinely general.

## Invariants and assumptions

- A map that routes nothing (missing, empty, malformed) leaves behavior brand-neutral and never crashes a hook or the judge.
- The three resolvers agree on the corpus; a change to the matching rules changes the corpus in the same commit.
- A brand is stamped only when a rule says so. An unrouted session, fact or insight carries no brand key.
- The backfill never overwrites an existing brand and never touches a canonical or insight record.

## Error handling

An unreadable map degrades to brand-neutral (Python: one warning; PowerShell: the stack-default rule). A failed PATCH in `--apply` is reported, the run continues and exits 1. The audit fails open when Qdrant is unreachable.

## Security and privacy notes

The brand names and folder paths are operator data and live only in the operator's `brands.json`; the repository ships neutral placeholders. The backfill's actor cannot write canonical or insight records; the HMAC key stays with the operator.

## Observability and debugging

- `brand-scope-audit.py` prints the counts and `~/.mem0/brand-scope-status.json` holds them.
- `~/.claude/logs/l1a.log` names a brand-routing failure ("facts post brand-neutral").
- `admission-rejected.jsonl` shows `brand_scope_required:<brand>` for what a brandless search hid; a label that appears there and should be visible to all belongs in `MEM0_SHARED_BRANDS`.

## Testing notes

- `mem0-server/tests/test_brand_routing.py` (Python resolver over the corpus, map loading, shared brands).
- `scripts/windows/tests/BrandRouting.Tests.ps1` (the same corpus, the pinned copies, the client backstop).
- `mem0-server/tests/test_admission_gate.py` (shared brands), `test_dream_consolidate.py` (insight brand), `test_brand_backfill.py` (audit and backfill against a fake store).
- `scripts/windows/tests/L1aExtract.Tests.ps1` runs the real `l1a-extract.ps1` under Windows PowerShell 5.1 and checks the brand on every posted fact.

## Common pitfalls

- A pattern written with a literal `-` between words does not match a path that has a space or a slash there unless the path is normalized the same way: write separators as `/` or space and let the resolver normalize.
- `brand: cross-brand` is not a brand. Brand-neutral means no brand key.
- The SessionStart banner's shell resolver (`storage-cap-check.sh`) reads `rules` only and does not normalize separators; keep a rule's pattern matchable against a plain working-directory path.

## Source map

- [`../../scripts/wsl/brand_routing.py`](../../scripts/wsl/brand_routing.py) — the Python resolver.
- [`../../scripts/windows/memory-common.ps1`](../../scripts/windows/memory-common.ps1), [`../../scripts/windows/user-prompt-lib.ps1`](../../scripts/windows/user-prompt-lib.ps1) — the pinned PowerShell resolver copies.
- [`../../scripts/windows/l1a-extract.ps1`](../../scripts/windows/l1a-extract.ps1) — brands episodes and facts.
- [`../../mem0-server/admission_gate.py`](../../mem0-server/admission_gate.py) — shared brands in the gate.
- [`../../scripts/wsl/dream-consolidate.py`](../../scripts/wsl/dream-consolidate.py) — the insight brand.
- [`../../scripts/wsl/brand-scope-audit.py`](../../scripts/wsl/brand-scope-audit.py), [`../../scripts/wsl/brand-backfill.py`](../../scripts/wsl/brand-backfill.py) — audit and backfill.
- [`../../claude-config/brands.example.json`](../../claude-config/brands.example.json) — the neutral template.
- [`../../tests/fixtures/brand-routing-cases.jsonl`](../../tests/fixtures/brand-routing-cases.jsonl) — the shared corpus.

## Related docs

- [admission-gate.md](./admission-gate.md) — the fail-closed read-side check these labels plug into.
- [codex-hooks.md](./codex-hooks.md) — the L1a extractor.
- [dream-skill.md](./dream-skill.md) — the nightly consolidator.
- [installer-and-deploy.md](./installer-and-deploy.md) — how `brands.json` and the stack.env keys are deployed.
