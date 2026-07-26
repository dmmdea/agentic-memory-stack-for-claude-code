# Testing

The product test suite lives in three places:

| Location | What it covers | Runner |
|---|---|---|
| `mem0-server/tests/` | server units (admission gate, canary, tier parity, redaction, shims, script contracts) | pytest |
| `claude-config/tests/` | the deployed hook helpers (PreCompact capture, SessionStart bundle, storage-cap advisory) | pytest (needs bash) |
| `scripts/windows/tests/` | Windows hook libs, installer parity, hook daemon/client, gates | Pester 5 (Windows) |
| `scripts/wsl/test_ship_log_*.py` | ship-log classifier/reclassifier | pytest |

## Headless (CI) subset

CI runs the subset that needs **no live stack** — see the explicit list in
`.github/workflows/ci.yml` (`tests` job) and the `pester` job. Dependencies:
Python 3.12+ with `pytest httpx fastapi fastmcp`; Windows with Pester 5.

```bash
# `python` does not exist on a stock Ubuntu/WSL box — use python3, or the stack venv
# (~/apps/mem0-server/.venv/bin/python) which already has the dependencies.
python3 -m pip install pytest httpx fastapi fastmcp
python3 -m pytest -q mem0-server/tests/test_admission_gate.py  # etc. — see ci.yml for the full list
```

```powershell
# Use the repo runner, NOT a bare Invoke-Pester: it pins Pester 5.x (the system 3.4.0 shadows it
# and breaks `Should -Be`) and imports from a repo-local, non-synced module cache.
pwsh -NoProfile -File .\scripts\windows\Run-PesterTests.ps1
```

## Live-stack suites

The remaining files in `mem0-server/tests/` exercise a **running deployment**
(mem0 on `:18791`, Qdrant on `:6333`, the llama-swap embedder on `:11436`, and
for some suites the Codex CLI). Run them on a box with the stack installed:

```bash
python3 -m pytest -q mem0-server/tests   # every mem0-server suite, live ones included
```

`mem0-server/tests/` is **not** the whole repo. Two more pytest trees exist and are not
collected by the command above — run them explicitly:

```bash
python3 -m pytest -q claude-config/tests    # hook-side: bundle, precompact, storage-cap
python3 -m pytest -q scripts/wsl            # ship-log classifier / reclassifier
```

`test_egemma_embedder.py` additionally needs the `mem0` package installed
(the server venv has it) and self-skips its live parts when the embedder is down.

## PII leak-guard patterns (operator-specific)

`InstallerParity.Tests.ps1` and `UserPromptExtract.Tests.ps1` include a
leak-guard that asserts deployed scripts carry no operator-specific values.
The generic assertions always run. To also guard YOUR names (machines, brands,
people), create `scripts/windows/tests/pii-patterns.local.txt` (gitignored;
one regex per line, `#` comments allowed) — start from the shipped
`pii-patterns.local.txt.example`.
