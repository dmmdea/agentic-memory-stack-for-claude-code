# Key custody — what the secrets are, where they live, and how to get them back

## Purpose

Every credential this stack depends on, where each copy lives, and the restore path. The
operating rule is simple and absolute: **no key is ever recoverable from only one place.** (The
one deliberate exception is the regenerable service key, below: it is re-made, not recovered.)

## Questions this doc answers

- Which secrets does the stack actually depend on?
- Which secrets cost data if lost, and which only cost a re-issue?
- Why is the DPAPI blob *not* a backup?
- If this machine died right now, what would it take to be running again?
- What is the service key, who may hold it, and why is it the one secret with no backup?
- Where does the Claude API key live (when the judge provider is the Claude API), and what happens if it is lost?

## Scope

`scripts/wsl/key-backup.sh`, the key material it protects, and the restore procedure; and the
authority-only service key (1.32.5), which `key-backup.sh` deliberately does not collect; and the
Claude API key, which exists only on the Claude API judge provider.

## Non-scope

The DPAPI at-rest mechanism itself — see [`dpapi-canonical-key.md`](./dpapi-canonical-key.md).
Corpus backup (collections + SQLite) is `memory-backup.sh`, documented in
[`../data-backup.md`](../data-backup.md).

## The inventory

| secret | where it lives | class |
|---|---|---|
| **canonical HMAC key** | `~/.mem0/canonical-key`, its DPAPI blob, tmpfs at runtime; native authority: `<secrets-dir>/ams-canonical-key.cred` (systemd credential) | **replaceable, costs no data**; without a key no canonical or insight write can be authorised |
| mem0 API key | `~/.mem0/api-key`; native authority: `<secrets-dir>/ams-api-key.cred` (systemd credential), exposed to a unit as the file named by `MEM0_API_KEY_FILE` | re-issuable, but everything is wired to it |
| **service key** (`ams-service-key`, 1.32.5) | native authority: `<secrets-dir>/ams-service-key.cred` (systemd credential); WSL authority: `~/.mem0/service-key` (mode 600) | **regenerable, authority-only** — never on a replica or a PC; restore = regenerate |
| Claude API key (`ams-anthropic-key`; only when `MEM0_JUDGE_PROVIDER=anthropic`) | native authority: `<secrets-dir>/ams-anthropic-key.cred` (systemd credential); PC or WSL-hosted box: `~/.mem0/anthropic-key` (mode 600) | re-issuable at the provider; no backup set carries it |
| authority URL | `~/.mem0/authority-url` | config, trivially rebuilt |
| optional vendor API key (`NVIDIA_API_KEY`) | `~/.claude.json` (an MCP server's `env`, plaintext); `key-backup.sh` extracts it when present | re-issuable from the vendor |
| GitHub tokens | OS keyring | re-issuable — `gh auth login` |
| Codex / ChatGPT OAuth | `~/.codex/auth.json` | re-issuable — re-authenticate |

**Where a process finds the API key.** The shim (`mem0-mcp-shim.py`) and the replay script (`replay-ops.py`) read, in order, the file named by `MEM0_API_KEY_FILE` (when it reads non-empty) and then `~/.mem0/api-key`; neither has a plaintext environment fallback, and a native authority holds no `~/.mem0/api-key`. A process there that is not a unit must decrypt the credential itself and point `MEM0_API_KEY_FILE` at the file it wrote (see [operations](../operations.md#mcp-tools-not-appearing-in-claude-code)), and remove that file when it is done.

**What the canonical key signs.** Only the short-lived user-direct token a canonical or insight
write must carry: HMAC-SHA256 over `<ts>|<nonce>|<action>|<memory_id>|<reason>`, accepted within a
300 s skew window and once per nonce (`mem0-server/security_invariants.py`, `validate_hmac_user_direct`).
No signature is stored on a record and no record, ledger line or replay entry is verified against the key
later. So losing the key loses no data and breaks no audit chain, and a fresh key is a valid
replacement: old tokens die with the old key (they expire in minutes anyway), the tier ledger is
unaffected, and nothing already canonical changes. What a missing key does cost is every canonical,
insight and demote write until a key is back: the server answers `503` (no key loaded) and the
canonize scripts cannot mint a token. A backup of the key therefore saves minting and re-wiring a
new one; it is not what keeps the canonical tier trustworthy. The same reading is in
[`dpapi-canonical-key.md`](./dpapi-canonical-key.md) and [`MIGRATION.md`](../MIGRATION.md).
On a box that still holds the DPAPI blob, restore the existing key rather than minting a new one, as
the server's own `503` message directs; mint a new key only where no blob exists.

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
| native authority | systemd credential `ams-service-key`, file `<secrets-dir>/ams-service-key.cred` (`--with-key=host+tpm2`). Loaded by `mem0.service`, the dream, contradiction-sweep and decay-scan step units, and the transient units of `ams-dream-now.sh` and `ams-service-run.sh` |
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

## The Claude API key

The key exists only when the box's judge provider is the Claude API (`MEM0_JUDGE_PROVIDER=anthropic`;
see [`judge-provider.md`](./judge-provider.md)). On the default Codex provider there is no such key
and no unit names one.

| | |
|---|---|
| class | **re-issuable** |
| native authority | systemd credential `ams-anthropic-key`, file `<secrets-dir>/ams-anthropic-key.cred` (`--with-key=host+tpm2`). Loaded only by the units that judge, `mem0.service`, `ams-step-dream.service`, `ams-step-contradiction-sweep.service` and `ams-step-retrieval-pairs.service`, each through a `<unit>.d/judge-anthropic.conf` drop-in; the hand-run wrappers `ams-dream-now.sh` and `ams-service-run.sh` load it the same way |
| PC, WSL-hosted box | `~/.mem0/anthropic-key`, mode 600 (inside the WSL distro on Windows) |
| backup | none; no backup set carries it |
| restore | issue a new key at the provider |

Nothing stored depends on its value, so a lost key costs no data. A leaked key is different: whoever
holds it can spend on the account until the key is revoked at the provider, which is why the file is
owner-only and the key never enters the environment. Until a new one is in place the judged jobs
report `no_key` (or `auth` for a revoked key) and skip their work. To replace it, issue a new key at the provider and write it the same way
as the first. On the native authority the file is made by hand (the installer refuses
`--judge-provider anthropic` while it is missing or does not decrypt on this host):

```bash
# paste the key, press Enter, then Ctrl-D; nothing is written in plaintext
systemd-creds --user encrypt --with-key=host+tpm2 --name=ams-anthropic-key - <secrets-dir>/ams-anthropic-key.cred
```

A systemd credential is read when a unit starts, so restart `mem0.service` after replacing the file.

**It is never an environment variable.** A coding agent that inherits `ANTHROPIC_API_KEY` stops using
its own login, and every child process inherits the environment; both transports read the file at call
time instead.

## Why the DPAPI blob is not a backup

`canonical-key.dpapi` is encrypted **against the Windows user profile on this machine**. That
is exactly the right protection for data at rest, and exactly the wrong thing to rely on for
recovery: it is worthless on new hardware, after a profile rebuild, after an account change,
or when restoring an image to a different box. Those are the scenarios a backup exists for.

Treating the blob as the second copy is the trap this doc exists to prevent.

## What `key-backup.sh` does

Collects the key files that are awkward to replace, writes a `sha256` manifest, and
publishes to **two destinations** — a local volume off the source disk, and an offsite
(cloud-synced or remote) folder that survives the machine entirely. It then **re-reads both copies and
verifies them against the manifest**, and **fails with a non-zero exit if fewer than two
destinations verify**. One copy is not durability, so the script refuses to call it success.

A bundle without keys is refused. If the canonical key or the API key was not collected, the run
prints which one is missing, writes nothing to either destination and exits 4; `--allow-partial`
backs up what exists anyway, and the final line then says the bundle is partial. (The script used
to pass with a bundle holding only `authority-url`.)

Re-issuable credentials (GitHub, Codex) are deliberately excluded: backing them up widens the
blast radius for no recovery benefit. The service key is excluded for the same reason, and a
second one: a copy anywhere but the authority defeats what the key is for.

```bash
# from the Windows-side shell, which is the only runtime that sees both destinations
MEM0_DIR="//wsl.localhost/<distro>/home/<user>/.mem0" KEY_BACKUP_LOCAL=<dir> KEY_BACKUP_OFFSITE=<dir> bash scripts/wsl/key-backup.sh
```

Two environment traps are handled in code, because both were hit while building it: the
offsite volume is typically **not mounted inside WSL**, and `python3` on Windows resolves to
a Microsoft Store alias stub that exits with an advert instead of running. The script probes
its interpreter by executing it rather than trusting `command -v`.

| env var | purpose |
|---|---|
| `MEM0_DIR` | source key directory (use the WSL share when running from Windows) |
| `KEY_BACKUP_LOCAL` | local destination on a different disk from `~/.mem0`; required, no default (the script exits 64 naming both variables when either is unset) |
| `KEY_BACKUP_OFFSITE` | offsite destination that survives the machine (a synced cloud folder, a mounted remote share); required, no default |
| `OFFSITE_WIN_DEST` | optional: the Windows-side path of the offsite folder, for a volume that is not mounted inside WSL; no default |

The one command-line flag is `--allow-partial` (see above).

### Native authority

On a native Linux authority (`MEM0_HOST_KIND=native` in `~/.mem0/stack.env`) the canonical key and the
API key are systemd credentials, `<secrets-dir>/ams-canonical-key.cred` and `ams-api-key.cred`,
encrypted with `--with-key=host+tpm2`. There are no key files in `~/.mem0`, and a sealed credential is
bound to the host that made it: copying the `.cred` file to a backup gives a file nothing can decrypt
after a rebuild. So `key-backup.sh` cannot back these keys up. It says so and exits 4; it does not
report a green run that holds no key. The procedure on that box:

- **The API key.** Its plaintext is also held by every client and replica (`~/.mem0/api-key` on each).
  Keep one of those copies, or a copy made when the key was created, off the box.
- **The canonical key.** Losing it costs no data (see above). To replace it, make a new key in
  memory-backed scratch, encrypt it as the installer's own error message prints for a missing `.cred`
  (`systemd-creds --user encrypt --with-key=host+tpm2 --name=ams-canonical-key <plaintext>
  <secrets-dir>/ams-canonical-key.cred`), remove the plaintext, and restart `mem0.service`. Keep a
  plaintext copy off the box at creation time if you want to skip that step after a rebuild.
- **The service key** is regenerated, never backed up ([The service key](#the-service-key)).

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
- **Passing `--allow-partial` by habit.** It exists for a box that really holds one of the two keys; the
  bundle it writes cannot restore the other.
- **Running it on a native authority and expecting keys.** It exits 4 there; see
  [Native authority](#native-authority).
- **Copying the service key to a replica, a PC or a backup bundle.** It exists to be held by the
  authority alone; a copy next to the API key removes the separation it provides. Regenerate it
  on the authority instead.
- **Assuming a green run means the bundle is complete.** An interpreter or mount failure can
  silently drop an artifact; the run prints the artifact count, and the manifest is what a
  restore must be checked against.

## Related

- [`dpapi-canonical-key.md`](./dpapi-canonical-key.md) — at-rest protection and rotation
- [`judge-provider.md`](./judge-provider.md) — the judge provider setting, the Claude API key's use, and the spend cap
- [`../data-backup.md`](../data-backup.md) — the corpus half of the backup story
