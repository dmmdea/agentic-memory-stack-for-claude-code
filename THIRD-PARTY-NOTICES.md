# Third-party notices

This project's own code (the installers, scripts, the mem0-server FastAPI wrapper and the `ams-store` Go client) is licensed under the **Apache License 2.0** (see `LICENSE` / `NOTICE`).

Two different things are listed here:

1. **Redistributed:** third-party code compiled into the `ams-store` binaries that each GitHub release attaches. Their license terms apply to anyone who receives those binaries. Section 1.
2. **Fetched at install time, not redistributed:** software and model weights the installers (or the documented setup) download onto your machine from their own projects. This repository ships none of it. Each is governed by its own license, which you accept when you obtain it. Sections 2 to 4.

Licenses below were read from the sources named in each row (a module's `LICENSE` file, a package's installed metadata, a model file's `general.license` header). Where a row says "see the project's license", the license was not checked here.

## 1. Embedded in the release binaries (redistributed)

Each release attaches `ams-store-windows-amd64.exe`, `ams-store-linux-amd64` and `ams-store-linux-arm64`, built with Go from `ams-store/` (`ams-store/go.mod`, `ams-store/go.sum`). Each binary contains:

| Component | Version | License | Copyright |
|---|---|---|---|
| [fsnotify](https://github.com/fsnotify/fsnotify) | v1.10.1 | BSD-3-Clause | Copyright © 2012 The Go Authors; Copyright © fsnotify Authors |
| [golang.org/x/sys](https://pkg.go.dev/golang.org/x/sys) | v0.48.0 | BSD-3-Clause | Copyright 2009 The Go Authors |
| The Go standard library and runtime | the toolchain that built the release | BSD-3-Clause | Copyright 2009 The Go Authors |

The Go standard library and `golang.org/x/sys` also ship a `PATENTS` file (an additional patent grant) with their sources; see the Go project.

### fsnotify (BSD-3-Clause)

```
Copyright © 2012 The Go Authors. All rights reserved.
Copyright © fsnotify Authors. All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright notice, this
  list of conditions and the following disclaimer in the documentation and/or
  other materials provided with the distribution.
* Neither the name of Google Inc. nor the names of its contributors may be used
  to endorse or promote products derived from this software without specific
  prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### golang.org/x/sys and the Go standard library (BSD-3-Clause)

```
Copyright 2009 The Go Authors.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

   * Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
   * Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
   * Neither the name of Google LLC nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## 2. Software the installers fetch (not redistributed)

| Component | License | How it is obtained |
|---|---|---|
| [Qdrant](https://github.com/qdrant/qdrant) | Apache-2.0 | binary release download (`install/1-wsl-services.sh`, `linux-authority.sh`, `linux-replica.sh`) |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT | built by you ([`install/llama-swap-setup.md`](./install/llama-swap-setup.md)) |
| [llama-swap](https://github.com/mostlygeek/llama-swap) | MIT | release download by you (same guide) |
| [uv](https://github.com/astral-sh/uv) | see the project's license | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, run by `install/linux-authority.sh` when `uv` is absent |
| [Codex CLI](https://github.com/openai/codex) | Apache-2.0 | `npm i -g @openai/codex`, by you |
| [nvm](https://github.com/nvm-sh/nvm) | MIT | only the hint `install/0-prereqs.ps1` prints when Node is missing; you run it |
| Claude Code (Anthropic) | proprietary; Anthropic's terms | by you; the host being augmented |

Codex CLI is used through your own ChatGPT subscription and is subject to OpenAI's terms.

## 3. Python packages (installed with `pip`, not redistributed)

`install/1-wsl-services.sh` and `install/linux-authority.sh` install these into the server venv; `mem0-server/requirements.txt` lists the version floors. Licenses are from the installed package metadata. Each package pulls in its own dependencies under their own licenses (`mem0ai[nlp]` pulls the most); inspect them with `pip-licenses` or `pip show` on your venv.

| Package | License |
|---|---|
| [mem0ai](https://github.com/mem0ai/mem0) | Apache-2.0 |
| [fastmcp](https://github.com/jlowin/fastmcp) | Apache-2.0 |
| [fastembed](https://github.com/qdrant/fastembed) | Apache-2.0 |
| [qdrant-client](https://github.com/qdrant/qdrant-client) | Apache-2.0 |
| [pip-audit](https://github.com/pypa/pip-audit) | Apache Software License (see the project's license for the version) |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 OR BSD-3-Clause |
| [FastAPI](https://github.com/fastapi/fastapi), [pydantic](https://github.com/pydantic/pydantic), [anyio](https://github.com/agronholm/anyio), [PyJWT](https://github.com/jpadilla/pyjwt) | MIT |
| [uvicorn](https://github.com/encode/uvicorn), [starlette](https://github.com/encode/starlette), [httpx](https://github.com/encode/httpx) | BSD-3-Clause |
| [numpy](https://github.com/numpy/numpy) | BSD-3-Clause (its wheels bundle components under 0BSD, MIT, Zlib and CC0-1.0) |

## 4. Models (fetched at install or setup; NOT redistributed by this repo)

This repository ships no model weights.

| Model | License | Notes |
|---|---|---|
| **EmbeddingGemma-2** (`ggml-org/embeddinggemma-2-GGUF`: `embeddinggemma-2-Q8_0.gguf` and its projector `mmproj-embeddinggemma-2-Q8_0.gguf`) | Apache-2.0 (the files' `general.license` header) | The default embedder (`egemma2`). `install/1-wsl-services.sh` downloads both files and checks their sha256. Needs llama.cpp b11452 or later. |
| **EmbeddingGemma-300m** (`ggml-org/embeddinggemma-300M-GGUF`) | **Gemma Terms of Use**, <https://ai.google.dev/gemma/terms> (the file's `general.license` header says `gemma`) | The legacy embedder. `install/1-wsl-services.sh` downloads this GGUF from Hugging Face on every install, whichever profile is active, without a prompt; by downloading it you accept the Gemma Terms of Use. They are a custom license with a prohibited-use policy, not a standard open-source license, and they attach conditions to hosting or redistributing the weights or derivatives of them (including passing the use restrictions on): read them before you share the file or a model made from it. |
| **bge-reranker-v2-m3** (`BAAI/bge-reranker-v2-m3`; the guide downloads the `gpustack/bge-reranker-v2-m3-GGUF` conversion) | Apache-2.0 (the converted file's `general.license` header; check the model card) | The reranker, downloaded by you ([`install/llama-swap-setup.md`](./install/llama-swap-setup.md)). |
| **Qdrant/bm25** | Apache-2.0 (the license `fastembed` records for it) | The BM25 sparse-encoder files `fastembed` downloads from Hugging Face on first use. |

## Attribution

- `scripts/windows/dream-consolidate.ps1` is a port of [grandamenium/dream-skill](https://github.com/grandamenium/dream-skill) (MIT).
