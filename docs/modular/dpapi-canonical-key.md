# Canonical-key backend: DPAPI blob + runtime tmpfs injection (v0.19 Phase H)

Status: **ACTIVE** — plaintext `~/.mem0/canonical-key` was removed from the
production WSL box on 2026-06-12 after the verified cutover below. This doc
supersedes the v0.18 runbook section (rescinded in the v0.18 fix-pass because
the WSL server could not read the DPAPI blob — see `docs/v018-resume.md`).

## Architecture

At rest, the ONLY canonical-key artifact on disk is the DPAPI blob:

```
~/.mem0/canonical-key.dpapi    mode 600, ~262 bytes
```

encrypted under Windows user `youruser`'s DPAPI user scope
(`ProtectedData::Protect(..., CurrentUser)`). The Linux server reads the key
through a runtime injection chain:

```
mem0.service start
  └─ ExecStartPre=-%h/apps/mem0-server/dpapi-fetch-key.sh   (repo: scripts/wsl/)
       ├─ calls /mnt/c/.../powershell.exe via WSL interop
       ├─ DPAPI-decrypts the blob (key travels stdin/stdout/base64, never disk)
       └─ writes $XDG_RUNTIME_DIR/mem0/canonical-key
            (tmpfs /run/user/1000/mem0, dir 0700 via RuntimeDirectoryMode,
             file 0600 — RAM-backed, removed by systemd on service stop)
  └─ ExecStart uvicorn → canonical_key_provider.CanonicalKeyProvider
```

Provider precedence (`mem0-server/canonical_key_provider.py`):

1. `$XDG_RUNTIME_DIR/mem0/canonical-key` — runtime-injected tmpfs key
2. `~/.mem0/canonical-key.dpapi` — decrypted directly (Windows hosts only)
3. `~/.mem0/canonical-key` — plaintext (dev/recovery fallback; absent in prod)
4. `None` → canonical/insight mutations 503 with a loud journal error naming
   the runtime path and `journalctl --user -u mem0` as the first diagnostic.

Client-side consumers resolve the same chain:

| Consumer | Resolution |
|---|---|
| `scripts/wsl/mem0-canonize.sh` | runtime tmpfs → plaintext → inline interop DPAPI decrypt |
| pytest suite (`conftest.py`, `test_security_invariants.py`, `test_tier_policy.py`, `test_actor_auth.py`, `test_h_fixes.py`, `test_episodic.py`) | `CanonicalKeyProvider().get_key()` |
| `scripts/wsl/test-debris-purge.py` | `CanonicalKeyProvider().get_key()` |
| `scripts/windows/Test-MemoryStack.ps1` (I3 probe) | runtime tmpfs via `\\wsl.localhost\Ubuntu\run\user\1000\mem0\canonical-key` → plaintext → native `ProtectedData::Unprotect` |

## Why this design (Phase H PoC, 2026-06-12)

- **Candidate A — DPAPI interop hybrid (CHOSEN):** powershell.exe interop works
  from systemd user services on this box (WSL 2.6.3, systemd 255) even though
  `WSL_INTEROP` is absent from the user-manager environment — the binfmt
  interpreter falls back to the boot socket `/run/WSL/1_interop` (symlink to
  `2_interop`, created by `/init` at VM boot). Measured decrypt+inject:
  0.43–0.9 s at service start. Key bytes verified identical to the original
  plaintext (sha256 match, raw bytes).
- **Candidate B — systemd-creds (REJECTED on this box):** `--user` scope needs
  systemd ≥ 256 (box has 255: `systemd-creds: unrecognized option '--user'`);
  system-scope encrypt fails as non-root (`Failed to determine local credential
  host secret: Permission denied`); and WSL2 has no TPM, so the unlock secret
  would live in `/var/lib/systemd/credential.secret` on the SAME disk — a
  marginal threat-model gain over plaintext. Re-evaluate if Ubuntu moves to
  systemd ≥ 256 AND a TPM-backed path exists.

## At-rest threat model

Same scope as v0.18 Phase A (single-user trust, v0.17 F.1.3):

- **Backup leak / offline disk forensics / accidental `~/.mem0` share:** the
  at-rest artifact is the DPAPI blob — undecryptable without the Windows user's
  credential chain. CLOSED (this was the Phase H goal).
- **Same-user runtime access:** any process running as `youruser` (WSL) or
  `youruser` (Windows) can read the tmpfs key / decrypt the blob — intentional,
  in-scope-acceptable.
- The tmpfs copy is RAM-backed; it vanishes on service stop (systemd removes
  the RuntimeDirectory) and on VM shutdown. It is never written to persistent
  disk.

## Boot ordering

`Linger=yes` for youruser, so the user manager (and mem0.service) starts at WSL
VM boot with no interactive session. The interop fallback socket
`/run/WSL/1_interop` is created by `/init` before systemd user units run, and
`/mnt/c` is mounted by the same boot path, so the fetch normally succeeds
first-try. Residual risk (interop socket dead at boot): `dpapi-fetch-key.sh`
retries 5× at 3 s intervals, probing every live `/run/WSL/*_interop` socket and
exporting `WSL_INTEROP` if the default path is dead. `ExecStartPre=-` (non-
blocking): if all retries fail the server still starts DEGRADED — search/
episodic work, canonical/insight mutations 503 loudly — rather than hard-down.
Recovery = `systemctl --user restart mem0` once Windows interop is back.
Do NOT `wsl --shutdown` to "test" this while the stack is live.

## Provisioning (new box / first install)

1. Generate a key (WSL): `bash scripts/wsl/generate-canonical-key.sh`
   (writes plaintext `~/.mem0/canonical-key`, mode 600).
2. Encrypt it (Windows): `scripts/windows/dpapi-store-canonical-key.ps1
   -KeyDir \\wsl.localhost\Ubuntu\home\<user>\.mem0` — writes the blob and
   verifies roundtrip.
3. The unit lines and the fetch script SHIP VIA THE REPO + INSTALLER (v0.19
   fix-pass closure): `systemd/mem0.service` carries the three Phase H lines
   and `install/1-wsl-services.sh` deploys a CRLF-stripped executable
   `~/apps/mem0-server/dpapi-fetch-key.sh` on every run — a redeploy or
   fresh install can no longer strip the key chain (pinned by
   `mem0-server/tests/test_systemd_parity.py`). Live unit backup of the
   pre-H hand-edit: `mem0.service.bak-v019`. The lines:
   ```ini
   RuntimeDirectory=mem0
   RuntimeDirectoryMode=0700
   ExecStartPre=-%h/apps/mem0-server/dpapi-fetch-key.sh
   ```
   Manual deploy (no installer run): `tr -d '\r' < scripts/wsl/dpapi-fetch-key.sh >
   ~/apps/mem0-server/dpapi-fetch-key.sh && chmod +x ...` then
   `systemctl --user daemon-reload && systemctl --user restart mem0`.
4. Verify: tmpfs key exists + sha256 matches plaintext; mem0-canonize.sh
   scratch cycle passes (promote + HMAC delete).
5. Remove the plaintext ONLY via `dpapi-store-canonical-key.ps1
   -RemovePlaintext` (it refuses unless the live tmpfs key matches) or the
   manual cutover procedure used in Phase H (rename → restart → canonize cycle
   → blob-decrypt hash proof → second restart cycle → delete).

## Rotation

1. Write the NEW key to plaintext `~/.mem0/canonical-key` (mode 600).
2. Re-run `dpapi-store-canonical-key.ps1` (re-encrypts blob from plaintext).
3. `systemctl --user restart mem0` (re-injects the new key to tmpfs — note the
   provider prefers the runtime key, which now carries the new value).
4. Verify canonize scratch cycle, then remove the plaintext (step 5 above).
   Old HMAC tokens/nonces die with the old key; the tier ledger is unaffected.

## Recovery (server has no key / blob suspect)

From Windows (pwsh or powershell.exe), restore the plaintext:

```powershell
$dec = [System.Security.Cryptography.ProtectedData]::Unprotect(
  (Get-Content -AsByteStream '\\wsl.localhost\Ubuntu\home\youruser\.mem0\canonical-key.dpapi'),
  $null, 'CurrentUser')
[System.IO.File]::WriteAllBytes('\\wsl.localhost\Ubuntu\home\youruser\.mem0\canonical-key', $dec)
wsl.exe -e bash -c "chmod 600 ~/.mem0/canonical-key && systemctl --user restart mem0"
```

The provider's plaintext fallback then serves the key even if interop is
broken. IMPORTANT: the blob is decryptable ONLY by Windows user `youruser` on this
machine while the DPAPI master-key chain survives (profile intact). A Windows
reinstall/profile loss destroys it — before any such operation, restore the
plaintext via the block above and store it in a password manager, or re-run
rotation afterwards.

## Cutover evidence (2026-06-12)

- Rename plaintext → `.cutover-hold`; restart; health OK; canonize scratch
  cycle PASS on runtime-injected key only.
- Blob decrypted from Windows at cutover time: sha256
  `5e4c8bb4…7796d58c` == hold-file sha256 (exact match).
- Second restart + canonize cycle PASS → hold file deleted.
- Post-delete restart + cycle PASS; full pytest 154 passed / 3 skipped;
  Test-MemoryStack HEALTHY 3/3, 28 PASS, 0 WARN.
