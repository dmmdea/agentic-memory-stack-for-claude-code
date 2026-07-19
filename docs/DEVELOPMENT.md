# Development guide — working on the stack itself

How to change this system safely: the repo tour, the test suites, the deploy path, and the conventions that have kept ~90 releases regression-light. Development happens **here**: this repository is the primary source of truth for the product, edited directly through pull requests, and the test suites live here and run in CI. The eval harnesses and the per-release research and build material are not part of the shipped product and live in a separate maintainer archive.

## Repo tour (where a change goes)

| You're changing… | Edit here | Then |
|---|---|---|
| Server behavior (API, admission, tiers, freshness, episodic, write-gate) | `mem0-server/*.py` | pytest → `deploy.sh` |
| Capture / dream / hooks (Windows side) | `scripts/windows/*.ps1` | Pester → installer redeploy (`install\2-windows-config.ps1`) |
| Maintenance jobs (sweeps, decay, dedup, backup) | `scripts/wsl/*.py|sh` | pytest (where covered) → `deploy.sh` |
| MCP tool surface | `scripts/wsl/mem0-mcp-shim.py` | `deploy.sh` + restart the Claude Code session |
| systemd units / timers | `systemd/*` | `deploy.sh` (installs sentinel-resolved units) |
| Installer | `install/*` | run it — it's idempotent by contract |
| Docs | `*.md`, `docs/` | accuracy-review before merge (see conventions) |

Two invariants to respect when placing code: **all LLM judgment goes to Codex** (local models embed/rerank only), and **anything that mutates tiers or deletes must write the ledger**.

## Running the tests

```bash
# WSL, from the repo root — the Python suite (server + maintenance scripts; ~580 tests).
# It runs against the
# LIVE stack (mem0 + Qdrant + llama-swap must be up) and needs the API env, exactly as
# deploy.sh's own gate invokes it:
cd mem0-server && MEM0_KEY=$(cat ~/.mem0/api-key) MEM0_URL=http://127.0.0.1:18791 \
  ~/apps/mem0-server/.venv/bin/python -m pytest -q
# a focused file while iterating (same env vars — several modules read MEM0_KEY at import):
cd mem0-server && MEM0_KEY=$(cat ~/.mem0/api-key) MEM0_URL=http://127.0.0.1:18791 \
  ~/apps/mem0-server/.venv/bin/python -m pytest tests/test_admission_gate.py -q
```

```powershell
# Windows — the Pester suites (hooks, dream, autopromote, shim). Use the repo's runner —
# NOT a bare Invoke-Pester, which hits three known failure modes (system Pester 3.4.0
# shadowing 5.x, OneDrive/Defender DLL locks, and leaked hook-daemon processes hanging
# the shared-process run). The runner isolates each suite in its own child pwsh:
pwsh -NoProfile -File .\scripts\windows\Run-PesterTests.ps1
```

Conventions the suites encode: pure logic is factored into unit-testable helpers (decision matrices, parsers, prompt builders) pinned by tests; injection-defense prompt *structure* is pinned by tests (delimiter blocks, closing-tag neutralization); "the installer covers the server's import closure" is itself a test (`test_config_import_closure.py`) — and a CI gate.

## Deploying a change to the live runtime

**One path** (v1.12, MEM-7 — born from a P0 where a hand-copied module never reached the installer):

```bash
bash scripts/wsl/deploy.sh [--dry-run]   # from the repo root
```

It rsyncs server modules + maintenance scripts + sentinel-resolved systemd units, **import-smokes the server in its venv and refuses to restart on failure**, then restarts `mem0.service` and asserts `/health/deep` is green. Never hand-copy files into `~/apps/` — that's the exact failure class the single path exists to kill. Windows-side hooks redeploy via `install\2-windows-config.ps1` (idempotent). Rollback = `git checkout <last-good> && bash deploy.sh` (previous bytes also live in the weekly stack backup).

## The eval harnesses (private repo)

`eval/` holds the measurement layer — run the relevant one before/after touching what it measures:

| Harness | Measures | Cost |
|---|---|---|
| `eval/faithfulness/` | does injected memory actually change behavior (causal-intervention, CMI loop) | Codex-judged (spend) |
| `eval/injection-gating/` | relevance-gate calibration + paraphrase robustness | free |
| `eval/findability/` | multi-hop + temporal retrieval guard (consumer-exact, deterministic) | free |
| `eval/promotion-gate/` | 4C gate calibration | Codex-judged |

(Plus three narrower harnesses: `extractor-specificity/`, `intensification/`, `retrieval-drift/`.)

The free ones are regression guards — re-run them on any retrieval-path change; they exist precisely because "retrieval feels fine" has been wrong before.

## Conventions (the process that ships releases)

1. **Branch per change; never commit to `main`.** PR + merge even solo — the history is the audit trail.
2. **TDD for behavior** (failing test → minimal code → green) and **adversarial review before merge**: a fresh-context reviewer hunts the diff for defects; releases historically merge at 0 critical/high findings. For docs, the same gate verifies factual claims against code — measured necessity: doc reviews have caught confidently-wrong operator commands every time.
3. **Release ritual:** work happens on a feature branch and lands on `dev` by pull request with CI green; a release then promotes `dev` → `main` by pull request and tags `v<VERSION>`. CI is the set of jobs in [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml): the installer import-closure check, the Python and Pester product suites, and the ruff / shellcheck / PSScriptAnalyzer linters. Bump the root `VERSION`, add the matching `CHANGELOG.md` entry, and bring the `.claude-plugin/*.json` manifest versions to the same release together in that change — keeping the three in step is a manual discipline today, not an enforced gate (no CI job checks VERSION / CHANGELOG / manifest parity).
4. **Docs and code are edited here directly — nothing is generated from an upstream mirror.** Every PR must keep the docs and the code in agreement. The docs gate ([`../scripts/ci/check-docs.py`](../scripts/ci/check-docs.py)) enforces the structural floor: every relative Markdown link must resolve and the prose must stay operator-neutral (placeholder values only — no operator handles, machine names, or local paths).
5. **Keep the ledgers honest.** New tier mutations/deletions must append to the monthly tier-ledger; new background jobs need a health/summary line (`~/.mem0/*.jsonl`) and should fail *visible* (nonzero exit under systemd), while anything on the prompt hot path fails *open*.
6. **Update the docs with the change.** `VERSIONS.md` (private) is the dependency source of truth; `docs/systems/` deep-dives, `docs/flows/` pipeline walkthroughs, and `ARCHITECTURE.md` describe verified behavior — if your change makes a doc claim false, the same PR fixes the doc. Stale docs here have caused real operator damage (a runbook once instructed starting a decommissioned service).
7. **Check the private maintainer archive on every release.** The private archive repo tracks this repo as the code home; it holds only the non-shipped material (eval harnesses, research, audit records, build plans, private glue) and no live copy of the product code. On every promotion to `main`, confirm the archive needs no parity update — the product `VERSION`/`CHANGELOG` authority lives here (the archive's are frozen at the inversion point) and the old scrub/mirror pipeline is retired — and if a release changed something the archive references, update it in the same pass. This is a manual discipline; there is no cross-repo gate.

## Debugging entry points

- Server: `journalctl --user -u mem0.service -n 50`; request-level behavior via `/health/deep`, `~/.mem0/admission-rejected.jsonl`, and the retrieval log.
- Hooks: `~/.claude/logs/*.log` (per-component); the deployed `Test-MemoryStack.ps1` for the full liveness+invariants sweep.
- Background jobs: `systemctl --user list-timers` + each job's summary JSONL in `~/.mem0/`.
- Day-2 symptom → fix: [`operations.md`](./operations.md).
