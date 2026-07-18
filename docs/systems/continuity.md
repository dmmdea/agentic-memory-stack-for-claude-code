# Continuity — Phase 0 Design (v0.17)

## Why Phase 0 Exists

On 2026-06-11, the operator sent "1 and 2" answering two numbered questions I asked.
He then interrupted the action and restarted VS Code. The conversation context
window survived — but the memory stack hooks did not fire because no `Stop`
event was emitted between the decision and the restart. The `l1a-extract.ps1`
worker (which runs on Stop) never captured the decision. When the operator sent
"continue" in the new session, there was no memory trace of "1 and 2" anywhere
outside the running conversation window, forcing me to ask again.

This is the demonstrated hole: **the L1a extractor only fires on clean Stop
events**. Interruptions (VS Code restart, process kill, crash) produce no Stop,
so partial state is silently lost.

Phase 0 closes this hole with three complementary mechanisms.

---

## Architecture: Episode State Machine

```
UserPromptSubmit → upsert_in_progress_episode()
                     → episodes.state = 'in_progress'

(session proceeds; more UserPromptSubmit events → message_count++)

Stop / PreCompact → finalize_episode()
                     → episodes.state = 'complete'
                     → goal_text + summary_text set from L1a extraction

Stale-sweep (v0.18+) → episodes.state = 'abandoned'
                         → for in_progress rows older than N days
```

The critical property: after the FIRST user message in any session, there is
a durable `in_progress` row in `episodic.db`. If VS Code crashes before Stop
fires, that row remains. The next SessionStart will show the decision log via
`recent-decisions.jsonl` (Phase 0.C), and the next dream cycle will find the
orphaned `in_progress` row.

---

## Phase 0.A — UserPromptSubmit Hook

**Script:** `scripts/windows/user-prompt-extract.ps1`  
**Deployed to:** `C:\Users\youruser\.claude\scripts\user-prompt-extract.ps1`  
**Hook entry in settings.json:**

```json
"UserPromptSubmit": [
  {
    "matcher": "*",
    "hooks": [
      {
        "type": "command",
        "command": "powershell -NoProfile -ExecutionPolicy Bypass -File C:/Users/youruser/.claude/scripts/user-prompt-extract.ps1"
      }
    ]
  }
]
```

**v0.20 A.6 — registered command is now the COMPILED client.** The settings.json
entry above is the v0.17–A.5 shape; since A.6 the registered command is
`C:/Users/youruser/.claude/scripts/mem0-hook-client.exe` (built from
`scripts/windows/mem0-hook-client.cs`), which performs the A.5 daemon
transaction natively and falls back to `user-prompt-extract.ps1 -SkipDaemon`
on ANY failure (identical inline behavior). **Deploy flow:** edit the repo
`.cs` (or any hook script) → `Copy-Item` the changed `.ps1`s to
`~\.claude\scripts\` → run `scripts\windows\build-hook-client.ps1` (deploys the
`.cs`, compiles with framework csc, SMOKE-GATES the candidate — a broken exe is
never installed — then installs + re-smokes). R9 in Test-MemoryStack hashes the
deployed `.cs` and WARNs when the exe is missing or older than it. ROLLBACK:
set the UserPromptSubmit command back to
`powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:/Users/youruser/.claude/scripts/user-prompt-extract.ps1`.
Full rollback additionally removes the SessionStart hook entry running
`mem0-hook-daemon-spawn.ps1` from settings.json (daemon self-terminates after 2h
idle, or kill the hidden powershell running `mem0-hook-daemon.ps1`).

**Contract:** Claude Code delivers hook event data via stdin as JSON:
```json
{ "hook_event_name": "UserPromptSubmit", "prompt": "<text>", "transcript_path": "<path>" }
```

**Flow:**
1. Read stdin JSON. Validate `hook_event_name == 'UserPromptSubmit'`.
2. Extract `session_id` from transcript filename (UUID).
3. Infer `brand` from transcript path keywords (brand-a, brand-b, brand-d, ai-ecosystem).
4. POST to `http://127.0.0.1:18791/v1/episodes/checkpoint` with `{session_id, prompt_text[:300], brand, workspace, project}`.
5. Server calls `upsert_in_progress_episode()` — creates or updates the in_progress row.
6. Proceed to Phase 0.B decision detection.

**Performance contract:** sub-200ms. No Codex calls. No heavy I/O. One HTTP POST
to localhost. Failures logged to `~/.claude/logs/user-prompt-extract.log`, never block.

**Server endpoint:** `POST /v1/episodes/checkpoint`  
Returns `{ok, episode_id, action: 'created'|'updated', state: 'in_progress'}`.

**Backward compatibility:** The existing `POST /v1/episodes` (Stop hook) now calls
`finalize_episode()` which UPDATES the `in_progress` row to `state='complete'`
instead of always inserting a new row. If no in_progress row exists (hook not yet
active, direct API call), it falls back to inserting a new complete row —
v0.16 callers are unaffected.

---

## Phase 0.B — Auto-Capture User Decisions

**Trigger conditions (all must be true):**
1. Current user prompt is short: word count ≤ 25.
2. Current user prompt matches a decision token pattern:
   - Standalone number: `^\s*\d+\s*$`
   - Letter A-D: `^\s*[ABCD]\s*$`
   - Multi-numeric: `\b\d+\s+(and|y|o|or|,|&)\s+\d+\b` (e.g. "1 and 2")
   - Decision word: yes, no, both, all, skip, cancel, ok, proceed, done
3. The LAST assistant turn in the transcript (last 8 lines) matches a numbered-options pattern:
   - Two or more occurrences of `\b\d+\.\s` in a single message, OR
   - `\n\d+\.\s` / `\n[ABCD]\.\s` pattern in message content.

**False-positive mitigations:**
- All three conditions must hold simultaneously.
- Word count cap (≤ 25) prevents long answers from triggering.
- The assistant turn must be in the last 8 transcript lines — stale numbered lists don't match.
- Decisions are logged to `~/.claude/logs/user-prompt-extract.log` for audit.

**Decision record format:**
```json
{
  "ts": "2026-06-11T19:32:00.000+00:00",
  "session_id": "a71c302b-...",
  "question_preview": "Two questions:\n1. v0.17 scope = Tier-0 only ...",
  "answer": "1 and 2",
  "kind": "user-decision",
  "transcript_path": "C:\\Users\\youruser\\.claude\\projects\\...\\session.jsonl"
}
```

**Outputs:**
- Appended as JSON line to `\\wsl.localhost\Ubuntu\home\youruser\.mem0\recent-decisions.jsonl`
- POSTed to mem0 as `tier='stable'`, `source='user-decision'`, `kind='decision'`

---

## Phase 0.C — SessionStart Hydration of Recent Decisions

**Modified file:** `claude-config/storage-cap-check.sh` (deployed to `~/.claude/scripts/`)

At SessionStart, reads the last 5 entries from `~/.mem0/recent-decisions.jsonl`
(most recent first via `tail -5 | tac`) and emits:

```
[agentic-memory-stack] recent decisions (last 5):
  - 2026-06-11 19:32: 1 and 2 (Q: v0.17 scope = Tier-0 only OR Tier-1 + Tier-2?)
  - 2026-06-11 17:55: bump (Q: should the evidence threshold bump from 0.92...)
  ...
[agentic-memory-stack] last session next-up: ...
```

This output appears in the SessionStart hook output that Claude Code injects
into the beginning of each session context. It persists across VS Code restarts
because `recent-decisions.jsonl` is a file on WSL disk, not in-memory state.

**If `recent-decisions.jsonl` does not exist or is empty:** the section is
silently skipped (no error output).

---

## Episode State Transitions

| State | Who writes | When |
|---|---|---|
| `in_progress` | `upsert_in_progress_episode()` | Every UserPromptSubmit |
| `complete` | `finalize_episode()` | Stop / PreCompact hook |
| `abandoned` | stale-sweep (v0.18+) | Weekly sweep of old in_progress rows |

**Idempotency:** `upsert_in_progress_episode` uses a SELECT-then-INSERT/UPDATE
pattern (not UPSERT SQL) to handle the `state='in_progress'` filter correctly.
Multiple calls for the same session increment `message_count` and update `ended_at`.

**Schema:** The `episodes.state` column is added via `_add_column_if_missing()` —
a Python-side helper that catches `OperationalError: duplicate column name` so
`init_schema()` is idempotent on both fresh and migrated databases.

---

## Known Limitations

1. **Transcript format dependency:** Decision detection reads the transcript JSONL
   and parses assistant turn content. If Claude Code changes the transcript format,
   `user-prompt-extract.ps1` may fail silently (logged to user-prompt-extract.log).
   Monitor the log file if decisions stop appearing.

2. **UNC path for WSL files:** `recent-decisions.jsonl` is written via
   `\\wsl.localhost\Ubuntu\...` UNC path from PowerShell. This requires WSL2
   with mirrored networking to be running. If WSL is stopped, the write fails
   silently (logged).

3. **Hook timing:** UserPromptSubmit fires BEFORE the assistant responds, so the
   `in_progress` episode captures the user's prompt, not the assistant's output.
   The final `goal_text` and `summary_text` are only accurate after Stop finalizes.

4. **Decision false negatives:** The numbered-option detector requires the assistant
   turn to be in the last 8 lines of the transcript. Long tool-use sequences between
   the numbered question and the user reply may push the assistant turn out of range.
   If this becomes a problem, increase the `Tail` value in the script.

5. **mem0 write for decisions requires WSL warm:** If llama-swap (the embedder) is not running,
   the mem0 POST will fail. The `recent-decisions.jsonl` append still succeeds
   (it's a file write, not a service call). The SessionStart hydration still works
   from the JSONL file.

---

## Phase 0.D — UserPromptSubmit Proactive Memory Search (READ side)

**Added:** v0.17 Phase 0.D (2026-06-11), closes the operator's diagnosis: "proactive memory fetching is weak, you always forget to look, forget to use tools, forget where stuff is, and start trashing before looking at memory."

**The gap:** Phase 0.A–C fixes the WRITE side (decisions survive interruptions). But agentic Claude still has to remember to call `memory_search` before responding. It often doesn't. The READ side closes this by surfacing memory proactively — **before Claude responds** — so the data is in Claude's context without requiring any action from Claude.

**Script section:** `scripts/windows/user-prompt-extract.ps1` section 6 (Phase 0.D block, runs after 0.B decision capture, unconditionally for non-trivial prompts).

**Trivial-prompt filter:** Skip if word count < 3 or prompt is in the trivial list: `continue, yes, no, ok, okay, sure, go, next, stop, 1, 2, 3, a, b, c, thanks, thx`.

**What it fetches (in order):**

1. **mem0 semantic search** — `POST /v1/memories/search` with `{query: prompt[:500], filters:{user_id:'youruser', brand?}, limit:5, threshold:0.4, rerank:true}`. Top 3 hits rendered with tier and brand tags.
2. **Open goals** — `GET /v1/goals?status=open&limit=5&brand=<inferred>`. Rendered with priority.
3. **Open questions** — `GET /v1/open_questions?status=open&limit=3&brand=<inferred>` (404-safe; silently skipped pre-Phase-D since that endpoint ships later in v0.17).

**Output format (emitted to stdout → Claude Code injects into LLM context):**
```
[MEMORY CONTEXT — auto-surfaced by user-prompt-extract.ps1 v0.17 Phase 0.D]

Top 3 relevant memories:
  - [canonical|ai-ecosystem] mem0-server runs on :18791; episodic.db at ~/.mem0/episodic.db
  - [stable|brand-a] the operator chose SQLite+FTS5 sidecar over recency-weighted mem0 (v0.15 decision)
  - [evidence|ai-ecosystem] bge-reranker upgraded base (ctx=512, RERANK_DOC_MAX_CHARS=380) → v2-m3 (ctx=8192)

Open goals (3 shown):
  - [P2 OPEN] Ship v0.17 Phase 0.D + 0.E proactive fetch
  - [P3 OPEN] Restore drill for v0.17 Phase B
```

**Hook stdout injection:** Claude Code's UserPromptSubmit hook contract emits hook stdout as a system message injected into the LLM context **before** Claude processes the user's prompt. This is confirmed by the existing SessionStart mechanism (storage-cap-check.sh output appears as context at session start). If Anthropic changes this contract, the fallback is to write to `~/.mem0/last-surfaced-context.txt` and update CLAUDE.md to instruct Claude to read it.

**Performance budget:** Total hook ≤ 500ms.

| Operation | Typical latency |
|---|---|
| Phase 0.A checkpoint POST | ~50ms |
| Phase 0.B decision detection | ~5ms |
| Phase 0.D mem0 search + rerank | ~100–250ms |
| Phase 0.D goals fetch | ~5ms |
| Phase 0.D open_questions (404) | ~2ms |
| **Total** | **~165–315ms** |

Search and goals each have a 2s `TimeoutSec`. The overall hook latency stays well under 500ms in the normal case.

**Failure modes:**
- mem0 down/ECONNREFUSED: search block absent; logged; prompt not blocked.
- Timeout (> 2s): block absent; logged; prompt not blocked.
- Any exception in 0.D block: logged to `~/.claude/logs/user-prompt-extract.log`; hook exits 0.
- open_questions 404 (pre-Phase-D): silently skipped (catch block has no log entry to avoid noise).

---

## Phase 0.E — SessionStart Brand Context Auto-Load

**Added:** v0.17 Phase 0.E (2026-06-11).

**Goal:** Every new session starts with the relevant brand's top canonical facts and open goals already in Claude's context — before the first user message.

**Script section:** `claude-config/storage-cap-check.sh` (Brand context block, after recent-decisions section).

**Brand inference helper (`infer_brand_from_cwd`):**

| cwd pattern | brand |
|---|---|
| `*agentic-memory-stack*`, `*AI*Ecosystem*`, `*.claude*` | `ai-ecosystem` |
| `*brand-a*`, `*brand-a*`, `*brand-a*`, `*brand-a*` | `brand-a` |
| `*brand-b*` | `brand-b` |
| `*brand-c*` | `brand-c` |
| `*brand-d*` | `brand-d` |
| (no match) | `""` (block skipped) |

The hook uses `${CLAUDE_CWD:-$PWD}`. If Claude Code sets `CLAUDE_CWD` at hook invocation time, that is preferred. Otherwise `$PWD` at hook fire time is used. If neither resolves to a known brand, the block is silently skipped (recent-decisions and session_summary sections still emit).

**What it emits:**

1. **Top 5 canonical memories for the inferred brand** — fetches all memories (`limit=300`), filters in Python for `tier=canonical AND brand=<brand>`, takes first 5.
2. **Top 3 open goals for the brand** — `GET /v1/goals?status=open&brand=<brand>&limit=3`.

**Example output (ai-ecosystem):**
```
[agentic-memory-stack] brand context (ai-ecosystem):
  - [canonical] mem0-server runs on :18791; episodic.db at /home/youruser/.mem0/episodic.db
  - [canonical] bge-reranker-v2-m3 on :11436 — ctx=8192; use for all reranking
  - [goal P2 OPEN] Ship v0.17 Phase 0.D + 0.E proactive fetch
  - [goal P3 OPEN] Restore drill for v0.17 Phase B
```

**Performance:** curl `--max-time 4` for memories list, `--max-time 3` for goals. SessionStart has no strict latency budget (runs once at session start). Expected ~200–600ms.

**Failure modes:**
- mem0 down: curl `-fsS` fails silently; brand block absent; rest of SessionStart unaffected.
- No canonical memories for brand: block header emits but no `- [canonical]` lines — acceptable.
- `CLAUDE_CWD` / `$PWD` doesn't match any brand: block skipped silently.
- python3 not found: block absent (python3 is always available in the WSL Ubuntu environment).

---

## Pre-Tool Contradiction Check (Phase 0.F — shipped v0.17 as logging-only stub)

**Status:** Shipped in v0.17. Deployed to `C:\Users\youruser\.claude\scripts\pre-tool-check.ps1`.
Registered as PreToolUse hook in `settings.json` scoped to `Bash|Edit|Write|MultiEdit`.

**Intent:** Before Claude runs Edit / Write / Bash on a file or command, check if a canonical
memory contradicts the intended action. Surface "wait — you locked decision X about this" warnings
WITHOUT blocking — logging only until false-positive rate is measured.

**Threat model (false-positive costs):** Every Edit/Write/Bash call fires the hook. A false
positive on every tool call destroys developer experience. The cost asymmetry:
- False negative (miss a real contradiction): Claude proceeds with a potentially wrong action.
  Cost: 1 bad action. Recoverable via git.
- False positive (warn on a valid action): Every tool call triggers a warning. Cost: Claude
  becomes unreliable / noise-habituated. DX degrades permanently.

**The stub approach:** Ship with `exit 0` always. Log warnings to `~/.mem0/pre-tool-warnings.jsonl`
but NEVER block. After 1 week of real tool calls logged, review the JSONL for signal:
- High signal (real contradictions caught, low false positives): promote to enforcement in v0.17.1.
- Low signal / high noise: keep as logging-only or disable the hook.

**Implementation:**
- `scripts/windows/pre-tool-check.ps1`:
  - Reads stdin JSON: `{hook_event_name, tool_name, tool_input, transcript_path, session_id}`.
  - Skips if tool_name not in `{Bash, Edit, MultiEdit, Write}`.
  - Extracts query from tool args:
    - Bash: `tool_input.command[:500]`
    - Edit/MultiEdit: `file_path + old_string[:200]`
    - Write: `file_path + content[:200]`
  - POSTs to `POST /v1/memories/search` with `filters={user_id:'youruser'}`, `limit=5`,
    `threshold=0.55`, `rerank=false` (speed). Post-filters results for `tier=canonical`.
  - If canonical hits found: appends JSONL entry to `~/.mem0/pre-tool-warnings.jsonl`.
  - **Always exits 0** — never blocks, never returns non-zero.
  - Performance: ~300ms for search logic (powershell.exe spawn adds ~500ms — irreducible).
  - PS5.1 compatible: no em-dashes, no `??` operator, no null-coalescing.

**Warning format:**
```json
{
  "ts": "2026-06-11T05:30:00.000+02:00",
  "session_id": "a71c302b-...",
  "tool": "Edit",
  "tool_args_preview": "~/.mem0/canonical-key ...",
  "matched_canonical": ["canonical-key must remain at mode 600 ..."],
  "match_scores": [0.71]
}
```

**v0.17.1 promotion path:**
After 1 week (target: 2026-06-18), review `~/.mem0/pre-tool-warnings.jsonl`:
1. Parse entries: `python3 -c "import json; [print(l['tool'], l['tool_args_preview'][:60]) for l in map(json.loads, open('/home/youruser/.mem0/pre-tool-warnings.jsonl'))]"`
2. Classify each as: TRUE_POSITIVE (real contradiction), FALSE_POSITIVE (valid action flagged), or NOISE (no relevance).
3. If signal rate > 50% of non-noise entries: add enforcement path in v0.17.1:
   - On canonical hit with score > 0.75: output `{"decision": "block", "reason": "canonical contradiction: <text>"}` to stdout.
   - This blocks the tool call per Claude Code's PreToolUse hook contract.
4. If signal rate < 50%: lower threshold or add domain-specific filters before enforcement.

---

## Summary: What Changed in v0.17 Phase 0.D + 0.E

| Phase | File | Effect |
|---|---|---|
| 0.D | `scripts/windows/user-prompt-extract.ps1` | Every non-trivial user prompt now automatically searches mem0 + lists open goals, injecting results into Claude's context before Claude responds |
| 0.E | `claude-config/storage-cap-check.sh` | Every new session auto-loads canonical facts + open goals for the inferred brand from cwd |
| Docs | `docs/systems/continuity.md` | This document — design rationale, format, performance budgets, failure modes |

**Self-assessment — would Claude's next user-prompt now have relevant canonical memories in front of it without Claude needing to call memory_search?**

Yes, for non-trivial prompts: Phase 0.D injects a `[MEMORY CONTEXT ...]` block into the LLM context via UserPromptSubmit hook stdout before Claude responds. The hook runs in ~165–315ms, well under the 500ms budget. For session starts, Phase 0.E surfaces brand-relevant canonical memories at SessionStart so even the very first message of a session has context. Claude doesn't need to call memory_search for the common case — the data arrives automatically.
