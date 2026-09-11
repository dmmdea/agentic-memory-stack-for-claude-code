# Invoke-BundleOnce.ps1 - P1-6 live driver (NOT a test): one POST /v1/context/bundle through
# the daemon's cold-embedder retry path, against $env:MEM0_URL, printing the diag prefix and the
# head of the response. The lead uses it for the induced-cold check: unload the authority's
# embedder (or wait past its 5-min idle ttl), run this, and expect
#   diag_prefix: cold-embedder retried=1 ok=True daemon_ms=<n>
# on a cold seat and an empty diag_prefix (first POST ok) on a warm one.
#
# Usage (Windows PowerShell 5.1 or pwsh):
#   $env:MEM0_URL = 'http://<authority-tailnet-ip>:18791'; pwsh -NoProfile -File scripts\windows\tests\Invoke-BundleOnce.ps1
# API key: the lib's Get-Mem0ApiKeyCached (same source as the hooks); -ApiKey overrides.
param(
    [string]$Mem0Url = $env:MEM0_URL,
    [string]$ApiKey = '',
    [string]$Prompt = 'what is the authority bind address',
    [string]$Brand = 'ai-ecosystem',
    [int]$TimeoutMs = 3000
)

$ErrorActionPreference = 'Stop'
$winDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $winDir 'user-prompt-lib.ps1')
. (Join-Path $winDir 'mem0-hook-daemon.ps1') -DefineOnly

if (-not $Mem0Url) { throw 'MEM0_URL is not set and -Mem0Url was not given' }
$Mem0Url = $Mem0Url.TrimEnd('/')
if (-not $ApiKey) { $ApiKey = Get-Mem0ApiKeyCached }
if (-not $ApiKey) { throw 'no API key: Get-Mem0ApiKeyCached returned nothing and -ApiKey was not given' }

$cwd = (Get-Location).Path
$workspace = Split-Path -Leaf $cwd
$project = $null
$initiative = $null
try { $initiative = Get-SessionInitiative -Cwd $cwd } catch { $initiative = $null }

$body = ConvertTo-HookJson @{
    session_id            = 'bundle-once'
    prompt                = $Prompt
    brand                 = $Brand
    workspace             = $workspace
    project               = $project
    initiative            = $initiative
    tier                  = 'frontier'
    transcript_path       = $null
    hook_contract_version = $script:HookContractVersion
}

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$r = Invoke-BundlePostWithColdRetry -Uri ($Mem0Url + '/v1/context/bundle') -Body $body -ApiKey $ApiKey -TimeoutMs $TimeoutMs
$sw.Stop()

Write-Output ('url:         ' + $Mem0Url + '/v1/context/bundle')
Write-Output ('ok:          ' + [string]$r.ok)
Write-Output ('retried:     ' + [string]$r.retried)
Write-Output ('diag_prefix: ' + [string]$r.diag_prefix)
Write-Output ('wall_ms:     ' + [string]$sw.ElapsedMilliseconds)
$head = ''
if ($r.text) { $head = [string]$r.text; if ($head.Length -gt 300) { $head = $head.Substring(0, 300) } }
Write-Output ('response:    ' + $head)
if (-not $r.ok) { exit 1 }
exit 0
