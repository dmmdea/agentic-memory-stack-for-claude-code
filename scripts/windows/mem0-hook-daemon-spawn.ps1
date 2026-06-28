# mem0-hook-daemon-spawn.ps1 — v0.20 A.5: SessionStart best-effort daemon spawn.
#
# Registered as an async SessionStart hook (settings.json) so the resident
# hook daemon is already warm by the time the first UserPromptSubmit fires.
# Probe is enumeration-only (no pipe open, ~1-3ms); the spawn is a detached
# hidden powershell.exe 5.1 — this launcher exits immediately either way.
# Self-contained on purpose for the SPAWN path (no lib dot-source for the
# daemon launch): a broken lib deploy must not break SessionStart, and the
# daemon's named mutex makes a duplicate spawn a no-op. Failure here is
# invisible by design — the UserPromptSubmit client also spawns the daemon on
# its no-pipe fallback.
#
# v0.22 Pillar 2 (D4): SessionStart ALSO writes a per-session tier sidecar
# (~/.mem0/session-tier/<session_id>.json = {model, tier, initiative, ts}). The
# SessionStart payload carries a `model` field (UserPromptSubmit does not), so
# resolving the tier here and caching it lets the prompt path read it instead of
# scanning the transcript. The sidecar `initiative` also lets UserPromptSubmit
# skip the per-prompt `git` spawn (Get-SessionInitiative). The sidecar block is
# fully isolated in a try/catch that dot-sources the lib lazily — any failure
# (missing/broken lib, no model, unreadable payload) writes nothing and leaves
# the daemon spawn below untouched (downstream falls back to transcript/default
# + a per-prompt git spawn, i.e. exactly the pre-v0.22 behavior).

$ErrorActionPreference = 'SilentlyContinue'
$pipeName = 'mem0-hook-daemon'

# v0.22 Pillar 2: read the SessionStart stdin payload ONCE (both SessionStart
# hooks receive the same payload). Consume it even if the sidecar write fails so
# stdin never blocks. Empty/unreadable -> no sidecar (fail-open).
$ssRaw = $null
try { $ssRaw = [Console]::In.ReadToEnd() } catch { $ssRaw = $null }

# --- session-tier sidecar (fail-open, isolated from the daemon spawn) ---
try {
    if ($ssRaw) {
        [void][System.Reflection.Assembly]::LoadWithPartialName('System.Web.Extensions')
        $ssJss = [Activator]::CreateInstance([System.Web.Script.Serialization.JavaScriptSerializer])
        $ssJss.MaxJsonLength = 16MB
        $ssEvent = $ssJss.DeserializeObject($ssRaw)
        if ($ssEvent) {
            $ssSid   = $null; $ssModel = $null; $ssCwd = $null
            try { $ssSid   = [string]$ssEvent.session_id } catch {}
            try { $ssModel = [string]$ssEvent.model }      catch {}
            try { $ssCwd   = [string]$ssEvent.cwd }        catch {}
            if (-not [string]::IsNullOrWhiteSpace($ssSid)) {
                # Lazily dot-source the lib for Resolve-ModelTier /
                # Get-SessionInitiative / ConvertTo-TierJsonString. A broken lib
                # throws here and the whole block fails open (no sidecar).
                $ssScriptDir = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path)
                . ([System.IO.Path]::Combine($ssScriptDir, 'user-prompt-lib.ps1'))
                $ssTier = Resolve-ModelTier -Model $ssModel -ConfigPath ([System.IO.Path]::Combine($ssScriptDir, 'model-tiers.json'))
                if ([string]::IsNullOrWhiteSpace($ssTier)) { $ssTier = 'frontier' }
                $ssInit = $null
                if (-not [string]::IsNullOrWhiteSpace($ssCwd)) {
                    try { $ssInit = Get-SessionInitiative -Cwd $ssCwd } catch { $ssInit = $null }
                }
                $ssTierDir = [System.IO.Path]::Combine($env:USERPROFILE, '.mem0', 'session-tier')
                if (-not [System.IO.Directory]::Exists($ssTierDir)) { [void][System.IO.Directory]::CreateDirectory($ssTierDir) }
                $ssSidecar = [System.IO.Path]::Combine($ssTierDir, $ssSid + '.json')
                $ssPayload = '{"model":' + (ConvertTo-TierJsonString $ssModel) +
                             ',"tier":"' + $ssTier + '"' +
                             ',"initiative":' + (ConvertTo-TierJsonString $ssInit) +
                             ',"ts":"' + [System.DateTime]::Now.ToString('o') + '"' +
                             ',"source":"sessionstart"}'
                # Last-writer-wins: startup/resume/compact all fire SessionStart.
                [System.IO.File]::WriteAllText($ssSidecar, $ssPayload)
            }
        }
    }
} catch {}

# v0.20 Final (adversarial-review HIGH): exe self-heal. settings.json registers
# UserPromptSubmit at the bare exe path with no fallback command — on a DR
# restore / cross-PC sync that copies settings.json but not the build artifact,
# every prompt silently loses checkpoint + memory injection. SessionStart is
# off the prompt hot path, so rebuild here: if the exe is missing but the .cs
# and the smoke-gated builder are deployed beside this script, run the builder
# (it compiles with the always-present framework csc and smoke-gates before
# install; a failed heal installs nothing and stays silent by design).
$dir = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path)
$exe = Join-Path $dir 'mem0-hook-client.exe'
$cs  = Join-Path $dir 'mem0-hook-client.cs'
$bld = Join-Path $dir 'build-hook-client.ps1'
if (-not (Test-Path $exe) -and (Test-Path $cs) -and (Test-Path $bld)) {
    try { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $bld } catch {}
}

$present = $false
try {
    foreach ($p in [System.IO.Directory]::EnumerateFiles('\\.\pipe\')) {
        if ($p -eq ('\\.\pipe\' + $pipeName)) { $present = $true; break }
    }
} catch { $present = $false }   # enumeration broken -> spawn anyway (mutex no-ops duplicates)

if (-not $present) {
    $daemon = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path) + '\mem0-hook-daemon.ps1'
    if ([System.IO.File]::Exists($daemon)) {
        try {
            # UseShellExecute=$true: no inherited std handles — with $false the
            # daemon would hold this hook's stdout open and Claude Code's
            # SessionStart hook read would wait on it (see lib
            # Start-HookDaemonDetached, same fix).
            $psi = [System.Diagnostics.ProcessStartInfo]::new()
            $psi.FileName = $env:SystemRoot + '\System32\WindowsPowerShell\v1.0\powershell.exe'
            $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $daemon + '"'
            $psi.UseShellExecute = $true
            $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
            $proc = [System.Diagnostics.Process]::Start($psi)
            if ($proc) { $proc.Dispose() }
        } catch {}
    }
}
exit 0
