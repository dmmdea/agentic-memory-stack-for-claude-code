# Test-MemoryStack.ps1 - non-mutating memory-stack health verifier.
#
# v0.17 Phase F.2.1: reorganized into THREE dimensions with subtotals:
#   LIVENESS   — services up, ports listening, models loaded
#   INVARIANTS — security gates hold, tier policy enforced, search filters correct
#   RECOVERY   — backup + schedule freshness, restore drill within last 30 days
#
# Top-line summary:
#   "Memory stack: HEALTHY (3/3 dimensions GREEN; N checks PASS)" if all 3 green
#   "Memory stack: DEGRADED (LIVENESS GREEN, INVARIANTS WARN, RECOVERY GREEN; ...)" etc.
#
# Exits 0 if no FAIL in any dimension, 1 if any FAIL.

param([switch]$Quiet)

$ErrorActionPreference = 'Continue'

# --------------------------------------------------------------------------
# v1.0 Phase 7A: operator receipt — resolve the four operator-specific
# dimensions so this verifier is fully operator-agnostic (no hardcoded
# developer handle / distro / repo path). The receipt is written by
# install/2-windows-config.ps1 to the deployed scripts dir; this script reads
# it whether it runs from the repo or the deployed copy. Live fallback keeps it
# working pre-install / if the receipt is absent.
# --------------------------------------------------------------------------
$TmsCfgPath = Join-Path $env:USERPROFILE '.claude\scripts\mem0-stack.config.psd1'
$TmsCfg = $null
try { if (Test-Path $TmsCfgPath) { $TmsCfg = Import-PowerShellDataFile $TmsCfgPath } } catch { $TmsCfg = $null }
# Distro first (the user/uid fallbacks below target it).
$TmsDistro  = if ($TmsCfg -and $TmsCfg.Distro)  { $TmsCfg.Distro } else {
    $prevEnc = [Console]::OutputEncoding
    try { [Console]::OutputEncoding = [System.Text.Encoding]::Unicode; (wsl.exe -l -q | Where-Object { $_.Trim() } | Select-Object -First 1).Trim() } finally { [Console]::OutputEncoding = $prevEnc }
}
$TmsWslUser = if ($TmsCfg -and $TmsCfg.WslUser) { $TmsCfg.WslUser } else { try { ([string](wsl.exe -d $TmsDistro -e bash -lc 'printf %s "$USER"')).Trim() } catch { '' } }
$TmsWinUser = if ($TmsCfg -and $TmsCfg.WinUser) { $TmsCfg.WinUser } else { $env:USERNAME }
$TmsRepoWin = if ($TmsCfg -and $TmsCfg.RepoRootWin) { $TmsCfg.RepoRootWin } else { Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
# Runtime UID for the tmpfs canonical-key path (/run/user/<uid>) — not always 1000.
$TmsUid = try { ([string](wsl.exe -d $TmsDistro -e id -u)).Trim() } catch { '1000' }
if (-not ($TmsUid -match '^\d+$')) { $TmsUid = '1000' }
$TmsDistroUnc = "\\wsl.localhost\$TmsDistro"
$TmsHomeUnc   = "$TmsDistroUnc\home\$TmsWslUser"

# --------------------------------------------------------------------------
# Check registry: each dimension accumulates into its own list
# --------------------------------------------------------------------------
$livenessRows  = @()
$invariantRows = @()
$recoveryRows  = @()

function Add-Check {
    param(
        [string]$Dimension,   # LIVENESS | INVARIANTS | RECOVERY
        [string]$Component,
        [string]$Status,      # OK | WARN | FAIL
        [string]$Detail = ''
    )
    $obj = [PSCustomObject]@{ Component = $Component; Status = $Status; Detail = $Detail }
    switch ($Dimension) {
        'LIVENESS'   { $script:livenessRows  += $obj }
        'INVARIANTS' { $script:invariantRows += $obj }
        'RECOVERY'   { $script:recoveryRows  += $obj }
    }
}

# --------------------------------------------------------------------------
# v0.22 Phase E: dot-source the hook lib for the Pillar-2 verification helpers
# (Test-OffloadNoBlockInvariant, Measure-MemoryContextBudget) used by the
# R-offload + R-budget INVARIANTS checks below. The lib has no side effects at
# load. Prefer the deployed copy (beside this script's deployed home) and fall
# back to the repo copy; either way, fail-open if it cannot be sourced.
# --------------------------------------------------------------------------
$script:OffloadLibLoaded = $false
try {
    $libCandidates = @(
        (Join-Path $PSScriptRoot 'user-prompt-lib.ps1'),
        (Join-Path $TmsRepoWin 'scripts\windows\user-prompt-lib.ps1')
    )
    foreach ($lc in $libCandidates) {
        if ($lc -and (Test-Path $lc)) { . $lc; $script:OffloadLibLoaded = $true; break }
    }
} catch { $script:OffloadLibLoaded = $false }

# --------------------------------------------------------------------------
# Read API key once (used by multiple checks)
# --------------------------------------------------------------------------
$keyPath = "$TmsHomeUnc\.mem0\api-key"
$key = $null
try { $key = (Get-Content $keyPath -Raw -ErrorAction Stop).Trim() } catch {}

# ==========================================================================
# DIMENSION 1: LIVENESS
# Services up, ports listening, models loaded
# ==========================================================================

# L1: mem0 :18791 basic health
try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/health' -TimeoutSec 3
    if ($h.ok) { Add-Check 'LIVENESS' 'mem0 :18791' 'OK' "version=$($h.version)" }
    else        { Add-Check 'LIVENESS' 'mem0 :18791' 'WARN' 'health.ok=false' }
} catch { Add-Check 'LIVENESS' 'mem0 :18791' 'FAIL' $_.Exception.Message }

# L2: mem0 deep health (Qdrant + embedder probe)
try {
    $hd = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/health/deep' -TimeoutSec 6
    if ($hd.ok) {
        Add-Check 'LIVENESS' 'mem0 /health/deep' 'OK' "qdrant_points=$($hd.checks.qdrant.points) embed_dim=$($hd.checks.embedder.dim)"
    } else {
        $errs = @()
        if (-not $hd.checks.qdrant.ok)   { $errs += "qdrant:$($hd.checks.qdrant.error)" }
        if (-not $hd.checks.embedder.ok) { $errs += "embedder:$($hd.checks.embedder.error)" }
        # v0.20 Phase D (M6): keyless-degraded server flips /health/deep ok=false
        if ($hd.checks.canonical_key -and -not $hd.checks.canonical_key.ok) { $errs += 'canonical_key: not loaded (dpapi-fetch-key ExecStartPre failed - restart mem0)' }
        Add-Check 'LIVENESS' 'mem0 /health/deep' 'FAIL' ($errs -join '; ')
    }
} catch { Add-Check 'LIVENESS' 'mem0 /health/deep' 'FAIL' $_.Exception.Message }

# L3: Qdrant :6333 direct (bind check in INVARIANTS; liveness only here)
try {
    $qh = Invoke-RestMethod -Uri 'http://127.0.0.1:6333/healthz' -TimeoutSec 3 -ErrorAction Stop
    Add-Check 'LIVENESS' 'Qdrant :6333' 'OK' 'healthz OK'
} catch {
    # Try /collections as fallback (older Qdrant versions)
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:6333/collections' -TimeoutSec 3 | Out-Null
        Add-Check 'LIVENESS' 'Qdrant :6333' 'OK' 'collections endpoint reachable'
    } catch { Add-Check 'LIVENESS' 'Qdrant :6333' 'FAIL' $_.Exception.Message }
}

# L4: EmbeddingGemma-300m embedder on llama-swap :11436 (v0.22 migration: replaced the
# Ollama :11435 nomic-embed-text backend; Ollama fully decommissioned 2026-06-13).
# Must return a 768-dim vector via the OpenAI-compatible /v1/embeddings endpoint.
try {
    $embBody = @{model='embeddinggemma'; input='title: none | text: ping'} | ConvertTo-Json
    $e = Invoke-RestMethod -Uri 'http://127.0.0.1:11436/v1/embeddings' -Method Post -Body $embBody -ContentType 'application/json' -TimeoutSec 20
    $dim = @($e.data[0].embedding).Count
    if ($dim -eq 768) { Add-Check 'LIVENESS' 'EmbeddingGemma :11436' 'OK'   "embeddinggemma live, dim=$dim (CPU)" }
    else              { Add-Check 'LIVENESS' 'EmbeddingGemma :11436' 'WARN' "responded but dim=$dim (expected 768)" }
} catch { Add-Check 'LIVENESS' 'EmbeddingGemma :11436' 'FAIL' $_.Exception.Message }

# L5: bge-reranker-v2-m3 on llama-swap :11436
try {
    $rerankBody = @{model='bge-reranker-v2-m3'; query='ping'; documents=@('a','b','c'); top_n=3} | ConvertTo-Json
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:11436/v1/rerank' -Method Post -Body $rerankBody -ContentType 'application/json' -TimeoutSec 20
    if ($r.results -and $r.results.Count -eq 3) { Add-Check 'LIVENESS' 'bge-reranker :11436' 'OK' 'live + ordered 3 docs' }
    else                                         { Add-Check 'LIVENESS' 'bge-reranker :11436' 'WARN' 'responded but unexpected shape' }
} catch { Add-Check 'LIVENESS' 'bge-reranker :11436' 'FAIL' $_.Exception.Message }

# L6: mem0 list pagination (> default cap of 20 returned when limit=100)
if ($key) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/memories?user_id=$TmsWslUser&limit=100" -Headers @{'X-API-Key'=$key} -TimeoutSec 10
        $n = @($r.results).Count
        if    ($n -gt 20) { Add-Check 'LIVENESS' 'mem0 list pagination' 'OK'   "limit=100 returned $n records" }
        elseif ($n -eq 20){ Add-Check 'LIVENESS' 'mem0 list pagination' 'WARN' "limit=100 returned exactly 20 — top_k bug may have regressed" }
        else               { Add-Check 'LIVENESS' 'mem0 list pagination' 'OK'   "returned $n (small store — inconclusive)" }
    } catch { Add-Check 'LIVENESS' 'mem0 list pagination' 'FAIL' $_.Exception.Message }
}

# L7: MEMORY.md exists and is fresh
try {
    $memPath = "$TmsHomeUnc\.mem0\MEMORY.md"
    if (Test-Path $memPath) {
        $age   = (Get-Date) - (Get-Item $memPath).LastWriteTime
        $lines = (Get-Content $memPath | Measure-Object -Line).Lines
        if ($age.TotalDays -lt 8) { Add-Check 'LIVENESS' 'MEMORY.md' 'OK'   "${lines} lines, $([int]$age.TotalHours)h old" }
        else                      { Add-Check 'LIVENESS' 'MEMORY.md' 'WARN' "${lines} lines but $([int]$age.TotalDays)d old (dream-consolidator may be failing)" }
    } else { Add-Check 'LIVENESS' 'MEMORY.md' 'WARN' 'not yet generated' }
} catch { Add-Check 'LIVENESS' 'MEMORY.md' 'WARN' $_.Exception.Message }


# ==========================================================================
# DIMENSION 2: INVARIANTS
# Security gates hold, bind constraints, tier policy enforced, search filters correct
# ==========================================================================

# I1: Qdrant bind — must NOT be 0.0.0.0 (v0.17 F.2.2)
try {
    $listenLine = wsl.exe -d $TmsDistro -e bash -lc "ss -ltn 2>/dev/null | grep ':6333' | head -1"
    if      ($listenLine -match '127\.0\.0\.1:6333') { Add-Check 'INVARIANTS' 'Qdrant bind' 'OK'   '127.0.0.1:6333' }
    elseif  ($listenLine -match '0\.0\.0\.0:6333')   { Add-Check 'INVARIANTS' 'Qdrant bind' 'FAIL' '0.0.0.0:6333 — LAN-EXPOSED; fix qdrant.service to bind 127.0.0.1' }
    else                                              { Add-Check 'INVARIANTS' 'Qdrant bind' 'WARN' "unexpected: $listenLine" }
} catch { Add-Check 'INVARIANTS' 'Qdrant bind' 'FAIL' $_.Exception.Message }

# I2: llama-swap bind — must NOT be 0.0.0.0 (v0.17 F.2.2)
try {
    $llLine = wsl.exe -d $TmsDistro -e bash -c "ss -tlpn 2>/dev/null | grep 11436 | head -1"
    if      ($llLine -match '127\.0\.0\.1:11436') { Add-Check 'INVARIANTS' 'llama-swap bind' 'OK'   '127.0.0.1:11436' }
    elseif  ($llLine -match '0\.0\.0\.0:11436' -or $llLine -match '\*:11436') {
        # On Linux, *:11436 is equivalent to 0.0.0.0:11436 (all interfaces)
        # v0.19 L9: remediation points at the systemd unit --listen flag — the actual
        # wildcard-bind source fixed in v0.18 Phase D. The yaml's llama-server cmd
        # lines already carry --host 127.0.0.1 (docs/modular/llama-swap-binding.md).
        Add-Check 'INVARIANTS' 'llama-swap bind' 'WARN' "LAN-exposed ($($llLine.Trim().Substring(0,[Math]::Min(60,$llLine.Trim().Length)))) — fix --listen in ~/.config/systemd/user/llama-swap.service to '--listen 127.0.0.1:11436', then: systemctl --user daemon-reload && systemctl --user restart llama-swap (see docs/modular/llama-swap-binding.md)"
    }
    elseif  ($llLine)                              { Add-Check 'INVARIANTS' 'llama-swap bind' 'WARN' "unexpected bind: $llLine" }
    else                                           { Add-Check 'INVARIANTS' 'llama-swap bind' 'WARN' 'no listener on :11436 (llama-swap may be down)' }
} catch { Add-Check 'INVARIANTS' 'llama-swap bind' 'WARN' $_.Exception.Message }

# I3: canonical immutability probe — POST evidence + assert PUT without HMAC = 403
if ($key) {
    try {
        $probeText = "invariant-probe-$(Get-Random)"
        $addBody = @{
            messages  = $probeText
            user_id   = 'test-inv-healthcheck'
            infer     = $false
            metadata  = @{tier='evidence'; source='test-memorystack-invariant-probe'}
        } | ConvertTo-Json
        $addResp = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories' -Method Post -Body $addBody `
            -ContentType 'application/json' -Headers @{'X-API-Key'=$key} -TimeoutSec 10
        $probeMid = $addResp.results[0].id

        if ($probeMid) {
            # Promote to canonical via HMAC (need the canonical key; skip if absent).
            # v0.19 Phase H resolution order (mirrors canonical_key_provider.py):
            #   1. runtime tmpfs key — present while mem0.service runs (injected by
            #      dpapi-fetch-key.sh ExecStartPre); visible over \\wsl.localhost 9P
            #   2. plaintext — dev/recovery only (removed at Phase H cutover)
            #   3. DPAPI blob decrypted natively (this script runs on Windows as the
            #      owning user, so ProtectedData::Unprotect works directly)
            $canonKey = $null
            $runtimeKeyPath = "$TmsDistroUnc\run\user\$TmsUid\mem0\canonical-key"
            $plainKeyPath   = "$TmsHomeUnc\.mem0\canonical-key"
            $dpapiBlobPath  = "$TmsHomeUnc\.mem0\canonical-key.dpapi"
            if (Test-Path $runtimeKeyPath) {
                $canonKey = (Get-Content $runtimeKeyPath -Raw).Trim()
            } elseif (Test-Path $plainKeyPath) {
                $canonKey = (Get-Content $plainKeyPath -Raw).Trim()
            } elseif (Test-Path $dpapiBlobPath) {
                try {
                    Add-Type -AssemblyName System.Security
                    $blobBytes = [System.IO.File]::ReadAllBytes($dpapiBlobPath)
                    $canonKey = [System.Text.Encoding]::UTF8.GetString(
                        [System.Security.Cryptography.ProtectedData]::Unprotect($blobBytes, $null, 'CurrentUser')).Trim()
                } catch { $canonKey = $null }
            }
            if ($canonKey) {
                $ts = [DateTime]::UtcNow.ToString('o') -replace '\+00:00', 'Z'
                $reason = 'healthcheck-probe'
                # v0.19 Phase G: promotion signs format-2 (<ts>|<nonce>|promote|<mid>|<reason>)
                # + X-User-Direct-Nonce — mirrors the I3 DELETE cleanup block below. The
                # nonce-less format-1 payload is deprecated (server WARNs per use; v0.20 rejects).
                $promoteNonce = [guid]::NewGuid().ToString()
                $msg = "$ts|$promoteNonce|promote|$probeMid|$reason"
                $hmacBytes = [System.Security.Cryptography.HMACSHA256]::new([System.Text.Encoding]::UTF8.GetBytes($canonKey)).ComputeHash([System.Text.Encoding]::UTF8.GetBytes($msg))
                $token = [Convert]::ToBase64String($hmacBytes)
                $tierBody = @{tier='canonical'; actor='user-direct'; reason=$reason} | ConvertTo-Json
                # Promote to canonical — if this throws, log WARN and bail
                $promoteOk = $false
                try {
                    Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/memories/$probeMid/tier" -Method Patch `
                        -Body $tierBody -ContentType 'application/json' `
                        -Headers @{'X-API-Key'=$key; 'X-User-Direct-Token'=$token; 'X-User-Direct-Ts'=$ts; 'X-User-Direct-Nonce'=$promoteNonce} -TimeoutSec 10 | Out-Null
                    $promoteOk = $true
                } catch {
                    Add-Check 'INVARIANTS' 'canonical immutability' 'WARN' "probe promote failed: $($_.Exception.Message.Substring(0,[Math]::Min(80,$_.Exception.Message.Length)))"
                    Add-Check 'INVARIANTS' 'admission gate' 'WARN' 'probe promote failed - admission-gate probe skipped'
                    try { Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/memories/$probeMid" -Method Delete -Headers @{'X-API-Key'=$key} -TimeoutSec 5 | Out-Null } catch {}
                }
                if ($promoteOk) {
                    # Assert ungated PUT is blocked (canonical immutability gate)
                    $putBody = @{text='tampered'} | ConvertTo-Json
                    try {
                        $putResp = Invoke-WebRequest -Uri "http://127.0.0.1:18791/v1/memories/$probeMid" -Method Put `
                            -Body $putBody -ContentType 'application/json' `
                            -Headers @{'X-API-Key'=$key} -TimeoutSec 5 -ErrorAction Stop
                        Add-Check 'INVARIANTS' 'canonical immutability' 'FAIL' "ungated PUT returned $($putResp.StatusCode) (expected 403)"
                    } catch {
                        $sc = $_.Exception.Response.StatusCode.value__
                        if ($sc -eq 403) { Add-Check 'INVARIANTS' 'canonical immutability' 'OK' 'ungated PUT correctly blocked (403)' }
                        else             { Add-Check 'INVARIANTS' 'canonical immutability' 'WARN' "ungated PUT returned $sc (expected 403)" }
                    }
                    # I10 (v0.19 M10): admission-gate behavioral probe — reuses the I3 record
                    # (now tier=canonical). A default-class search must NOT return it (Phase C
                    # apply_admission strips tier=canonical from durable class); the same search
                    # with query_class='canonical' MUST return it. Catches a rollback/partial
                    # redeploy of app.py that drops the apply_admission wiring, which every
                    # other INVARIANTS row missed (I6 tests the separate F.1.2 filter).
                    try {
                        $admBody = @{query=$probeText; filters=@{user_id='test-inv-healthcheck'}; limit=10; threshold=0.1; rerank=$false} | ConvertTo-Json
                        $admDefault = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories/search' -Method Post `
                            -Body $admBody -ContentType 'application/json' -Headers @{'X-API-Key'=$key} -TimeoutSec 20
                        $defaultIds = @($admDefault.results | ForEach-Object { $_.id })
                        if ($defaultIds -contains $probeMid) {
                            Add-Check 'INVARIANTS' 'admission gate' 'FAIL' 'admission gate not filtering tier=canonical from default search'
                        } else {
                            $admCanonBody = @{query=$probeText; filters=@{user_id='test-inv-healthcheck'}; limit=10; threshold=0.1; rerank=$false; query_class='canonical'} | ConvertTo-Json
                            $admCanon = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories/search' -Method Post `
                                -Body $admCanonBody -ContentType 'application/json' -Headers @{'X-API-Key'=$key} -TimeoutSec 20
                            $canonIds = @($admCanon.results | ForEach-Object { $_.id })
                            if ($canonIds -contains $probeMid) {
                                Add-Check 'INVARIANTS' 'admission gate' 'OK' 'canonical probe absent from default search, present via query_class=canonical'
                            } else {
                                Add-Check 'INVARIANTS' 'admission gate' 'WARN' 'canonical probe absent from BOTH searches (vector match may have missed; gate inconclusive)'
                            }
                        }
                    } catch { Add-Check 'INVARIANTS' 'admission gate' 'WARN' $_.Exception.Message }
                    # Cleanup — best-effort; errors are swallowed (don't double-log a check row)
                    # v0.18 fix-pass MED: v0.18 MED-7 added a nonce to the delete HMAC format
                    # (<ts>|<nonce>|delete|<mid>|<reason>); this block still signed the v0.17
                    # no-nonce payload, so every cleanup silently 403'd and leaked one
                    # canonical probe record per health run.
                    try {
                        $delTs = [DateTime]::UtcNow.ToString('o') -replace '\+00:00', 'Z'
                        $delNonce = [guid]::NewGuid().ToString()
                        $delReason = 'test cleanup'
                        $delMsg = "$delTs|$delNonce|delete|$probeMid|$delReason"
                        $delBytes = [System.Security.Cryptography.HMACSHA256]::new([System.Text.Encoding]::UTF8.GetBytes($canonKey)).ComputeHash([System.Text.Encoding]::UTF8.GetBytes($delMsg))
                        $delToken = [Convert]::ToBase64String($delBytes)
                        Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/memories/$probeMid`?actor=user-direct&reason=test+cleanup" `
                            -Method Delete -Headers @{'X-API-Key'=$key; 'X-User-Direct-Token'=$delToken; 'X-User-Direct-Ts'=$delTs; 'X-User-Direct-Nonce'=$delNonce} -TimeoutSec 5 | Out-Null
                    } catch {
                        # Swallow — cleanup failure is not a health signal worth surfacing
                    }
                }
            } else {
                Add-Check 'INVARIANTS' 'canonical immutability' 'WARN' 'canonical key unavailable (no runtime tmpfs key, no plaintext, DPAPI decrypt failed) — cannot run immutability probe'
                Add-Check 'INVARIANTS' 'admission gate' 'WARN' 'canonical key unavailable - cannot run admission-gate probe'
                # Cleanup the evidence record
                try { Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/memories/$probeMid" -Method Delete -Headers @{'X-API-Key'=$key} -TimeoutSec 5 | Out-Null } catch {}
            }
        } else {
            Add-Check 'INVARIANTS' 'canonical immutability' 'WARN' 'add returned no id — probe skipped'
        }
    } catch { Add-Check 'INVARIANTS' 'canonical immutability' 'WARN' $_.Exception.Message }
}

# I12 (v0.20 Phase D M6): keyless-degraded server must FAIL INVARIANTS, not WARN.
# Post-Phase-H the keyless state is reachable in production (ExecStartPre=- swallows
# a dpapi-fetch-key.sh failure); the server then 503s every canonical/insight HMAC
# mutation. /health/deep now reports checks.canonical_key {ok, present, source,
# dpapi_blob}; key provisioned (blob on disk) but not loaded = hard FAIL (flips
# exit code 1). A dev box with no blob at all stays OK (promotions disabled by design).
try {
    $ckHd = if ($hd -and $hd.checks) { $hd } else { Invoke-RestMethod -Uri 'http://127.0.0.1:18791/health/deep' -TimeoutSec 6 }
    $ck = $ckHd.checks.canonical_key
    if ($null -eq $ck) {
        Add-Check 'INVARIANTS' 'canonical key (server)' 'WARN' '/health/deep has no canonical_key check - pre-v0.20 server deployed? redeploy app.py + restart mem0'
    } elseif ($ck.present) {
        Add-Check 'INVARIANTS' 'canonical key (server)' 'OK' "loaded source=$($ck.source)"
    } elseif ($ck.dpapi_blob) {
        Add-Check 'INVARIANTS' 'canonical key (server)' 'FAIL' 'KEYLESS-DEGRADED: dpapi blob present but no key loaded - canonical/insight HMAC mutations 503; restart mem0 (re-runs dpapi-fetch-key ExecStartPre) or see docs/modular/dpapi-canonical-key.md Recovery'
    } else {
        Add-Check 'INVARIANTS' 'canonical key (server)' 'OK' 'no key configured (no dpapi blob) - dev box; canonical promotions disabled by design'
    }
} catch { Add-Check 'INVARIANTS' 'canonical key (server)' 'WARN' $_.Exception.Message }

# I4: insight tier exact-allowlist — POST insight without consolidator source must be 403
if ($key) {
    try {
        $insBody = @{
            messages = "insight-probe-$(Get-Random)"
            user_id  = 'test-inv-healthcheck'
            infer    = $false
            metadata = @{tier='insight'; source='not-a-consolidator'}
        } | ConvertTo-Json
        try {
            $insResp = Invoke-WebRequest -Uri 'http://127.0.0.1:18791/v1/memories' -Method Post `
                -Body $insBody -ContentType 'application/json' -Headers @{'X-API-Key'=$key} -TimeoutSec 5 -ErrorAction Stop
            Add-Check 'INVARIANTS' 'insight allowlist' 'FAIL' "non-consolidator insight write returned $($insResp.StatusCode) (expected 403)"
        } catch {
            $sc = $_.Exception.Response.StatusCode.value__
            if ($sc -eq 403) { Add-Check 'INVARIANTS' 'insight allowlist' 'OK' 'non-consolidator insight correctly blocked (403)' }
            else             { Add-Check 'INVARIANTS' 'insight allowlist' 'WARN' "unexpected status $sc (expected 403)" }
        }
    } catch { Add-Check 'INVARIANTS' 'insight allowlist' 'WARN' $_.Exception.Message }
}

# I5: search filters retrievable=false correctly (retired records excluded)
if ($key) {
    try {
        $srBody = @{query='health probe'; filters=@{user_id=$TmsWslUser}; limit=5; threshold=0.1; rerank=$false} | ConvertTo-Json
        $sr = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories/search' -Method Post `
            -Body $srBody -ContentType 'application/json' -Headers @{'X-API-Key'=$key} -TimeoutSec 20
        $count = if ($sr.results) { @($sr.results).Count } else { 0 }
        $retiredInResults = @($sr.results | Where-Object { ($_.metadata.retrievable) -eq $false }).Count
        if ($retiredInResults -eq 0) { Add-Check 'INVARIANTS' 'search retrievable filter' 'OK'   "returned $count; 0 retired records leaked" }
        else                          { Add-Check 'INVARIANTS' 'search retrievable filter' 'FAIL' "$retiredInResults retired record(s) appeared in results" }
    } catch { Add-Check 'INVARIANTS' 'search retrievable filter' 'FAIL' $_.Exception.Message }
}

# I6: search excludes _canonical_intent by default
if ($key) {
    try {
        $srBody = @{query='health probe'; filters=@{user_id=$TmsWslUser}; limit=5; threshold=0.1; rerank=$false} | ConvertTo-Json
        $sr = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories/search' -Method Post `
            -Body $srBody -ContentType 'application/json' -Headers @{'X-API-Key'=$key} -TimeoutSec 20
        $ciLeaked = @($sr.results | Where-Object { ($_.metadata).'_canonical_intent' -eq $true }).Count
        if ($ciLeaked -eq 0) { Add-Check 'INVARIANTS' '_canonical_intent exclusion' 'OK'   '0 _canonical_intent records in default search' }
        else                  { Add-Check 'INVARIANTS' '_canonical_intent exclusion' 'FAIL' "$ciLeaked _canonical_intent record(s) leaked into default search" }
    } catch { Add-Check 'INVARIANTS' '_canonical_intent exclusion' 'WARN' $_.Exception.Message }
}

# I7: v0.13 PATCH /v1/memories/{id}/metadata round-trip
if ($key) {
    try {
        $list = Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/memories?user_id=$TmsWslUser&limit=1" -Headers @{'X-API-Key'=$key} -TimeoutSec 5
        if ($list.results -and $list.results.Count -gt 0) {
            $mid = $list.results[0].id
            # Only probe if it is not canonical (canonical requires HMAC for PATCH /metadata)
            $tier = $list.results[0].metadata.tier
            if ($tier -ne 'canonical') {
                $body = @{metadata=@{test_probe=$true}; actor='test-memorystack'; reason='healthcheck probe'} | ConvertTo-Json
                $r = Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/memories/$mid/metadata" -Method Patch `
                    -Body $body -ContentType 'application/json' -Headers @{'X-API-Key'=$key} -TimeoutSec 5
                if ($r.ok) { Add-Check 'INVARIANTS' 'PATCH /metadata' 'OK' "merged $($r.merged_keys -join ',')" }
                else        { Add-Check 'INVARIANTS' 'PATCH /metadata' 'WARN' 'response missing ok=true' }
                # Cleanup
                $cleanBody = @{metadata=@{test_probe=$false}; actor='test-memorystack'; reason='cleanup'} | ConvertTo-Json
                Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/memories/$mid/metadata" -Method Patch `
                    -Body $cleanBody -ContentType 'application/json' -Headers @{'X-API-Key'=$key} -TimeoutSec 5 | Out-Null
            } else {
                Add-Check 'INVARIANTS' 'PATCH /metadata' 'OK' 'skipped (first record is canonical; gate would need HMAC)'
            }
        } else { Add-Check 'INVARIANTS' 'PATCH /metadata' 'WARN' 'no records to probe against' }
    } catch { Add-Check 'INVARIANTS' 'PATCH /metadata' 'FAIL' $_.Exception.Message }
}

# I8: rerank=True search wiring
if ($key) {
    try {
        $body = @{query='health probe'; filters=@{user_id=$TmsWslUser}; limit=5; threshold=0.1; rerank=$true} | ConvertTo-Json
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories/search' -Method Post `
            -Body $body -ContentType 'application/json' -Headers @{'X-API-Key'=$key} -TimeoutSec 25
        $reranked = $r.reranked
        $count    = if ($r.results) { @($r.results).Count } else { 0 }
        if ($reranked -or $count -lt 3) { Add-Check 'INVARIANTS' 'search rerank=True' 'OK'   "count=$count reranked=$reranked" }
        else                             { Add-Check 'INVARIANTS' 'search rerank=True' 'WARN' "count=$count reranked=null (should_rerank may have skipped)" }
    } catch { Add-Check 'INVARIANTS' 'search rerank=True' 'FAIL' $_.Exception.Message }
}

# I9: pip-audit (v0.17 F.2.4; v0.18 fix-pass HIGH: exit-code-first logic — the old
# OK condition "-not ($audit -match 'found vulnerability')" was structurally dead:
# pip-audit prints "Found N known vulnerabilities in M packages", which never matched,
# so vulnerable deps AND crashes both reported OK (exit code was masked by "| head").
try {
    $piPath = "/home/$TmsWslUser/apps/mem0-server/.venv/bin/pip-audit"
    $auditExists = wsl.exe -d $TmsDistro -e bash -c "test -x $piPath && echo yes || echo no"
    if ($auditExists -eq 'yes') {
        $audit = wsl.exe -d $TmsDistro -e bash -c "$piPath --progress-spinner=off 2>&1"
        $auditExit = $LASTEXITCODE
        if ($auditExit -eq 0 -and ($audit -match 'No known vulnerabilities')) {
            Add-Check 'INVARIANTS' 'pip-audit' 'OK' 'no known vulnerabilities'
        } elseif ($audit -match 'Found \d+ known vulnerabilit') {
            $summary = @($audit) | Where-Object { $_ -match 'Found \d+ known vulnerabilit' } | Select-Object -First 1
            Add-Check 'INVARIANTS' 'pip-audit' 'WARN' $summary
        } else {
            Add-Check 'INVARIANTS' 'pip-audit' 'WARN' "pip-audit did not complete (exit=$auditExit): $(@($audit) | Select-Object -First 1)"
        }
    } else {
        Add-Check 'INVARIANTS' 'pip-audit' 'WARN' 'pip-audit not installed; run: pip install pip-audit'
    }
} catch { Add-Check 'INVARIANTS' 'pip-audit' 'WARN' $_.Exception.Message }

# I11 (v0.19 M15/M10): hook-contract drift — the MED-17 unknown-version WARN was
# write-only (unmonitored journal). Grep the mem0 journal for it over the last 24h.
# Command MUST stay single-line (CRLF breaks bash — see R4 comment below).
# Test fingerprint exclusions (so test-generated WARNs never false-WARN health):
#   '-test'  — v0.19 convention: tests that deliberately send an unknown version
#              use a value containing '-test' (e.g. '99.0-test', test_episodic.py)
#   '99.9'   — legacy pre-v0.19 fixture value from the same test; journal entries
#              from runs before the rename would otherwise WARN for up to 24h
# Missing-version lines are logged INFO since v0.19 M10 and never match this grep.
try {
    $driftCount = wsl.exe -d $TmsDistro -e bash -c "journalctl --user -u mem0 --since '24 hours ago' --no-pager 2>/dev/null | grep 'MED-17:' | grep 'unknown hook_contract_version' | grep -vE -- '-test|99\.9' | wc -l"
    $driftN = ($driftCount -as [string]).Trim() -as [int]
    if ($null -eq $driftN) {
        Add-Check 'INVARIANTS' 'hook-contract drift' 'WARN' "journal grep returned non-numeric: $driftCount"
    } elseif ($driftN -eq 0) {
        Add-Check 'INVARIANTS' 'hook-contract drift' 'OK' '0 unknown-version WARNs in mem0 journal (24h)'
    } else {
        Add-Check 'INVARIANTS' 'hook-contract drift' 'WARN' "$driftN MED-17 unknown-version WARN(s) in mem0 journal (24h) - hook/server contract skew; check journalctl --user -u mem0"
    }
} catch { Add-Check 'INVARIANTS' 'hook-contract drift' 'WARN' $_.Exception.Message }

# I13 (v0.22 Phase E / R-offload, D7): the offload harness must provably never
# receive the [MEMORY CONTEXT] block. The block is produced ONLY by the
# UserPromptSubmit path (mem0-hook-client.exe + the daemon it spawns); two
# registration invariants keep mcp__local-offload__* (and every MCP call) off
# that path: (1) UserPromptSubmit binds only to the human-prompt client, never an
# mcp__ matcher/command (and UserPromptSubmit fires on human prompts only — never
# for tool/MCP calls or subagents); (2) no PreToolUse hook matcher names mcp__
# (the stack's gate is Bash|Edit|MultiEdit|Write). Parse the deployed Claude Code
# settings.json hooks and assert both. Fail-OPEN WARN if the config/helper is
# absent; FAIL only on a real violation (an mcp__ matcher/command on either path).
try {
    if (-not $script:OffloadLibLoaded) {
        Add-Check 'INVARIANTS' 'offload no-block (R-offload)' 'WARN' 'user-prompt-lib.ps1 not sourced - cannot run invariant (fail-open)'
    } else {
        $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
        $hooksObj = $null
        try {
            if (Test-Path $settingsPath) {
                $hooksObj = (Get-Content $settingsPath -Raw | ConvertFrom-Json).hooks
            }
        } catch { $hooksObj = $null }
        if ($null -eq $hooksObj) {
            Add-Check 'INVARIANTS' 'offload no-block (R-offload)' 'WARN' "settings.json hooks not found/parseable at $settingsPath (fail-open)"
        } else {
            $inv = Test-OffloadNoBlockInvariant -Hooks $hooksObj
            Add-Check 'INVARIANTS' 'offload no-block (R-offload)' $inv.Status $inv.Detail
        }
    }
} catch { Add-Check 'INVARIANTS' 'offload no-block (R-offload)' 'WARN' "fail-open: $($_.Exception.Message)" }

# I14 (v0.22 Phase E / R-budget, D8): the rendered [MEMORY CONTEXT] block must
# stay within a per-tier char-proxy budget derived from each tier's caps in
# model-tiers.json (small tightest by caps: 3/3/2 vs frontier 5/5/3). Render a
# worst-case (cap-filling, truncation-exercising) block per tier and assert each
# is <= its cap-implied ceiling. OPTIONAL precise leg: if $env:ANTHROPIC_API_KEY
# is set, call the Anthropic count_tokens API (NEVER tiktoken — it undercounts
# Claude 15-20%) and assert the small-tier block <= its token target; skip
# cleanly with no key. Non-fatal / fail-open: a render or API miss is a WARN, an
# over-budget tier is a WARN (visible drift), never a FAIL (no exit-1 flip).
try {
    if (-not $script:OffloadLibLoaded) {
        Add-Check 'INVARIANTS' 'memory-block budget (R-budget)' 'WARN' 'user-prompt-lib.ps1 not sourced - cannot render block (fail-open)'
    } else {
        $tiersCfg = $null
        foreach ($cand in @((Join-Path $PSScriptRoot 'model-tiers.json'),
                            (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'claude-config\model-tiers.json'))) {
            if ($cand -and (Test-Path $cand)) { $tiersCfg = $cand; break }
        }
        $budgetAudit = Join-Path $env:TEMP ("memstack-rbudget-" + [guid]::NewGuid().ToString('N') + '.jsonl')
        $overBudget = @()
        $tierChars = @()
        # v0.23: enumerate tiers from model-tiers.json — do NOT hardcode (the 'mid'
        # tier was removed when Sonnet folded into frontier). Fall back to the known set.
        $tierNames = @('frontier', 'small')
        if ($tiersCfg) {
            try { $tierNames = @((Get-Content $tiersCfg -Raw | ConvertFrom-Json).tiers.PSObject.Properties.Name) } catch {}
        }
        foreach ($t in $tierNames) {
            $m = Measure-MemoryContextBudget -Tier $t -ConfigPath $tiersCfg -AuditPath $budgetAudit
            $tierChars += "${t}=$($m.Chars)/$($m.Ceiling)"
            if (-not $m.WithinBudget) { $overBudget += "$t ($($m.Chars) > $($m.Ceiling))" }
        }
        try { if (Test-Path $budgetAudit) { Remove-Item $budgetAudit -Force -ErrorAction SilentlyContinue } } catch {}

        # Optional precise leg: Anthropic count_tokens on the small-tier block.
        $tokenNote = ''
        if ($env:ANTHROPIC_API_KEY) {
            try {
                $smallM = Measure-MemoryContextBudget -Tier 'small' -ConfigPath $tiersCfg -AuditPath $budgetAudit
                try { if (Test-Path $budgetAudit) { Remove-Item $budgetAudit -Force -ErrorAction SilentlyContinue } } catch {}
                if ($smallM.Block) {
                    $ctBody = @{
                        model    = 'claude-haiku-4-5'
                        messages = @(@{ role = 'user'; content = $smallM.Block })
                    } | ConvertTo-Json -Depth 6
                    $ctHeaders = @{
                        'x-api-key'         = $env:ANTHROPIC_API_KEY
                        'anthropic-version' = '2023-06-01'
                        'content-type'      = 'application/json'
                    }
                    $ct = Invoke-RestMethod -Uri 'https://api.anthropic.com/v1/messages/count_tokens' `
                        -Method Post -Headers $ctHeaders -Body $ctBody -TimeoutSec 15
                    $smallTokenTarget = 600   # small-tier block token target (worst-case fixture)
                    $itok = [int]$ct.input_tokens
                    if ($itok -le $smallTokenTarget) { $tokenNote = "; small=${itok}tok <= ${smallTokenTarget} (count_tokens)" }
                    else { $tokenNote = "; small=${itok}tok > ${smallTokenTarget} target (count_tokens)"; $overBudget += "small-tokens ($itok > $smallTokenTarget)" }
                }
            } catch { $tokenNote = "; count_tokens leg skipped: $($_.Exception.Message.Substring(0,[Math]::Min(50,$_.Exception.Message.Length)))" }
        } else {
            $tokenNote = '; count_tokens leg skipped (no ANTHROPIC_API_KEY)'
        }

        if ($overBudget.Count -eq 0) {
            Add-Check 'INVARIANTS' 'memory-block budget (R-budget)' 'OK' ("all tiers within char-proxy budget [$($tierChars -join ' ')]" + $tokenNote)
        } else {
            Add-Check 'INVARIANTS' 'memory-block budget (R-budget)' 'WARN' ("over budget: $($overBudget -join ', ') [$($tierChars -join ' ')]" + $tokenNote)
        }
    }
} catch { Add-Check 'INVARIANTS' 'memory-block budget (R-budget)' 'WARN' "fail-open: $($_.Exception.Message)" }

# I15 (v0.30 / R-surface): canonical surfacing — the SessionStart brand block
# (storage-cap-check.sh) must fetch canonical via the SEARCH path (query_class=canonical),
# NOT the list endpoint. GET /v1/memories is a plain get_all(top_k) with no tier filter, so
# canonical facts outside the top-N window are silently dropped (the hook surfaced 1 of 7
# ai-ecosystem canonical facts - found 2026-06-19). Structural: the deployed hook uses the
# search path. Behavioral: that path returns the brand's canonical facts. Non-fatal / fail-open.
try {
    $scHook = Join-Path $env:USERPROFILE '.claude\scripts\storage-cap-check.sh'
    $surfBrand = 'ai-ecosystem'
    if (-not (Test-Path $scHook)) {
        Add-Check 'INVARIANTS' 'canonical surfacing (R-surface)' 'WARN' "deployed storage-cap-check.sh not found at $scHook"
    } elseif (-not (Select-String -Path $scHook -Pattern 'query_class.*canonical' -Quiet)) {
        Add-Check 'INVARIANTS' 'canonical surfacing (R-surface)' 'WARN' 'SessionStart hook fetches canonical via the LIST endpoint (lossy) - drops canonical facts outside the top-N window; repoint to query_class=canonical search'
    } elseif ($key) {
        $surfBody = @{ query='canonical ground-truth facts'; query_class='canonical'; threshold=0; limit=50; rerank=$false; filters=@{ tier='canonical'; user_id=$TmsWslUser; brand=$surfBrand } } | ConvertTo-Json
        try {
            $surf = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories/search' -Method Post -Body $surfBody -ContentType 'application/json' -Headers @{'X-API-Key'=$key} -TimeoutSec 15
            $surfCount = @($surf.results | Where-Object { $_.metadata.tier -eq 'canonical' }).Count
            if ($surfCount -gt 0) { Add-Check 'INVARIANTS' 'canonical surfacing (R-surface)' 'OK' "hook uses search path; $surfCount canonical fact(s) surfaceable for $surfBrand" }
            else { Add-Check 'INVARIANTS' 'canonical surfacing (R-surface)' 'WARN' "search path wired but 0 canonical returned for $surfBrand (populate canonical or check brand/user_id)" }
        } catch { Add-Check 'INVARIANTS' 'canonical surfacing (R-surface)' 'WARN' "search probe failed: $($_.Exception.Message.Substring(0,[Math]::Min(70,$_.Exception.Message.Length)))" }
    } else {
        Add-Check 'INVARIANTS' 'canonical surfacing (R-surface)' 'WARN' 'hook uses search path (structural OK); behavioral probe skipped (no API key)'
    }
} catch { Add-Check 'INVARIANTS' 'canonical surfacing (R-surface)' 'WARN' "fail-open: $($_.Exception.Message)" }


# ==========================================================================
# DIMENSION 3: RECOVERY
# Backup + schedule freshness, restore drill recency
# ==========================================================================

# R1: WSL systemd timers (decay-scan + stack-backup)
try {
    $timers  = wsl.exe -d $TmsDistro -e bash -c "systemctl --user list-timers --no-pager 2>/dev/null | grep -E 'decay-scan|stack-backup|l10-audit' || true"
    $decayOk  = $timers -match 'decay-scan'
    $backupOk = $timers -match 'stack-backup'
    if      ($decayOk -and $backupOk) { Add-Check 'RECOVERY' 'WSL systemd timers' 'OK'   'decay-scan + stack-backup enabled' }
    elseif  ($decayOk -or $backupOk)  { Add-Check 'RECOVERY' 'WSL systemd timers' 'WARN' "partial: decay=$decayOk backup=$backupOk" }
    else                               { Add-Check 'RECOVERY' 'WSL systemd timers' 'FAIL' 'neither decay-scan nor stack-backup timer found' }
} catch { Add-Check 'RECOVERY' 'WSL systemd timers' 'FAIL' $_.Exception.Message }

# R2: Windows Task Scheduler 3am Dream
try {
    $task = Get-ScheduledTask -TaskName 'ClaudeCode-DreamConsolidator-3am' -ErrorAction Stop
    $taskArgs = $task.Actions[0].Arguments
    if ($taskArgs -match 'dream-consolidate\.ps1') {
        Add-Check 'RECOVERY' 'Task Scheduler 3am Dream' 'OK' "state=$($task.State); script=dream-consolidate.ps1"
    } else {
        Add-Check 'RECOVERY' 'Task Scheduler 3am Dream' 'WARN' "state=$($task.State); script not dream-consolidate.ps1: $taskArgs"
    }
} catch { Add-Check 'RECOVERY' 'Task Scheduler 3am Dream' 'FAIL' 'not registered' }

# R3: Latest backup manifest exists and is fresh (< 48h)
try {
    $backupDir = "$TmsHomeUnc\.mem0\backups"
    if (Test-Path $backupDir) {
        $manifests = Get-ChildItem -Path $backupDir -Filter 'manifest-*.json' -ErrorAction SilentlyContinue |
                     Sort-Object LastWriteTime -Descending
        if ($manifests.Count -gt 0) {
            $latest = $manifests[0]
            $age = (Get-Date) - $latest.LastWriteTime
            if ($age.TotalHours -lt 48) {
                Add-Check 'RECOVERY' 'backup manifest' 'OK' "$($latest.Name) — $([int]$age.TotalHours)h old"
            } else {
                Add-Check 'RECOVERY' 'backup manifest' 'WARN' "$($latest.Name) — $([int]$age.TotalDays)d old (stack-backup.sh may be failing)"
            }
        } else {
            Add-Check 'RECOVERY' 'backup manifest' 'WARN' "no manifest-*.json in $backupDir (run stack-backup-manifest.sh)"
        }
    } else {
        Add-Check 'RECOVERY' 'backup manifest' 'WARN' 'backups dir not found at ~/.mem0/backups'
    }
} catch { Add-Check 'RECOVERY' 'backup manifest' 'WARN' $_.Exception.Message }

# R4: Restore drill check (v0.19 M9 rewrite; v0.20 Phase E M10: mode+outcome honored)
# Proof = ~/.mem0/restore-drill.jsonl, appended by stack-restore.sh at the end of
# every completed run AND on failure (fields: ts, mode=dry-run|live, snapshot, outcome).
# Reading the drill's own artifact instead of grepping git commit messages makes the
# check self-match-proof: a commit MENTIONING "restore drill" (e.g. the commit that
# fixed this very check) can never turn R4 green — only an actual stack-restore.sh
# run can.
# v0.20 M10 decision logic (an indefinite chain of dry-runs no longer satisfies):
#   1. tail entry has outcome != ok            -> WARN (latest drill FAILED — outcome != ok never satisfies)
#   2. live drill with outcome=ok, age < 90d   -> OK   (the restore path is actually proven)
#   3. any drill < 30d (dry-run-only chain)    -> WARN dry-run only, live restore unproven
#   4. last drill >= 30d / no entry            -> WARN (existing staleness/no-drill messages)
# A live drill is safe by design: stack-restore.sh targets ALTERNATE collection/db
# paths (memories-restore, episodic-restore.db) — hence the remediation drops --dry-run.
# v0.18 Phase H convention still applies: commands must stay single-line — a
# multi-line here-string sends CRLF into bash ($'\r': command not found) and the
# empty result false-WARNed R4 throughout v0.17/v0.18.
try {
    $drillLine = wsl.exe -d $TmsDistro -e bash -c "tail -1 /home/$TmsWslUser/.mem0/restore-drill.jsonl 2>/dev/null"
    $liveLine  = wsl.exe -d $TmsDistro -e bash -c "grep -E 'mode.:.live' /home/$TmsWslUser/.mem0/restore-drill.jsonl 2>/dev/null | grep -E 'outcome.:.ok' | tail -1"
    $drill = $null; $live = $null
    if ($drillLine) { try { $drill = (($drillLine -as [string]).Trim()) | ConvertFrom-Json } catch {} }
    if ($liveLine)  { try { $live  = (($liveLine  -as [string]).Trim()) | ConvertFrom-Json } catch {} }
    # grep is only a candidate-selector — re-verify the parsed fields (back-compat:
    # entries missing mode/outcome can never count as a proven live drill)
    if ($live -and -not ($live.mode -eq 'live' -and $live.outcome -eq 'ok' -and $live.ts)) { $live = $null }
    $liveOkFresh = $false
    if ($live) {
        $liveAge = (Get-Date) - [datetime]$live.ts
        if ($liveAge.TotalDays -lt 90) { $liveOkFresh = $true }
    }
    if ($drill -and $drill.ts -and $drill.outcome -and $drill.outcome -ne 'ok') {
        Add-Check 'RECOVERY' 'restore drill <30d' 'WARN' "last drill FAILED: $($drill.ts) mode=$($drill.mode) snapshot=$($drill.snapshot) outcome=$($drill.outcome) - fix the restore path, then run: bash scripts/wsl/stack-restore.sh --snapshot <TS>"
    } elseif ($liveOkFresh) {
        Add-Check 'RECOVERY' 'restore drill <30d' 'OK' "live drill $($live.ts) outcome=ok snapshot=$($live.snapshot) ($([int]$liveAge.TotalDays)d ago)"
    } elseif ($drill -and $drill.ts) {
        $drillAge = (Get-Date) - [datetime]$drill.ts
        if ($drillAge.TotalDays -lt 30) {
            $since = if ($live) { "last live ok $($live.ts)" } else { 'never' }
            Add-Check 'RECOVERY' 'restore drill <30d' 'WARN' "dry-run only - live restore unproven ($since); run: bash scripts/wsl/stack-restore.sh --snapshot <TS> (no --dry-run; alternate targets are non-destructive)"
        } else {
            Add-Check 'RECOVERY' 'restore drill <30d' 'WARN' "last drill $($drill.ts) is $([int]$drillAge.TotalDays)d old - run: bash scripts/wsl/stack-restore.sh --snapshot <TS>"
        }
    } else {
        Add-Check 'RECOVERY' 'restore drill <30d' 'WARN' 'no entry in ~/.mem0/restore-drill.jsonl - run: bash scripts/wsl/stack-restore.sh --snapshot <TS>'
    }
} catch { Add-Check 'RECOVERY' 'restore drill <30d' 'WARN' $_.Exception.Message }

# R5: episodic.db health (v0.15)
if ($key) {
    try {
        $countResp = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/episodes/count' -Headers @{'X-API-Key'=$key} -TimeoutSec 5
        $n        = if ($countResp.count) { [int]$countResp.count } else { 0 }
        $lastIso  = $countResp.last_ended_at
        if ($n -ge 1 -and $lastIso) {
            $ageHours = [int]((Get-Date) - [DateTime]::Parse($lastIso)).TotalHours
            if ($ageHours -lt 168) { Add-Check 'RECOVERY' 'episodic.db :v0.15' 'OK'   "$n episodes; last ${ageHours}h ago" }
            else                   { Add-Check 'RECOVERY' 'episodic.db :v0.15' 'WARN' "$n episodes but last ${ageHours}h ago (stale — L1a Stop hook may be failing)" }
        } elseif ($n -ge 1) {
            Add-Check 'RECOVERY' 'episodic.db :v0.15' 'WARN' "$n episodes; last_ended_at unset"
        } else {
            Add-Check 'RECOVERY' 'episodic.db :v0.15' 'WARN' 'empty (no Stop events captured yet — normal post-ship)'
        }
    } catch { Add-Check 'RECOVERY' 'episodic.db :v0.15' 'FAIL' $_.Exception.Message }
}

# R6: goals health (v0.16)
if ($key) {
    try {
        $r     = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/goals?limit=200' -Headers @{'X-API-Key'=$key} -TimeoutSec 5
        $arr   = @($r)
        $total   = $arr.Count
        $open    = @($arr | Where-Object { $_.status -eq 'open' }).Count
        $blocked = @($arr | Where-Object { $_.status -eq 'blocked' }).Count
        if ($total -ge 1) {
            Add-Check 'RECOVERY' 'goals :v0.16' 'OK' "$total total ($open open, $blocked blocked)"
        } else {
            try {
                $epc = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/episodes/count' -Headers @{'X-API-Key'=$key} -TimeoutSec 5
                $epCount = if ($epc.count) { [int]$epc.count } else { 0 }
                if ($epCount -ge 3) { Add-Check 'RECOVERY' 'goals :v0.16' 'WARN' "0 goals but $epCount episodes — Codex extraction may be failing (check l1a.log)" }
                else                { Add-Check 'RECOVERY' 'goals :v0.16' 'WARN' 'no goals yet (normal post-ship; needs sessions)' }
            } catch { Add-Check 'RECOVERY' 'goals :v0.16' 'WARN' 'no goals yet (normal post-ship; needs sessions)' }
        }
    } catch { Add-Check 'RECOVERY' 'goals :v0.16' 'FAIL' $_.Exception.Message }
}

# R6b: goals stale-sweep freshness (v0.18 MED-13)
# goals-stale-sweep.py logs every run to ~/.mem0/goals-stale-sweep.jsonl with
# fields ts / found_count / auto_abandon. Weekly systemd-user timer Sun 04:00.
try {
    $sweepLog = "$TmsHomeUnc\.mem0\goals-stale-sweep.jsonl"
    if (Test-Path $sweepLog) {
        $last = Get-Content $sweepLog -Tail 1 | ConvertFrom-Json
        $age = (Get-Date) - [datetime]$last.ts
        if ($age.TotalDays -gt 14) { Add-Check 'RECOVERY' 'goals stale-sweep' 'WARN' "last run $($last.ts) >14d ago - timer may not be firing" }
        elseif ($last.found_count -gt 10 -and -not $last.auto_abandon) { Add-Check 'RECOVERY' 'goals stale-sweep' 'WARN' "$($last.found_count) stale goals piled up - run with --auto-abandon" }
        else { Add-Check 'RECOVERY' 'goals stale-sweep' 'OK' "last $($last.ts) found=$($last.found_count)" }
    } else { Add-Check 'RECOVERY' 'goals stale-sweep' 'WARN' 'no sweep log yet' }
} catch { Add-Check 'RECOVERY' 'goals stale-sweep' 'WARN' $_.Exception.Message }

# R6c: contradiction-sweep freshness + YES-stamp visibility + run outcome
# (v0.19 I.3 + fix-pass; v0.20 Phase C M7/M16/L6)
# contradiction-sweep.py logs every run (incl. dry-run) to
# ~/.mem0/contradiction-sweep.jsonl with fields ts / pairs_checked / yes_count /
# stamped_count / stamped_ids. v0.20 summaries add outcome ('ok' |
# 'degraded:<reason>' | 'no-op:<reason>') and canonical_total (pre---limit) vs
# canonical_count (processed): WARN on any non-ok outcome (degenerate/no-op runs
# are no longer invisible); --limit truncation shows as 'N/M canonicals
# processed' in the OK detail. Back-compat: pre-v0.20 entries lack outcome ->
# freshness-only check, never a false WARN on historical lines.
# Weekly systemd-user timer Sun 05:00 (contradiction-sweep.timer). Fix-pass:
# the last APPLY-mode run's yes_count>0 WARNs with the stamped memory ids —
# every retrieval suppression gets human review within a week (one-command
# recovery: contradiction-sweep.py --unstamp <id>; admission-gate.md runbook).
# Grace: timer enabled but no run yet -> OK with note (first weekly fire pending).
# v0.18 Phase H convention: wsl commands stay single-line (CRLF in a here-string
# breaks bash).
try {
    $csLog = "$TmsHomeUnc\.mem0\contradiction-sweep.jsonl"
    if (Test-Path $csLog) {
        $csRuns = @(Get-Content $csLog | ForEach-Object { try { $_ | ConvertFrom-Json } catch {} })
        # v0.27.3: a --rejudge-stamped run (Codex) authoritatively resolves the existing flags and
        # has its OWN schema (mode='rejudge-stamped'; kept/cleared, no yes_count). Report it directly
        # when it is the most recent apply-type action; the normal-sweep freshness/outcome logic
        # reads only NORMAL lines (mode absent) so the rejudge line can't be misparsed.
        $rejudge = @($csRuns | Where-Object { $_.PSObject.Properties['mode'] -and $_.mode -eq 'rejudge-stamped' }) | Select-Object -Last 1
        $normalRuns = @($csRuns | Where-Object { -not ($_.PSObject.Properties['mode'] -and $_.mode -eq 'rejudge-stamped') })
        $last = if ($normalRuns.Count) { $normalRuns[-1] } else { $csRuns[-1] }
        $lastApply = @($normalRuns | Where-Object { $_.dry_run -eq $false }) | Select-Object -Last 1
        $rejudgeNewer = $rejudge -and (($null -eq $lastApply) -or ([datetime]$rejudge.ts -ge [datetime]$lastApply.ts))
        if ($rejudgeNewer) {
            $kept = [int]$rejudge.kept; $cleared = [int]$rejudge.cleared
            if ([string]$rejudge.outcome -ne 'ok') {
                Add-Check 'RECOVERY' 'contradiction sweep' 'WARN' "last rejudge-stamped $($rejudge.ts) outcome=$($rejudge.outcome) (judge=$($rejudge.judge))"
            } elseif ($kept -gt 0) {
                $kids = @($rejudge.kept_ids | ForEach-Object { $_.memory_id }) -join ', '
                $rjJudge = if ($rejudge.PSObject.Properties['judge']) { [string]$rejudge.judge } else { 'codex' }
                if ($rjJudge -eq 'codex') {
                    Add-Check 'RECOVERY' 'contradiction sweep' 'WARN' "rejudge-stamped $($rejudge.ts) (judge=codex): $kept Codex-confirmed contradiction(s) hidden by design + $cleared false-positive(s) cleared. The $kept are authoritative (not suspect): $kids - --unstamp <id> only if a record should resurface"
                } else {
                    # v0.29.4: a NON-codex re-judge cannot produce authoritative confirmations — the guard
                    # now refuses --rejudge-stamped --judge!=codex, so an entry like this is a pre-guard
                    # artifact. Flag for an authoritative codex re-resolution.
                    Add-Check 'RECOVERY' 'contradiction sweep' 'WARN' "rejudge-stamped $($rejudge.ts) (judge=$rjJudge) is NON-AUTHORITATIVE — $kept enforced stamp(s) from a weak judge ($kids). Re-resolve: contradiction-sweep.py --rejudge-stamped --judge codex --apply"
                }
            } else {
                Add-Check 'RECOVERY' 'contradiction sweep' 'OK' "rejudge-stamped $($rejudge.ts) (judge=$($rejudge.judge)): 0 confirmed contradictions remain ($cleared false-positive(s) cleared)"
            }
        }
        elseif ($null -eq $last) {
            Add-Check 'RECOVERY' 'contradiction sweep' 'OK' 'only rejudge-stamped runs logged (no normal sweep yet)'
        }
        else {
            $age = (Get-Date) - [datetime]$last.ts
            $lastOutcome = if ($last.PSObject.Properties['outcome']) { [string]$last.outcome } else { $null }
            $procDetail = ''
            if ($last.PSObject.Properties['canonical_total'] -and [int]$last.canonical_total -gt [int]$last.canonical_count) {
                $procDetail = " $($last.canonical_count)/$($last.canonical_total) canonicals processed (--limit truncation)"
            }
            $applyJudge = if ($lastApply -and $lastApply.PSObject.Properties['judge']) { [string]$lastApply.judge } else { '' }
            if ($lastApply -and $lastApply.yes_count -gt 0 -and $applyJudge -eq 'local') {
                # v0.29.4: a LOCAL-judge sweep stamps contradicts_canonical_PENDING (advisory) —
                # the admission gate IGNORES it, so these are NOT hidden and need no --unstamp.
                # They are resolved authoritatively by --rejudge-stamped --judge codex. OK, not WARN.
                Add-Check 'RECOVERY' 'contradiction sweep' 'OK' "last APPLY run $($lastApply.ts) (judge=local) flagged yes=$($lastApply.yes_count) as ADVISORY pending (NOT hidden); resolve authoritatively with: contradiction-sweep.py --rejudge-stamped --judge codex --apply"
            }
            elseif ($lastApply -and $lastApply.yes_count -gt 0) {
                $ids = @($lastApply.stamped_ids | ForEach-Object { $_.memory_id }) -join ', '
                if (-not $ids) { $ids = '(ids not recorded - pre-fix-pass run)' }
                Add-Check 'RECOVERY' 'contradiction sweep' 'WARN' "last APPLY run $($lastApply.ts) (judge=$(if ($applyJudge) { $applyJudge } else { 'codex' })) stamped yes=$($lastApply.yes_count) ENFORCED record(s): $ids - review; clear false positives: contradiction-sweep.py --unstamp <id> (admission-gate.md sweep runbook)"
            }
            elseif ($lastOutcome -and $lastOutcome -ne 'ok') { Add-Check 'RECOVERY' 'contradiction sweep' 'WARN' "last run $($last.ts) outcome=$lastOutcome - sweep did no protective work (journalctl --user -u contradiction-sweep; admission-gate.md runbook)" }
            elseif ($age.TotalDays -gt 14) { Add-Check 'RECOVERY' 'contradiction sweep' 'WARN' "last run $($last.ts) >14d ago - timer may not be firing" }
            else { Add-Check 'RECOVERY' 'contradiction sweep' 'OK' "last $($last.ts) outcome=$(if ($lastOutcome) { $lastOutcome } else { 'n/a (pre-v0.20 entry)' }) pairs=$($last.pairs_checked) yes=$($last.yes_count) stamped=$($last.stamped_count)$procDetail; last apply yes=$(if ($lastApply) { $lastApply.yes_count } else { 'n/a' })" }
        }
    } else {
        $csTimer = wsl.exe -d $TmsDistro -e bash -c "systemctl --user list-timers --no-pager 2>/dev/null | grep contradiction-sweep || true"
        if ($csTimer -match 'contradiction-sweep') { Add-Check 'RECOVERY' 'contradiction sweep' 'OK' 'no run yet (timer enabled - first weekly fire pending)' }
        else { Add-Check 'RECOVERY' 'contradiction sweep' 'WARN' 'no sweep log and no timer - install systemd/contradiction-sweep.{service,timer}' }
    }
} catch { Add-Check 'RECOVERY' 'contradiction sweep' 'WARN' $_.Exception.Message }

# R6d: episodic-ledger reconciliation freshness + drift (v0.27.4 R5)
# episodic-reconcile.py logs one JSONL line per run to ~/.mem0/episodic-reconciliation.jsonl
# (READ-ONLY drift detection: orphaned mem0 links / dangling episodes). WARN on any drift, a
# non-ok outcome, or >14d stale; OK on a fresh clean run. Weekly timer Sun 05:30.
try {
    $erLog = "$TmsHomeUnc\.mem0\episodic-reconciliation.jsonl"
    if (Test-Path $erLog) {
        $erRuns = @(Get-Content $erLog | ForEach-Object { try { $_ | ConvertFrom-Json } catch {} })
        $erLast = $erRuns[-1]
        $erAge = (Get-Date) - [datetime]$erLast.ts
        $erOutcome = if ($erLast.PSObject.Properties['outcome']) { [string]$erLast.outcome } else { 'n/a' }
        $orphan = [int]$erLast.orphaned_count; $dangling = [int]$erLast.dangling_count
        if ($erOutcome -ne 'ok') { Add-Check 'RECOVERY' 'episodic reconcile' 'WARN' "last run $($erLast.ts) outcome=$erOutcome (journalctl --user -u episodic-reconcile)" }
        elseif (($orphan + $dangling) -gt 0) { Add-Check 'RECOVERY' 'episodic reconcile' 'WARN' "last run $($erLast.ts): $orphan orphaned mem0 link(s) + $dangling dangling episode link(s) of $($erLast.total_links) total - ledger<->store drift (READ-ONLY report; ledger is immutable by design)" }
        elseif ($erAge.TotalDays -gt 14) { Add-Check 'RECOVERY' 'episodic reconcile' 'WARN' "last run $($erLast.ts) >14d ago - timer may not be firing" }
        else { Add-Check 'RECOVERY' 'episodic reconcile' 'OK' "last $($erLast.ts): 0 drift over $($erLast.memory_links) mem0 links / $($erLast.episodes) episodes" }
    } else {
        $erTimer = wsl.exe -d $TmsDistro -e bash -c "systemctl --user list-timers --no-pager 2>/dev/null | grep episodic-reconcile || true"
        if ($erTimer -match 'episodic-reconcile') { Add-Check 'RECOVERY' 'episodic reconcile' 'OK' 'no run yet (timer enabled - first weekly fire pending)' }
        else { Add-Check 'RECOVERY' 'episodic reconcile' 'WARN' 'no reconcile log and no timer - install systemd/episodic-reconcile.{service,timer}' }
    }
} catch { Add-Check 'RECOVERY' 'episodic reconcile' 'WARN' $_.Exception.Message }

# Phase 5 anti-drift: consolidation retrieval-drift alarm surface.
# dream-consolidate.ps1 appends ONE JSONL record to ~/.mem0/consolidation-drift.jsonl ONLY when a
# consolidation made a canary self-fact unretrievable (the before/after snapshot compare returned
# exit 2). The file is ABSENT in the healthy steady state. WARN if an alarm fired within 14d
# (investigate the canary / re-index the store); OK if absent or only historical (none since).
try {
    $cdLog = "$TmsHomeUnc\.mem0\consolidation-drift.jsonl"
    if (Test-Path $cdLog) {
        $cdRuns = @(Get-Content $cdLog | ForEach-Object { try { $_ | ConvertFrom-Json } catch {} } | Where-Object { $_ })
        if ($cdRuns.Count -gt 0) {
            $cdLast = $cdRuns[-1]
            $cdAge  = (Get-Date) - [datetime]$cdLast.ts
            if ($cdAge.TotalDays -le 14) {
                Add-Check 'RECOVERY' 'consolidation drift' 'WARN' "drift alarm $($cdLast.ts) ($([int]$cdAge.TotalDays)d ago): $($cdLast.detail) - a canary fact became unretrievable after a consolidation; check eval/retrieval-drift + the dream log ($($cdRuns.Count) alarm(s) total)"
            } else {
                Add-Check 'RECOVERY' 'consolidation drift' 'OK' "last drift alarm $($cdLast.ts) >$([int]$cdAge.TotalDays)d ago, none since ($($cdRuns.Count) historical)"
            }
        } else {
            Add-Check 'RECOVERY' 'consolidation drift' 'OK' 'flag file present but no parseable alarm records'
        }
    } else {
        Add-Check 'RECOVERY' 'consolidation drift' 'OK' 'no drift alarms (no flag file yet)'
    }
} catch { Add-Check 'RECOVERY' 'consolidation drift' 'WARN' $_.Exception.Message }

# R7: open_questions health (v0.17 Phase D)
if ($key) {
    try {
        $r     = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/open_questions?status=open&limit=200' -Headers @{'X-API-Key'=$key} -TimeoutSec 5
        $total = @($r).Count
        Add-Check 'RECOVERY' 'open_questions :v0.17' 'OK' "$total open frontier questions"
    } catch { Add-Check 'RECOVERY' 'open_questions :v0.17' 'FAIL' $_.Exception.Message }
}

# R8: systemd-user linger enabled (v0.18 LOW-7)
# Without linger, the WSL user's systemd-user instance (and its timers: decay-scan,
# stack-backup, goals-stale-sweep, l10-audit) only runs while a login session is alive.
try {
    $linger = wsl.exe -d $TmsDistro -e bash -c "loginctl show-user $TmsWslUser --property=Linger 2>/dev/null"
    if ($linger -match 'Linger=yes') {
        Add-Check 'RECOVERY' 'systemd-user linger' 'OK' 'Linger=yes (user timers survive logout)'
    } else {
        $got = (($linger | Where-Object { $_ }) -join ' ').Trim(); if (-not $got) { $got = '(no output)' }
        Add-Check 'RECOVERY' 'systemd-user linger' 'WARN' "expected Linger=yes, got: $got - fix: wsl.exe -u root -e loginctl enable-linger $TmsWslUser"
    }
} catch { Add-Check 'RECOVERY' 'systemd-user linger' 'WARN' $_.Exception.Message }

# R9: deployed-hooks freshness (v0.19 M13)
# The production hook pipeline at ~/.claude/scripts is a multi-file deployment
# (user-prompt-extract.ps1 REQUIRES user-prompt-lib.ps1; a missing/stale lib
# silently disables Phase 0.B decision capture and 0.D memory injection, leaving
# only an unmonitored log line). SHA256-compare repo vs deployed for every script
# the stack deploys; any mismatch/missing file = WARN naming the offenders.
try {
    $repoHookDir     = Join-Path $TmsRepoWin 'scripts\windows'
    $deployedHookDir = Join-Path $env:USERPROFILE '.claude\scripts'
    $hookNames = @(
        'user-prompt-extract.ps1', 'user-prompt-lib.ps1', 'stop-extract.ps1',
        'pre-tool-check.ps1', 'l1a-extract.ps1', 'memory-common.ps1', 'Test-MemoryStack.ps1',
        'mem0-hook-daemon.ps1', 'mem0-hook-daemon-spawn.ps1',  # v0.20 A.5 resident daemon + SessionStart launcher
        'mem0-hook-client.cs',                                 # v0.20 A.6 compiled thin client SOURCE (exe is built FROM the deployed copy)
        'build-hook-client.ps1',                               # v0.20 Final: smoke-gated builder — deployed so a repo-less DR restore can rebuild the exe (SessionStart self-heal in mem0-hook-daemon-spawn.ps1)
        'dream-consolidate.ps1',                               # v0.20 Phase F (L4): installer deploys it + Task Scheduler runs it nightly — was the one deployed script R9 never checked
        'autopromote-lib.ps1',                                 # Phase 2c: dot-sourced by dream-consolidate.ps1 for the autonomous-promotion decision logic (must deploy beside it)
        'codex-shim.ps1', 'codex-shim-spawn.ps1'               # v0.27.1 R5 keystone: Windows-resident Codex HTTP shim + its flag-gated SessionStart launcher
    )
    if (-not (Test-Path $repoHookDir)) {
        Add-Check 'RECOVERY' 'deployed hooks freshness' 'WARN' "repo dir not found: $repoHookDir"
    } else {
        # v1.0 Phase 7A: deployed hook scripts carry bounded operator sentinels
        # (WSL-user / Windows-user / distro) that install/2-windows-config.ps1
        # substitutes to the operator's real values, so a raw repo-vs-deployed SHA
        # would emit a FALSE 'drift' WARN. Normalize the repo text with the SAME
        # substitution (operator values from the receipt) before hashing, so genuine
        # drift is still caught while the legitimate substitution is not.
        # IMPORTANT (audit fix): the sentinel SEARCH tokens are ASSEMBLED FROM
        # FRAGMENTS so this verifier carries NO literal sentinel of its own — the
        # installer's deploy-loop match/Resolve-StackTokens therefore cannot see or
        # rewrite this file (it must deploy verbatim, receipt-driven). Embedding the
        # literal sentinel here previously let the installer substitute the
        # normalizer's own search-strings to the operator's real values, turning the
        # deployed normalizer into a no-op -> false drift for every sentinel-bearing
        # R9 file on a fresh box.
        $snU = '__WSL' + '_USER__'; $snW = '__WIN' + '_USER__'; $snD = '__WSL' + '_DISTRO__'
        $staleHooks = @()
        foreach ($hn in $hookNames) {
            $repoFile = Join-Path $repoHookDir $hn
            $depFile  = Join-Path $deployedHookDir $hn
            if     (-not (Test-Path $depFile))  { $staleHooks += "$hn(MISSING)" }
            elseif (-not (Test-Path $repoFile)) { $staleHooks += "$hn(no-repo-copy)" }
            else {
                $repoText = [System.IO.File]::ReadAllText($repoFile)
                if ($TmsWslUser) { $repoText = $repoText.Replace($snU, $TmsWslUser) }
                if ($TmsWinUser) { $repoText = $repoText.Replace($snW, $TmsWinUser) }
                if ($TmsDistro)  { $repoText = $repoText.Replace($snD, $TmsDistro) }
                $repoHash = [BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($repoText))).Replace('-','')
                $depHash  = (Get-FileHash $depFile -Algorithm SHA256).Hash
                if ($repoHash -ne $depHash) { $staleHooks += "$hn(drift)" }
            }
        }
        # v0.20 A.6: the REGISTERED UserPromptSubmit command is the compiled
        # client — a MISSING exe breaks the hook HARD (settings.json points at
        # one command), and an exe OLDER than the deployed .cs means a source
        # deploy happened without a rebuild. Fix either with
        # scripts\windows\build-hook-client.ps1 (compiles + smoke-gates before
        # install). Emergency rollback: settings.json UserPromptSubmit command
        # -> powershell.exe -NoProfile -ExecutionPolicy Bypass -File $env:USERPROFILE/.claude/scripts/user-prompt-extract.ps1
        $cliExe = Join-Path $deployedHookDir 'mem0-hook-client.exe'
        $cliCs  = Join-Path $deployedHookDir 'mem0-hook-client.cs'
        # v0.21 Phase B (L3): a .sha256 sidecar written at install time gives R9
        # a content anchor for the exe (no committed binary to hash against) —
        # detects accidental/out-of-band same-user replacement at the same trust
        # level as the script SHA checks. Order: MISSING > STALE-vs-.cs >
        # CONTENT-DRIFT-vs-sidecar > sidecar-absent.
        $cliSidecar = $cliExe + '.sha256'
        if     (-not (Test-Path $cliExe)) { $staleHooks += 'mem0-hook-client.exe(MISSING - UserPromptSubmit registration broken; run build-hook-client.ps1 or roll back settings.json to powershell.exe)' }
        elseif ((Test-Path $cliCs) -and ((Get-Item $cliExe).LastWriteTime -lt (Get-Item $cliCs).LastWriteTime)) { $staleHooks += 'mem0-hook-client.exe(STALE - exe older than deployed .cs; re-run build-hook-client.ps1)' }
        elseif ((Test-Path $cliSidecar) -and ((Get-FileHash $cliExe -Algorithm SHA256).Hash -ne (Get-Content $cliSidecar -Raw).Trim())) { $staleHooks += 'mem0-hook-client.exe(CONTENT-DRIFT - hash differs from build-time record; re-run build-hook-client.ps1)' }
        elseif (-not (Test-Path $cliSidecar)) { $staleHooks += 'mem0-hook-client.exe(no .sha256 sidecar - re-run build-hook-client.ps1 to record one)' }

        # v0.22 Pillar 2 (D4): the model-tier policy is a config file read at
        # runtime by the hook lib (Resolve-ModelTier / Get-SessionTier) and the
        # SessionStart spawn launcher. Its REPO source is claude-config\ (not
        # scripts\windows\, so it cannot ride the $hookNames loop above); track
        # it here with the same SHA256 repo-vs-deployed parity. A missing/stale
        # copy makes every session silently fall back to the default frontier
        # tier (today's behavior) — WARN so a drift is visible.
        $tiersRepo = Join-Path (Split-Path -Parent (Split-Path -Parent $repoHookDir)) 'claude-config\model-tiers.json'
        $tiersDep  = Join-Path $deployedHookDir 'model-tiers.json'
        if     (-not (Test-Path $tiersDep))  { $staleHooks += 'model-tiers.json(MISSING - sessions default to frontier tier; redeploy from claude-config)' }
        elseif (-not (Test-Path $tiersRepo)) { $staleHooks += 'model-tiers.json(no-repo-copy)' }
        elseif ((Get-FileHash $tiersRepo -Algorithm SHA256).Hash -ne (Get-FileHash $tiersDep -Algorithm SHA256).Hash) { $staleHooks += 'model-tiers.json(drift - re-deploy claude-config\model-tiers.json)' }

        # v0.30: storage-cap-check.sh (SessionStart brand/canonical block) repo source is
        # claude-config\ (not scripts\windows\), so it can't ride the $hookNames loop. Track it
        # here with the SAME sentinel-aware SHA parity (it carries the WSL-user sentinel).
        # An untracked drift here is how the 1-of-7 canonical-surfacing defect hid (2026-06-19).
        $scRepo = Join-Path (Split-Path -Parent (Split-Path -Parent $repoHookDir)) 'claude-config\storage-cap-check.sh'
        $scDep  = Join-Path $deployedHookDir 'storage-cap-check.sh'
        if     (-not (Test-Path $scDep))  { $staleHooks += 'storage-cap-check.sh(MISSING - SessionStart canonical/brand block absent; redeploy from claude-config)' }
        elseif (-not (Test-Path $scRepo)) { $staleHooks += 'storage-cap-check.sh(no-repo-copy)' }
        else {
            $scText = [System.IO.File]::ReadAllText($scRepo)
            if ($TmsWslUser) { $scText = $scText.Replace($snU, $TmsWslUser) }
            if ($TmsWinUser) { $scText = $scText.Replace($snW, $TmsWinUser) }
            if ($TmsDistro)  { $scText = $scText.Replace($snD, $TmsDistro) }
            $scRepoHash = [BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($scText))).Replace('-','')
            if ($scRepoHash -ne (Get-FileHash $scDep -Algorithm SHA256).Hash) { $staleHooks += 'storage-cap-check.sh(drift - re-deploy claude-config\storage-cap-check.sh with WSL-user substitution)' }
        }

        if ($staleHooks.Count -eq 0) {
            Add-Check 'RECOVERY' 'deployed hooks freshness' 'OK' "$($hookNames.Count)/$($hookNames.Count) deployed scripts SHA256-match repo + model-tiers.json + client exe present and fresh"
        } else {
            Add-Check 'RECOVERY' 'deployed hooks freshness' 'WARN' (($staleHooks -join ', ') + " - redeploy: Copy-Item (Join-Path '$TmsRepoWin' 'scripts\windows\<name>') ~\.claude\scripts\")
        }
    }
} catch { Add-Check 'RECOVERY' 'deployed hooks freshness' 'WARN' $_.Exception.Message }


# ==========================================================================
# RECENT ACTIVITY (informational — outside dimensions, no subtotal impact)
# ==========================================================================
function Get-LastLogLine {
    param([string]$Path, [int]$Max = 80)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $line = Get-Content -LiteralPath $Path -Tail 1 -ErrorAction SilentlyContinue
    if (-not $line) { return $null }
    $s = (($line -as [string]) -replace '\s+', ' ').Trim()
    if ($s.Length -eq 0) { return $null }
    if ($s.Length -gt $Max) { return $s.Substring(0, $Max) + '...' } else { return $s }
}

$infoRows = @()
function Add-Info { param([string]$C, [string]$S, [string]$D = '')
    $script:infoRows += [PSCustomObject]@{ Component = $C; Status = $S; Detail = $D }
}

# Hooks
try {
    $s = Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw | ConvertFrom-Json
    # v1.0 Phase 7A: scan ALL entries for the stack hook. The idempotent installer
    # appends stack hooks AFTER any preserved user hooks, so [0] is not necessarily
    # ours — and Claude Code fires every registered hook regardless of order. The
    # old [0]-only check false-FAILed once unrelated SessionStart hooks were present.
    $stopCmds = @($s.hooks.Stop | ForEach-Object { $_.hooks } | ForEach-Object { $_.command })
    if (($stopCmds | Where-Object { $_ -match 'stop-extract\.ps1' })) { Add-Info 'Stop hook' 'OK' '' }
    else { Add-Info 'Stop hook' 'WARN' "no stop-extract.ps1 entry; commands: $($stopCmds -join '; ')" }
    $sessCmds = @($s.hooks.SessionStart | ForEach-Object { $_.hooks } | ForEach-Object { $_.command })
    $sessCmd = @($sessCmds | Where-Object { $_ -match 'wsl\.exe.*storage-cap-check' }) | Select-Object -First 1
    if ($sessCmd) {
        $deployedScript = "$TmsHomeUnc\.claude\scripts\storage-cap-check.sh"
        if (Test-Path $deployedScript) {
            $sc = Get-Content $deployedScript -Raw -ErrorAction SilentlyContinue
            $hasBaseline = $sc -match 'audit-flags\.baseline'
            $hasSessionSummary = $sc -match 'session_summary'
            if ($hasBaseline -and $hasSessionSummary) { Add-Info 'SessionStart hook' 'OK' 'wsl.exe wrapper + v0.13 deployed (baseline-aware)' }
            else { Add-Info 'SessionStart hook' 'WARN' "deployed script may be old v0.12 (baseline=$hasBaseline ss=$hasSessionSummary)" }
        } else { Add-Info 'SessionStart hook' 'OK' 'wsl.exe wrapper correct' }
    } else { Add-Info 'SessionStart hook' 'FAIL' 'Bash-only command will exit 127' }
} catch { Add-Info 'Claude settings.json' 'FAIL' $_.Exception.Message }

$lastL1a = Get-LastLogLine -Path "$env:USERPROFILE\.claude\logs\l1a.log"
if ($lastL1a) { Add-Info 'L1a last activity' 'OK' $lastL1a } else { Add-Info 'L1a last activity' 'WARN' 'no l1a.log yet' }

$lastDream = Get-LastLogLine -Path "$env:USERPROFILE\.claude\logs\dream.log"
if ($lastDream) { Add-Info 'Dream last activity' 'OK' $lastDream } else { Add-Info 'Dream last activity' 'WARN' 'no dream.log yet (fires nightly 3am)' }

$dlq = "$env:USERPROFILE\.claude\state\mem0-post-failures.jsonl"
if (Test-Path -LiteralPath $dlq) {
    $n = (Get-Content -LiteralPath $dlq | Measure-Object -Line).Lines
    if ($n -gt 0) { Add-Info 'mem0 DLQ' 'WARN' "$n queued failures — will drain on next L1a/C1 run" }
}

try {
    $flags = wsl.exe -d $TmsDistro -e bash -lc "wc -l < /home/$TmsWslUser/.mem0/audit-flags.jsonl 2>/dev/null"
    $flagN = ($flags -as [string]).Trim()
    if ($flagN -as [int] -gt 1000) { Add-Info 'L10 audit flags' 'WARN' "$flagN flags — large; review periodically" }
    else                            { Add-Info 'L10 audit flags' 'OK'   "$flagN flags" }
} catch {}


# ==========================================================================
# RENDER + TOP-LINE SUMMARY
# ==========================================================================

function Get-DimensionStatus {
    param([array]$rows, [string]$name)
    $fails = @($rows | Where-Object { $_.Status -eq 'FAIL' }).Count
    $warns = @($rows | Where-Object { $_.Status -eq 'WARN' }).Count
    if    ($fails -gt 0) { return @{Color='Red';    Label='FAIL'; Text="$name FAIL ($fails fail)" } }
    elseif ($warns -gt 0){ return @{Color='Yellow'; Label='WARN'; Text="$name WARN ($warns warn)" } }
    else                  { return @{Color='Green';  Label='GREEN'; Text="$name GREEN" } }
}

if (-not $Quiet) {
    Write-Host ''
    Write-Host '  ── LIVENESS ──────────────────────────────────────' -ForegroundColor Cyan
    $livenessRows  | Format-Table -AutoSize | Out-String | ForEach-Object { $_.TrimEnd() } | Write-Host
    Write-Host '  ── INVARIANTS ────────────────────────────────────' -ForegroundColor Cyan
    $invariantRows | Format-Table -AutoSize | Out-String | ForEach-Object { $_.TrimEnd() } | Write-Host
    Write-Host '  ── RECOVERY ──────────────────────────────────────' -ForegroundColor Cyan
    $recoveryRows  | Format-Table -AutoSize | Out-String | ForEach-Object { $_.TrimEnd() } | Write-Host
    Write-Host '  ── INFO (no subtotal) ────────────────────────────' -ForegroundColor DarkGray
    $infoRows      | Format-Table -AutoSize | Out-String | ForEach-Object { $_.TrimEnd() } | Write-Host
}

$ls = Get-DimensionStatus $livenessRows  'LIVENESS'
$is = Get-DimensionStatus $invariantRows 'INVARIANTS'
$rs = Get-DimensionStatus $recoveryRows  'RECOVERY'

$allRows   = $livenessRows + $invariantRows + $recoveryRows
$totalPass = @($allRows | Where-Object { $_.Status -eq 'OK' }).Count
$totalWarn = @($allRows | Where-Object { $_.Status -eq 'WARN' }).Count
$totalFail = @($allRows | Where-Object { $_.Status -eq 'FAIL' }).Count

$greenCount = @($ls, $is, $rs | Where-Object { $_.Label -eq 'GREEN' }).Count
$warnCount  = @($ls, $is, $rs | Where-Object { $_.Label -eq 'WARN'  }).Count
$failCount  = @($ls, $is, $rs | Where-Object { $_.Label -eq 'FAIL'  }).Count

$dimSummary = "$($ls.Text); $($is.Text); $($rs.Text)"

Write-Host ''
if ($failCount -eq 0 -and $warnCount -eq 0) {
    Write-Host "  Memory stack: HEALTHY (3/3 dimensions GREEN; $totalPass checks PASS)" -ForegroundColor Green
} elseif ($failCount -eq 0) {
    Write-Host "  Memory stack: DEGRADED ($dimSummary; $totalPass PASS, $totalWarn WARN)" -ForegroundColor Yellow
} else {
    Write-Host "  Memory stack: UNHEALTHY ($dimSummary; $totalPass PASS, $totalWarn WARN, $totalFail FAIL)" -ForegroundColor Red
}

if ($totalFail -gt 0) { exit 1 } else { exit 0 }
