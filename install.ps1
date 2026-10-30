# install.ps1 - top-level orchestrator
# Runs all 4 install phases. Idempotent: safe to re-run.
#
# Usage from a fresh PowerShell session:
#   cd $env:USERPROFILE\agentic-memory-stack
#   .\install.ps1
#
# Or non-interactive (skip prompts, log to file):
#   .\install.ps1 -NonInteractive -LogFile install.log
#
# Switch this box's embedding profile (a replica restoring sets made in another space; see docs/MIGRATION.md):
#   .\install.ps1 -Role replica -EmbedProfile egemma2
#
# A replica's first install needs the authority's URL and its API key (a file holding the key alone):
#   .\install.ps1 -Role replica -AuthorityUrl http://<authority-host>:18791 -ApiKeyFile <path to the key file>

param(
    [switch]$NonInteractive,
    [string]$LogFile = '',
    # v1.0 Phase 7A: operator-agnostic install. The WSL distro is auto-detected
    # (default distro from `wsl -l -q`) but can be overridden for multi-distro boxes.
    [string]$Distro = '',
    # v1.16 one-brain role gate: 'brain' (default) = this box is the memory write
    # authority and runs the nightly dream/dedup scheduled tasks; 'replica' = a
    # read-replica box where those canonical-mutation tasks must never run (and
    # any previously-registered ones are removed). 'brain' is the default of a FIRST
    # install only: an omitted -Role keeps the role the box recorded (install/role-lib.ps1),
    # so re-running install.ps1 on a replica leaves it a replica. A record that exists but cannot
    # be used (an empty or garbled role file, a receipt that will not parse) stops the run and asks
    # for an explicit -Role: it is never read as "nothing recorded".
    [ValidateSet('brain','replica')][string]$Role = 'brain',
    # v1.23 P2-5: forwarded to 2-windows-config.ps1. Empty = inherit what is on the box
    # (a plain re-run never re-points a replica); a replica needs its brain's URL once.
    [string]$AuthorityUrl = '',
    # v1.23 P2-8: the brain's ssh alias (as WSL knows it) for canonize forwarding on a replica.
    [string]$AuthoritySsh = '',
    # 1.35.1: switch the embedding profile this box records in ~/.mem0/stack.env (embedder_profile.py names
    # them). Omitted, the recorded profile is kept: a re-run never changes it. It reaches the WSL phase as
    # MEM0_SET_EMBED_PROFILE (install/1-wsl-services.sh validates the name, refuses either role when stack.env
    # pins the memories or episodes collection to another than the new profile's own, and refuses a brain whose
    # new space holds no points or whose alias is not served). A replica then restores a set made in the new
    # space; its local llama-swap must serve that profile's alias, and the offline snapshot cache must hold such
    # a set (a note printed after the WSL phase says how).
    # Case-sensitive: profile names are lower-case.
    [ValidatePattern('^[a-z0-9][a-z0-9-]*$', Options = 'None')][string]$EmbedProfile = '',
    # 1.35.1: whether this box's embedding alias is served WITH the media projector (on) or text-only (off: a
    # replica on a small card; the same text vectors, media memories are added and searched on the authority).
    # Recorded in ~/.mem0/stack.env as MEM0_MEDIA_EMBEDDER (MEM0_SET_MEDIA_EMBEDDER in the WSL phase); omitted,
    # the recorded value is kept. Case-sensitive like -EmbedProfile: ValidateSet ignores case and does not
    # normalise, so 'ON' would reach the WSL phase, which accepts on|off only.
    [ValidateSet('', 'on', 'off', IgnoreCase = $false)][string]$MediaEmbedder = '',
    # A replica authenticates to the authority with the authority's API key. Without this, phase 1 generates a
    # random local key that the authority rejects (HTTP 401 on every call). A file holding the key alone, e.g. the
    # output of `systemd-creds --user decrypt --name=ams-api-key <secrets-dir>/ams-api-key.cred -` on a native
    # authority, or the authority's ~/.mem0/api-key. It is copied to ~/.mem0/api-key in the WSL distro (mode 600)
    # before phase 1, replacing the key there. Replica role only; omitted, the key on the box is kept.
    [string]$ApiKeyFile = '',
    # Forwarded to install/2-windows-config.ps1 only when given (omitted, the box keeps what it recorded).
    # -HubHost: the MagicDNS name of the store hub; without one the store's sync hooks are not registered.
    # -BinaryPath / -BinarySums: an offline drop of ams-store.exe and the SHA256SUMS it shipped with, for a box that
    # cannot reach GitHub (phase 2 otherwise downloads the release asset of the tag in VERSION and verifies it).
    # -ReleaseRepo: the GitHub <owner>/<repo> whose releases carry that binary (a fork's).
    [string]$HubHost = '',
    [string]$BinaryPath = '',
    [string]$BinarySums = '',
    [string]$ReleaseRepo = ''
)

$ErrorActionPreference = 'Stop'
# v1.16: the install phases are pwsh-only (2-windows-config.ps1 does not even PARSE under
# Windows PowerShell 5.1 — BOM-less UTF-8 + em-dashes decode as ANSI and break quote
# tracking, yielding five cryptic parse errors). Fail loud here instead, where 5.1 parses.
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw "This installer requires PowerShell 7+ (pwsh). You are on $($PSVersionTable.PSVersion). Run: pwsh -File install.ps1"
}
# File parameters are resolved against the caller's directory, before this script moves to the repo root.
foreach ($fp in 'ApiKeyFile', 'BinaryPath', 'BinarySums') {
    $fv = Get-Variable -Name $fp -ValueOnly
    if ($fv) {
        if (-not (Test-Path -LiteralPath $fv -PathType Leaf)) { throw "-$fp '$fv' is not a file." }
        Set-Variable -Name $fp -Value (Resolve-Path -LiteralPath $fv).Path
    }
}
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

# The role this run applies: an explicit -Role wins; otherwise the role this box recorded on an
# earlier install; 'brain' only when nothing is recorded. The bare default used to reach
# 2-windows-config.ps1 unconditionally and turned a replica into a brain on a plain re-run.
# A record that exists but yields no role throws HERE, before this script launches or writes anything
# (the first wsl.exe call, the transcript and phase 0 all come after), so the box is left untouched.
. (Join-Path $RepoRoot 'install\role-lib.ps1')
$RoleChoice = Resolve-InstallRole -Explicit ($PSBoundParameters.ContainsKey('Role')) -Requested $Role -ProfileDir $env:USERPROFILE
$Role = $RoleChoice.Role
if ($ApiKeyFile -and $Role -ne 'replica') {
    throw "-ApiKeyFile is for a replica, which uses the authority's key. This box is a $Role and keeps the key phase 1 generates; pass -Role replica, or drop -ApiKeyFile."
}

# v1.0 Phase 7A: resolve the WSL distro (never hardcode 'Ubuntu'). `wsl -l -q`
# emits UTF-16 — read it with the right console encoding or names arrive
# space-padded. Default = the first (default) installed distro.
if (-not $Distro) {
    $prevEnc = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::Unicode
        $Distro = (wsl.exe -l -q | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
    } finally { [Console]::OutputEncoding = $prevEnc }
}
if (-not $Distro) { throw "No WSL distro found. Install one (wsl --install -d Ubuntu) or pass -Distro <name> (see: wsl -l -q)." }

function Write-Phase {
    param([string]$Title)
    Write-Host ""
    Write-Host "=================================================================" -ForegroundColor Cyan
    Write-Host " $Title" -ForegroundColor Cyan
    Write-Host "=================================================================" -ForegroundColor Cyan
}

if ($LogFile) { Start-Transcript -Path $LogFile -Append | Out-Null }

try {
    Write-Phase "Agentic Memory Stack - Install"
    Write-Host "Repo root: $RepoRoot"
    Write-Host "Windows user: $env:USERNAME"
    Write-Host "WSL distro: $Distro"
    $wslUser = (wsl.exe -d $Distro -e whoami).Trim()
    Write-Host "WSL user: $wslUser"
    Write-Host "Memory role: $Role ($($RoleChoice.Source); brain = runs nightly dream/dedup; replica = never)"
    if ($EmbedProfile) { Write-Host "Embedding profile: switching to $EmbedProfile (a re-run without -EmbedProfile keeps the recorded one)" }
    Write-Host ""

    Write-Phase "[0/4] Prerequisites check"
    & "$RepoRoot\install\0-prereqs.ps1" -Distro $Distro
    if ($LASTEXITCODE -ne 0) { throw "Prerequisites check failed - resolve issues above and re-run." }

    Write-Phase "[1/4] WSL services (mem0, Qdrant, l10-audit, llama-swap)"
    if ($ApiKeyFile) {
        # Written before the WSL phase, which keeps an api-key file that already exists.
        $apiKey = (Get-Content -LiteralPath $ApiKeyFile -Raw).Trim()
        if (-not $apiKey -or $apiKey -match '\s') { throw "-ApiKeyFile '$ApiKeyFile' must hold the API key alone, on one line." }
        # Staged as api-key.new and checked there; the key in place is kept as api-key.prev-<epoch> and replaced
        # only once the staged copy matches, so a bad file never costs the working key.
        $apiKey | wsl.exe -d $Distro -e bash -c 'umask 077; mkdir -p ~/.mem0 && tr -d "\r" > ~/.mem0/api-key.new && chmod 600 ~/.mem0/api-key.new'
        if ($LASTEXITCODE -ne 0) { throw "Could not write ~/.mem0/api-key.new in WSL distro $Distro." }
        $keyLen = [string](wsl.exe -d $Distro -e bash -c 'tr -d "\n" < ~/.mem0/api-key.new | wc -c')
        if ($keyLen.Trim() -ne [string]$apiKey.Length) {
            wsl.exe -d $Distro -e bash -c 'rm -f ~/.mem0/api-key.new' | Out-Null
            throw "The key staged in WSL does not match -ApiKeyFile; ~/.mem0/api-key is unchanged."
        }
        wsl.exe -d $Distro -e bash -c 'if [ -f ~/.mem0/api-key ] && ! cmp -s ~/.mem0/api-key ~/.mem0/api-key.new; then cp -p ~/.mem0/api-key ~/.mem0/api-key.prev-$(date +%s); fi; mv -f ~/.mem0/api-key.new ~/.mem0/api-key'
        if ($LASTEXITCODE -ne 0) { throw "Could not move the staged key into ~/.mem0/api-key in WSL distro $Distro." }
        $apiKey = $null
        Write-Host "Authority API key installed to ~/.mem0/api-key in $Distro (mode 600; a different previous key is kept as api-key.prev-<epoch>)."
    }
    # wslpath needs forward slashes (backslashes are stripped by the wsl.exe arg pass);
    # fall back to manual /mnt/<drive>/... computation.
    $rrFwd = $RepoRoot -replace '\\', '/'
    $repoWsl = (wsl.exe -d $Distro wslpath -u "$rrFwd" 2>$null)
    if ($repoWsl) { $repoWsl = ([string]$repoWsl).Trim() }
    if (-not $repoWsl) { $repoWsl = "/mnt/" + $RepoRoot.Substring(0,1).ToLower() + "/" + ($RepoRoot.Substring(3) -replace '\\', '/') }
    # v1.23 P2-5: an EXPLICIT -Role reaches the WSL phase as MEM0_ROLE (wsl.exe -e passes no
    # environment), so `install.ps1 -Role replica` also disables the brain-only units there. With
    # no -Role the WSL side keeps its inherit-never-revert rule (stack.env -> ~/.mem0/role).
    # 1.35.1: -EmbedProfile rides the same bash line as MEM0_SET_EMBED_PROFILE, only when given (the parameter
    # admits [a-z0-9-] only, so the name is safe inside the single quotes). With no -Role the WSL side still
    # inherits its role, and the switch is the only thing added.
    if ($PSBoundParameters.ContainsKey('Role')) {
        wsl.exe -d $Distro -e bash -c "MEM0_ROLE='$Role' $(if ($EmbedProfile) { "MEM0_SET_EMBED_PROFILE='$EmbedProfile' " })$(if ($MediaEmbedder) { "MEM0_SET_MEDIA_EMBEDDER='$MediaEmbedder' " })exec bash '$repoWsl/install/1-wsl-services.sh' '$wslUser' '$env:USERNAME' '$Distro'"
    } elseif ($EmbedProfile -or $MediaEmbedder) {
        wsl.exe -d $Distro -e bash -c "$(if ($EmbedProfile) { "MEM0_SET_EMBED_PROFILE='$EmbedProfile' " })$(if ($MediaEmbedder) { "MEM0_SET_MEDIA_EMBEDDER='$MediaEmbedder' " })exec bash '$repoWsl/install/1-wsl-services.sh' '$wslUser' '$env:USERNAME' '$Distro'"
    } else {
        wsl.exe -d $Distro -e bash "$repoWsl/install/1-wsl-services.sh" "$wslUser" "$env:USERNAME" "$Distro"
    }
    if ($LASTEXITCODE -ne 0) { throw "WSL services install failed." }
    # A switch the WSL phase made clears the offline watcher's replica-restored.txt marker (install/1-wsl-services.sh
    # does it, where it knows a switch happened, so the in-WSL form of the command clears it too). The restore that
    # forces runs at go_offline, when the brain is unreachable, and travel-mode.ps1 seeds its local snapshot cache
    # from pCloud only while the brain answers: it restores what the cache already holds. So the cache needs a set
    # made in the new space BEFORE the trip, and only the operator, online, can put it there (`on -DryRun` seeds and
    # touches nothing else). Without one the restore refuses the old-space sets loudly, which beats a store that
    # starts empty. This note is printed whether or not a switch was needed (the WSL phase says which).
    if ($EmbedProfile -and $Role -eq 'replica') {
        Write-Host ""
        Write-Host "Replica: the offline watcher restores from the local snapshot cache and cannot fetch a set once the brain is unreachable."
        Write-Host "  After the brain has written a backup set in the $EmbedProfile space, run this while online:  scripts\travel\travel-mode.ps1 on -DryRun"
        Write-Host "  It seeds the cache and changes nothing else; check that the 'snapshot:' stamp it prints is newer than the brain's switch."
        Write-Host "  The local llama-swap must also serve this profile's alias (install/llama-swap-setup.md) before a set in that space restores."
    }

    Write-Phase "[2/4] Windows config (hooks, Task Scheduler, MCP registrations, CLAUDE.md patch)"
    # These four reach phase 2 only when given; omitted, phase 2 inherits what the box recorded.
    $phase2Extra = @{}
    foreach ($fwd in 'HubHost', 'BinaryPath', 'BinarySums', 'ReleaseRepo') {
        if ($PSBoundParameters.ContainsKey($fwd)) { $phase2Extra[$fwd] = Get-Variable -Name $fwd -ValueOnly }
    }
    & "$RepoRoot\install\2-windows-config.ps1" -WslUser $wslUser -Distro $Distro -Role $Role -AuthorityUrl $AuthorityUrl -AuthoritySsh $AuthoritySsh @phase2Extra
    if ($LASTEXITCODE -ne 0) { throw "Windows config failed." }

    Write-Phase "[3/4] Verify (end-to-end smoke test)"
    & "$RepoRoot\install\3-verify.ps1" -WslUser $wslUser -Distro $Distro
    if ($LASTEXITCODE -ne 0) {
        Write-Phase "INSTALL INCOMPLETE"
        Write-Host "The install ran, but verify failed. The failed checks are listed under 'VERIFY FAILED' above, each with its fix." -ForegroundColor Red
        Write-Host "Fix them and re-run .\install.ps1: finished steps are skipped and the recorded role and flags are kept." -ForegroundColor Red
        exit 1
    }

    Write-Phase "DONE"
    Write-Host "Restart VS Code / Claude Code to pick up new hooks + MCP servers." -ForegroundColor Green
    Write-Host "First C1 consolidation fires daily at 03:00 (Windows Task Scheduler with -WakeToRun)." -ForegroundColor Green
    Write-Host "L1a extraction fires on every Stop/PreCompact hook (10-minute throttle)." -ForegroundColor Green

} finally {
    if ($LogFile) { Stop-Transcript | Out-Null }
}
