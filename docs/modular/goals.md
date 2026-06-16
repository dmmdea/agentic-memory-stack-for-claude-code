# Goals — Design & Schema Reference (v0.16)

Goals are the persistent, multi-session objectives that episodic memory tracks across Claude Code sessions. They connect the AGI paper's **Value Improvement** and **Epistemic Reachability** principles to a concrete SQLite table — letting the system know not just what happened last session, but what still needs doing.

## Why goals?

> "An agent that makes progress on a goal it later abandons has wasted trajectory.  
> An agent that cannot tell which goals are blocked cannot prioritise exploration."  
> — adapted from *Agent Exploration Toward AGI*, SSRN-6748619

| Need | mem0 / Qdrant | episodic.db goals |
|---|---|---|
| "What am I trying to accomplish long-term?" | No — atomic fact granularity | Yes — `goals` table with title, status, priority |
| "Which goal did this session advance?" | No — no session→goal linkage | Yes — `episode_links` with link_type=advanced_goal |
| "Which goals are blocked right now?" | No | Yes — status='blocked' + episode_links |
| "Show me the full goal tree" | No | Yes — recursive CTE in `get_goal_tree` |
| "What open questions are blocking progress?" | No | Yes — `episodes.open_questions` JSON column |
| FTS5 fuzzy-match on goal title | No | Yes — `goals_fts` virtual table |

The design choice (locked v0.16): **SQLite adjacency-list hierarchy** over mem0 metadata tagging. Goals need tree traversal, brand isolation, status transitions, and FTS5 fuzzy-match for Codex extraction — all better served by a dedicated table than by Qdrant metadata.

---

## Schema (v16.0)

```sql
-- v0.16: goals table — adjacency-list hierarchy (parent_goal_id self-FK)
CREATE TABLE IF NOT EXISTS goals (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_goal_id        INTEGER,                 -- self-FK; NULL = root goal
    title                 TEXT NOT NULL,           -- short durable noun phrase, e.g. "Ship v0.16 episodic memory"
    description           TEXT,                    -- optional free-text; may be Codex delta_text or block_reason
    brand                 TEXT,                    -- brand scope: 'ai-ecosystem'|'brand-a'|'brand-b'|null
    status                TEXT NOT NULL DEFAULT 'open',   -- open|blocked|advanced|completed|abandoned
    priority              INTEGER DEFAULT 3,       -- 1=highest urgency, 5=lowest (Field(ge=1,le=5) on API)
    first_seen_session_id TEXT,                    -- session_id that created this goal
    completed_at          TEXT,                    -- ISO 8601 UTC; set when status→completed
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (parent_goal_id) REFERENCES goals(id),
    FOREIGN KEY (first_seen_session_id) REFERENCES sessions(session_id)
);
-- Hot read paths
CREATE INDEX IF NOT EXISTS idx_goals_parent        ON goals(parent_goal_id);
CREATE INDEX IF NOT EXISTS idx_goals_brand_status  ON goals(brand, status);
CREATE INDEX IF NOT EXISTS idx_goals_status_priority ON goals(status, priority, updated_at DESC);

-- FTS5 for fuzzy title-match during Codex extraction
CREATE VIRTUAL TABLE IF NOT EXISTS goals_fts USING fts5(
    title,
    description,
    content='goals',       -- content table: reads from goals at query time
    content_rowid='id',
    tokenize='porter unicode61'
);
-- Triggers keep goals_fts in sync: goals_ai (INSERT), goals_ad (DELETE), goals_au (UPDATE)
```

Valid statuses: `open`, `blocked`, `advanced`, `completed`, `abandoned`.

`episode_links` rows with `target_kind='goal'` + `link_type` ∈ `{advanced_goal, blocked_goal, completed_goal, cited_goal}` connect episodes to goals. `get_goal` returns `linked_episode_count` from this table.

---

## Write path

Every session end (via the L1a Stop hook chain) can create or match goals automatically:

```
Claude Code session ends
  → stop-extract.ps1 → l1a-extract.ps1
      1. Codex extraction returns:
           { facts: [...], episode: {
               goal, summary,
               advanced_goals: [{goal_title, delta_text}, ...],
               blocked_goals:  [{goal_title, block_reason}, ...],
               open_questions: ["...?", ...]
           }}
      2. l1a-extract.ps1 force-arrays all three list fields (@(...) wrap):
           $advancedGoals = @($parsed.episode.advanced_goals | Where-Object { $_ })
           $blockedGoals  = @($parsed.episode.blocked_goals  | Where-Object { $_ })
           $openQuestions = @($parsed.episode.open_questions | Where-Object { $_ })
         (fixes HIGH-2: Codex single-object → array coercion)
      3. POST /v1/episodes  →  app.py:create_episode  (ATOMIC — HIGH-5)
           For each advanced_goal:
             a. FTS5 fuzzy-match → find_goal_by_title_fuzzy (brand-scoped, NULL-safe IS)
             b. Match found → link to existing; if status=blocked → flip to open (MED-B)
             c. No match   → create new goal row
             d. Insert episode_link (link_type=advanced_goal)
           For each blocked_goal:
             a. Same fuzzy-match / auto-create
             b. flip status=blocked, insert episode_link (link_type=blocked_goal)
           Update episodes.advanced_goals / blocked_goals / open_questions JSON columns
           Single conn.commit() covers ALL writes (rollback on any failure)
```

### Fuzzy-match (NULL-safe, brand-scoped)

`find_goal_by_title_fuzzy` (episodic.py) always applies brand filter using SQL `IS` operator:

```sql
WHERE goals_fts MATCH ?
AND g.brand IS ?   -- IS is NULL-safe: NULL IS NULL → true; 'foo' IS 'foo' → true
```

This prevents cross-brand contamination when `brand=None` — a `None`-brand episode only matches `None`-brand goals, never brand-scoped ones (HIGH-4 fix).

FTS5 query strings are sanitized via `_sanitize_fts`: non-word characters stripped, each token phrase-quoted (`"AND"` not `AND`) so FTS5 reserved operators cannot crash the query (HIGH-1 fix).

---

## Read path

### REST endpoints (auth: `X-API-Key` header)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/goals` | Create a goal manually. Body: `{title, description?, brand?, parent_goal_id?, priority?}`. `priority` is validated `ge=1, le=5`. |
| `GET` | `/v1/goals` | List goals with optional `?status=&brand=&parent_id=&limit=` filters. |
| `GET` | `/v1/goals/tree` | Recursive CTE tree (flat list + depth). `?root_id=` optional. Must be registered BEFORE `/v1/goals/{id}` in FastAPI. |
| `GET` | `/v1/goals/{id}` | Single goal detail including `linked_episode_count`. |
| `PATCH` | `/v1/goals/{id}/status` | Update status. Body: `{status, actor (required), reason?, completed_at?}`. Appends a ledger entry to `~/.mem0/tier-ledger.jsonl` (HIGH-3 fix). |

### MCP tools (in `scripts/wsl/mem0-mcp-shim.py`)

| Tool | Description |
|---|---|
| `goal_create_manual(title, description?, brand?, parent_goal_id?, priority?)` | Manually create a goal |
| `goal_list(status?, brand?, parent_id?, limit?)` | List goals with optional filters |
| `goal_tree(root_id?)` | Full tree as flat list with depth |
| `goal_get(goal_id)` | Single goal detail |
| `goal_status_patch(goal_id, status, actor, reason?)` | Status transition with audit trail |
| `goals_gather_recent(brand?, limit?)` | Dream-consolidator gather helper |

### Dream-consolidator gather phase

`dream-consolidate.ps1` includes a goal context block in the nightly consolidation prompt (Phase 2 Gather). Open + blocked goals surface as priority signals — a goal blocked across multiple episodes escalates as a consolidation target.

### MEMORY.md hydration

`memory-index-build.py` appends an "Open goals" section (top-10 by priority) to MEMORY.md at dream time:

```
## Open goals (top 10 by priority)

- [P1] **ai-ecosystem** — Ship v0.16 episodic memory stack [open]
- [P2] **brand-a** — Launch Brand-A staging env [blocked: env var secrets missing]
```

This section loads at SessionStart so Claude sees active goals without a tool call.

---

## Operational

**DB location:** `~/.mem0/episodic.db` (WSL Ubuntu home, same file as episodic memory)

**Backup coverage:** `scripts/wsl/stack-backup.sh` backs up `episodic.db` (which contains the goals table). Keeps last 8 weekly snapshots.

**Query manually:**
```bash
# All open goals
sqlite3 ~/.mem0/episodic.db \
  "SELECT id, title, brand, priority, status FROM goals WHERE status='open' ORDER BY priority, updated_at DESC;"

# Goals linked to a specific episode
sqlite3 ~/.mem0/episodic.db \
  "SELECT g.id, g.title, el.link_type FROM episode_links el
   JOIN goals g ON el.target_id = CAST(g.id AS TEXT)
   WHERE el.episode_id = 42 AND el.target_kind = 'goal';"

# Blocked goals with block reason
sqlite3 ~/.mem0/episodic.db \
  "SELECT g.id, g.title, g.description, g.brand FROM goals g WHERE status='blocked';"

# Schema version
sqlite3 ~/.mem0/episodic.db "SELECT * FROM schema_meta;"
```

**Audit trail:** every `PATCH /v1/goals/{id}/status` appends a line to `~/.mem0/tier-ledger.jsonl`:
```json
{"ts":"2026-06-10T...","event":"goal-status-change","goal_id":42,"new_status":"completed","actor":"user-direct","reason":"shipped"}
```

---

## v0.17 hooks (reserved, not yet implemented)

The following features are scoped to v0.17 — do not add them in v0.16:

- **Global `open_questions` registry:** a separate table linking `open_questions` strings to goals, making them query-able rather than JSON blobs inside episode rows.
- **`resolved_in_session_id` tracking:** an FK on goals pointing to the session that completed them, enabling "when was this resolved?" queries.
- **Cycle guard in `create_goal`:** walk the parent chain at write time and reject cycles (currently only depth-bounded in the recursive CTE).
- **`DELETE /v1/goals/{id}` endpoint:** no hard-delete today; use `status='abandoned'` instead.
- **Rate-limiting on `/v1/goals`:** POST and GET are currently unthrottled; add per-minute cap when external callers beyond L1a are added.

---

## Known limitations

The v0.16 adversarial review (38 confirmed findings, artifact: `audit/v016-adversarial-review.json`) produced 17 LOW findings deferred to v0.17 and beyond. Listed verbatim with brief context:

**Correctness**
1. **Recursive CTE cycle guard absent** — a `parent_goal_id` pointing back up the chain produces depth-padded duplicate rows instead of an error (file: `episodic.py:404-439`). Triggers require a direct SQL UPDATE bypassing the API; no end-user path today. Fix: add `NOT IN (SELECT id FROM goal_tree)` to recursive arm + write-time ancestor walk.
2. **`create_session` overwrites `started_at` on re-fire** — `ON CONFLICT DO UPDATE SET started_at=excluded.started_at` means long sessions with multiple Stop events drift `started_at` forward (file: `episodic.py:207-217`). Fix: `COALESCE(sessions.started_at, excluded.started_at)`.
3. **`get_goal` linked_episode_count omits `cited_goal`** — `link_type IN ('advanced_goal','blocked_goal','completed_goal')` at `episodic.py:348-352` excludes `cited_goal`, which `link_episode_to_goal` accepts. Fix: add `cited_goal` to the COUNT filter.
4. **`_sanitize_fts` returns `'_'` for pure-punctuation input** — underscore is `\w`, so `!@#$_` yields `"_"` instead of `None`. FTS5 roundtrip is wasted; theoretically matches titles with underscores. Fix: add explicit `_` to the strip pattern or post-check for empty after stripping underscores.
5. **`GET /v1/goals/tree` returns `[]` for nonexistent root_id** — indistinguishable from an empty leaf node. Fix: check goal existence first and return 404.
6. **`get_goal_tree` max_depth=10 silently truncates deeper trees** — no warning, no truncation flag, endpoint cannot raise the cap. Fix: add `?max_depth=` query param; emit `truncated` flag in response.

**Security**
7. **Goal titles / transcript content forwarded to Codex without secret-scrubbing** — API keys, tokens, env vars in conversation may appear in Codex prompt (file: `l1a-extract.ps1:60-92`). Fix: pre-scrub common secret patterns before Codex call.
8. **No uniqueness constraint on `(session_id, started_at)`** — duplicate episode rows accumulate on Stop hook retry (file: `episodic.py:46-64`). Fix: `UNIQUE(session_id, started_at)` or a CHECK constraint.

**Operations**
9. **Goal cardinality explosion** — Codex hallucinated titles auto-create new goal rows without a dedup ceiling. Pollutes MEMORY.md and degrades FTS5 ranking over time. Fix: cap auto-creates per episode (e.g., max 3 new goals); add a periodic dedup job.
10. **WAL not auto-checkpointed** — `episodic.db-wal` will grow unbounded under concurrent L1a + dream writes. Fix: add `PRAGMA wal_autocheckpoint=1000` on each connection open or a nightly checkpoint cron.
11. **`find_goal_by_title_fuzzy` returns `[]` for very short titles** — FTS5 stops returning results for 1-2 character tokens after porter stemming. Very short goal titles (e.g. "CI") always auto-create instead of matching existing. Fix: fall back to `LIKE` match when FTS5 returns empty for short queries.
12. **No DELETE endpoint for goals** — no API path to permanently remove a goal. Operators must use `status='abandoned'` or raw SQLite. Fix: add `DELETE /v1/goals/{id}` in v0.17 with `actor` and ledger entry.
13. **No rate limit on goal creation** — `POST /v1/goals` and the auto-create path in `POST /v1/episodes` are unthrottled. Fix: add per-minute cap.

**Coverage gaps (deferred tests)**
14. **Empty-title goal skip is untested** — `POST /v1/episodes` with `advanced_goals=[{goal_title:''}]` silently skips it (correct), but no assertion prevents a future refactor from breaking this.
15. **`list_goals` parent_goal_id=0/'root' special case is untested** — `GET /v1/goals?parent_id=0` is the natural caller; no test covers it.
16. **`goal_create_manual` MCP tool deployed but never invoked through MCP in tests** — unit-level test coverage only.
17. **`link_type='cited_goal'` and `'completed_goal'` branches untested** — only `advanced_goal` is covered; invalid-link-type ValueError branch also untested.

---

## Audit trail

- **v0.16 adversarial review JSON:** `audit/v016-adversarial-review.json` (38 confirmed / 39 total findings; 4 lenses: correctness, security, operations, coverage)
- **v0.16 Phase D fix pass:** commit on `v0.16-build` branch — 5 HIGH + 3 MED fixed inline; 11 MED + 17 LOW deferred to v0.17
- **Live smoke:** see `audit/v016-live-smoke.md`
