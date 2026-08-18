# Key custody — what the secrets are, where they live, and how to get them back

## Purpose

Every credential this stack depends on, where each copy lives, and the restore path. The
operating rule is simple and absolute: **no key is ever recoverable from only one place.**

## Questions this doc answers

- Which secrets does the stack actually depend on?
- Which are irreplaceable and which can just be re-issued?
- Why is the DPAPI blob *not* a backup?
- If this machine died right now, what would it take to be running again?

## Scope

`scripts/wsl/key-backup.sh`, the key material it protects, and the restore procedure.

## Non-scope

The DPAPI at-rest mechanism itself — see [`dpapi-canonical-key.md`](./dpapi-canonical-key.md).
Corpus backup (collections + SQLite) is `memory-backup.sh`, documented in
[`../data-backup.md`](../data-backup.md).

## The inventory

| secret | where it lives | class |
|---|---|---|
| **canonical HMAC signing key** | `~/.mem0/canonical-key`, its DPAPI blob, tmpfs at runtime | **irreplaceable** |
| mem0 API key | `~/.mem0/api-key` | re-issuable, but everything is wired to it |
| authority URL | `~/.mem0/authority-url` | config, trivially rebuilt |
| NVIDIA API key | `~/.claude.json` (`local-offload` env, plaintext) | re-issuable from the vendor |
| GitHub tokens | OS keyring | re-issuable — `gh auth login` |
| Codex / ChatGPT OAuth | `~/.codex/auth.json` | re-issuable — re-authenticate |

**Only the first is irreplaceable**, and it deserves the emphasis. Losing it does not merely
block future canonical promotions: existing canonical records were signed under that key, so
the audit chain that makes the canonical tier trustworthy cannot be re-established. It cannot
be regenerated, only restored.

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
blast radius for no recovery benefit.

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

Expect `ok: True, present: True`. On a rebuilt machine you will also want to re-create the
DPAPI blob (see [`dpapi-canonical-key.md`](./dpapi-canonical-key.md)) — the restored plaintext
is what that blob gets built *from*, which is the whole reason it must survive.

## Common pitfalls

- **Treating the DPAPI blob as the second copy.** It is machine-bound. See above.
- **Backing up to one place and calling it durable.** The script exits non-zero on purpose.
- **Assuming a green run means the bundle is complete.** An interpreter or mount failure can
  silently drop an artifact; the run prints the artifact count, and the manifest is what a
  restore must be checked against.

## Related

- [`dpapi-canonical-key.md`](./dpapi-canonical-key.md) — at-rest protection and rotation
- [`../data-backup.md`](../data-backup.md) — the corpus half of the backup story
