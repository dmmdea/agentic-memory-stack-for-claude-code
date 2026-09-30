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

`deploy.sh` in the table means a **WSL** brain or replica. On a **native Linux authority** (`~/.mem0/stack.env` `MEM0_HOST_KIND=native`) every `deploy.sh` step above is instead a re-run `install/linux-authority.sh --bind-ip <MEM0_BIND> --secrets-dir <MEM0_SECRETS_DIR>` from the checkout; `deploy.sh` refuses that host (see below).

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

**The live suites refuse production.** The files in `mem0-server/tests` that talk HTTP (the tier, actor-auth, security-invariant, episodic, bundle and retrieval suites) write real rows into whatever `MEM0_URL` names, and they used to be protected only by nobody exporting it. `tests/conftest.py` now runs `tests/_live_guard.py` before collection:

- **Target.** `MEM0_URL` (default `http://127.0.0.1:18791`) and, when set, `QDRANT_URL` must be loopback hosts, or the run exits with code 2 before any request. `AMS_ALLOW_LIVE_PROD_TESTS=1` is the explicit opt-in for a remote target; the pytest header then announces that the tests will write to it. `deploy.sh`'s post-restart retrieval gate sets it for its own authority (which may bind a non-loopback address) and prints it in the full-gate command it suggests on such a box. Loopback is not the same as scratch: on a brain, `127.0.0.1:18791` is still the production authority, so the tenant rule below is what keeps a local run out of your retrieval namespace.
- **Tenant.** Suites write under `test-*` tenants only. A suite that needs one tenant name for all its rows takes it from `live_test_tenant()` (`MEM0_TEST_USER_ID` when set, else `test-live`); it is never derived from `MEM0_DEFAULT_USER_ID` or the login name, and every request to the target is judged before it is sent: one carrying the stack's own tenant (env, `stack.env`, or the login user the unit substitutes) is refused, so is any `user_id` (body, nested filters or query string) that does not start with `test-`, and so is an add (`POST /v1/memories`) with no `user_id`, which would land in the server's default tenant. Calls that carry no tenant by design (health, by-id reads, deletes, tier patches, diagnose) pass, and a request to any other host is not judged. A static scan (`test_no_live_suite_hard_codes_a_tenant_outside_test_prefix`) also fails on a literal non-`test-` tenant in a live suite. Only calls made through `httpx.Client` are covered, so a suite that used another HTTP client would bypass the per-request check. The bundle suite's absence checks read the *server's* default tenant, so they skip unless `MEM0_TEST_USER_ID` names a `test-*` tenant that a scratch server uses as its default.
- **Cleanup asserts.** A seeded point is removed with `_test_cleanup.delete_memory(...)`, which signs the delete (format-2, with the nonce header the server requires) so canonical and insight seeds are removable too, then asserts a 2xx and reads the point back expecting 404. Insight seeds skip on a box without the canonical key rather than seed what they could never remove.

Conventions the suites encode: pure logic is factored into unit-testable helpers (decision matrices, parsers, prompt builders) pinned by tests; injection-defense prompt *structure* is pinned by tests (delimiter blocks, closing-tag neutralization); "the installer covers the server's import closure" is itself a test (`test_config_import_closure.py`) — and a CI gate.

**A suite never touches the operator's live state.** Much of this code resolves its paths from `$env:USERPROFILE` (`~\.claude\state`, `~\.mem0`), usually as a *parameter default* that a caller may omit — so a test that exercises the real entry point inherits the real directory. Any suite covering such a path sandboxes `$env:USERPROFILE` to a `TestDrive` root in `BeforeEach` and restores it in `AfterEach`. Two rules make the sandbox self-enforcing, because a leaking suite is otherwise indistinguishable from a passing one: assert against the sandbox path the code actually writes to (an assertion aimed at a directory the code never touches passes unconditionally and proves nothing), and include at least one *positive control* — a write the code is supposed to make, asserted present inside the sandbox — so removing the sandbox turns the suite red instead of silently redirecting the write to the real state directory.

The Python suites follow the same rule with a platform trap of their own: `HOME` alone moves `~` on POSIX but not on Windows, where Python resolves it from `USERPROFILE`, so a test that set only `HOME` and ran a script calling `Path.home()` wrote its receipt into the real profile. Tests redirect the home through `tests/_home_isolation.py` (`home_env(path)` for a child process, `apply_home(monkeypatch, path)` or the `isolated_home` fixture in-process), which sets `HOME`, `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH` and `Path.home()` together; `test_home_isolation.py` fails a suite that goes back to setting `HOME` by hand, in every directory CI's pytest step collects from (`mem0-server/tests`, `claude-config/tests`, `scripts/wsl`, `scripts/wsl/tests` and `scripts/tests`; the list is read from `ci.yml`, so a new directory is covered without an edit; the ones outside `mem0-server/tests` set `USERPROFILE` and `HOMEDRIVE`/`HOMEPATH` inline next to `HOME`). The single-runner locks of `contradiction-sweep.py` live under `~/.mem0` unless `MEM0_REJUDGE_LOCK`, `MEM0_EVIDENCE_LOCK` or `MEM0_PAIRS_LOCK` moves them, and its tests always do.

## Deploying a change to the live runtime

**One path** (v1.12, MEM-7 — born from a P0 where a hand-copied module never reached the installer):

```bash
bash scripts/wsl/deploy.sh [--dry-run]   # from the repo root, on a WSL brain or replica
```

It rsyncs server modules + maintenance scripts + sentinel-resolved systemd units, **import-smokes the server in its venv and refuses to restart on failure**, then restarts `mem0.service` and asserts `/health/deep` is green. Never hand-copy files into `~/apps/` — that's the exact failure class the single path exists to kill. Windows-side hooks redeploy via `install\2-windows-config.ps1` (idempotent). Rollback = `git checkout <last-good> && bash deploy.sh` (previous bytes also live in the weekly stack backup).

**A replica PC (Windows + WSL2): three steps, in this order.** A change reaches a replica in two places, the Windows copies under `~\.claude\scripts` and the WSL runtime under `~/apps`, and the scripts check that they agree:

1. **Windows side**, from PowerShell 7 in the checkout: `pwsh -NoProfile -File .\install\2-windows-config.ps1 -WslUser <wsl user> -Distro <distro>`. Everything else is recorded on the box and inherited when the flag is omitted: `-Role` (`%USERPROFILE%\.mem0\role`, else the receipt's `Role`; `install/role-lib.ps1`), `-AuthorityUrl` (`~/.mem0/authority-url` in WSL, else the receipt), `-AuthoritySsh` (the receipt, mirrored in `~/.mem0/replica.env` as `BRAIN_SSH`), `-HubHost` and `-EvalRootWsl` (the receipt). `-WslUser` is mandatory, and `-Distro` is needed when the stack's distro is not the WSL default. The recorded values live in the receipt `%USERPROFILE%\.claude\scripts\mem0-stack.config.psd1`, the `role` and `authority-url` files beside `%USERPROFILE%\.mem0` and their WSL twins, and `~/.mem0/stack.env` ([installer-and-deploy.md](./systems/installer-and-deploy.md), *Windows side*). `install.ps1` runs all four phases (prerequisites, WSL services, this step, verify) and, with the role inherited, is safe to re-run on a replica.
2. **WSL side**, from the repo root in WSL: `bash scripts/wsl/deploy.sh [--dry-run]`. Its pre-flight compares the Windows copies step 1 just wrote with the repo's and aborts before writing anything (exit 3, naming the step-1 command) when they are stale, which is why step 1 comes first. On a replica whose local mem0 is dormant it syncs the files, byte-compiles the modules and stops: no restart and no health gate, because this box reads the authority. A replica in travel mode (its local mem0 up) is restarted on loopback with the retrieval gate skipped.
3. **Verify**: `pwsh -NoProfile -File .\install\3-verify.ps1 -WslUser <wsl user> -Distro <distro>`. It checks the recorded authority is remote and answering, that no nightly task is registered on a replica, and the skew guard (every script `settings.json` names exists on disk).

**Native Linux authority: not `deploy.sh`.** A box whose `~/.mem0/stack.env` says `MEM0_HOST_KIND=native` renders units `deploy.sh` cannot (`__SECRETS_DIR__` credentials, the native `mem0.service` drop-in, one `ams-nightly` chain instead of the per-job timers). Since 1.31.2 `deploy.sh` refuses it before any write, on `--dry-run` too, and prints the right command. On that box, deploy (and roll back) with:

```bash
bash install/linux-authority.sh --bind-ip <MEM0_BIND> --secrets-dir <MEM0_SECRETS_DIR> [--dry-run]
```

Both values are in `stack.env` (`MEM0_BIND`, `MEM0_SECRETS_DIR`); every other flag is inherited from it on a re-run ([installer-and-deploy.md](./systems/installer-and-deploy.md), *Linux authority, native*). The authority is deployed from a **release tree**: extract the release archive of the tag (or check the tag out) and run the installer from it. The store binary is fetched for the tag `VERSION` names, and a tree without a `.git` stamps `DEPLOYED_SHA` from the stamp it carries, else `unknown` (never an empty file). The installer restarts `mem0.service` on every run and probes `/health` and `/health/deep` itself; `--dry-run` previews first.

## The eval harnesses (private repo)

`eval/` holds the measurement layer — run the relevant one before/after touching what it measures:

| Harness | Measures | Cost |
|---|---|---|
| `eval/faithfulness/` | does injected memory actually change behavior (causal-intervention, CMI loop) | Codex-judged (spend) |
| `eval/injection-gating/` | relevance-gate calibration + paraphrase robustness | free |
| `eval/findability/` | multi-hop + temporal retrieval guard (consumer-exact, deterministic; floors + exit 2 since W5) | free |
| `eval/promotion-gate/` | 4C gate calibration | Codex-judged |
| `eval/replay/` | real-query export/replay/compare (Jaccard@10 + top-1 stability; replay-vs-replay primary; needs the MEM0_LOG_FULL_QUERY operator opt-in for data) | free |

(Plus three narrower harnesses: `extractor-specificity/`, `intensification/`, `retrieval-drift/`.)

The free ones are regression guards — re-run them on any retrieval-path change; they exist precisely because "retrieval feels fine" has been wrong before. **Since W5 the honor system has a floor:** `deploy.sh` runs the PUBLIC retrieval-families suite (`mem0-server/tests/test_retrieval_families.py` — paraphrase, dilution, temporal supersession, cross-brand hard-negative with positive control, ES exact-token, keyword-only-tail union rescue) post-restart and ABORTS with a rollback hint on a family breach (`MEM0_SKIP_RETRIEVAL_GATE=1` is the recorded escape hatch). On any reranker/embedder/mem0 bump, additionally run `eval/replay/` export-before/replay-after.

## Conventions (the process that ships releases)

1. **Branch per change; never commit to `main`.** PR + merge even solo — the history is the audit trail.
2. **TDD for behavior** (failing test → minimal code → green) and **adversarial review before merge**: a fresh-context reviewer hunts the diff for defects; releases historically merge at 0 critical/high findings. For docs, the same gate verifies factual claims against code — measured necessity: doc reviews have caught confidently-wrong operator commands every time.
3. **Release ritual:** work happens on a feature branch and lands on `dev` by pull request with CI green; a release then promotes `dev` → `main` by pull request and tags `v<VERSION>`. CI is the set of jobs in [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml): the installer import-closure check, the Python and Pester product suites, and the ruff / shellcheck / PSScriptAnalyzer linters. Bump the root `VERSION`, add the matching `CHANGELOG.md` entry, and bring the `.claude-plugin/*.json` manifest versions to the same release together in that change — keeping the three in step is a manual discipline today, not an enforced gate (no CI job checks VERSION / CHANGELOG / manifest parity).
4. **Docs and code are edited here directly — nothing is generated from an upstream mirror.** Every PR must keep the docs and the code in agreement. The docs gate ([`../scripts/ci/check-docs.py`](../scripts/ci/check-docs.py)) enforces the structural floor: every relative Markdown link must resolve and the prose must stay operator-neutral (placeholder values only — no operator handles, machine names, or local paths).
5. **Keep the ledgers honest.** New tier mutations/deletions must append to the monthly tier-ledger; new background jobs need a health/summary line (`~/.mem0/*.jsonl`) and should fail *visible* (nonzero exit under systemd), while anything on the prompt hot path fails *open*.
6. **Update the docs with the change.** `mem0-server/requirements.txt` (floors, held in step with the installer's pip lines by `scripts/tests/test_dependency_floors.py`) is the dependency source of truth; `docs/systems/` deep-dives, `docs/flows/` pipeline walkthroughs, and `ARCHITECTURE.md` describe verified behavior — if your change makes a doc claim false, the same PR fixes the doc. Stale docs here have caused real operator damage (a runbook once instructed starting a decommissioned service).
7. **Check the private maintainer archive on every release.** The private archive repo tracks this repo as the code home; it holds only the non-shipped material (eval harnesses, research, audit records, build plans, private glue) and no live copy of the product code. On every promotion to `main`, confirm the archive needs no parity update — the product `VERSION`/`CHANGELOG` authority lives here (the archive's are frozen at the inversion point) and the old scrub/mirror pipeline is retired — and if a release changed something the archive references, update it in the same pass. This is a manual discipline; there is no cross-repo gate.

## Debugging entry points

- Server: `journalctl --user -u mem0.service -n 50`; request-level behavior via `/health/deep`, `~/.mem0/admission-rejected.jsonl`, and the retrieval log.
- Hooks: `~/.claude/logs/*.log` (per-component); the deployed `Test-MemoryStack.ps1` for the full liveness+invariants sweep.
- Background jobs: `systemctl --user list-timers` + each job's summary JSONL in `~/.mem0/`.
- Day-2 symptom → fix: [`operations.md`](./operations.md).
