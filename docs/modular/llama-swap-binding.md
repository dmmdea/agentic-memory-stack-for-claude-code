# llama-swap Binding — 127.0.0.1 Closure (v0.18 Phase D)

> **Status:** SHIPPED 2026-06-11. Closes the v0.17 `Test-MemoryStack` INVARIANTS WARN: llama-swap was listening on `0.0.0.0:11436` (wildcard), exposing bge-reranker-v2-m3 and every other model behind llama-swap to the LAN. The bind row is now GREEN (`127.0.0.1:11436`).

## What changed

The config files live OUTSIDE this repo (external user config on the WSL side); this document is the repo-side record of the change.

Two places could carry the listen address; both were audited:

1. **systemd user unit** `/home/youruser/.config/systemd/user/llama-swap.service` — **this was the source of the wildcard bind** and the only file edited:

   ```diff
   ExecStart=/home/youruser/.local/bin/llama-swap \
     --config /home/youruser/.zora/llama-swap.yaml \
   -  --listen :11436
   +  --listen 127.0.0.1:11436
   ```

   (`--listen :11436` with no address = bind all interfaces, shown by `ss` as `*:11436`.)

2. **llama-swap yaml** `/home/youruser/.zora/llama-swap.yaml` — audited, **no edit needed**: every `cmd:` line invoking `llama-server` (nomic-embed, bge-reranker, bge-reranker-v2-m3, all chat models) already carries `--host 127.0.0.1`, so the per-model backend ports were never LAN-exposed. Only the llama-swap proxy listener itself was.

## Why

- v0.17 `Test-MemoryStack.ps1` INVARIANTS flagged `llama-swap bind` WARN: `0.0.0.0:11436`.
- :11436 fronts bge-reranker-v2-m3 (mem0 search reranking) plus every local LLM. A wildcard bind on the WSL side is reachable from the LAN — no auth on llama-swap, so any LAN host could run inference or enumerate models.
- Loopback-only is sufficient: the only consumers are mem0-server and Claude Code on this machine.

## Why Windows localhost still works (mirrored networking)

WSL2 runs with `networkingMode=mirrored` (`.wslconfig`). Under mirrored networking, localhost is bidirectional between Windows and WSL: a `127.0.0.1` bind inside WSL is reachable from Windows at `http://127.0.0.1:11436`, exactly like mem0 on `:18791`. Verified post-change with a rerank smoke call from Windows PowerShell (below) — 3-result response, no fallback.

## Restart procedure

```bash
# from Windows:
wsl.exe -e bash -c "systemctl --user daemon-reload && systemctl --user restart llama-swap"
# daemon-reload is required only when the unit file changed.
```

## Rollback procedure

Backups taken 2026-06-11 before the change:

- `/home/youruser/.zora/llama-swap.yaml.bak-v018`
- `/home/youruser/.config/systemd/user/llama-swap.service.bak-v018`

```bash
wsl.exe -e bash -c "cp /home/youruser/.config/systemd/user/llama-swap.service.bak-v018 /home/youruser/.config/systemd/user/llama-swap.service && cp /home/youruser/.zora/llama-swap.yaml.bak-v018 /home/youruser/.zora/llama-swap.yaml && systemctl --user daemon-reload && systemctl --user restart llama-swap"
wsl.exe -e bash -c "ss -tlpn | grep 11436"   # expect *:11436 after rollback
```

## Verification commands

Bind check (must show `127.0.0.1:11436`, not `*:11436` / `0.0.0.0:11436`):

```bash
wsl.exe -e bash -c "ss -tlpn | grep 11436"
# LISTEN 0  4096  127.0.0.1:11436  0.0.0.0:*  users:(("llama-swap",...))
```

Rerank smoke from Windows (mirrored-networking proof; first call may cold-load the model — allow a generous timeout):

```powershell
$body = @{model='bge-reranker-v2-m3'; query='ping'; documents=@('a','b','c'); top_n=3} | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:11436/v1/rerank' -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 180
# expect: results array with 3 entries (index + relevance_score)
```

WSL-internal check:

```bash
wsl.exe -e bash -c "curl -s http://127.0.0.1:11436/v1/models | head -c 300"
```

Full stack check: `& C:\path\to\agentic-memory-stack-for-claude-code\scripts\windows\Test-MemoryStack.ps1` — INVARIANTS row `llama-swap bind` must be `OK 127.0.0.1:11436`.
