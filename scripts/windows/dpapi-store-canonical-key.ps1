# v0.18 Phase A: one-time migration of ~/.mem0/canonical-key from plaintext to DPAPI.
# Reads existing plaintext key, encrypts via DPAPI user-scope, writes to canonical-key.dpapi.
# Idempotent: re-run is safe (overwrites .dpapi from current plaintext if both exist).
# Use -RemovePlaintext after verification to delete the plaintext file.
#
# v0.19 Phase H: the .dpapi blob is now LOAD-BEARING for the WSL server — it is
# the at-rest key source. dpapi-fetch-key.sh (ExecStartPre on mem0.service)
# decrypts it via WSL interop at every service start and injects the key into
# tmpfs ($XDG_RUNTIME_DIR/mem0/canonical-key). Re-run this script after any key
# rotation so the blob always tracks the current key. Full runbook:
# docs/modular/dpapi-canonical-key.md.

[CmdletBinding()]
param(
    [switch]$RemovePlaintext,
    [string]$KeyDir = "$env:USERPROFILE\.mem0"
)

# v0.18 fix-pass HIGH, relaxed in v0.19 Phase H: -RemovePlaintext on a WSL path
# is allowed ONLY when the runtime injection chain is verifiably live (the tmpfs
# key provisioned by dpapi-fetch-key.sh exists and matches the current plaintext).
# Otherwise refuse: without that chain a WSL-hosted server has no readable key.
$isWslPath = $KeyDir -match '^\\\\wsl(\$|\.localhost)\\'
if ($RemovePlaintext -and $isWslPath) {
    # v1.0 Phase 7A: derive the distro from the operator-supplied -KeyDir (a WSL UNC
    # path), never hardcode 'Ubuntu'. Falls back to the default distro.
    $distro = if ($KeyDir -match '^\\\\wsl(?:\$|\.localhost)\\([^\\]+)\\') { $Matches[1] } else {
        $prevEnc = [Console]::OutputEncoding
        try { [Console]::OutputEncoding = [System.Text.Encoding]::Unicode; (wsl.exe -l -q | Where-Object { $_.Trim() } | Select-Object -First 1).Trim() } finally { [Console]::OutputEncoding = $prevEnc }
    }
    $uid = try { ([string](wsl.exe -d $distro -e id -u)).Trim() } catch { '1000' }
    if (-not ($uid -match '^\d+$')) { $uid = '1000' }
    $runtimeKey = "\\wsl.localhost\$distro\run\user\$uid\mem0\canonical-key"
    $plainProbe = Join-Path $KeyDir 'canonical-key'
    $chainLive = $false
    if ((Test-Path $runtimeKey) -and (Test-Path $plainProbe)) {
        $chainLive = ((Get-Content $runtimeKey -Raw).Trim() -eq (Get-Content $plainProbe -Raw).Trim())
    }
    if (-not $chainLive) {
        Write-Host "REFUSING -RemovePlaintext: runtime tmpfs key at $runtimeKey is absent or does not match the plaintext. The WSL mem0-server needs the dpapi-fetch-key.sh ExecStartPre chain live (v0.19 Phase H) before the plaintext can go. Restart mem0.service, verify, and re-run - see docs/modular/dpapi-canonical-key.md." -ForegroundColor Red
        exit 1
    }
    Write-Host "Runtime tmpfs key matches plaintext - Phase H injection chain is live, removal is safe." -ForegroundColor Green
}

$plaintext = Join-Path $KeyDir 'canonical-key'
$dpapi = Join-Path $KeyDir 'canonical-key.dpapi'

if (-not (Test-Path $plaintext)) {
    Write-Host "No plaintext canonical-key at $plaintext - nothing to migrate." -ForegroundColor Yellow
    exit 0
}

Add-Type -AssemblyName System.Security
$bytes = [System.IO.File]::ReadAllBytes($plaintext)
$encrypted = [System.Security.Cryptography.ProtectedData]::Protect(
    $bytes, $null,
    [System.Security.Cryptography.DataProtectionScope]::CurrentUser
)
[System.IO.File]::WriteAllBytes($dpapi, $encrypted)
Write-Host "Encrypted canonical-key written to $dpapi ($($encrypted.Length) bytes)" -ForegroundColor Green

# Verify roundtrip BEFORE removing plaintext
$verify = [System.Security.Cryptography.ProtectedData]::Unprotect(
    [System.IO.File]::ReadAllBytes($dpapi), $null,
    [System.Security.Cryptography.DataProtectionScope]::CurrentUser
)
if (-not [System.Linq.Enumerable]::SequenceEqual($verify, $bytes)) {
    Write-Host "VERIFY FAILED: decrypted bytes do not match plaintext. Aborting." -ForegroundColor Red
    Remove-Item $dpapi -Force
    exit 1
}
Write-Host "Verify OK: roundtrip yields original plaintext." -ForegroundColor Green

if ($RemovePlaintext) {
    Remove-Item $plaintext -Force
    Write-Host "Removed plaintext $plaintext. The WSL server now reads the key via the dpapi-fetch-key.sh runtime injection (v0.19 Phase H); recovery = ProtectedData::Unprotect on the blob (docs/modular/dpapi-canonical-key.md)." -ForegroundColor Yellow
} else {
    Write-Host "Plaintext retained at $plaintext. Re-run with -RemovePlaintext after verifying live mem0-server flows (runtime tmpfs key present + canonize cycle passing)." -ForegroundColor Yellow
}
