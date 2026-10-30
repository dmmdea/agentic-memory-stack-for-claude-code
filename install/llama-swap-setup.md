# llama-swap setup — the one prerequisite the installer can't do for you

The memory stack needs a local inference endpoint on `127.0.0.1:11436` serving two
small models: the **EmbeddingGemma-2** embedder (768-dim, multilingual, multimodal; the `egemma2`
embedding profile, the default of a fresh install) and **bge-reranker-v2-m3** (the reranker). Both are served by
[llama-swap](https://github.com/mostlygeek/llama-swap), a tiny proxy that starts and
swaps [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` processes on
demand. `install/0-prereqs.ps1` (and `install/linux-authority.sh`) check this endpoint and fail until it's up.

Do this **before** the installer. It works the same on a Windows PC's WSL distro (Ubuntu assumed; adjust
paths for yours) and on a native Linux authority; the differences are called out. The authority needs the same
entries and the same aliases as its embedding profile: a replica that restores the authority's backups needs them too.

**Hardware.** The stack is sized for a GPU: every layer on the card (`-ngl 99` / `--n-gpu-layers 999`), unloading after
300 s idle. Rough VRAM: about 1.5 GiB at peak for EmbeddingGemma-2 with its projector (about 0.5 GiB text-only,
section 4), plus a few hundred MiB for the reranker. If a model does not fit, llama.cpp keeps the layers that fit
on the GPU and spills the rest to RAM. Total model files: about 1.3 GB.

- **WSL:** install the NVIDIA driver on **Windows** only; it exposes the GPU to WSL. Do not install a Linux display driver inside the distro. Check with `nvidia-smi` in WSL.
- **Native Linux:** the vendor driver, then the same `nvidia-smi` check.

## 1. Build llama.cpp (release b11452 or newer)

EmbeddingGemma-2 uses the `gemma-embedding2` architecture, which llama.cpp has from release **b11452**. An older
build fails the embeddings call with a model-arch error. (A box that stays on the legacy EmbeddingGemma-300m
profile, section 7, needs only b6384.)

```bash
sudo apt install -y build-essential cmake git
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
git -C ~/llama.cpp describe --tags          # must be b11452 or later
# NVIDIA: install the CUDA toolkit first (in WSL: the "WSL-Ubuntu" toolkit package from NVIDIA), then:
cmake -S ~/llama.cpp -B ~/llama.cpp/build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON
# other GPUs: -DGGML_VULKAN=ON instead of -DGGML_CUDA=ON
cmake --build ~/llama.cpp/build --target llama-server -j
```

Without a backend flag the build is CPU-only and the stack's GPU settings do nothing.

## 2. Install llama-swap

Download the Linux build from the
[llama-swap releases page](https://github.com/mostlygeek/llama-swap/releases): the archive whose name ends in
`linux_amd64.tar.gz` (`linux_arm64.tar.gz` on arm64). Extract it and put the `llama-swap` binary at
`~/.local/bin/llama-swap` (`chmod +x` it). Compare the archive's checksum with the one the release lists, if it lists one.

## 3. Download the models

```bash
mkdir -p ~/models
# EmbeddingGemma-2 (Q8_0, text model) and its projector — Apache-2.0
curl -fL -o ~/models/embeddinggemma-2-Q8_0.gguf \
  https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/resolve/main/embeddinggemma-2-Q8_0.gguf
curl -fL -o ~/models/mmproj-embeddinggemma-2-Q8_0.gguf \
  https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/resolve/main/mmproj-embeddinggemma-2-Q8_0.gguf
# bge-reranker-v2-m3 (Q4_K_M, ~440 MB)
curl -fL -o ~/models/bge-reranker-v2-m3-Q4_K_M.gguf \
  https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q4_K_M.gguf
# the two EmbeddingGemma-2 files must match these checksums (the installer checks them too)
echo "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135  $HOME/models/embeddinggemma-2-Q8_0.gguf
c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f  $HOME/models/mmproj-embeddinggemma-2-Q8_0.gguf" | sha256sum -c -
```

The installer's WSL phase stages the same two EmbeddingGemma-2 files when they are missing, and also fetches
the legacy EmbeddingGemma-300m file; fetching them yourself first is what lets llama-swap be configured before any
installer phase runs. No checksum is published for the reranker file here: take it from the model page you download it from.

## 4. Config — `~/llama-swap/config.yaml`

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
    members: ["embeddinggemma2", "bge-reranker-v2-m3"]

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

`--n-gpu-layers 999` puts every layer on the GPU: both models are small, so one
card holds them next to a chat seat, and a GPU cold start after the 300 s idle unload is
much faster than a CPU one. Host RAM is overflow, not the plan.

**Already running llama-swap for other models?** Add the two entries to your existing `models:` and add their names
to the `members:` of the `support` group you already have. Pasting a one-member group over it drops the other
members out of the group: they fall back into llama-swap's implicit swapping group and evict each other, which reads as
`/health/deep: embedder timed out`. A config generated from a llama-swap `matrix` instead of `groups` follows the same
rule: put the alias in the set the memory models already share and keep its other members.

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

## 6. Verify (this is what the installers check)

```bash
curl -sf http://127.0.0.1:11436/v1/models            # lists both models
curl -sf http://127.0.0.1:11436/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"embeddinggemma2","input":"title: none | text: hello"}' | head -c 200   # a 768-dim vector
```

Both return 200 → run the installer (`install.ps1`, or `install/linux-authority.sh`). If the embeddings call fails
with a model-arch error, your llama.cpp build is older than b11452 — rebuild from current master.

## 7. The legacy profile (EmbeddingGemma-300m)

A box that already holds a store from before 1.35.0 stays on the `egemma-300m` profile, whose vectors are a
different space from EmbeddingGemma-2's. Keep serving its alias while any store or backup set is still in that
space, and move to the new space with the migration in [`docs/MIGRATION.md`](../docs/MIGRATION.md). The 300m
model is under the Gemma Terms of Use (see `THIRD-PARTY-NOTICES.md`). It needs llama.cpp b6384 or newer, and its
entry joins the same `support` group (`members: ["embeddinggemma", "embeddinggemma2", "bge-reranker-v2-m3"]`):

```bash
curl -fL -o ~/models/embeddinggemma-300M-Q8_0.gguf \
  https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/main/embeddinggemma-300M-Q8_0.gguf   # ~330 MB
```

```yaml
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
```

Do not raise `--ctx-size` above 2048 for this model (its trained limit). Check it with
`-d '{"model":"embeddinggemma","input":"hello"}'` against `/v1/embeddings`.
