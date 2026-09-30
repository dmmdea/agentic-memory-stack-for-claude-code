# role-lib.ps1 - which role an install run applies (dot-sourced by install.ps1 and
# 2-windows-config.ps1; not run on its own). Plain ASCII on purpose: install.ps1 parses under
# every host encoding, and so must what it loads.
#
# Why this exists: -Role defaults to 'brain', and 2-windows-config.ps1 writes whatever it is given
# to the receipt, to ~/.mem0/role on both sides and to the brain gate that registers the nightly
# dream/dedup tasks. install.ps1 used to hand it the default unconditionally, so a plain
# `.\install.ps1` re-run on a replica made the replica a brain (phase 1 already kept the recorded
# role when -Role was omitted; phase 2 did not). Precedence, the same order Get-Mem0Role reads at
# run time:
#   an explicit -Role  >  %USERPROFILE%\.mem0\role (2-windows-config.ps1 writes it)
#                      >  the receipt's Role (%USERPROFILE%\.claude\scripts\mem0-stack.config.psd1)
#                      >  'brain', the first-install default.
# A recorded value that is neither 'brain' nor 'replica' is ignored with a warning, never passed on.

function ConvertTo-InstallRole {
    param([string]$Text)
    $r = "$Text".Trim().TrimStart([char]0xFEFF).Trim().ToLowerInvariant()
    if ($r -eq 'brain' -or $r -eq 'replica') { return $r }
    return $null
}

function Resolve-InstallRole {
    param(
        [Parameter(Mandatory)][bool]$Explicit,
        [string]$Requested = 'brain',
        [string]$ProfileDir = $env:USERPROFILE
    )
    if ($Explicit) { return [pscustomobject]@{ Role = $Requested; Source = 'explicit -Role' } }
    if ($ProfileDir) {
        $roleFile = Join-Path (Join-Path $ProfileDir '.mem0') 'role'
        try {
            if (Test-Path -LiteralPath $roleFile) {
                $raw = [System.IO.File]::ReadAllText($roleFile)
                $r = ConvertTo-InstallRole $raw
                if ($r) { return [pscustomobject]@{ Role = $r; Source = "recorded in $roleFile" } }
                if ($raw.Trim()) { Write-Warning "$roleFile holds '$($raw.Trim())', neither brain nor replica; ignoring it" }
            }
        } catch { Write-Verbose "role file unreadable: $($_.Exception.Message)" }
        $receipt = Join-Path (Join-Path (Join-Path $ProfileDir '.claude') 'scripts') 'mem0-stack.config.psd1'
        try {
            if (Test-Path -LiteralPath $receipt) {
                $r = ConvertTo-InstallRole ((Import-PowerShellDataFile -LiteralPath $receipt).Role)
                if ($r) { return [pscustomobject]@{ Role = $r; Source = "recorded in the receipt $receipt" } }
            }
        } catch { Write-Verbose "receipt unreadable: $($_.Exception.Message)" }
    }
    return [pscustomobject]@{ Role = 'brain'; Source = 'default (no role recorded on this box)' }
}
