# llama-swap setup — the one prerequisite the installer can't do for you

The memory stack needs a local inference endpoint on `127.0.0.1:11436` serving two
small models: an **EmbeddingGemma** embedder (768-dim, multilingual; EmbeddingGemma-300m, or
EmbeddingGemma-2 on a box whose embedding profile is `egemma2`, section 4b) and
**bge-reranker-v2-m3** (the reranker). Both are served by
[llama-swap](https://github.com/mostlygeek/llama-swap), a tiny proxy that starts and
swaps [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` processes on
demand. `install/0-prereqs.ps1` checks this endpoint and fails until it's up.

Everything below happens **inside WSL** (Ubuntu assumed; adjust paths for your distro).
Total footprint: ~600 MB of models. Run them on a GPU (every layer, `--n-gpu-layers 999`); see the note after the config.

## 1. Build llama.cpp (needs release b6384 or newer for the gemma-embedding arch; b11452 or newer for EmbeddingGemma-2)

The b6384 floor is EmbeddingGemma-300m's. EmbeddingGemma-2 uses the newer `gemma-embedding2`
architecture, which llama.cpp has from b11452: a box that serves it builds to that floor.

```bash
sudo apt install -y build-essential cmake git
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
cmake -S ~/llama.cpp -B ~/llama.cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build ~/llama.cpp/build --target llama-server -j
```

## 2. Install llama-swap

Download the latest linux binary from the
[llama-swap releases page](https://github.com/mostlygeek/llama-swap/releases) and put
it at `~/.local/bin/llama-swap` (`chmod +x` it).

## 3. Download the two models

```bash
mkdir -p ~/models
# EmbeddingGemma-300m (Q8_0, ~330 MB) — the installer also stages this if missing
curl -L -o ~/models/embeddinggemma-300M-Q8_0.gguf \
  https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/main/embeddinggemma-300M-Q8_0.gguf
# bge-reranker-v2-m3 (Q4_K_M, ~270 MB)
curl -L -o ~/models/bge-reranker-v2-m3-Q4_K_M.gguf \
  https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q4_K_M.gguf
```

## 4. Config — `~/llama-swap/config.yaml`

(A box on the `egemma2` profile adds the entry in section 4b to this file; it does not replace the ones below.)

```yaml
healthCheckTimeout: 300
logLevel: info

groups:
  # Both models mem0 depends on go in a NON-exclusive, NON-swapping group. This is the one
  # setting people get wrong. A model listed in NO group falls into llama-swap's implicit
  # default group (`swap: true, exclusive: true`), so every embed first has to drain whatever
  # chat seat happens to be loaded — on a box that also serves a large model that reads as
  # `/health/deep: embedder timed out` and a retrieval canary at 0/N, with nothing obviously
  # wrong in the logs. `swap: false, exclusive: false` lets them co-reside.
  support:
    swap: false
    exclusive: false
    members: ["embeddinggemma", "bge-reranker-v2-m3"]

models:
  embeddinggemma:
    cmd: ~/llama.cpp/build/bin/llama-server
      --model ~/models/embeddinggemma-300M-Q8_0.gguf
      --embeddings --pooling mean --n-gpu-layers 999
      --ctx-size 2048 --batch-size 2048 --ubatch-size 2048
      --port ${PORT} --host 127.0.0.1
    checkEndpoint: /v1/models
    ttl: 300
    aliases: ["embeddinggemma-300m"]

  bge-reranker-v2-m3:
    cmd: ~/llama.cpp/build/bin/llama-server
      --model ~/models/bge-reranker-v2-m3-Q4_K_M.gguf
      --reranking --pooling rank --n-gpu-layers 999
      --ctx-size 8192 --batch-size 4096 --ubatch-size 4096 --parallel 4
      --port ${PORT} --host 127.0.0.1
    checkEndpoint: /v1/models
    ttl: 300
```

(If you expand `~` manually, use your real home path — llama-swap does not expand `~`
inside `cmd` on every platform.)

`ttl: 300` on both. A model with no idle timeout holds its weights for the rest of the day on
a box you may also want for something else; 300 s is long enough that a working session
rarely pays a reload and short enough that an idle box gives the memory back.
The `support` group above keeps the models from fighting a chat seat for the slot — they
co-reside, so a reload is a cold start, not a queue behind someone else's model.

The 300 s is not free, though: the first embed or rerank after an idle spell pays that cold
start. The memory server absorbs it (an embedder that cannot answer yet gets a `503` +
`Retry-After` so writes queue and retry; a reranker that times out is retried once with a
longer allowance, then search falls back to dense order), and the SessionStart hook pre-warms
both through `GET /health/embedder?warm=rerank`. See
[`docs/systems/reranker.md`](../docs/systems/reranker.md).

`--n-gpu-layers 999` puts every layer on the GPU: both models are a few hundred MB, so one
card holds them next to a chat seat, and a GPU cold start after the 300 s idle unload is
much faster than a CPU one. Host RAM is overflow, not the plan. If a model ever does not fit
the card, let llama.cpp keep the layers that fit on the GPU and spill only the rest, rather
than forcing zero GPU layers.

## 4b. EmbeddingGemma-2 (the `egemma2` embedding profile)

A fresh install records `egemma2` (1.35.0), and a replica switches to it with `install.ps1 -EmbedProfile egemma2`
(docs/MIGRATION.md, "Replicas and PCs"). It needs its own llama-swap entry, because its vectors are a different
space from EmbeddingGemma-300m's, and it needs llama.cpp b11452 or newer. `install/1-wsl-services.sh` stages the
two files (the Q8_0 text model and its projector, sha256-checked) when the profile is `egemma2`, or with
`MEM0_STAGE_EG2=1` ahead of a move, and prints the entry below.

**Where the files come from.** Nothing else downloads those two files (section 3 fetches the 300m and reranker
files only), and the entry needs them. On a box that is not on `egemma2` yet, either stage them first
(`MEM0_STAGE_EG2=1 bash install/1-wsl-services.sh <wsluser> <winuser> <distro>`, inside WSL) or switch the profile
first (`install.ps1 -EmbedProfile egemma2` stages them on the same run, harmlessly on a dormant replica), and then
add the entry. Fetching `embeddinggemma-2-Q8_0.gguf` and `mmproj-embeddinggemma-2-Q8_0.gguf` from
`ggml-org/embeddinggemma-2-GGUF` into `~/models/` by hand also works; check them against the installer's
checksums (`EG2_SHA256`, `EG2_MMPROJ_SHA256` in `install/1-wsl-services.sh`).

```yaml
models:
  embeddinggemma2:
    cmd: ~/llama.cpp/build/bin/llama-server
      --model ~/models/embeddinggemma-2-Q8_0.gguf
      --mmproj ~/models/mmproj-embeddinggemma-2-Q8_0.gguf
      --embeddings --pooling mean
      --ctx-size 4096 --batch-size 4096 --ubatch-size 2048
      -ngl 99 --flash-attn on
      --port ${PORT} --host 127.0.0.1
    checkEndpoint: /v1/models
    ttl: 300
```

- `--mmproj` is the projector: images, audio and video embed into the same space as text. Text vectors are
  identical with and without it.
- Context 4096 with a 2048 micro-batch, never the 262144 the GGUF header advertises: the stack's hot
  window is 2048 tokens (budget 1,900), and a bigger micro-batch costs VRAM for inputs that never arrive
  (measured at these settings, flash attention on: about 1.2 GiB loaded and 1.5 GiB at peak after media
  embeds).
- The alias the stack asks for is `embeddinggemma2`; a box that serves the file under another name
  records it with `MEM0_EMBED_MODEL_EGEMMA2` in `~/.mem0/stack.env`.
- **Text-only on a small card** (a replica that cannot hold the projector beside its other models): drop the
  `--mmproj` line; the entry then loads at about 0.5 GiB. Text vectors are the same, so the replica searches
  and restores the same space. Record it on the box with `install.ps1 -MediaEmbedder off` (or
  `MEM0_SET_MEDIA_EMBEDDER=off` for `install/1-wsl-services.sh`; on a Linux replica, the line
  `MEM0_MEDIA_EMBEDDER=off` in `~/.mem0/stack.env`, which installers carry). Media memories are added and
  searched on the authority; this box then answers a media add or a media search with a 400 instead of sending
  image parts to a server that cannot read them, and `/health/deep` reports `checks.media.enabled: false`.

**It joins the group; it does not replace it.** The `support` group in section 4 already lists what the box
serves. Add `embeddinggemma2` to that `members:` list (for example `members: ["embeddinggemma",
"embeddinggemma2", "bge-reranker-v2-m3"]`). The installer prints a one-member `support: {..., members:
[embeddinggemma2]}` line as the *shape* of the group, and pasting it over an existing group drops the
reranker and the other embedder out of it: they fall back into llama-swap's implicit swapping group and
evict each other, which reads as `/health/deep: embedder timed out`. A config generated from a llama-swap
`matrix` instead of `groups` follows the same rule: put the alias in the set the memory models already share
(the `residents` set, in a config that names it so) and keep its other members. Every model still unloads after
`ttl: 300`. Keep `embeddinggemma` served while any store or backup set is still in the old space; a box
retires it only after its last set in that space is gone.

## 5. Run it as a service (systemd user unit)

`~/.config/systemd/user/llama-swap.service`:

```ini
[Unit]
Description=llama-swap local inference proxy (:11436)
After=network.target

[Service]
ExecStart=%h/.local/bin/llama-swap --config %h/llama-swap/config.yaml --listen 127.0.0.1:11436
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now llama-swap.service
```

## 6. Verify (this is exactly what 0-prereqs.ps1 checks)

```bash
curl -sf http://127.0.0.1:11436/v1/models            # lists both models
curl -sf http://127.0.0.1:11436/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"embeddinggemma","input":"hello"}' | head -c 200   # returns a 768-dim vector
```

Both return 200 → re-run `install.ps1`. If the embeddings call fails with a model-arch
error, your llama.cpp build is older than b6384 — rebuild from current master.

On a box that serves EmbeddingGemma-2 (section 4b), check its alias the same way. A model-arch error here
means the build is older than b11452:

```bash
curl -sf http://127.0.0.1:11436/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"embeddinggemma2","input":"title: none | text: hello"}' | head -c 200   # a 768-dim vector
```
