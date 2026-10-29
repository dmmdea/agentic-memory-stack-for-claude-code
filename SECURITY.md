# Security policy

## Reporting a vulnerability

Report it privately. Do not open a public issue, pull request or discussion for a vulnerability.

1. Open this repository's **Security** tab on GitHub.
2. Choose **Report a vulnerability** and fill in the private advisory form.

Include what you found, the version (`VERSION` file or release tag), the steps or a minimal proof of concept, and what an attacker gains. Do not include real keys, memory contents or personal data; redact them. If the Security tab offers no **Report a vulnerability** button, open an issue that says only that you need a private channel, with no details, and a maintainer will arrange one.

## What is in scope

- **The server** (`mem0-server/`): authentication (`X-API-Key`, the service-key header, the HMAC-signed canonical path), the admission gate and tier rules, tenant isolation, input handling, anything reachable over the network.
- **The installers** (`install.ps1`, `install/`): file permissions of what they create, what they download and whether they verify it, and what they execute.
- **The hooks and scripts** (`scripts/windows/`, `scripts/wsl/`, `claude-config/`, `ams-store/`): code that runs in your Claude Code sessions or on a schedule, including prompt-injection paths that turn recalled memory into an action.
- **Key handling**: the API key, the canonical key and the service key, how they are generated, stored (`docs/systems/key-custody.md`), passed to processes and logged.

Out of scope: vulnerabilities in third-party components themselves (Qdrant, llama.cpp, llama-swap, mem0, the Codex CLI, Python packages; report those upstream, and tell us if a version floor here should move), and findings that need an attacker who already controls your machine as the same user.

## Supported versions

Only the latest release receives security fixes. Upgrade by pulling the latest tag and re-running the installer (README, "Upgrade").

## What to expect

A maintainer reads each private report, confirms or rejects it, and fixes confirmed issues in a new release. Reporters who ask are credited in the release notes.
