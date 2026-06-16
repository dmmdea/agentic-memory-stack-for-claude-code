# Open Questions Registry (v0.17 Phase D)

## Why Q2(b): global registry vs per-session JSON only

Prior to v0.17, open questions were stored as a raw JSON array in `episodes.open_questions` — written by the Codex extraction hook but never surfaced cross-session. This meant:

- "What frontier questions are still open?" required scanning every episode row manually.
- Dream consolidator couldn't see what we don't know (Epistemic Reachability was dark).
- No deduplication: the same unresolved question could accumulate across dozens of sessions.
- No lifecycle: questions had no resolved/abandoned/duplicate status.

The v0.17 Phase D decision (locked 2026-06-11): promote per-session JSON to a first-class queryable table supporting cross-session frontier tracking, FTS5 keyword search, and lifecycle management.

---

## Schema

```sql
CREATE TABLE IF NOT EXISTS open_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_text TEXT NOT NULL,
    topic TEXT,
    brand TEXT,
    first_seen_session_id TEXT,        -- FK → sessions.session_id
    first_seen_episode_id INTEGER,     -- episode that first surfaced this question
    resolved_in_session_id TEXT,       -- FK → sessions.session_id (set on resolve)
    resolved_at TEXT,                  -- ISO 8601
    resolution_text TEXT,              -- summary of how/why it was resolved
    status TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'resolved' | 'abandoned' | 'duplicate'
    priority INTEGER DEFAULT 3,        -- 1=highest, 5=lowest (same convention as goals)
    related_goal_id INTEGER,           -- optional FK → goals.id
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

FTS5 virtual table `open_questions_fts` indexes `question_text` + `topic` using porter unicode61 tokenizer. Three triggers (`oq_ai`, `oq_ad`, `oq_au`) keep the FTS5 index in sync with the content table. Two B-tree indexes support status+priority queries and brand+status queries.

---

## Write path

### 1. Episode POST sync (automatic)

Every `POST /v1/episodes` that includes a non-empty `open_questions` list triggers a promotion step:

1. For each question string, `find_open_question_by_text_fuzzy()` runs an FTS5 match.
2. If a match exists with `status='open'` (same brand): skip — already tracked.
3. If no match: `create_open_question()` inserts a new row with `commit=False` so it's part of the atomic episode POST transaction. On rollback, no orphan row is created.

This means every session that Codex extracts open questions from automatically populates the global registry, with deduplication preventing accumulation.

### 2. Manual create

`POST /v1/open_questions` with body `{question_text, brand?, topic?, priority?, first_seen_session_id?, first_seen_episode_id?, related_goal_id?}` creates a question directly (e.g. for questions the operator identifies mid-session without going through episode extraction).

### 3. Resolution

`PATCH /v1/open_questions/{id}/resolve` with `{resolved_in_session_id, resolution_text, actor}`:
- Flips `status='resolved'`, sets `resolved_at` + `resolved_in_session_id` + `resolution_text`.
- Only transitions from `status='open'` (already-resolved is a no-op returning 404).
- Appends a `open-question-resolved` ledger entry to `tier-ledger.jsonl`.

### 4. Status transitions

`PATCH /v1/open_questions/{id}/status` with `{status, actor, reason?}` handles `abandoned` and `duplicate` transitions. `resolved` is intentionally routed through the dedicated resolve endpoint to enforce the resolution_text field and ledger event.

---

## Read path

### REST

| Endpoint | Description |
|---|---|
| `GET /v1/open_questions?status=open&brand=ai-ecosystem&limit=20` | List with filters |
| `POST /v1/open_questions/search` body `{query, brand?, status?, limit}` | FTS5 keyword search |
| `GET /v1/open_questions/{id}` | Single question detail + related goal title |

Route order in `app.py`: `/v1/open_questions/search` (POST) is registered BEFORE `/v1/open_questions/{oq_id}` (GET) to prevent FastAPI from attempting to parse the literal `"search"` as an integer id.

### MCP tools (4 tools in `mem0-mcp-shim.py`)

| Tool | Use |
|---|---|
| `open_questions_open(brand, limit=7)` | Start-of-session frontier check; limit 7 = Miller's working-memory anchor |
| `open_question_search(query, brand?, status?)` | "What did we ask about X across sessions?" |
| `open_question_resolve(oq_id, resolution_text, resolved_in_session_id)` | Mark resolved when answer becomes clear |
| `open_question_details(oq_id)` | Full record including related goal title |

### Dream consolidator (Phase 2 gather)

`Get-OpenQuestionsContext` (in `dream-consolidate.ps1`) fetches top 5 open questions and injects them into the Codex gather prompt:

```
Open frontier questions (Epistemic Reachability — what we know we don't know):
- [ai-ecosystem] Should canonical-key move to DPAPI vault?
...

PRIORITY: surprises in the transcripts that RESOLVE an open question are HIGHEST signal.
```

This operationalizes the AGI paper's Epistemic Reachability principle: surprises that close known unknowns are highest-information-gain events.

The gather phase `orient.json` artifact now includes `open_questions_count` for observability.

### MEMORY.md (SessionStart hydration)

`memory-index-build.py` appends an "Open frontier" section after Active goals:

```markdown
## Open frontier (3 questions, Epistemic Reachability)

- **ai-ecosystem** [P2] Should canonical-key move to DPAPI vault for v0.18?
- **brand-a** [P3] When does the nightly cart sync run?
```

This section appears in Claude's context at every SessionStart, so frontier questions are visible without requiring a memory_search call.

---

## Epistemic Reachability foundation (AGI paper SSRN-6748619)

The AGI paper identifies three foundations for goal-directed cognition:

1. **Value Improvement** — knowing what matters (goals table, v0.16)
2. **Continuity** — surviving interruption (episodes.state + UserPromptSubmit hook, v0.17 Phase 0)
3. **Epistemic Reachability** — knowing what you don't know (open_questions table, v0.17 Phase D)

Epistemic Reachability is the property that an agent can identify the boundaries of its own knowledge and track the frontier of unresolved questions. Without it, an agent can't distinguish "I know X" from "I never thought to ask about X" — leading to overconfident responses and missed contradictions.

The open_questions registry operationalizes this: every question surfaced by Codex extraction that hasn't been resolved is a signal of known ignorance. Dream consolidator reads this frontier before processing transcripts, prioritizing any surprise that resolves a known-unknown over generic observations.

---

## Known limitations

- **FTS5 dedup is approximate.** The fuzzy match uses porter tokenization, which means very differently-phrased versions of the same question may not dedup. The threshold is intentionally inclusive (any FTS5 hit = dedup) to avoid accumulation; the cost is occasionally merging distinct questions that share vocabulary.
- **`resolved_in_session_id` requires a real session.** The FK constraint means the resolve endpoint will 500 if a non-existent session_id is passed. Tests must create a session via episode POST first.
- **No cross-brand question promotion.** Questions are scoped by brand at creation. Cross-brand questions must be created with `brand=None` (shows as "cross-brand" in MEMORY.md).
- **Priority is not auto-adjusted.** Priority stays at the value set at creation. Future: Codex could re-score based on how many sessions reference the same question.
- **v0.18 candidate: batch resolution.** When the operator's session clearly resolves multiple open questions, there's no batch resolve endpoint. Each requires a separate PATCH. v0.18 could add `POST /v1/open_questions/batch_resolve`.
