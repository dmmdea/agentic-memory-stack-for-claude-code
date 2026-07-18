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
python -m pip install pytest httpx fastapi fastmcp
python -m pytest -q mem0-server/tests/test_admission_gate.py  # etc. — see ci.yml for the full list
```

```powershell
Invoke-Pester scripts/windows/tests -Output Detailed
```

## Live-stack suites

The remaining files in `mem0-server/tests/` exercise a **running deployment**
(mem0 on `:18791`, Qdrant on `:6333`, the llama-swap embedder on `:11436`, and
for some suites the Codex CLI). Run them on a box with the stack installed:

```bash
python -m pytest -q mem0-server/tests   # collects everything, live suites included
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
