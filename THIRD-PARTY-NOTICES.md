# Third-party notices

This project's own code (the installer, scripts, and the mem0-server FastAPI wrapper) is licensed under the **Apache License 2.0** (see `LICENSE` / `NOTICE`).

The stack **installs / fetches** the components below at install time — it does **not** redistribute them. Each is governed by its own license, which you accept when the installer obtains it. This list is informational.

## Runtime dependencies (permissive)

| Component | License | How it's obtained |
|---|---|---|
| [mem0ai](https://github.com/mem0ai/mem0) | Apache-2.0 | `pip install mem0ai` |
| [Qdrant](https://github.com/qdrant/qdrant) | Apache-2.0 | binary release download |
| [FastAPI](https://github.com/fastapi/fastapi), [pydantic](https://github.com/pydantic/pydantic) | MIT | pip |
| [uvicorn](https://github.com/encode/uvicorn), [starlette](https://github.com/encode/starlette), [httpx](https://github.com/encode/httpx) | BSD-3-Clause | pip |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 / BSD | pip (transitive) |
| [PyJWT](https://github.com/jpadilla/pyjwt) | MIT | pip (transitive; the installer sets its security floor) |
| [llama-swap](https://github.com/mostlygeek/llama-swap), [llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT | operator-provided local inference stack |

## Models (fetched at install; NOT redistributed by this repo)

| Model | License | Notes |
|---|---|---|
| **EmbeddingGemma-300m** (`ggml-org/embeddinggemma-300M-GGUF`) | **Gemma Terms of Use** — https://ai.google.dev/gemma/terms | The embedder. The installer downloads the GGUF from Hugging Face; **by downloading it you accept the Gemma Terms of Use** (a custom license with acceptable-use restrictions, not a standard OSS license). This repo ships no model weights. |
| **EmbeddingGemma-2** (`ggml-org/embeddinggemma-2-GGUF`, text model only) | **Apache License 2.0** | The optional second embedding space (`egemma2`: the wiki index by default on the operator's authority, the memories only after a measured migration). Downloaded by the operator; needs llama.cpp b11452 or later (`gemma-embedding2`). This repo ships no model weights. |
| **bge-reranker-v2-m3** (`BAAI/bge-reranker-v2-m3`) | MIT (see the model card) | Optional reranker; operator-provided in the llama-swap config. |

## Separately-installed proprietary tools (the operator's own)

- **Codex CLI** (OpenAI) — used as the subagent LLM via your ChatGPT subscription. Subject to OpenAI's terms.
- **Claude Code** (Anthropic) — the host being augmented. Subject to Anthropic's terms.

## Attribution

- `scripts/windows/dream-consolidate.ps1` is a port of [grandamenium/dream-skill](https://github.com/grandamenium/dream-skill) (MIT).
