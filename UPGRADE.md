# Upgrade procedures

Per-component upgrade procedures. All assume you've read the upstream changelog first and the version is supported by our compatibility matrix in [`VERSIONS.md`](./VERSIONS.md).

**Common preconditions for any upgrade:**
- Run `audit/version-drift-check.ps1` first to confirm current state matches `VERSIONS.md`. (On-demand only — not scheduled. Run when you're about to think about upgrades.)
- Snapshot data: `tar -czf ~/backups/pre-upgrade-$(date +%F).tgz ~/.mem0 ~/qdrant-server/storage` (see [`docs/data-backup.md`](./docs/data-backup.md))
- Free time window — don't upgrade in the middle of an active Claude Code session
- Have rollback path planned (last-known-good `.tgz` snapshot + previous version pin)

After ANY upgrade, run `audit/upgrade-smoke.ps1` and `audit/version-drift-check.ps1`. Update `VERSIONS.md` + `CHANGELOG.md` only after smoke passes.

> **Why no auto-scheduler?** the operator intentionally keeps scheduled background work minimal (you already have the 3am C1 cron + L10 6h timer). Drift check is the kind of thing you run when you've decided to spend cycles on upgrades — not something that should bug you unprompted.

---

## Codex CLI (`codex`)

**Frequency:** as needed (the project ships frequent model-compat updates; symptom = headless `codex exec` returns "model X requires a newer version" or "not supported for ChatGPT account").

```powershell
# 1. Upgrade
npm i -g @openai/codex@latest

# 2. Verify
codex --version

# 3. Smoke test (verify the L1a/C1 invocation pattern still works)
"Reply with exactly: ok" | codex exec --skip-git-repo-check --sandbox workspace-write -c model_reasoning_effort='"low"' -

# 4. Bump VERSIONS.md, run upgrade-smoke, commit
```

---

## Claude Code CLI (`claude`)

**Frequency:** as needed (frequent updates; user is prompted on launch).

```powershell
# 1. Upgrade
npm i -g @anthropic-ai/claude-code@latest

# 2. Verify
claude --version

# 3. Restart VS Code so the MCP servers reload against the new claude binary
```

No memory-stack code changes typically needed.

---

## mem0ai (Python package, in `~/apps/mem0-server/.venv`)

**Frequency:** monthly check; mem0 has historically shipped breaking changes (v0.1.x → v2.0 was a major migration).

```bash
# 0. Snapshot the venv state so we can roll back
cp -r ~/apps/mem0-server/.venv ~/apps/mem0-server/.venv.bak-$(date +%F)
~/apps/mem0-server/.venv/bin/pip freeze > ~/apps/mem0-server/requirements.lock.bak

# 1. Stop the service
systemctl --user stop mem0.service

# 2. READ THE CHANGELOG. mem0 v0.1→v2 broke ~everything. Check before bumping.
#    https://github.com/mem0ai/mem0/blob/main/CHANGELOG.md
#    Specifically watch for: filters dict signature changes, default param changes,
#    extractor removal/renames, graph store changes.

# 3. Bump
~/apps/mem0-server/.venv/bin/pip install --upgrade 'mem0ai[nlp]==X.Y.Z'

# 4. If the version changed any API surface that mem0-server/app.py uses,
#    update app.py. Common touch points: Memory.add, Memory.search, Memory.update,
#    Memory.delete signatures and the filters dict shape.

# 5. Restart service, smoke test
systemctl --user start mem0.service
curl http://127.0.0.1:18791/health   # expect {"ok":true,"version":"X.Y.Z-v012",...}

# 6. Run upgrade smoke
pwsh -File /path/to/agentic-memory-stack-for-claude-code/audit/upgrade-smoke.ps1
```

**Rollback if it breaks:** `rm -rf ~/apps/mem0-server/.venv && mv ~/apps/mem0-server/.venv.bak-YYYY-MM-DD ~/apps/mem0-server/.venv && systemctl --user restart mem0.service`

---

## Qdrant (vector store)

**Frequency:** quarterly. Major versions occasionally include storage-format changes.

```bash
# 0. Snapshot
systemctl --user stop qdrant.service
tar -czf ~/backups/qdrant-pre-upgrade-$(date +%F).tgz ~/qdrant-server/storage

# 1. Download new binary
VERSION=X.Y.Z
cd /tmp
curl -fsSL -o qdrant.tar.gz "https://github.com/qdrant/qdrant/releases/download/v${VERSION}/qdrant-x86_64-unknown-linux-gnu.tar.gz"

# 2. Atomic-ish swap: extract to a new dir, symlink
mkdir -p ~/qdrant-server/binaries/${VERSION}
tar -xzf qdrant.tar.gz -C ~/qdrant-server/binaries/${VERSION}
ln -sf ~/qdrant-server/binaries/${VERSION}/qdrant ~/qdrant-server/qdrant

# 3. Restart + verify
systemctl --user start qdrant.service
curl http://127.0.0.1:6333/   # expect {"version":"X.Y.Z",...}

# 4. Verify collection survives
curl http://127.0.0.1:6333/collections/memories
```

**Rollback:** point the symlink back at the previous version under `~/qdrant-server/binaries/`.

---

## agentmemory

> **REMOVED in v0.13** — agentmemory and its MCP server (`@agentmemory/*`) have been removed from the stack. No upgrade procedure applies. See CHANGELOG for details. Episodic memory is a deliberate v0.14 gap.

---

## EmbeddingGemma (embedder, on llama-swap `:11436`)

> **v0.22:** Ollama + nomic-embed-text were **DECOMMISSIONED**. The embedder is now
> EmbeddingGemma-300m (Q8_0 GGUF, 768d) served by llama-swap on `:11436`, via the asymmetric
> query/document prefix shim `mem0-server/egemma_embedder.py`. Live Qdrant collection:
> `mem0_egemma_768`.

```bash
# The model is served by llama-swap (config: ~/llama-swap/config.yaml, always_loaded group).
# "Upgrading" the embedder means SWAPPING the GGUF / model - a deliberate re-embed, not a
# patch (see the WARNING). Verify the live embedder + collection are healthy:
curl -s http://127.0.0.1:18791/health/deep \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('collection:', d.get('collection'), '| qdrant:', d.get('checks',{}).get('qdrant'))"
```

**WARNING:** swapping the embedder model (or its embedding dimension) makes the
`mem0_egemma_768` collection **incompatible** (different dim, no vectors match) — you'd need to
delete and re-index the entire collection. Don't do this without the re-embed plan (see the
EmbeddingGemma migration in CHANGELOG).

---

## fastmcp (the MCP-shim framework)

```bash
~/apps/mem0-server/.venv/bin/pip install --upgrade 'fastmcp==X.Y.Z'
```

**Critical:** confirm `mcp.run(show_banner=False)` is still the right call signature. fastmcp 3.x prints an ANSI banner to stdout by default; if a future version changes how this is suppressed, the MCP shim will break in subtle ways (Claude Code will time out on the stdio handshake).

After upgrade, restart Claude Code and verify in a new session that `mcp__mem0__memory_health` returns the expected JSON.

---

## Python runtime in mem0 venv

If you need a newer Python (rare; mem0 v2.0.4 supports 3.9+):

```bash
# Reinstall the venv on a different Python
deactivate 2>/dev/null
mv ~/apps/mem0-server/.venv ~/apps/mem0-server/.venv.bak-py-upgrade
python3.13 -m venv ~/apps/mem0-server/.venv
~/apps/mem0-server/.venv/bin/pip install -r ~/apps/mem0-server/requirements.lock.bak   # restore from snapshot
systemctl --user restart mem0.service
```

---

## WSL kernel / Ubuntu

These are rare upgrades. If you do them:
- `wsl --update` for the kernel
- `do-release-upgrade` for Ubuntu (don't skip an LTS)
- After upgrade, run all four `install/0-prereqs.ps1` + `install/1-wsl-services.sh` + `install/2-windows-config.ps1` + `install/3-verify.ps1` in order

systemd configs and venvs typically survive; the services need to be restarted at minimum.

---

## What to do AFTER any upgrade

1. `audit/upgrade-smoke.ps1` — end-to-end test
2. `audit/version-drift-check.ps1` — confirm drift is zero
3. Update `VERSIONS.md` (version cell + Last verified date + add row to compatibility matrix)
4. Append entry to `CHANGELOG.md` under `## [Unreleased]`
5. `git commit -m "upgrade: <component> <old> -> <new> (verified)"`
6. `git push`

If smoke fails, **roll back to the snapshot** and document what broke in `docs/operations.md` § Known issues.
