# role-lib.ps1 - which role an install run applies (dot-sourced by install.ps1 and
# 2-windows-config.ps1; not run on its own). Plain ASCII on purpose: install.ps1 parses under
# every host encoding, and so must what it loads. Windows PowerShell 5.1 compatible (no ??, no
# ternary, no pwsh-7-only cmdlet); InstallRole.Tests.ps1 runs the resolver under 5.1 as well.
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
#
# A record that EXISTS but yields no role is an error, never a fall-through to the default. A role
# file that is empty or holds anything but brain/replica (phase 2 writes it with a plain, non-atomic
# WriteAllText, so a torn write leaves it empty), a receipt whose Role is empty or unrecognized, a
# receipt that will not parse: on such a box an install has run, and 'brain' is exactly the wrong
# guess for a replica. The resolver throws, names the record and asks for an explicit -Role. Only
# "nothing recorded at all" and a receipt from before roles existed (no Role key, no role file) are
# a first install. A usable role in the other record still wins, with a warning for the skipped one.
# Both callers resolve before they write anything, so the throw leaves the box untouched.

function ConvertTo-InstallRole {
    param([string]$Text)
    $r = "$Text".Trim().TrimStart([char]0xFEFF).Trim().ToLowerInvariant()
    # Ordinal on purpose, and the literals are returned rather than the text that was read: -eq and
    # -ceq compare culture-aware, which ignores NUL, so a UTF-16 file read as UTF-8 ("r<NUL>e<NUL>p...")
    # compared equal to 'replica' and the NUL string was handed on as the role.
    if ([string]::Equals($r, 'brain', [System.StringComparison]::Ordinal)) { return 'brain' }
    if ([string]::Equals($r, 'replica', [System.StringComparison]::Ordinal)) { return 'replica' }
    return $null
}

function Format-InstallRoleValue {
    # A recorded value made safe to print: control characters and NULs shown as '?', cut at 40 characters.
    param($Value)
    $t = ("$Value").Trim()
    $t = [regex]::Replace($t, '[^\x20-\x7E]', '?')
    if ($t.Length -gt 40) { $t = $t.Substring(0, 40) + '...' }
    return "'" + $t + "'"
}

function Resolve-InstallRole {
    param(
        [Parameter(Mandatory)][bool]$Explicit,
        [string]$Requested = 'brain',
        [string]$ProfileDir = $env:USERPROFILE
    )
    if ($Explicit) { return [pscustomobject]@{ Role = $Requested; Source = 'explicit -Role' } }
    $default = 'default (no role recorded on this box)'
    if (-not $ProfileDir) { return [pscustomobject]@{ Role = 'brain'; Source = $default } }

    $roleFile = Join-Path (Join-Path $ProfileDir '.mem0') 'role'
    $receipt = Join-Path (Join-Path (Join-Path $ProfileDir '.claude') 'scripts') 'mem0-stack.config.psd1'
    $unusable = New-Object System.Collections.Generic.List[string]   # one line per record that exists but says nothing usable

    # The role file phase 2 writes.
    try {
        if (Test-Path -LiteralPath $roleFile) {
            $raw = [System.IO.File]::ReadAllText($roleFile)
            $r = ConvertTo-InstallRole $raw
            if ($r) { return [pscustomobject]@{ Role = $r; Source = "recorded in $roleFile" } }
            if ($raw.Trim()) { $unusable.Add("the role file $roleFile holds $(Format-InstallRoleValue $raw), neither brain nor replica") }
            else { $unusable.Add("the role file $roleFile is empty (a write that was cut short?)") }
        }
    } catch { $unusable.Add("the role file $roleFile could not be read ($($_.Exception.Message))") }

    # The receipt. -ErrorAction Stop: a file that will not parse is a NON-terminating error otherwise,
    # which the catch below would never see and a caller without ErrorActionPreference Stop would
    # read as "nothing recorded".
    $legacy = $false
    try {
        if (Test-Path -LiteralPath $receipt) {
            # (An empty file, a bare string or an array is rejected by Import-PowerShellDataFile itself and lands
            # in the catch; only a hashtable gets past this line.)
            $data = Import-PowerShellDataFile -LiteralPath $receipt -ErrorAction Stop
            if (-not $data.ContainsKey('Role')) {
                $legacy = $true    # written before roles existed: it records no role, and is not damaged
            } else {
                $val = $data['Role']
                $r = $null
                if ($val -is [string]) { $r = ConvertTo-InstallRole $val }
                if ($r) {
                    foreach ($u in $unusable) { Write-Warning "$u; using the receipt's Role instead" }
                    return [pscustomobject]@{ Role = $r; Source = "recorded in the receipt $receipt" }
                }
                if ($null -eq $val -or ("$val").Trim() -eq '') { $unusable.Add("the receipt $receipt has an empty Role") }
                elseif ($val -isnot [string]) { $unusable.Add("the receipt $receipt has a Role that is not plain text ($($val.GetType().Name))") }
                else { $unusable.Add("the receipt $receipt has Role = $(Format-InstallRoleValue $val), neither brain nor replica") }
            }
        }
    } catch { $unusable.Add("the receipt $receipt could not be read ($($_.Exception.Message))") }

    if ($unusable.Count -gt 0) {
        throw ("Cannot tell which role this box has: " + ($unusable -join '; ') + ". Not guessing: taking 'brain' from a record " +
               "that cannot be used would turn a replica into a brain (the receipt, both role files and the nightly dream/dedup " +
               "tasks). Pass -Role brain or -Role replica explicitly.")
    }
    if ($legacy) { $default = 'default (the receipt on this box predates roles and holds none)' }
    return [pscustomobject]@{ Role = 'brain'; Source = $default }
}
