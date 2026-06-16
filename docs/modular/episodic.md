# Episodic Memory — Design & Schema Reference (v0.15)

Episodic memory is a SQLite + FTS5 sidecar (`~/.mem0/episodic.db`) that complements mem0/Qdrant with session-level temporal records. It answers the questions mem0's vector search cannot:

- "What was I working on last Tuesday at 4pm?"
- "Every session that touched Brand-A checkout"
- "When did the canonical-key idea first come up?"
- "What happened the day before the mem0 migration?"

## Why episodic?

| Need | mem0 / Qdrant | episodic.db |
|---|---|---|
| "What fact do I know about X?" | Yes — semantic search | No (not a fact store) |
| "What was I working on last Tuesday?" | No — no temporal index | Yes — `ended_at DESC` query |
| "Every session that touched Brand-A" | No — cross-session aggregation | Yes — brand + FTS5 filter |
| "1-2 sentence goal per session" | No — atomic fact granularity | Yes — `goal_text` column |
| Keyword search over session summaries | No — vector distance only | Yes — FTS5 MATCH, porter stemmer |
| Open questions / goal advancement (v0.16) | No — no structured AGI-paper columns | Yes — reserved hook columns |

The design choice (locked 2026-06-11): **SQLite + FTS5 sidecar** over recency-weighted mem0 search. The latter is retrieval polish; episodic is a distinct memory layer.

## Schema (v15.0)

```sql
-- sessions: one row per Claude Code conversation
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,        -- UUID from transcript filename
    started_at   TEXT NOT NULL,           -- ISO 8601 UTC (from file CreationTime)
    ended_at     TEXT,                    -- ISO 8601 UTC (set at session end)
    transcript_path TEXT,                 -- absolute path to the .jsonl on disk
    message_count INTEGER DEFAULT 0,
    brand        TEXT,                    -- 'ai-ecosystem'|'brand-a'|'brand-b'|'brand-c'|'brand-d'|'brand-a'|null
    workspace    TEXT,                    -- e.g. 'agentic-memory-stack', 'brand-a-platform'
    project      TEXT,                    -- finer-grained project tag
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- episodes: 1+ rows per session (most sessions = 1; long sessions may split)
CREATE TABLE IF NOT EXISTS episodes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT NOT NULL,
    goal_text        TEXT,               -- 1-2 sentence goal (Codex-extracted)
    summary_text     TEXT,              -- 2-4 sentence summary of what happened
    source_msg_start INTEGER,           -- index into transcript .jsonl (future use)
    source_msg_end   INTEGER,
    -- v0.16 hook columns (NULL in v0.15; v0.16 fills them via prompt extension):
    open_questions   TEXT,              -- JSON array: Epistemic Reachability principle
    advanced_goals   TEXT,              -- JSON array: {goal_id, advance_delta} Value Improvement
    blocked_goals    TEXT,              -- JSON array: {goal_id, block_reason}
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

-- FTS5 virtual table: keyword search over goal + summary
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    goal_text,
    summary_text,
    content='episodes',     -- content table: re-reads from episodes at query time
    content_rowid='id',
    tokenize='porter unicode61'  -- porter stemming + unicode-aware tokenization
);
-- Triggers sync episodes_fts with episodes on INSERT / DELETE / UPDATE

-- episode_links: cross-references to mem0 memory IDs this session produced/cited
CREATE TABLE IF NOT EXISTS episode_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  INTEGER NOT NULL,
    link_type   TEXT NOT NULL,          -- 'produced_evidence'|'produced_insight'|'cited_evidence'|'cited_canonical'
    target_kind TEXT NOT NULL,          -- 'mem0' (extensible)
    target_id   TEXT NOT NULL,          -- mem0 memory UUID
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (episode_id) REFERENCES episodes(id)
);

-- schema_meta: version tracking for future migrations
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- Seeded: schema_version='15.0', created_by_release='v0.15'
```

WAL mode is enabled on every connection (`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL`) for safe concurrent reads during dream-consolidator gather + L1a writes.

## Write Path

Every Claude Code session end triggers an episode write via the L1a Stop hook chain:

```
Claude Code session ends
  → Claude Code fires Stop hook
    → stop-extract.ps1 (reads stdin JSON, dispatches detached)
      → l1a-extract.ps1
          1. Throttle check (10-min window)
          2. Test mem0 health
          3. Read last 24 transcript turns (12 000 chars max)
          4. Codex extraction (gpt-5.5, effort=low, timeout=60s)
             Prompt returns STRICT JSON: { facts: [...], episode: { goal, summary } | null }
          5. POST each fact to mem0 as evidence (existing path)
          6. [NEW v0.15] If episode.goal is non-null:
             POST /v1/episodes  →  episodic.db
```

The Codex call is shared (one call returns both facts AND episode) so no extra cost vs v0.14.

If the episode POST fails, it is logged to `l1a.log` and does NOT abort fact posting. Episodes are best-effort bonus signal — the existing fact path is the primary write.

### Brand inference

Brand is inferred from the Claude Code project directory name (transcript path segment):

| Path segment | brand |
|---|---|
| `d--My-Drive-AI-Ecosystem` | `ai-ecosystem` |
| `D--repos-agentic-memory` | `ai-ecosystem` |
| `D--repos-brand-a-platform` | `brand-a` |
| `D--repos-brand-b` | `brand-b` |
| `D--repos-brand-c` | `brand-c` |
| `D--repos-brand-d` | `brand-d` |
| No match | `null` |

## Read Path

### REST endpoints (auth: `X-API-Key` header)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/episodes` | Write one episode (L1a Stop hook) |
| `POST` | `/v1/episodes/search` | FTS5 keyword search; returns `{results, count}` |
| `GET` | `/v1/episodes` | List last N episodes (`?recent=N&brand=...`) |
| `GET` | `/v1/episodes/{id}` | Full episode detail + linked mem0 IDs |
| `GET` | `/v1/episodes/count` | `{count, last_ended_at}` for health checks |

**Important:** `/v1/episodes/count` and `/v1/episodes/search` (POST) are registered BEFORE `/v1/episodes/{id}` in `app.py` — FastAPI matches routes in declaration order and a literal `/count` path must not be matched as an integer `{episode_id}`.

### MCP tools (in `scripts/wsl/mem0-mcp-shim.py`)

| Tool | Description |
|---|---|
| `episodic_search(query, since?, until?, brand?, limit?)` | FTS5 keyword search over goal + summary |
| `episodic_recent(limit?, brand?)` | Last N episodes by ended_at desc (default 7 — Miller's Law) |
| `episodic_get(episode_id)` | Full detail for one episode + linked mem0 IDs |

Example agentic queries:
```python
episodic_search("checkout flow", brand="brand-a", since="2026-06-01")
episodic_recent(limit=10)
episodic_get(42)
```

### Dream-consolidator gather phase (Phase 2)

`dream-consolidate.ps1` calls `Get-RecentEpisodes(7)` during Phase 2 (Gather) to add episodic context to the consolidation prompt. This lets the consolidator detect goal-continuity and contradictions across sessions — e.g., if the same goal appears blocked across three episodes, it surfaces as a priority consolidation signal.

### MEMORY.md hydration

`scripts/wsl/memory-index-build.py` appends a "Recent episodes" section (last 7 episodes) after the tier index. Format:

```
## Recent episodes (last 7)

- [2026-06-10] **ai-ecosystem** — Implement v0.15 episodic sidecar: write path, REST endpoints, MCP tools
- [2026-06-09] **brand-a** — Debug checkout total calculation bug in brand-a store
```

This section is rendered at SessionStart (MEMORY.md loads in the hydration hint) so Claude sees recent session context without a tool call.

## Operational

**DB location:** `~/.mem0/episodic.db` (WSL Ubuntu home)

**Backup coverage:** `scripts/wsl/stack-backup.sh` section `1e` backs up episodic.db weekly using the SQLite online-backup API (same pattern as `history.db`). Keeps last 8 snapshots. Run manually: `bash ~/.claude/scripts/stack-backup.sh`.

**Query manually:**
```bash
# Last 10 episodes
sqlite3 ~/.mem0/episodic.db \
  "SELECT id, ended_at, goal_text FROM episodes ORDER BY ended_at DESC LIMIT 10;"

# FTS5 keyword search
sqlite3 ~/.mem0/episodic.db \
  "SELECT e.id, e.ended_at, e.goal_text FROM episodes_fts
   JOIN episodes e ON episodes_fts.rowid = e.id
   WHERE episodes_fts MATCH 'checkout brand-a'
   ORDER BY rank LIMIT 10;"

# Schema version
sqlite3 ~/.mem0/episodic.db "SELECT * FROM schema_meta;"
```

**Disk usage:** ~1–2 KB per episode. At 10 sessions/day × 365 days ≈ 3.6–7.3 MB/year. No pruning needed in v0.15.

**Health check:** `Test-MemoryStack.ps1` includes an `episodic.db :v0.15` row:
- `OK` — at least 1 episode, last < 168h ago
- `WARN: empty` — normal post-ship until first real Stop event lands an episode with non-null goal
- `WARN: stale` — episodes exist but last > 7 days ago; investigate `l1a.log`
- `FAIL` — server error; check mem0 service health

## v0.16 Hook Columns

The schema reserves three nullable TEXT columns on `episodes` for v0.16 (Value Improvement + Epistemic Reachability principles from [Agent Exploration Toward AGI, SSRN-6748619]):

| Column | v0.16 purpose |
|---|---|
| `open_questions` | JSON array of open questions/uncertainties from this session (Epistemic Reachability) |
| `advanced_goals` | JSON array of `{goal_id, advance_delta}` — which goals did this session advance? (Value Improvement) |
| `blocked_goals` | JSON array of `{goal_id, block_reason}` — what was blocked? |

v0.16 work is prompt extension + endpoint reads — no schema migration needed because these columns exist already as NULLs. The dream-consolidator gather phase can use `open_questions` as a curiosity signal.

## Limitations / Known Issues

- **Episode dedup on Stop hook retry (M1):** if L1a fires twice before the throttle marks (crash scenario), two near-identical episode rows will be created. No data loss, just cosmetic duplication. Fix planned for v0.16: UNIQUE constraint on (session_id, started_at).
- **FTS5 NULL column (M2):** episodes with `goal_text=NULL` are not indexed in FTS5 and will never appear in keyword search. The L1a guard prevents null-goal episodes from reaching the DB, but the behavior is documented.
- **Schema migration path (M3):** v0.15 is schema_version 15.0 (first version). v0.16 additive changes (ADD COLUMN) will work via `IF NOT EXISTS`; destructive changes require explicit ALTER TABLE migration logic in `init_schema()`. Undocumented pattern — see the episodic.py comment block for v0.16.
- **Goal/summary length (M4):** no server-side cap on `goal_text`/`summary_text`. Practical cap comes from the Codex prompt ("1-2 sentences"/"2-4 sentences"). v0.15.1 patch: add `max_length=2000` validators to `EpisodeIn`.
- **No UI dashboard:** deferred; query via sqlite3 CLI or MCP tools.
- **Cross-PC sync:** episodic.db lives in `~/.mem0/`, which is covered by Syncthing. No conflict-resolution story if two PCs write episodes simultaneously (unlikely in practice — one primary workstation).
- **L5 WARN masking:** `Test-MemoryStack WARN: empty` persists indefinitely on broken Stop hook. If you see this > 2 days after active work sessions, check `l1a.log`.
