# Judge provider

## Purpose

The stack sends its language-model work to one provider per box: session extraction, memory
compaction, the dream's consolidation and promotion, the contradiction and supersession judges
and the write gate. This doc says which providers exist, how a box picks one, where the
credential lives and how spend is capped.

## Questions this doc answers

- Which provider does this box use, and how do I change it?
- Where does the Claude API key live, and what may read it?
- Which model does each job use?
- What does it cost, and what stops it from costing more?
- A job says `no_key`, `budget_exceeded` or `HTTP 401`. What now?

## Scope

The provider setting, the two transports behind it (Python and PowerShell), the models per job
class, the key file and the systemd credential, the spend ledger and the monthly cap, and the
health surfaces that report them.

## Non-scope

- What each job asks the model and what it does with the answer: [dream skill](./dream-skill.md),
  [reconciliation](./reconciliation.md), [memory capture](../flows/memory-capture.md).
- The Codex CLI's own login and the Windows HTTP shim: [codex hooks](./codex-hooks.md).
- The other keys (the API key, the canonical key, the service key): [key custody](./key-custody.md).
- Local models. They embed and rerank; they never judge, on either provider.

## Key concepts

- **Judge provider.** Who answers the stack's LLM calls on this box: `codex` (the Codex CLI on a
  ChatGPT login; the default, no key and no API spend) or `anthropic` (the Claude API on a key the
  operator supplies). The setting is `MEM0_JUDGE_PROVIDER`.
- **Job class.** `synthesis` is open-ended or consequential work: the dream's consolidation, the
  promotion nominations and their gate. `classify` is everything bounded: session extraction, the
  dream's gather step, the store judge, the yes/no contradiction and supersession judges, memory
  compaction. A job names its class, not a model.
- **Spend ledger.** One line per Claude API call in `~/.mem0/maintenance/llm-spend-YYYY-MM.jsonl`
  (UTC month). The monthly cap is read from it.
- **The `codex` label.** Function names, the `--judge codex` flag value, the `codex-usage.jsonl`
  ledger and the `codex` log component keep their names on both providers. Read them as "the
  configured LLM judge".

## How the system works

Every LLM call in the stack already passed through one of two functions. The provider is a
dispatch inside each, so no caller changed.

| Side | Choke point | Claude API transport |
|---|---|---|
| Python (the server and the authority's jobs) | `codex_shim_client.judge()` | `mem0-server/anthropic_judge.py`, the official SDK |
| PowerShell (the hooks and jobs on a PC) | `Invoke-CodexSubagent` in `scripts/windows/memory-common.ps1` | `Invoke-AnthropicJudge` in the same file, raw HTTP (there is no SDK for PowerShell) |

Both transports read the same settings, the environment first and `~/.mem0/stack.env` second,
so a unit needs no `Environment=` line and a hand run resolves the same values:

| Key | Default | Meaning |
|---|---|---|
| `MEM0_JUDGE_PROVIDER` | `codex` | `codex` or `anthropic` |
| `MEM0_JUDGE_MODEL_SYNTHESIS` | `claude-opus-5-5` | model of the `synthesis` class |
| `MEM0_JUDGE_MODEL_CLASSIFY` | `claude-haiku-5-5` | model of the `classify` class |
| `MEM0_JUDGE_MONTHLY_BUDGET_USD` | `20` | per-box monthly cap; `0` turns it off; a value that does not parse keeps the default |
| `MEM0_ANTHROPIC_WORKSPACE_ID` | unset | sent as the `anthropic-workspace-id` header; a key that is not scoped to one workspace needs it |

One call is one Messages request with the job's effort (`low`, `medium`, `high`) and a 16,000
token ceiling. Models that support it are called with the provider's server-side fallback, so a
declined request is re-served inside the same call; on the others a refusal is final. The
PowerShell transport returns its answer in the shape of a Codex CLI transcript, which is why the
existing parsers and usage log work unchanged.

Choosing the provider:

- **Native authority:** `bash install/linux-authority.sh ... --judge-provider anthropic
  [--anthropic-workspace-id <id>]`. The installer refuses the provider while the credential is
  missing or does not decrypt on this host, renders one drop-in per judging unit, and fails after
  the restart unless `/health/deep` shows the server sees the key and the SDK.
- **PC (Windows + WSL):** `.\install.ps1 -JudgeProvider anthropic [-AnthropicWorkspaceId <id>]`.
  The prerequisite check asks for the key file instead of the Codex CLI and its login, and the
  verify step makes one real call through the deployed transport.
- Both record the choice in `stack.env`; a re-run without the flag keeps it. `codex` goes back.

## Important flows

1. **A PC extracts a session.** The Stop hook starts `l1a-extract.ps1`, which calls
   `Invoke-CodexSubagent` with the classify pin. On the Claude API the call reads the key file,
   checks the cap, posts to the API, appends a ledger row and returns the transcript-shaped answer.
2. **The authority dreams.** `ams-step-dream.service` loads the credential through its drop-in.
   The dream checks the monthly cap where it checks the Codex quota on the other provider, then
   makes its gather (classify), consolidate and promote (synthesis) calls through `judge()`.
3. **A deploy.** The authority installer probes the credential with a real decrypt before it
   writes anything, and reads `checks.judge` back after the restart.

## Data and state

| What | Where | Notes |
|---|---|---|
| The setting and its four companions | `~/.mem0/stack.env` | carried across installer re-runs |
| The key, native authority | `<secrets-dir>/ams-anthropic-key.cred` | a systemd credential, encrypted to the host key and TPM |
| The key, PC or WSL-hosted box | `~/.mem0/anthropic-key` | owner-only (mode 600); on Windows it sits inside the WSL distro |
| Spend ledger | `~/.mem0/maintenance/llm-spend-YYYY-MM.jsonl` | `{ts, provider, model, input_tokens, output_tokens, cost_usd}`, plus `est: true` on a row estimated for a timed-out call; both transports append to the same file |
| Per-job usage | `codex-usage.jsonl` | unchanged; `model_resolved` names the Claude model that served |

The cost in the ledger is an estimate from a price table in each transport (kept in sync by
hand). The provider's console is the bill. A model the table does not know is priced at the
highest rate in it, so it can trip the cap early and never late.

What the ledger does not hold, and why the cap is an early warning and not the hard stop:

- **The true cost of a call that timed out on this side.** The provider may still finish and bill
  it, and no usage came back. The transport writes an estimated row for it (`"est": true`): the
  prompt at one token per three characters and no answer tokens. That can be under the bill, so
  set a spend limit on the workspace in the provider's console; that is the hard stop.
- **A row lost to a second writer on a WSL-hosted brain.** On a PC the PowerShell writers take a
  named mutex for the append, because the WSL share lets two writers take the same file offset
  (measured: 8 concurrent writers lost 74 of 320 rows without it and none with it). The Python
  jobs inside WSL append atomically but do not take that mutex, so a hook and a job that finish a
  call in the same millisecond can still cost one row.
- **A ledger that cannot be read.** It reads as 0 and the cap is not enforced for that call. The
  transport says so: on stderr (the journal) on the Python side, in `codex.log` on a PC. A row
  that cannot be written goes to the same place, so the spend is still on record.

## Interfaces and entry points

- `anthropic_judge.judge_once(prompt, effort, timeout_s, model)`: one call, a dict, never raises.
- `anthropic_judge.health()`: local state only (key present, SDK importable, cap), no network call.
- `anthropic_judge.usage_summary()`: this month's spend against the cap.
- `Invoke-AnthropicJudge` (PowerShell): throws on failure, like the Codex path; a timeout message
  contains `timed out`.
- `GET /health/deep` adds `checks.judge` on the Claude API provider.
- `GET /health/maintenance` reports `usage` from the spend ledger on that provider.
- The `codex-auth` capability row reads the key and the SDK on that provider.

## Dependencies

- The `anthropic` Python package in the server's virtual environment (the installers add it).
- Outbound HTTPS to the provider from every box that makes calls.
- systemd credentials on a native authority; the WSL distro's file system on a PC.

## Downstream effects

- The dream, the weekly sweeps and the write gate skip their judged work when the transport fails,
  as they do when Codex is unreachable. The chain stays green and the receipts say why.
- A PC whose key file is missing extracts nothing until the file exists. Its per-prompt
  checkpoints and retrieval do not use the judge and are unaffected.
- Model changes alter extraction and insight wording. They do not alter the trust model.

## Invariants and assumptions

1. **The key is never in an environment variable.** A coding agent that inherits
   `ANTHROPIC_API_KEY` stops using its own login, and every child process inherits the
   environment. Both transports read the file at call time and pass the key to the request only.
2. **A missing key is an error, not a reason to use the other provider.** The operator chose this
   provider; a silent switch would hide a broken install.
3. **A cap that misses a caller is not a cap.** Each transport writes the ledger row itself,
   because not every caller keeps a usage log.
4. **The cap is per box.** Each box keeps its own ledger, so the fleet's ceiling is the sum.
5. **Local models never judge.** Unchanged.
6. **A unit on the Codex provider names no Claude credential.** `LoadCredentialEncrypted=` for a
   file that is absent stops a unit from starting, so the drop-ins exist only on the Claude API.
7. **The key and the prompts go to one host.** Both transports send to `api.anthropic.com` and
   nowhere else. The Python transport hands that address to the SDK itself: left unset, the SDK
   takes `ANTHROPIC_BASE_URL` from the environment, and a job started from a coding agent's hook
   inherits that session's environment, so a gateway named there would receive the key.

## Error handling

The Python transport returns `{ok: false, error_type, error}`:

| `error_type` | Meaning | What to do |
|---|---|---|
| `no_key` | no credential and no key file | make the credential or the file (below) |
| `no_sdk` | the `anthropic` package is not importable | re-run the installer |
| `auth` | the provider refused the key (401, 403) | check the key and the workspace id |
| `budget_exceeded` | this month's estimate reached the cap | raise `MEM0_JUDGE_MONTHLY_BUDGET_USD`, or wait for the month to turn |
| `rate_limited` | 429 | waited out by callers that pass a retry budget |
| `client_timeout`, `unreachable` | the call did not complete | same handling as the Codex transports |
| `refusal` | the model declined, and no fallback served it | the job skips that item |
| `truncated`, `empty` | no text came back | the job treats it as a failed call |
| `http_<status>` | any other API error | read `error` |

The PowerShell transport throws with the same facts in the message. Both transports retry once
on a rate limit, a server error or a connection failure, inside the caller's time bound, and
never on a timeout: the bound is the caller's, and a repeated timed-out call can be billed twice.

Making the key:

```bash
# native authority (paste the key, press Enter, then Ctrl-D; nothing is written in plaintext)
systemd-creds --user encrypt --with-key=host+tpm2 --name=ams-anthropic-key - <secrets-dir>/ams-anthropic-key.cred

# PC: inside the WSL distro
mkdir -p ~/.mem0 && (umask 077; cat > ~/.mem0/anthropic-key)
```

## Security and privacy notes

- Session text leaves the box for the provider on either setting; the redaction pass runs before
  the call on both. Choosing the Claude API changes which provider receives it, nothing else.
- The credential on a native authority is readable only by the units that judge: the server, the
  dream and the two weekly sweeps. Hand-run wrappers (`ams-dream-now.sh`, `ams-service-run.sh`)
  load it the same way.
- Error messages pass through the redaction rules and never carry request headers.
- The spend ledger holds token counts and model names, no prompt text.

## Observability and debugging

```bash
# what the server sees (no network call, no spend)
curl -s http://<authority>:18791/health/deep | jq .checks.judge

# this month's spend against the cap
curl -s http://<authority>:18791/health/maintenance | jq .usage
tail -n 5 ~/.mem0/maintenance/llm-spend-$(date -u +%Y-%m).jsonl
```

On a PC, `install\3-verify.ps1` prints the transport's own error when the smoke call fails, and
`~\.claude\logs\codex.log` carries one line per call (`judge: provider=anthropic model=...`).

## Testing notes

The checks that matter are real runs: the installer's decrypt probe and post-restart read-back on
the authority, and the verify step's live call on a PC. `anthropic_judge.judge_once` takes a
client factory, so a test can drive the cap and the key order without the SDK or the network.

## Common pitfalls

- **Exporting the key "to test".** Do not. See invariant 1.
- **Expecting one cap for the fleet.** The cap is per box.
- **Reading `codex` in a log as the Codex CLI.** On the Claude API it is only a label.
- **A key made on another box.** A `.cred` file decrypts only on the host that encrypted it.
- **Replacing the credential without a restart.** systemd hands a credential to a unit when the
  unit starts. After replacing `ams-anthropic-key.cred`, restart `mem0.service`; the step units
  pick the new one up on their next run.
- **A native Linux client.** `install/linux-client.sh` has no provider switch yet: its capture step
  still asks for the Codex CLI and its login. The provider choice covers the native authority and
  the Windows + WSL boxes.
- **The Codex shim.** Only the Codex provider on a WSL-hosted brain uses it. A replica never
  starts it, and on the Claude API nothing calls it.
- **A PC whose WSL share is down.** A PC reads the provider setting from `stack.env` inside WSL on
  every call. While the share is unreachable the setting reads as its default, `codex`, so a hook
  that fires in that window takes the Codex path (and fails there if the CLI or its login is
  gone); `codex.log` gets a line saying the file could not be read. The mem0 key sits on the same
  share, so nothing is stored until WSL is back either way.
- **`ANTHROPIC_LOG=debug`.** The SDK's debug log does not carry the key (checked on SDK 2.5.0); it
  does carry the workspace id.

## Source map

- [`mem0-server/anthropic_judge.py`](../../mem0-server/anthropic_judge.py): the Python transport, the ledger, the cap.
- [`mem0-server/codex_shim_client.py`](../../mem0-server/codex_shim_client.py): `judge()`, `judge_transport()`, `model_for()`.
- [`scripts/windows/memory-common.ps1`](../../scripts/windows/memory-common.ps1): `Invoke-CodexSubagent`, `Invoke-AnthropicJudge`.
- [`scripts/wsl/ams_env.py`](../../scripts/wsl/ams_env.py): `judge_provider()`, `judge_model()`.
- [`systemd/judge-anthropic.conf`](../../systemd/judge-anthropic.conf): the per-unit drop-in.
- [`install/linux-authority.sh`](../../install/linux-authority.sh), [`install.ps1`](../../install.ps1), [`install/1-wsl-services.sh`](../../install/1-wsl-services.sh): the switches.

## Related docs

- [Selectable judge provider (ADR)](../architecture/decisions/selectable-judge-provider.md)
- [Codex as judge and extractor (ADR)](../architecture/decisions/codex-as-judge-and-extractor.md)
- [Key custody](./key-custody.md)
- [Codex hooks](./codex-hooks.md)
- [Dream skill](./dream-skill.md)
- [Operations](../operations.md)
