# llama-swap setup — the one prerequisite the installer can't do for you

The memory stack needs a local inference endpoint on `127.0.0.1:11436` serving two
small CPU models: **EmbeddingGemma-300m** (the embedder, 768-dim, multilingual) and
**bge-reranker-v2-m3** (the reranker). Both are served by
[llama-swap](https://github.com/mostlygeek/llama-swap), a tiny proxy that starts and
swaps [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` processes on
demand. `install/0-prereqs.ps1` checks this endpoint and fails until it's up.

Everything below happens **inside WSL** (Ubuntu assumed; adjust paths for your distro).
Total footprint: ~600 MB of models, CPU-only — no GPU required for the memory stack.

## 1. Build llama.cpp (needs release b6384 or newer for the gemma-embedding arch)

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

```yaml
healthCheckTimeout: 300
logLevel: info

groups:
  always_loaded:
    swap: false        # both models stay resident; they're small and CPU-only
    exclusive: false
    members: ["embeddinggemma", "bge-reranker-v2-m3"]

models:
  embeddinggemma:
    cmd: ~/llama.cpp/build/bin/llama-server
      --model ~/models/embeddinggemma-300M-Q8_0.gguf
      --embeddings --pooling mean --n-gpu-layers 0
      --ctx-size 2048 --batch-size 2048 --ubatch-size 2048
      --port ${PORT} --host 127.0.0.1
    checkEndpoint: /v1/models
    ttl: 0
    aliases: ["embeddinggemma-300m"]

  bge-reranker-v2-m3:
    cmd: ~/llama.cpp/build/bin/llama-server
      --model ~/models/bge-reranker-v2-m3-Q4_K_M.gguf
      --reranking --pooling rank --n-gpu-layers 0
      --ctx-size 8192 --batch-size 4096 --ubatch-size 4096 --parallel 4
      --port ${PORT} --host 127.0.0.1
    checkEndpoint: /v1/models
    ttl: 300
```

(If you expand `~` manually, use your real home path — llama-swap does not expand `~`
inside `cmd` on every platform.)

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
