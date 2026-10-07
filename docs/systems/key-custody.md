# Key custody — what the secrets are, where they live, and how to get them back

## Purpose

Every credential this stack depends on, where each copy lives, and the restore path. The
operating rule is simple and absolute: **no key is ever recoverable from only one place.** (The
one deliberate exception is the regenerable service key, below: it is re-made, not recovered.)

## Questions this doc answers

- Which secrets does the stack actually depend on?
- Which are irreplaceable and which can just be re-issued?
- Why is the DPAPI blob *not* a backup?
- If this machine died right now, what would it take to be running again?
- What is the service key, who may hold it, and why is it the one secret with no backup?

## Scope

`scripts/wsl/key-backup.sh`, the key material it protects, and the restore procedure; and the
authority-only service key (1.32.5), which `key-backup.sh` deliberately does not collect.

## Non-scope

The DPAPI at-rest mechanism itself — see [`dpapi-canonical-key.md`](./dpapi-canonical-key.md).
Corpus backup (collections + SQLite) is `memory-backup.sh`, documented in
[`../data-backup.md`](../data-backup.md).

## The inventory

| secret | where it lives | class |
|---|---|---|
| **canonical HMAC signing key** | `~/.mem0/canonical-key`, its DPAPI blob, tmpfs at runtime | **irreplaceable** |
| mem0 API key | `~/.mem0/api-key`; native authority: `<secrets-dir>/ams-api-key.cred` (systemd credential), exposed to a unit as the file named by `MEM0_API_KEY_FILE` | re-issuable, but everything is wired to it |
| **service key** (`ams-service-key`, 1.32.5) | native authority: `<secrets-dir>/ams-service-key.cred` (systemd credential); WSL authority: `~/.mem0/service-key` (mode 600) | **regenerable, authority-only** — never on a replica or a PC; restore = regenerate |
| authority URL | `~/.mem0/authority-url` | config, trivially rebuilt |
| NVIDIA API key | `~/.claude.json` (`local-offload` env, plaintext) | re-issuable from the vendor |
| GitHub tokens | OS keyring | re-issuable — `gh auth login` |
| Codex / ChatGPT OAuth | `~/.codex/auth.json` | re-issuable — re-authenticate |

**Where a process finds the API key.** The shim (`mem0-mcp-shim.py`) and the replay script (`replay-ops.py`) read, in order, the file named by `MEM0_API_KEY_FILE` (when it reads non-empty) and then `~/.mem0/api-key`; neither has a plaintext environment fallback, and a native authority holds no `~/.mem0/api-key`. A process there that is not a unit must decrypt the credential itself and point `MEM0_API_KEY_FILE` at the file it wrote (see [operations](../operations.md#mcp-tools-not-appearing-in-claude-code)), and remove that file when it is done.

**Only the first is irreplaceable**, and it deserves the emphasis. Losing it does not merely
block future canonical promotions: existing canonical records were signed under that key, so
the audit chain that makes the canonical tier trustworthy cannot be re-established. It cannot
be regenerated, only restored.

## The service key

The authority's own jobs write under labels: the dream's insight `source` and `actor`, and the
`actor` of the contradiction sweep, the retired-at stamper and the ship-log reclassifier. Before
1.32.5 those labels were free text, so any holder of the shared API key (every PC and every MCP
session) could type one and write what the job writes. Since 1.32.5 a label counts only when the
request also carries the **service key** in the `X-AMS-Service-Key` header; without it the server
answers `403 service-credential-required`. Nothing else about the API key changed.

| | |
|---|---|
| class | **regenerable, authority-only** |
| native authority | systemd credential `ams-service-key`, file `<secrets-dir>/ams-service-key.cred` (`--with-key=host+tpm2`). Loaded by `mem0.service`, the dream and contradiction-sweep step units, and the transient units of `ams-dream-now.sh` and `ams-service-run.sh` |
| WSL authority | `~/.mem0/service-key`, mode 600. Made by `install/1-wsl-services.sh` on a `brain` and by `scripts/wsl/deploy.sh` before its restart; there is no DPAPI blob |
| replica, PC, thin client | never. `1-wsl-services.sh` removes the file on a box that becomes a replica; a replica's server, dormant or live, refuses every job label by design |
| backup | none; `key-backup.sh` does not collect it |
| restore | regenerate |

It is the one secret deliberately kept in a single place. The rule above is about keys that
cannot be re-made; this one can, because only the authority's server and units hold it and no
stored record depends on its value. A lost key costs no data: until a new one is loaded, the
server refuses the dream's insight writes and the sweep's stamps, and on the native authority a
missing or undecryptable `.cred` stops `mem0.service` from starting (its drop-in loads the
credential). To regenerate it, re-run `install/linux-authority.sh`, which makes a missing key and
replaces one that does not decrypt on this host (the old file is kept as
`ams-service-key.cred.undecryptable-<UTC stamp>`), and fails the install unless `/health/deep`
reports `checks.service_key.present: true`. By hand:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))' \
  | systemd-creds --user encrypt --with-key=host+tpm2 --name=ams-service-key - <secrets-dir>/ams-service-key.cred
systemctl --user restart mem0.service   # the server reads the key once, at start
curl -s "$(head -n1 ~/.mem0/authority-url)/health/deep" | jq '.checks.service_key'   # present: true
```

The restart is not optional: the dream and sweep units load the new file on their next run while a
running `mem0.service` still holds the old key, so every job label would be refused until it restarts.
The installer re-run does the restart and the check for you, which is why it is the preferred path.

On a WSL authority, `install/1-wsl-services.sh` or `deploy.sh` writes a missing `~/.mem0/service-key`
(then restart `mem0.service`, which reads it once). The operator's hand runs of the scripts that send
a job label go through `bash ~/apps/mem0-scripts/ams-service-run.sh <script> [args]`, which loads
the key for the run ([operations](../operations.md#a-hand-run-is-refused-with-service-credential-required)).

**What it is not.** It separates the authority's own jobs from every caller that holds only the
shared API key: MCP shim sessions, hooks, PCs and replicas. It does not resist a shell on the
authority as the service user, an ssh session to the brain, or (WSL brain) a Windows-side process of
the same user. It is one key for every job, not per-job least privilege. It is separate from the
canonical key, so a job that holds it cannot mint canonical tokens. The Codex judge, which reads
memory text any API-key holder can write, runs without the environment variables that carry or point at
the credentials (the credential directory, the API key file, the API key); that removes the pointer, not
the files, and the sandbox is the real boundary.

## Why the DPAPI blob is not a backup

`canonical-key.dpapi` is encrypted **against the Windows user profile on this machine**. That
is exactly the right protection for data at rest, and exactly the wrong thing to rely on for
recovery: it is worthless on new hardware, after a profile rebuild, after an account change,
or when restoring an image to a different box. Those are the scenarios a backup exists for.

Treating the blob as the second copy is the trap this doc exists to prevent.

## What `key-backup.sh` does

Collects the irreplaceable and awkward-to-replace material, writes a `sha256` manifest, and
publishes to **two destinations** — a local volume off the source disk, and an offsite
(Drive-synced) folder that survives the machine entirely. It then **re-reads both copies and
verifies them against the manifest**, and **fails with a non-zero exit if fewer than two
destinations verify**. One copy is not durability, so the script refuses to call it success.

Re-issuable credentials (GitHub, Codex) are deliberately excluded: backing them up widens the
blast radius for no recovery benefit. The service key is excluded for the same reason, and a
second one: a copy anywhere but the authority defeats what the key is for.

```bash
# from the Windows-side shell, which is the only runtime that sees both destinations
MEM0_DIR="//wsl.localhost/<distro>/home/<user>/.mem0" bash scripts/wsl/key-backup.sh
```

Two environment traps are handled in code, because both were hit while building it: the
offsite volume is typically **not mounted inside WSL**, and `python3` on Windows resolves to
a Microsoft Store alias stub that exits with an advert instead of running. The script probes
its interpreter by executing it rather than trusting `command -v`.

| env var | purpose |
|---|---|
| `MEM0_DIR` | source key directory (use the WSL share when running from Windows) |
| `KEY_BACKUP_LOCAL` | local destination, default `/v/mem0-backups/keys` |
| `KEY_BACKUP_OFFSITE` | offsite destination, default the Drive keys folder |

## Restore

```bash
cd <bundle>
sha256sum -c MANIFEST.sha256          # verify BEFORE trusting anything in it
cp canonical-key api-key authority-url ~/.mem0/
chmod 600 ~/.mem0/canonical-key ~/.mem0/api-key ~/.mem0/authority-url
systemctl --user restart mem0.service
curl -s localhost:18791/health/deep | python3 -c \
  "import json,sys;print(json.load(sys.stdin)['checks']['canonical_key'])"
```

Expect `ok: True, present: True`. The bundle does not carry the service key and nothing here
restores it: it is regenerated (see [The service key](#the-service-key)). On a WSL brain,
`scripts/wsl/deploy.sh` writes a missing `~/.mem0/service-key` before its restart (a re-run of
`install/1-wsl-services.sh` does too; restart `mem0.service` after it). Expect
`checks.service_key.present: true` from `/health/deep` as well: without it the restored server
starts and then refuses every job label. On the native authority, re-run `install/linux-authority.sh`. On a rebuilt machine you will also want to re-create the
DPAPI blob (see [`dpapi-canonical-key.md`](./dpapi-canonical-key.md)) — the restored plaintext
is what that blob gets built *from*, which is the whole reason it must survive.

## Common pitfalls

- **Treating the DPAPI blob as the second copy.** It is machine-bound. See above.
- **Backing up to one place and calling it durable.** The script exits non-zero on purpose.
- **Copying the service key to a replica, a PC or a backup bundle.** It exists to be held by the
  authority alone; a copy next to the API key removes the separation it provides. Regenerate it
  on the authority instead.
- **Assuming a green run means the bundle is complete.** An interpreter or mount failure can
  silently drop an artifact; the run prints the artifact count, and the manifest is what a
  restore must be checked against.

## Related

- [`dpapi-canonical-key.md`](./dpapi-canonical-key.md) — at-rest protection and rotation
- [`../data-backup.md`](../data-backup.md) — the corpus half of the backup story
