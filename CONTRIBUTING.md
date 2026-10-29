# Contributing

Thanks for helping. Pull requests are welcome against `main`. For a vulnerability, do not open a public issue; see [SECURITY.md](./SECURITY.md).

## Where to start

- [docs/DEVELOPMENT.md](./docs/DEVELOPMENT.md): the repo tour (where each kind of change goes), the deploy path and the conventions.
- [docs/TESTING.md](./docs/TESTING.md): where the test suites live and how to run them.
- [docs/README.md](./docs/README.md): the documentation map. Read the system or flow doc for the area you change before you change it.
- `CLAUDE.md` and `AGENTS.md` are guides for AI coding agents working in this repo; humans can skip them.

## What a pull request must do

1. **Branch per change.** Never commit to `main` directly; open a pull request against `main`.
2. **Keep docs and code in agreement.** If your change alters behavior, a flag, a default or an interface, update the affected docs in the same pull request.
3. **Add or update tests for behavior you change**, in the suite that covers the area (`docs/TESTING.md`). Changing the installer's server file list must keep `mem0-server/tests/test_config_import_closure.py` green.
4. **Stay operator-neutral.** Write placeholders in code, docs, comments, test data and commit messages: `<user>`, `<authority-host>`, `<tailnet-ip>`, `192.0.2.10`, `example.test`. No real user names, host names, IP addresses, home paths, drive letters, e-mail addresses or names of private tools or repositories. The vocabulary is in [docs/STYLE.md](./docs/STYLE.md).
5. **Do not add ticket IDs or dated incident stories** to docs and comments. State the plain fact and the reason.

## The gates CI runs

CI runs on every pull request into `main` (and `dev`) and must be green. The jobs are in [.github/workflows/ci.yml](./.github/workflows/ci.yml):

| Job | What it checks | Run it locally |
|---|---|---|
| Installer import closure | every `mem0-server` module is shipped by the installer | `bash scripts/ci/check-import-closure.sh` |
| Python tests | the headless public pytest suites | the file list in `ci.yml` (`tests` job); `python -m pytest -q <files>` |
| Docs gate | every relative Markdown link resolves; ADR front matter; no CPU-inference or zero-ttl guidance | `python scripts/ci/check-docs.py` |
| Privacy gate | no personal or machine-specific identifier in files, file names or commit messages | `python scripts/ci/privacy-gate.py` |
| Pester | the Windows hook and installer suites | `pwsh -NoProfile -File .\scripts\windows\Run-PesterTests.ps1` |
| ruff, shellcheck, PSScriptAnalyzer | lint at the error tier (ruff rule set `F`, shellcheck and PSScriptAnalyzer at `error` severity) | `ruff check --select F .` and the linters' own CLIs |
| ams-store Go | `go build`, `go vet`, `go test` for `ams-store/` on Linux and Windows | `cd ams-store && go test ./...` |

The privacy gate has a second layer that needs a key held by the maintainers; it runs in CI on pull requests from branches of this repository. The local run covers the pattern layer.

## Commit messages

Short and plain: what changed and why. They are scanned by the privacy gate too.
