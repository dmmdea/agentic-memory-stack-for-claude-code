# Agentic Memory Stack — troubleshooting & runbook

Read on demand from `SKILL.md`. All paths use `$HOME` / `<distro>` / `<wsl-user>` — substitute your own (the installer's receipt `~/.claude/scripts/mem0-stack.config.psd1` records the resolved values).

## llama-swap model entries (the one manual step)

The installer fetches `~/models/embeddinggemma-300M-Q8_0.gguf` and verifies the embed endpoint, but does not rewrite your llama-swap config. Add both models to the `always_loaded` group, bound to loopback:

```yaml
models:
  embeddinggemma:
    cmd: <llama.cpp>/build/bin/llama-server --model $HOME/models/embeddinggemma-300M-Q8_0.gguf
         --embeddings --pooling mean --n-gpu-layers 0
         --ctx-size 2048 --batch-size 2048 --ubatch-size 2048
         --port ${PORT} --host 127.0.0.1
    checkEndpoint: /v1/models
    ttl: 0    # never auto-unload
    aliases: ["embeddinggemma", "egemma", "embeddinggemma-300m"]
  bge-reranker-v2-m3:
    cmd: <llama.cpp>/build/bin/llama-server --model /path/to/bge-reranker-v2-m3.Q4_K_M.gguf
         --reranking --host 127.0.0.1 --port ${PORT}
    env: ["RERANK_DOC_MAX_CHARS=6000"]
groups:
  always_loaded:
    persistent: true
    swap: false
    exclusive: false
    members: ["embeddinggemma", "bge-reranker-v2-m3"]
```

> Do NOT raise `--ctx-size` above 2048 (the model's trained limit; the shim `mem0-server/egemma_embedder.py` truncates input to stay within it). llama-swap must bind `127.0.0.1:11436` (Test-MemoryStack WARNs if LAN-exposed). Verify:

```powershell
# 768-dim embedder:
$b = @{model='embeddinggemma'; input='title: none | text: ping'} | ConvertTo-Json
(Invoke-RestMethod -Uri 'http://127.0.0.1:11436/v1/embeddings' -Method Post -Body $b -ContentType 'application/json' -TimeoutSec 30).data[0].embedding.Count   # -> 768
# reranker:
$body = @{model='bge-reranker-v2-m3';query='test';documents=@('a','b');top_n=2} | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:11436/v1/rerank' -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 20
```

## Canonical-key: DPAPI interop

`tier=canonical` is the highest trust tier; the server enforces it via HMAC (nonce-signed `X-User-Direct-Token`). At rest the only artifact is the DPAPI-encrypted blob `~/.mem0/canonical-key.dpapi` (Windows user-scope). At `mem0.service` start, `ExecStartPre=-dpapi-fetch-key.sh` decrypts it via WSL→Windows interop into a tmpfs runtime dir (`/run/user/<uid>/mem0/canonical-key`, mode 0600). Provider precedence: runtime tmpfs → DPAPI blob → plaintext (dev fallback) → None (503).

**Provision a new box** (the installer generates a plaintext key only if neither plaintext nor blob exists):
1. WSL: `bash scripts/wsl/generate-canonical-key.sh`
2. Windows: `scripts\windows\dpapi-store-canonical-key.ps1 -KeyDir \\wsl.localhost\<distro>\home\<wsl-user>\.mem0`
3. Verify the tmpfs key appears + a canonize cycle passes, then remove the plaintext: `dpapi-store-canonical-key.ps1 -RemovePlaintext` (refuses unless the runtime injection chain is live).

**Confirm the key is loaded:** `/health/deep` reports `canonical_key {present, source}`; `source=runtime` means the tmpfs injection worked.

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:18791/health/deep' -TimeoutSec 5 | Select-Object -ExpandProperty checks
```

**Recovery** (server has no key / blob suspect): decrypt `canonical-key.dpapi` via `ProtectedData::Unprotect` → write `~/.mem0/canonical-key` → `chmod 600` → `systemctl --user restart mem0`. The blob is decryptable ONLY by the original Windows user while the DPAPI master-key chain survives — a Windows reinstall/profile loss destroys it, so back up the plaintext (or re-provision) before any such operation. Full backend detail: `docs/modular/dpapi-canonical-key.md`.

## Forgetting + backup infrastructure

- `decay-scan.timer` (Sun 02:00): soft-deletes expired `tier=temporal` records (by `valid_until`) + near-duplicate cleanup (`semantic-dedup.py`, 0.94 cosine).
- `stack-backup.timer` (Sun 03:30): snapshots Qdrant + history.db + episodic.db + tier-ledger + MEMORY.md + a manifest (app/schema version, git SHA, counts) to `~/.mem0/backups/` (keeps 8 weeks). Restore: `bash scripts/wsl/stack-restore.sh --snapshot <TS>` (supports `--dry-run`).

```bash
systemctl --user list-timers | grep -E 'decay-scan|stack-backup'
ls -lh ~/.mem0/backups/
```

## Troubleshooting matrix

| Symptom | Fix |
|---|---|
| `Prereq: WSL mirrored networking MISSING` | Add `[wsl2]\nnetworkingMode = mirrored` to `%USERPROFILE%\.wslconfig`, then `wsl --shutdown` and re-run |
| `Prereq: systemd in WSL MISSING` | Add `[boot]\nsystemd=true` to `/etc/wsl.conf` (inside WSL), then `wsl --shutdown` and re-run |
| `No WSL distro found` | `wsl --install -d Ubuntu` (or any distro), or pass `-Distro <name>` (see `wsl -l -q`) |
| `Prereq: Codex authenticated MISSING` | `codex login` → "Sign in with ChatGPT" |
| `Service mem0 not active` | `wsl -d <distro> -e bash -c "journalctl --user -u mem0.service -n 50"` |
| `mem0.service` crash-loops on `ModuleNotFoundError` | A module is missing from the deploy — re-run `install.ps1` (refreshes all `MEM0_MODULES` incl. `egemma_embedder.py`) |
| `Codex headless call MISSING` | `npm i -g @openai/codex@latest` (frequent model-compat updates) |
| `mem0 add+search MISSING` | Confirm llama-swap serves `embeddinggemma` on :11436 (returns a 768-dim vector), then `systemctl --user restart mem0.service` |
| `canonical key (server)` FAIL / 503 on canonical promote | DPAPI blob present: `systemctl --user restart mem0` (re-runs `dpapi-fetch-key`); no blob: `bash scripts/wsl/generate-canonical-key.sh`. Never generate a fresh key next to an existing blob |
| `build-hook-client.ps1 failed` | Confirm `Microsoft.NET\Framework64\v4.0.30319\csc.exe` exists; the build smoke-gates so a bad exe is discarded and `settings.json` stays untouched. Re-run |
| UserPromptSubmit slow / no `[MEMORY CONTEXT]` | Check the resident daemon (a hidden `powershell` running `mem0-hook-daemon.ps1`) + that `mem0-hook-client.exe` exists; rebuild via `build-hook-client.ps1`. The exe fails open to inline `user-prompt-extract.ps1` (which dot-sources `user-prompt-lib.ps1` — both must be deployed) |
| `deployed hooks freshness` WARN | A deployed script drifted from the repo or the exe is stale vs its `.cs`. Re-run `build-hook-client.ps1` and/or `install/2-windows-config.ps1` |
| `ConvertFrom-Json` error during MCP registration | Your `~/.claude.json` has case-colliding keys — the installer falls back to `-AsHashtable` (PS7) or warns + skips (PS5.1, config unchanged); add the `mem0` server manually if it warns (see below) |
| Timers not found | `systemctl --user enable --now decay-scan.timer stack-backup.timer goals-stale-sweep.timer contradiction-sweep.timer episodic-reconcile.timer l10-audit.timer` |

**Manual mem0 MCP registration** (if the installer warns it couldn't update `~/.claude.json`): add under `mcpServers.mem0`: `command=wsl.exe`, `args=["-d","<distro>","-e","/home/<wsl-user>/apps/mem0-server/.venv/bin/python","/mnt/c/Users/<win-user>/.claude/scripts/mem0-mcp-shim.py"]`.

## Rolling back the UserPromptSubmit hook

Point the `settings.json` UserPromptSubmit command at the inline extractor:

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:/Users/<win-user>/.claude/scripts/user-prompt-extract.ps1
```

Full rollback also removes the SessionStart entry running `mem0-hook-daemon-spawn.ps1` (the daemon self-terminates after 2h idle).

## Verbose install log

`.\install.ps1 -LogFile install.log` captures every step.
