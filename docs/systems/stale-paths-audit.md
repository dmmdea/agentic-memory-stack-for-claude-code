# Stale-paths audit — measuring memory staleness before designing for it

## Purpose

A read-only audit that finds memories asserting filesystem paths which no longer exist,
and turns them into a **hand-label worksheet**. It is step one of the memory-frontier
item *milestone-conditioned memory validity* — deliberately built **before** any schema,
because the evidence needed to specify that schema does not exist yet.

## Questions this doc answers

- Why is staleness measured with filesystem paths rather than something semantic?
- What counts as STALE, and what is deliberately held UNDECIDABLE?
- Why does this write its own receipt instead of `audit-flags.jsonl`?
- Which interpreter should it run under, and why does that change the answer?
- What outcome would **kill** the validity feature rather than justify it?

## Scope

`scripts/wsl/stale-paths-audit.py` — classification, the report, and the hand-label
worksheet round-trip.

## Non-scope

The validity schema itself (`valid_while: goal:<id>`, `superseded_by: <memory_id>`) is
**not implemented and not designed**. This tool produces the dataset that decides whether
it should be. Admission-time filtering belongs to [`admission-gate.md`](./admission-gate.md);
tier semantics to [`memory-model.md`](./memory-model.md).

## Background — why paths, and why now

The corpus has no notion of a memory's *validity*. The canonical injury is on record: the
line *"the 5060 Ti is the only GPU"* was true until a second card went in. That is a
**milestone**, not a date, which is why a Zep-style `valid_until: <date>` was rejected in
the plan — dates are unpredictable for exactly the facts that rot here (a hardware spec, a
port, a deployment state).

Before building the schema, the plan gated on a hand-labelled staleness rate. A bench proxy
(`bench/mem0-stale-rate.py`, 2026-08-17) measured **56%** over the first 50 mechanically
decidable memories, clearing the 20% materiality bar. That proxy is strong enough to say
*material*; it is **not** strong enough to specify a schema — it sampled 1,500 points, stopped
at 50 decisions, and admitted three biases.

Filesystem paths are used because they are the only claim in a memory that can be checked
**mechanically and objectively**. Nothing else in a free-text memory can be falsified without
a human or an LLM judge.

## How the system works

### Classification

Each extracted path resolves to exactly one verdict:

| verdict | meaning | counted as |
|---|---|---|
| `exists` | path resolves on this runtime | decidable, fresh |
| `missing-recorded` | missing, and the memory's prose *records* the removal | decidable, **correct** |
| `missing-unexplained` | missing, no explanation | decidable, **STALE** |
| `root-unavailable` | the drive/root is not reachable from this runtime | **undecidable** |
| `shared-root-unavailable` | a shared-Drive claim on a box where no local root carries the shared dir | **undecidable** |
| `missing-foreign-host` | path belongs to another machine in the fleet | **undecidable** |
| `artifact` | extraction noise, not a path claim | excluded |

A memory takes the **worst decidable** verdict among its paths, so one surviving path cannot
whitewash a record that also names a dead one. Undecidable reasons never masquerade as fresh
**and never enter the denominator** — a coverage gap must not deflate the rate the way it
once inflated it.

### The shared-Drive alias

The hand-label round's **single largest systematic error**: the same synced Google Drive is
mounted at a **different drive letter per machine** (`D:\My Drive` on one box is `G:\My Drive`
on another). The letter is a property of the *machine*, not of the memory, so a path-existence
check on one box invented staleness for a whole class of perfectly valid memories written on
the other.

The fix is a normalisation, not a pardon: before any `missing-*` verdict, a path under the
shared-Drive dir (default `My Drive`, override `MEM0_SHARED_DRIVE_DIR` — normalised, a
trailing separator or empty value cannot silently disable matching) is re-checked under
**every local root that carries that dir**, whether the memory recorded it in the Windows
(`X:\My Drive\…`) or the WSL (`/mnt/x/My Drive/…`) spelling. An alias hit is a decidable
`exists` (even when the recorded letter does not exist here at all); a path *outside* the
shared dir is never excused; and a file deleted **from** the Drive is missing under every
root and still reads stale.

Two seams are handled explicitly. **Zero local roots** (Drive app not mounted yet, crashed,
or absent): the box cannot falsify any shared-Drive claim, so those paths take their own
verdict, `shared-root-unavailable` — undecidable, never stale, counted separately from
`root-unavailable` because the drive *letters* may all exist while none carries the shared
dir, which keeps the letter-based coverage banner silent. Both the console report and the
JSONL receipt print the detected root set (`shared_drive_roots`), with a loud banner when it
is empty. **More than one root** (a backup mirror or a second Drive account also mounts a
`My Drive`): detection would trust the wrong copy, so `MEM0_SHARED_DRIVE_LETTERS` pins the
letters. The pin fails **loud** in both directions — a malformed entry (`gg`, `g;h`) and a
letter that does not carry the shared dir here are each a `SystemExit`, because a silently
dropped or stale pin would reinstate the very false-accusation class the alias kills. A hit
through a stale mirror is at least traceable: the worksheet row carries the alias-resolved
path, wrong letter and all.

### The three proxy biases, and what changed

1. **Selection bias — uncorrectable, so it is measured.** Memories naming a path skew
   operational and ephemeral; durable memories (decisions, preferences, identity) rarely name
   one. The report prints the **decidable fraction of the corpus** and cross-tabs staleness by
   tier and source, so the reader sees which stratum is rotting instead of being handed a bare
   percentage. The rate remains an **upper bound**.
2. **Other machines.** A path that lives on another machine in the fleet is correct there and
   always reads missing here. Memories naming a foreign host are undecidable — unless the memory
   also names *this* box, in which case the local claim wins. Fleet topology is **operator
   config**, not source: set `MEM0_FOREIGN_HOSTS` (comma-separated). **Unset means the correction
   is OFF**, cross-machine paths read as stale, and the report says so rather than hiding it.
3. **Deletion records.** "The GLM model was deleted from `V:\models\...`" is a **correct**
   memory whose subject *is* the removal; scoring it stale inverts its meaning. The vocabulary
   here is much wider than the proxy's, which found **1** such record where this finds **137**.

### Two instrument bugs found while building it

Both would have manufactured or hidden staleness, so they are pinned by tests:

- **Unreachable roots.** `G:` and `P:` are **not mounted under WSL**. The naive check marked
  every `G:\` path missing and invented staleness. Paths on an unreachable root are now
  undecidable, and the coverage gap is printed loudly.
- **Removal vocabulary inside the path.** `DEAD_VERB` originally scanned the raw text, so a
  path spelling `...\gone.md`, `D:\archive\...` or `V:\models\deleted\...` **excused itself**
  as a deletion record. Vocabulary is now matched against **prose only** (path literals
  stripped). This direction is the dangerous one: it *deletes* staleness from the dataset the
  schema would be designed from.

### Runtime coverage

Neither interpreter alone sees everything, so the script translates **both directions** and
one run decides everything:

| runtime | sees | reaches the other side via |
|---|---|---|
| Windows *(recommended)* | all drives incl. `G:`, `P:` | `//wsl.localhost/<distro>/…` for POSIX paths |
| WSL | POSIX + mounted drives | `/mnt/<d>/…` for drive paths; `G:`/`P:` unreachable |

Receipts always land in the **WSL sidecar** `~/.mem0/`, whichever interpreter runs, so the
audit trail never forks in two. On Windows the sidecar is located through the WSL share and is
inferred **only when unambiguous** (exactly one home already carrying a `.mem0`); guessing a
username is what would fork the trail. Override with `MEM0_SIDECAR_DIR`.

### Configuration

| env var | effect if unset |
|---|---|
| `MEM0_FOREIGN_HOSTS` | foreign-host correction **off** — cross-machine paths counted stale |
| `MEM0_LOCAL_HOST` | defaults to this machine's hostname |
| `MEM0_SHARED_DRIVE_DIR` | defaults to `My Drive` — the synced-Drive dir checked across all local drive letters (normalised; empty falls back to the default) |
| `MEM0_SHARED_DRIVE_LETTERS` | shared roots auto-detected — set (comma-separated letters) to pin them when a mirror or second account also carries the dir; pinned letters are validated and refused loudly when malformed or not carrying the dir |
| `MEM0_WSL_DISTRO` | POSIX paths stay undecidable when run from Windows |
| `MEM0_WSL_USER` | sidecar inferred only if exactly one candidate home exists |
| `MEM0_SIDECAR_DIR` | receipts follow the rules above |
| `MEM0_QDRANT_URL` / `MEM0_QDRANT_COLLECTION` | local defaults; scheme allow-listed to http/https |

## Guarantees

- **Zero mutations.** Reads Qdrant's scroll API; writes only its own report and worksheet.
  It never deletes, PATCHes, or flags a memory in the store — pinned by a test that greps the
  source for mutating verbs. This estate has already lost 688 MB to an automated process
  behaving exactly as designed.
- **Not the admission axis.** It does **not** write `~/.mem0/audit-flags.jsonl`. That file
  belongs to `l10-audit` on the admission axis (`admission_gate.py`, `layer="server-search"`,
  `schema_version "v18"`). Validity is a different axis; overloading admission reasons would
  corrupt both readings.
- **No scheduler.** A manual subcommand, run when an operator wants it.

## Usage

```bash
# recommended: Windows interpreter (full drive coverage), stdlib only
python scripts/wsl/stale-paths-audit.py                    # scan all, print report
python scripts/wsl/stale-paths-audit.py --worksheet        # + emit hand-label worksheet
python scripts/wsl/stale-paths-audit.py --sample 500       # reproducible subset (seed 42)
python scripts/wsl/stale-paths-audit.py --json             # machine-readable
python scripts/wsl/stale-paths-audit.py --summarise-worksheet   # read back hand-labels
```

Receipts: `~/.mem0/stale-paths-audit.jsonl` (append-only summaries),
`~/.mem0/stale-paths-worksheet.jsonl` (rewritten per run).

## First full-corpus result (2026-08-18, Windows, 10,384 points)

**These numbers replace an earlier run of the same tool. Two fresh-context reviews found
nine defects in the instrument, three of which changed which memories reach the human; the
numbers moved twice as they were fixed. Treat any earlier figure as retracted.**

| | n |
|---|---|
| paths still exist | 233 |
| missing, memory records the removal | 86 |
| **missing, unexplained -> STALE** | **644** |
| undecidable: foreign host / unreachable root | 183 / 1 |
| artifacts only / names no path | 18 / 9,148 |

**Stale rate 66.9% of decidable**, and decidable is **9.3%** of the corpus — so ~644 of
10,316 memories, about **6%** in absolute terms.

| tier | stale / scanned |
|---|---|
| evidence | 628 / 9,892 (6.3%) |
| unknown | 6 / 75 (8.0%) |
| **canonical** | **4 / 57 (7.0%)** |
| stable | 4 / 16 (25.0%) |
| insight | 2 / 276 (0.7%) |

### A retracted claim, kept on the record

An earlier run of this tool reported **canonical 0/52 stale** and concluded *"the tiers the
system actually trusts are clean."* That was an artifact of a broken extractor: paths
containing spaces were truncated, and forward-slash and `~/` paths were invisible entirely,
so most canonical path claims were never checked. With the extractor fixed, **canonical
shows 4 stale entries and `stable` shows 25%**. The reassuring version of this finding was
the buggy one — which is exactly why the instrument gets tests before the output gets
trusted.

### The worksheet is a labelling task, not a dump

Emitting every accused row produced **890 rows** with the blind controls diluted to about
1-in-14. Labelling a realistic few dozen would then have caught roughly **four** controls —
far too few to measure recall, which is the only reason the controls exist. So the worksheet
**samples the accused down** (`--worksheet-size`, default 60) and keeps the full control
draw, making the ratio a human actually labels the ratio that was designed. `--worksheet-all`
emits every row when that is genuinely wanted.

The header records `sampled`, the `population` it was drawn from, and the row counts, because
a labeller who cannot tell a sample from the population will mis-scale any rate computed from
the sheet.

## The decision this feeds — including the kill

`--summarise-worksheet` reads the hand-labels back and reports the split. The label set
includes **EPHEMERAL** ("was never worth storing") precisely so the cheap outcome stays
visible:

- If STALE rows are mostly **EPHEMERAL** and never recalled → **do not build a validity
  schema.** Fix the **write** path (extraction filter) so these are never written. Cheaper,
  and better.
- If they are substantive facts that were true and later stopped being true → the validity
  schema (`valid_while: goal:<id>` / `superseded_by`) is justified.

The worksheet also carries `label_recalled`, because a memory that has never been recalled is
not worth a schema to invalidate.

### The verdict landed (2026-08-19): DO NOT BUILD

135 rows were hand-labelled against evidence (recall counts from 21,320 retrieval-log rows,
live payloads, fresh path re-checks, git history). Mechanical precision was **8.3%** (5 of 60
accused genuinely stale); recall ~71% via the blind controls; true staleness extrapolates to
**~0.6%** of the corpus against the 20% bar — a **30× miss**. 81% of the hand-labelled
STALE+EPHEMERAL rows (across all 135) were EPHEMERAL, and 86% of the genuinely stale rows
were never served once. The validity schema is
**killed**; what shipped instead is exactly the cheapest-kill pair this section anticipated:

- the **EPHEMERA GATE** in the L1a extraction prompt (`l1a-extract.ps1`) — one-off
  task/PR/worktree pointers, session checkpoints, temp dirs, and point-in-time counts are
  dropped at write time (see [memory-capture.md](../flows/memory-capture.md));
- the **shared-Drive alias** in this instrument (above), which removes its largest class of
  false accusations.

Full verdict: `Ecosystem/Arquitechture/2026-08-16_memory-frontier/07-GATE-VERDICT-milestone-validity.md`
(operator archive, outside this repo). The instrument survives as an evidence harness; its
precision makes it a poor detector, and it must never auto-act on its accusations.

## Constraints any future validity schema must honour

Verified in live code (line numbers drift; the constraints do not):

- **`CANONICAL_REQUIRES_USER_DIRECT = True`** (`app.py`) — canonical promotion requires
  `actor="user-direct"` (HMAC token + nonce) or an explicit allow-list. A validity/supersede
  edge must sit **beside** that audit boundary, never bypass or duplicate it. Since supersede
  edges are planner-seat/operator-only, reuse the existing enforcement.
- **`_ADD_FORBIDDEN_META`** (`app.py`) — metadata was once validated for `tier` only, so any
  API-key holder could POST arbitrary `metadata.contradicts_canonical`. Any new key
  (`valid_while`, `superseded_by`) **must** go through the same forbidden-metadata strip or
  that hole returns under a new name.
- **Goals are already first-class** (`goal_complete`, `goal_abandon`, `goals_open`). Goal
  closure should **enqueue a review flag**, never mutate memories.
- **Flag for review, never auto-delete.**

## Common pitfalls

- **Quoting the rate bare.** "38.3%" without "of the 5.1% that is decidable" overstates the
  problem by an order of magnitude.
- **Running under WSL and trusting the total.** `G:` and `P:` are invisible there; the report
  says so, but the number will cover less of the corpus.
- **Treating mechanical verdicts as labels.** They are a *proposal*. The `label` field is the
  evidence; `--summarise-worksheet` reports mechanical precision against it.

## Related

- [`memory-model.md`](./memory-model.md) — tiers, query classes, and the life of a memory
- [`admission-gate.md`](./admission-gate.md) — the *other* axis, deliberately kept separate
- [`l10-audit.md`](./l10-audit.md) — the admission-axis flag writer and its review flow
