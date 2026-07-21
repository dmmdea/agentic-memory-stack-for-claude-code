# 2-windows-config.ps1 - register Claude Code hooks, MCP servers, Task Scheduler.
# Idempotent: safe to re-run.
# pwsh-only (v1.16 review finding): this file has never parsed under Windows PowerShell 5.1
# (BOM-less UTF-8 + em-dashes in strings decode as ANSI and break quote tracking). NOTE:
# #Requires cannot make that loud here — 5.1 fails at PARSE time, before #Requires is
# evaluated — so the loud pre-flight guard lives in install.ps1 (which 5.1 does parse).
# This #Requires still documents + enforces the contract for direct standalone invocation
# from pwsh <7.
#Requires -Version 7

param(
    [Parameter(Mandatory)][string]$WslUser,
    # v1.0 Phase 7A: operator-agnostic. Resolved by the orchestrator; auto-detect if standalone.
    [string]$Distro = '',
    # v1.16 (2026-07-17 deploy-layer-skew remediation §6.3): one-brain role gate. 'brain' (default) =
    # this box is the memory write authority and runs the nightly dream-consolidate +
    # semantic-dedup scheduled tasks. 'replica' = read-replica box (one-brain rule,
    # docs/superpowers/specs/2026-07-15-offline-first-memory-design.md): consolidation and
    # dedup are canonical-mutation operations and must NEVER run here — the installer
    # skips registration AND removes any previously-registered tasks.
    [ValidateSet('brain','replica')][string]$Role = 'brain',
    # Optional (2026-07-18): WSL path of a checkout carrying eval/ harnesses (the
    # maintainer moat repo). The dream's retrieval-drift canary reads it from the
    # receipt; empty -> falls back to RepoRootWsl (a missing eval/ degrades to the
    # graceful skip, never a false alarm).
    [string]$EvalRootWsl = '',
    # 2026-07-20: the memory AUTHORITY address this box talks to. Written to the per-host file
    # ~/.mem0/authority-url (inside WSL), which the MCP shim and replay-ops read.
    #
    # WHY A FILE AND NOT AN ENV VAR: the mem0 MCP entry launches the shim as
    # `wsl.exe -d <distro> -e <python> <shim>`, which execs the binary directly — no login shell
    # (profile never sourced) and no WSLENV pass-through. A MEM0_URL set on the Windows side is
    # therefore invisible to the shim, so a replica silently fell back to loopback, found no local
    # server, and returned OfflineError/QUEUED_OFFLINE on every operation while writes piled up in
    # the outbox unnoticed. Reading the authority from disk removes that whole class of failure.
    #
    # Default is loopback, correct for -Role brain (the authority talks to itself). A
    # -Role replica MUST pass its brain's address: -AuthorityUrl http://<brain-host>:18791
    # Prefer a stable hostname over a DHCP LAN IP, which churns.
    [string]$AuthorityUrl = 'http://127.0.0.1:18791'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ClaudeDir = Join-Path $env:USERPROFILE '.claude'
$ScriptsDir = Join-Path $ClaudeDir 'scripts'
$LogsDir = Join-Path $ClaudeDir 'logs'
$StateDir = Join-Path $ClaudeDir 'state'

if (-not $Distro) {
    $prevEnc = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::Unicode
        $Distro = (wsl.exe -l -q | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
    } finally { [Console]::OutputEncoding = $prevEnc }
}
if (-not $Distro) { throw "No WSL distro found. Pass -Distro <name> (see: wsl -l -q)." }

# v1.0 Phase 7A: the four operator-specific dimensions, resolved once. Deployed
# scripts that carry sentinels get them substituted (see the deploy loops below);
# R9-tracked scripts that need the operator-chosen REPO path read the receipt
# written at the end of this script.
$WinUser     = $env:USERNAME
$RepoRootWin = $RepoRoot
# wslpath needs forward slashes — backslashes are stripped by the wsl.exe arg pass.
# Fall back to manual /mnt/<drive>/... if wslpath yields nothing.
$rrFwd = $RepoRoot -replace '\\', '/'
$RepoRootWsl = (wsl.exe -d $Distro wslpath -u "$rrFwd" 2>$null)
if ($RepoRootWsl) { $RepoRootWsl = ([string]$RepoRootWsl).Trim() }
if (-not $RepoRootWsl) { $RepoRootWsl = "/mnt/" + $RepoRoot.Substring(0,1).ToLower() + "/" + ($RepoRoot.Substring(3) -replace '\\', '/') }

# Substitute the bounded sentinels (+ legacy raw handles, transitional) into a
# deployed script's text. Sentinels are the PII-free tokens shipped in the repo;
# on this box they resolve to the install values (a no-op on a box whose values
# already equal the tokens). R9 (Test-MemoryStack) normalizes the same sentinels.
function Resolve-StackTokens {
    param([string]$Text)
    # Literal .Replace (NOT -replace): the values are paths/usernames/distros that
    # may contain regex/$-replacement metacharacters; literal replacement is
    # byte-for-byte and matches the R9 normalizer's .Replace() (audit: keep symmetric).
    $Text = $Text.Replace('__WSL_USER__',   $WslUser)
    $Text = $Text.Replace('__WIN_USER__',   $WinUser)
    $Text = $Text.Replace('__WSL_DISTRO__', $Distro)
    return $Text
}

# v1.0 Phase 7A: write UTF-8 *without* BOM on BOTH PowerShell 5.1 and 7. PS 5.1
# `Set-Content -Encoding UTF8` emits a BOM, which (a) breaks bash files the WSL
# side sources (BOM before the shebang) and (b) breaks R9 repo-vs-deployed hash
# parity (repo files are no-BOM). WriteAllText preserves the string's existing
# line endings, so no CRLF/LF churn.
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-StackFile {
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, $script:Utf8NoBom)
}

function Backup-File {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        $ts = Get-Date -Format 'yyyyMMddHHmmss'
        $bak = "$Path.bak-$ts-pre-ams-install"
        Copy-Item -LiteralPath $Path -Destination $bak
        Write-Host "    backed up to $bak"
    }
}

# ----------------------------------------------------------------------
# 1. Place runtime scripts
# ----------------------------------------------------------------------
Write-Host "==> [1] Installing runtime scripts to $ScriptsDir"
foreach ($d in @($ClaudeDir, $ScriptsDir, $LogsDir, $StateDir)) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# ----------------------------------------------------------------------
# 1a. Write the operator receipt (v1.0 Phase 7A)
# ----------------------------------------------------------------------
# The single source of truth for the four operator-specific dimensions. R9-tracked
# deployed scripts (Test-MemoryStack.ps1, dream-consolidate.ps1) read this at
# runtime to resolve the operator-chosen repo location + distro + users WITHOUT
# hardcoding any developer path/handle. Written PII-free + BOM-free.
$receiptPath = Join-Path $ScriptsDir 'mem0-stack.config.psd1'
# Escape single-quotes in EVERY interpolated value (psd1 single-quoted strings),
# not just the repo path — defensive against any path/name containing a quote.
$eWslUser = $WslUser.Replace("'", "''")
$eWinUser = $WinUser.Replace("'", "''")
$eDistro  = $Distro.Replace("'", "''")
$eRole    = $Role.Replace("'", "''")
$eRepoWin = $RepoRootWin.Replace("'", "''")
$eRepoWsl = $RepoRootWsl.Replace("'", "''")
$eEvalWsl = $EvalRootWsl.Replace("'", "''")
$AuthorityUrl = $AuthorityUrl.Trim().TrimEnd('/')
$eAuthorityUrl = $AuthorityUrl.Replace("'", "''")
$receipt = @"
@{
    WslUser     = '$eWslUser'
    WinUser     = '$eWinUser'
    Distro      = '$eDistro'
    # v1.16: one-brain role. 'brain' = write authority (dream/dedup tasks registered);
    # 'replica' = read-replica (nightly canonical-mutation tasks forbidden here).
    Role        = '$eRole'
    RepoRootWin = '$eRepoWin'
    RepoRootWsl = '$eRepoWsl'
    # Optional: checkout carrying eval/ (drift canaries). Empty -> RepoRootWsl fallback.
    EvalRootWsl = '$eEvalWsl'
    ApiKeyUnc   = '\\wsl.localhost\$eDistro\home\$eWslUser\.mem0\api-key'
    # 2026-07-20: memory authority this box talks to (mirrored into ~/.mem0/authority-url,
    # which is what the shim actually reads). Loopback on the brain; the brain's address
    # on a replica. Recorded here so verify/diagnostics can report it without guessing.
    AuthorityUrl = '$eAuthorityUrl'
    # 4C autonomous-canonical-promotion gate (E/T4): off | shadow | enforce.
    # Ships 'shadow' (compute + log, never blocks). Flip to 'enforce' only after the
    # contradiction judge is calibrated (eval/promotion-gate/CALIBRATION.md). Reversible.
    PromotionGateMode = 'shadow'
}
"@
Write-StackFile $receiptPath $receipt
Write-Host "    receipt written: $receiptPath (WslUser=$WslUser WinUser=$WinUser Distro=$Distro Role=$Role)"

# --- per-host memory authority (2026-07-20) ------------------------------------------------
# The shim + replay-ops read ~/.mem0/authority-url INSIDE WSL (env MEM0_URL still wins if set).
# Written unconditionally so every box states its authority explicitly instead of relying on a
# default — on the brain that is loopback, on a replica it is the brain's address. Idempotent.
# Fail-soft: a bad write must not abort an otherwise-good install (verify reports it instead).
if ($AuthorityUrl -notmatch '^https?://') {
    Write-Host "    WARN: -AuthorityUrl '$AuthorityUrl' is not an http(s) URL; skipping authority-url write" -ForegroundColor Yellow
} else {
    try {
        # Single-quoted inside bash so nothing in the URL is expanded; the value is validated above.
        $authCmd = "mkdir -p ~/.mem0 && printf '%s\n' '$AuthorityUrl' > ~/.mem0/authority-url && chmod 600 ~/.mem0/authority-url"
        wsl.exe -d $Distro -e bash -lc $authCmd 2>&1 | Out-Null
        $authBack = (wsl.exe -d $Distro -e bash -lc 'cat ~/.mem0/authority-url 2>/dev/null' 2>$null | Where-Object { "$_".Trim() } | Select-Object -First 1)
        if ("$authBack".Trim() -eq $AuthorityUrl) {
            Write-Host "    memory authority: $AuthorityUrl (~/.mem0/authority-url)"
        } else {
            Write-Host "    WARN: authority-url readback mismatch (wrote '$AuthorityUrl', read '$authBack')" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "    WARN: could not write ~/.mem0/authority-url: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# H12: added user-prompt-extract.ps1 and pre-tool-check.ps1 for v0.17 Phase 0 hooks
# v0.19 M13: added user-prompt-lib.ps1 — user-prompt-extract.ps1 dot-sources it;
# deploying the extractor without the lib silently disables Phase 0.B/0.D.
# v0.20 Final (adversarial-review HIGH, A.5/A.6 parity): added the resident
# daemon (mem0-hook-daemon.ps1), its SessionStart launcher
# (mem0-hook-daemon-spawn.ps1), the compiled-client source
# (mem0-hook-client.cs), the smoke-gated builder (build-hook-client.ps1 — also
# enables the SessionStart exe self-heal on a repo-less restore), and
# Test-MemoryStack.ps1 — every file Test-MemoryStack R9 hash-tracks must be
# deployed here or a clean install self-reports DEGRADED on day one
# (InstallerParity.Tests.ps1 pins $winScripts ⊇ R9 $hookNames).
# v0.27.1 R5: added codex-shim.ps1 (the Windows-resident Codex HTTP shim that lets
# WSL-side python — the mem0 write-gate + contradiction-sweep — reach Codex over
# loopback HTTP) and its flag-gated SessionStart launcher codex-shim-spawn.ps1. Both
# are R9 hash-tracked (Test-MemoryStack $hookNames), so they MUST be deployed here.
# Step 3: added dream-catchup.ps1 (debt-based missed-run catch-up, SessionStart-spawned) and
# memory-index-refresh.ps1 (standalone MEMORY.md index refresh decoupled from the dream) — both
# spawn detached from SessionStart. Not R9 hash-tracked (they are not hot-path clients), so this
# stays a superset of $hookNames (InstallerParity holds).
$winScripts = @('memory-common.ps1', 'l1a-extract.ps1', 'dream-consolidate.ps1', 'dream-catchup.ps1', 'memory-index-refresh.ps1', 'memory-maintenance-spawn.ps1', 'autopromote-lib.ps1', 'stop-extract.ps1', 'sessionstart-capture.ps1', 'user-prompt-extract.ps1', 'user-prompt-lib.ps1', 'pre-tool-check.ps1', 'mem0-hook-daemon.ps1', 'mem0-hook-daemon-spawn.ps1', 'mem0-hook-client.cs', 'build-hook-client.ps1', 'Test-MemoryStack.ps1', 'codex-shim.ps1', 'codex-shim-spawn.ps1')
foreach ($s in $winScripts) {
    $src = Join-Path $RepoRoot "scripts\windows\$s"
    $dst = Join-Path $ScriptsDir $s
    if (-not (Test-Path -LiteralPath $src)) { Write-Host "    WARN: $src not found" -ForegroundColor Yellow; continue }
    Copy-Item -LiteralPath $src -Destination $dst -Force
    # v1.0 Phase 7A: resolve operator sentinels (+ legacy raw handle). Byte-identical
    # R9 scripts carry NO token -> the conditional skips them -> bytes preserved.
    $raw = Get-Content -LiteralPath $dst -Raw
    if ($raw -match '__WSL_USER__|__WIN_USER__|__WSL_DISTRO__') {
        Write-StackFile $dst (Resolve-StackTokens $raw)
    }
    Write-Host "    installed: $s"
}

# v0.21 Phase C (review L-hygiene): storage-cap-check.sh dropped from
# $wslScripts — the scripts/wsl/ copy was RETIRED in v0.14 (divergent v0.12
# dead code; the canonical SessionStart storage-caps script is
# claude-config/storage-cap-check.sh). Listing it here deployed nothing (WARN:
# not found) yet still registered a SessionStart hook pointing at the missing
# deployed file. It is now deployed from claude-config\ in the dedicated block
# below so the SessionStart cap-check stays wired (matching the live box).
# replay-ops.py rides along (2026-07-20): the shim drains the outbox at startup by spawning its
# SIBLING replay-ops.py, so the drainer must live in the same deployed directory as the shim.
$wslScripts = @('mem0-mcp-shim.py', 'l10-audit.py', 'replay-ops.py')
foreach ($s in $wslScripts) {
    $src = Join-Path $RepoRoot "scripts\wsl\$s"
    $dst = Join-Path $ScriptsDir $s
    if (-not (Test-Path -LiteralPath $src)) { Write-Host "    WARN: $src not found" -ForegroundColor Yellow; continue }
    Copy-Item -LiteralPath $src -Destination $dst -Force
    # v1.0 Phase 7A: resolve operator sentinels (mem0 tenant + UNC paths) + legacy handle.
    $raw = Get-Content -LiteralPath $dst -Raw
    if ($raw -match '__WSL_USER__|__WIN_USER__|__WSL_DISTRO__') {
        Write-StackFile $dst (Resolve-StackTokens $raw)
    }
    Write-Host "    installed: $s"
}

# Canonical SessionStart storage-caps script (claude-config/, NOT the retired
# scripts/wsl/ copy). Deployed beside the others so $bashCapCheck below resolves.
$capCheckSrc = Join-Path $RepoRoot 'claude-config\storage-cap-check.sh'
$capCheckDst = Join-Path $ScriptsDir 'storage-cap-check.sh'
if (Test-Path -LiteralPath $capCheckSrc) {
    Copy-Item -LiteralPath $capCheckSrc -Destination $capCheckDst -Force
    # v1.0 Phase 7A: bash file — BOM-safe write is mandatory (a BOM before the shebang breaks it).
    $raw = Get-Content -LiteralPath $capCheckDst -Raw
    if ($raw -match '__WSL_USER__|__WIN_USER__|__WSL_DISTRO__') {
        Write-StackFile $capCheckDst (Resolve-StackTokens $raw)
    }
    Write-Host "    installed: storage-cap-check.sh (from claude-config)"
} else {
    Write-Host "    WARN: $capCheckSrc not found" -ForegroundColor Yellow
}

# B1 (2026-06-28): SessionStart durable/evidence bundle enrichment helper, invoked by
# storage-cap-check.sh as $SDIR_B1/sessionstart_bundle.py. MUST be deployed BESIDE that script
# or the B1 enrichment silently no-ops. No sentinel substitution (the .py reads $HOME at runtime,
# stdlib-only) so the deployed copy stays byte-identical to the repo copy.
$ssBundleSrc = Join-Path $RepoRoot 'claude-config\sessionstart_bundle.py'
$ssBundleDst = Join-Path $ScriptsDir 'sessionstart_bundle.py'
if (Test-Path -LiteralPath $ssBundleSrc) {
    Copy-Item -LiteralPath $ssBundleSrc -Destination $ssBundleDst -Force
    Write-Host "    installed: sessionstart_bundle.py (from claude-config)"
} else {
    Write-Host "    WARN: $ssBundleSrc not found" -ForegroundColor Yellow
}

# B1 Phase 2 (2026-06-28): PreCompact conversation-query capture helper (WSL python). Tails the
# transcript at PreCompact and stashes a redacted query marker the post-compact SessionStart helper
# consumes. Deployed beside the others; stdlib-only, no sentinel substitution.
$pcCaptureSrc = Join-Path $RepoRoot 'claude-config\precompact_capture.py'
$pcCaptureDst = Join-Path $ScriptsDir 'precompact_capture.py'
if (Test-Path -LiteralPath $pcCaptureSrc) {
    Copy-Item -LiteralPath $pcCaptureSrc -Destination $pcCaptureDst -Force
    Write-Host "    installed: precompact_capture.py (from claude-config)"
} else {
    Write-Host "    WARN: $pcCaptureSrc not found" -ForegroundColor Yellow
}

# v0.22 Pillar 2 (D4): model-tier policy read at runtime by the hook lib
# (Resolve-ModelTier / Get-SessionTier) and the SessionStart spawn launcher.
# Deployed beside the lib in ScriptsDir so $PSScriptRoot\model-tiers.json
# resolves. No sentinel substitution (the JSON has no path/tenant tokens) so the
# deployed copy stays byte-identical to the repo copy — R9 SHA256-tracks it.
$modelTiersSrc = Join-Path $RepoRoot 'claude-config\model-tiers.json'
$modelTiersDst = Join-Path $ScriptsDir 'model-tiers.json'
if (Test-Path -LiteralPath $modelTiersSrc) {
    Copy-Item -LiteralPath $modelTiersSrc -Destination $modelTiersDst -Force
    Write-Host "    installed: model-tiers.json (from claude-config)"
} else {
    Write-Host "    WARN: $modelTiersSrc not found" -ForegroundColor Yellow
}

# v1.0 Phase 7B: operator brand-routing rules. DEPLOY-IF-MISSING so an operator's
# customized brand map survives re-installs (the shipped default is neutral —
# ai-ecosystem only; operators add their own projects). Read by the hook lib's
# Get-StackBrandRules + storage-cap-check.sh.
# Operator-neutral-at-rest: claude-config/brands.json is GITIGNORED (it is the
# operator's private project map). A fresh clone therefore has only the tracked
# template brands.example.json — deploy that when no local brands.json exists.
$brandsSrc = Join-Path $RepoRoot 'claude-config\brands.json'
if (-not (Test-Path -LiteralPath $brandsSrc)) {
    $brandsSrc = Join-Path $RepoRoot 'claude-config\brands.example.json'
}
$brandsDst = Join-Path $ScriptsDir 'brands.json'
if (-not (Test-Path -LiteralPath $brandsDst)) {
    if (Test-Path -LiteralPath $brandsSrc) {
        Copy-Item -LiteralPath $brandsSrc -Destination $brandsDst -Force
        Write-Host "    installed: brands.json (from $(Split-Path -Leaf $brandsSrc) — edit ~/.claude/scripts/brands.json or keep a local claude-config/brands.json to add your projects)"
    } else {
        Write-Host "    WARN: $brandsSrc not found" -ForegroundColor Yellow
    }
} else {
    Write-Host "    brands.json present — keeping your customized brand rules"
}

# ----------------------------------------------------------------------
# 1b. Build + install the compiled UserPromptSubmit client (v0.20 A.6)
# ----------------------------------------------------------------------
# v0.20 Final (adversarial-review HIGH): the production UserPromptSubmit
# registration is the compiled exe, not the PS wrapper. build-hook-client.ps1
# refreshes the deployed .cs, compiles with the always-present framework csc,
# SMOKE-GATES the candidate, and installs it. A failed build aborts the install
# BEFORE hook registration so settings.json never points at a missing exe.
Write-Host "==> [1b] Building mem0-hook-client.exe (smoke-gated)"
& (Join-Path $RepoRoot 'scripts\windows\build-hook-client.ps1')
if ($LASTEXITCODE -ne 0) {
    Write-Host "FATAL: build-hook-client.ps1 failed (exit $LASTEXITCODE) - aborting before hook registration; settings.json untouched" -ForegroundColor Red
    exit 1
}

# ----------------------------------------------------------------------
# 2. Patch ~/.claude/settings.json with hooks
# ----------------------------------------------------------------------
Write-Host "==> [2] Registering hooks in settings.json"
$settingsPath = Join-Path $ClaudeDir 'settings.json'
Backup-File $settingsPath

$settings = if (Test-Path -LiteralPath $settingsPath) {
    Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
} else {
    [ordered]@{}
}

# Add hooks property if missing - using a hashtable so PS can mutate it
$hooks = if ($settings.PSObject.Properties['hooks']) { $settings.hooks } else { New-Object PSCustomObject }

# 2026-06-25: prefer PowerShell 7 (pwsh) for ALL stack invocations (hooks + the nightly dream task).
# Windows PowerShell 5.1 has quirks that silently broke the dream under Task Scheduler — empty
# $LASTEXITCODE across the wsl.exe boundary, lost codex token counts, and the autopromote phase
# dying mid-loop on a gate BLOCK (verified 2026-06-25: identical run completes end-to-end under
# pwsh). pwsh 7 runs the same 5.1-clean scripts correctly. Resolve a concrete pwsh path; fall back
# to powershell.exe ONLY if pwsh is genuinely absent. $psRunner = bare path (for -Execute);
# $psQuoted = path quoted for embedding in a hook command STRING.
$psRunner = 'powershell.exe'
foreach ($cand in @(
    (Get-Command pwsh.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source),
    (Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'),
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe')
)) { if ($cand -and (Test-Path $cand)) { $psRunner = $cand; break } }
$psQuoted = if ($psRunner -match '\s') { '"' + $psRunner + '"' } else { $psRunner }
Write-Host "    PowerShell runner (hooks + dream): $psRunner"

$psDispatcher = $psQuoted + ' -NoProfile -ExecutionPolicy Bypass -File C:\Users\' + $env:USERNAME + '\.claude\scripts\stop-extract.ps1'
# v1.16 (2026-07-17 remediation §6.1.5): emit DISTRO-AGNOSTIC hook commands (no `-d`)
# when the AMS distro IS the box's WSL default — `wsl.exe` with no `-d` reaches it, and the
# resulting settings.json stays portable across machines whose default distros differ (a
# shared/synced settings.json with a hardcoded `-d <distro>` breaks every other box).
# Only a NON-default AMS distro needs the explicit `-d` for correctness. The probe asks the
# default distro its own name — the exact semantic "what does the no-`-d` form reach?".
# Non-login bash (-c, not -lc): WSL_DISTRO_NAME is process env so no profile is needed, and a
# profile that prints to stdout can't pollute the probe. Take the LAST non-empty line for the
# same reason (v1.16 review finding 4).
$defaultDistro = ''
try { $defaultDistro = ((wsl.exe -e bash -c 'echo $WSL_DISTRO_NAME' 2>$null | Where-Object { "$_".Trim() } | Select-Object -Last 1) -as [string]).Trim() } catch {}
$wslDistroArg = if ($defaultDistro -eq $Distro) { '' } else { '-d ' + $Distro + ' ' }
if ($wslDistroArg) { Write-Host "    NOTE: $Distro is not the WSL default ($defaultDistro) - hook commands carry -d $Distro (settings.json becomes machine-specific)" -ForegroundColor Yellow }
else { Write-Host "    hook commands emitted distro-agnostic (no -d; $Distro is the WSL default)" }
# Use wsl.exe to invoke the bash script - Git Bash on Windows can't find /mnt/c paths
# from this command form, so calling `bash C:/...` exits 127. The wsl.exe form works.
# (Audit finding 2026-06-08: SessionStart hook silently failed before this fix.)
$bashCapCheck = 'wsl.exe ' + $wslDistroArg + '-e bash -lc "bash /mnt/c/Users/' + $env:USERNAME + '/.claude/scripts/storage-cap-check.sh"'
# B1 Phase 2 (2026-06-28): PreCompact conversation-query capture — a WSL python hook that tails the
# transcript and stashes a redacted query marker the post-compact SessionStart helper consumes.
# v1.16 FAIL-OPEN (2026-07-17 remediation §6.2.1 — the root-cause fix for the compaction
# deadlock class): Claude Code treats a PreCompact hook exit code 2 as a HARD BLOCK on compaction,
# and python3 exits 2 when the capture script is missing (deploy-layer skew wiped it on the
# brain box 2026-07-17 → every long session deadlocked at "Prompt is too long"). Capture is best-effort by
# contract (same as h13-postcompact.js: "never blocks, always exit 0") — `|| true` makes bash
# exit 0 no matter what python3 does, so a missing/erroring capture script can never again block
# compaction. A failure of wsl.exe itself returns non-2 codes, which PreCompact treats as
# non-blocking.
$bashPreCompactCapture = 'wsl.exe ' + $wslDistroArg + '-e bash -lc "python3 /mnt/c/Users/' + $env:USERNAME + '/.claude/scripts/precompact_capture.py || true"'

# H12: v0.17 Phase 0 hooks — UserPromptSubmit (checkpoint + decision-capture + proactive-search)
# and PreToolUse (audit gate). Previously only registered in the operator's local settings.json;
# now registered idempotently by the installer so fresh installs also get Phase 0.
# v0.20 Final (adversarial-review HIGH): UserPromptSubmit now registers the
# COMPILED client built in section 1b (A.6 production shape — the PS wrapper is
# the rollback, not the registration), and SessionStart additionally registers
# the daemon-spawn launcher (A.5) so the resident daemon is warm before the
# first prompt. The UserPromptSubmit dedupe markers match BOTH the legacy
# wrapper shape (user-prompt-extract.ps1) and the exe shape (mem0-hook-client)
# so a re-run over a live exe-registered box replaces rather than appends —
# the global `allowed`-style single-marker logic previously APPENDED a second
# UserPromptSubmit hook on re-run.
$psUserPrompt   = 'C:\Users\' + $env:USERNAME + '\.claude\scripts\mem0-hook-client.exe'
$psPreToolUse   = $psQuoted + ' -NoProfile -ExecutionPolicy Bypass -File C:\Users\' + $env:USERNAME + '\.claude\scripts\pre-tool-check.ps1'
$psDaemonSpawn  = $psQuoted + ' -NoProfile -ExecutionPolicy Bypass -File C:\Users\' + $env:USERNAME + '\.claude\scripts\mem0-hook-daemon-spawn.ps1'
# v0.27.1 R5: the Codex HTTP shim's SessionStart launcher. Flag-gated (no-op unless
# ~/.claude/state/codex-shim.enabled exists), so registering it costs nothing until
# the NLI write-gate is turned on.
$psShimSpawn    = $psQuoted + ' -NoProfile -ExecutionPolicy Bypass -File C:\Users\' + $env:USERNAME + '\.claude\scripts\codex-shim-spawn.ps1'
# 2026-06-24: SessionStart capture of the PRIOR session's transcript. The per-turn
# Stop/UserPromptSubmit hooks do NOT fire in the Claude Code VSCode-extension / Agent-SDK
# runtime (verified via fire-marker probe), so Stop-driven capture is dead there. This
# lifecycle hook (which DOES fire) plus PreCompact carry capture instead. Async + detached.
$psSessionCapture = $psQuoted + ' -NoProfile -ExecutionPolicy Bypass -File C:\Users\' + $env:USERNAME + '\.claude\scripts\sessionstart-capture.ps1'

# Each event maps to an ARRAY of stack-owned entries (SessionStart has two).
# Every entry carries its own dedupe markers; an existing hook matching ANY
# marker of ANY entry for that event is treated as ours and replaced.
$hookEntries = @{
    'Stop'               = @(@{ markers = @('stop-extract.ps1');           command = $psDispatcher })
    'PreCompact'         = @(
        @{ markers = @('stop-extract.ps1');                                command = $psDispatcher },
        # B1 Phase 2: capture-only conversation query for the post-compact SessionStart top-up.
        @{ markers = @('precompact_capture.py');                           command = $bashPreCompactCapture }
    )
    'SessionStart'       = @(
        @{ markers = @('storage-cap-check.sh');                            command = $bashCapCheck },
        # v0.20 A.5: async daemon pre-warm (mirrors the live-box registration shape)
        @{ markers = @('mem0-hook-daemon-spawn.ps1');                      command = $psDaemonSpawn; async = $true; timeout = 10 },
        # v0.27.1 R5: async Codex-shim pre-warm (flag-gated; no-op until the write-gate is enabled)
        @{ markers = @('codex-shim-spawn.ps1');                            command = $psShimSpawn; async = $true; timeout = 10 },
        # 2026-06-24: prior-session capture (per-turn hooks dead in VSCode-ext/SDK runtime; this carries capture)
        @{ markers = @('sessionstart-capture.ps1');                        command = $psSessionCapture; async = $true; timeout = 15 }
    )
    # H12: Phase 0 hooks (v0.20 Final: exe registration + both-shape dedupe)
    'UserPromptSubmit'   = @(@{ markers = @('user-prompt-extract.ps1', 'mem0-hook-client'); command = $psUserPrompt; timeout = 5 })
    'PreToolUse'         = @(@{ markers = @('pre-tool-check.ps1');         command = $psPreToolUse; timeout = 3; matcher = 'Bash|Edit|Write|MultiEdit' })
}

# Merge our entries into existing event arrays (don't stomp other hooks).
# Audit finding 2026-06-08: previous installer replaced entire event arrays,
# silently deleting unrelated user hooks. Fix: identify our entries by command-
# substring marker, remove only those, append fresh.
foreach ($evt in $hookEntries.Keys) {
    $entries = @($hookEntries[$evt])
    $allMarkers = @($entries | ForEach-Object { $_.markers })

    # Build hook command blocks; include optional timeout/async/matcher fields (H12)
    $newBlocks = @()
    foreach ($entry in $entries) {
        $hookCmd = [ordered]@{ command = $entry.command; type = 'command' }
        if ($entry.timeout) { $hookCmd['timeout'] = $entry.timeout }
        if ($entry.async)   { $hookCmd['async']   = $true }
        $newHookBlock = @{ hooks = @($hookCmd) }
        if ($entry.matcher) { $newHookBlock['matcher'] = $entry.matcher }
        $newBlocks += $newHookBlock
    }

    if ($hooks.PSObject.Properties[$evt]) {
        # Filter existing entries: keep only those WITHOUT any of our markers
        $existing = @($hooks.$evt)
        $preserved = @()
        foreach ($e in $existing) {
            $isOurs = $false
            foreach ($h in @($e.hooks)) {
                foreach ($mk in $allMarkers) {
                    if ($h.command -like "*$mk*") { $isOurs = $true; break }
                }
                if ($isOurs) { break }
            }
            if (-not $isOurs) { $preserved += $e }
        }
        $merged = @($preserved + $newBlocks)
        $hooks.PSObject.Properties.Remove($evt)
        $hooks | Add-Member -NotePropertyName $evt -NotePropertyValue $merged -Force
        if ($preserved.Count -gt 0) {
            Write-Host "    hook: $evt  (merged with $($preserved.Count) preserved entry/entries)"
        } else {
            Write-Host "    hook: $evt"
        }
    } else {
        $hooks | Add-Member -NotePropertyName $evt -NotePropertyValue @($newBlocks) -Force
        Write-Host "    hook: $evt  (new)"
    }
}

if (-not $settings.PSObject.Properties['hooks']) {
    $settings | Add-Member -NotePropertyName 'hooks' -NotePropertyValue $hooks -Force
} else {
    $settings.hooks = $hooks
}

# Ensure ENABLE_TOOL_SEARCH=true in env (native MCP tool search)
if (-not $settings.PSObject.Properties['env']) {
    $settings | Add-Member -NotePropertyName 'env' -NotePropertyValue (@{ ENABLE_TOOL_SEARCH = 'true' }) -Force
} else {
    $settings.env | Add-Member -NotePropertyName 'ENABLE_TOOL_SEARCH' -NotePropertyValue 'true' -Force
}

$settings | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $settingsPath -Encoding UTF8
Write-Host "    settings.json updated"

# ----------------------------------------------------------------------
# 3. Patch ~/.claude.json with MCP server registrations
# ----------------------------------------------------------------------
Write-Host "==> [3] Registering MCP servers (mem0)"
$mcpConfigPath = Join-Path $env:USERPROFILE '.claude.json'
Backup-File $mcpConfigPath

# v1.0 Phase 7A: robust + fail-soft. A real-world ~/.claude.json can contain keys
# that differ only in casing (e.g. project paths 'd:/...' vs 'D:/...' — a Claude
# Code quirk). PowerShell's default (case-insensitive) ConvertFrom-Json THROWS on
# those. On PS7 parse with -AsHashtable (case-sensitive, preserves all keys); on
# 5.1 (no -AsHashtable) use the object path. Either way, never abort the whole
# install on an MCP-registration hiccup — warn and continue.
# 2026-07-20 ROOT CAUSE of the "shattered mem0 args" bug (repaired by hand twice before, and
# silently re-broken by every install run): PowerShell's `,` binds TIGHTER than `+`, so an
# unparenthesized concat inside an array literal is parsed as array concatenation, not string
# concatenation — @('-e', $py, '/mnt/c/Users/' + $env:USERNAME + '/...shim.py') yielded FIVE
# elements with the shim path split across three of them. wsl.exe then ran python against
# '/mnt/c/Users/' and the MCP server never started. Use one interpolated string (same idiom as
# the python path beside it) so there is no `+` to mis-bind.
$mem0Args = @('-d', $Distro, '-e', "/home/$WslUser/apps/mem0-server/.venv/bin/python", "/mnt/c/Users/$env:USERNAME/.claude/scripts/mem0-mcp-shim.py")
$useHashtable = $PSVersionTable.PSVersion.Major -ge 6
try {
    if ($useHashtable) {
        $mcpConfig = if (Test-Path -LiteralPath $mcpConfigPath) {
            Get-Content -LiteralPath $mcpConfigPath -Raw | ConvertFrom-Json -AsHashtable
        } else { @{} }
        if (-not $mcpConfig.ContainsKey('mcpServers') -or $null -eq $mcpConfig['mcpServers']) { $mcpConfig['mcpServers'] = @{} }
        $mcpConfig['mcpServers']['mem0'] = @{ type = 'stdio'; command = 'wsl.exe'; args = $mem0Args }
    } else {
        $mcpConfig = if (Test-Path -LiteralPath $mcpConfigPath) {
            Get-Content -LiteralPath $mcpConfigPath -Raw | ConvertFrom-Json
        } else { [PSCustomObject]@{} }
        if (-not $mcpConfig.PSObject.Properties['mcpServers']) {
            $mcpConfig | Add-Member -NotePropertyName 'mcpServers' -NotePropertyValue ([PSCustomObject]@{}) -Force
        }
        $mem0Entry = [PSCustomObject]@{ type = 'stdio'; command = 'wsl.exe'; args = $mem0Args }
        if ($mcpConfig.mcpServers.PSObject.Properties['mem0']) { $mcpConfig.mcpServers.PSObject.Properties.Remove('mem0') }
        $mcpConfig.mcpServers | Add-Member -NotePropertyName 'mem0' -NotePropertyValue $mem0Entry -Force
    }
    $mcpConfig | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $mcpConfigPath -Encoding UTF8
    Write-Host "    mem0 registered (will load on next Claude Code restart)"
} catch {
    Write-Host "    WARN: could not auto-update ~/.claude.json (mem0 MCP): $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "          Your config is unchanged (backup made). If the mem0 MCP tools are missing after restart," -ForegroundColor Yellow
    Write-Host "          add this server manually under mcpServers.mem0: command=wsl.exe args=$($mem0Args -join ' ')" -ForegroundColor Yellow
}

# ----------------------------------------------------------------------
# 4. Append CLAUDE.md memory tier protocol snippet (if not already present)
# ----------------------------------------------------------------------
Write-Host "==> [4] CLAUDE.md memory tier protocol section"
$claudeMd = Join-Path $ClaudeDir 'CLAUDE.md'
$snippetPath = Join-Path $RepoRoot 'claude-config\claude-md-memory-protocol.md'
$marker = '## Memory tier protocol (agentic-memory-stack)'

if (Test-Path -LiteralPath $claudeMd) {
    if (Select-String -LiteralPath $claudeMd -Pattern ([regex]::Escape($marker)) -Quiet) {
        Write-Host "    already present (skipping)"
    } else {
        Backup-File $claudeMd
        if (Test-Path -LiteralPath $snippetPath) {
            Add-Content -LiteralPath $claudeMd -Value "`n`n$(Get-Content -LiteralPath $snippetPath -Raw)"
            Write-Host "    appended snippet to CLAUDE.md"
        } else {
            Write-Host "    WARN: snippet not found at $snippetPath - skipped" -ForegroundColor Yellow
        }
    }
} else {
    if (Test-Path -LiteralPath $snippetPath) {
        Copy-Item -LiteralPath $snippetPath -Destination $claudeMd
        Write-Host "    CLAUDE.md created with memory protocol"
    }
}

# ----------------------------------------------------------------------
# 5. Register the 3am Task Scheduler entry for C1 consolidator
# ----------------------------------------------------------------------
# v1.16 one-brain role gate (2026-07-17 remediation §6.3): dream-consolidate and
# semantic-dedup are canonical-mutation / write-authority operations. Registering them
# unconditionally on every box let a read-replica run a destructive nightly dedup against
# the one shared brain (no cross-machine lock exists — dedup.lock is per-machine). On a
# 'replica' box: skip registration AND remove any tasks a pre-v1.16 install left behind.
$taskName = 'ClaudeCode-DreamConsolidator-3am'
$dedupTaskName = 'ClaudeCode-SemanticDedup-430am'
if ($Role -ne 'brain') {
    Write-Host "==> [5/5b] Role=replica: dream/dedup scheduled tasks NOT registered (one-brain rule)"
    foreach ($t in @($taskName, $dedupTaskName)) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Write-Host "    removed stale task from replica: $t" -ForegroundColor Yellow
        }
    }
} else {
Write-Host "==> [5] Registering Task Scheduler entry: ClaudeCode-DreamConsolidator-3am"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null

$action = New-ScheduledTaskAction `
    -Execute $psRunner `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\Users\$env:USERNAME\.claude\scripts\dream-consolidate.ps1"
$trigger = New-ScheduledTaskTrigger -Daily -At 3:00am
$settingsTask = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settingsTask `
    -Principal $principal `
    -Description 'Daily 3am dream-consolidate 4-phase consolidator (orient->gather->consolidate->prune). Uses Codex CLI via ChatGPT subscription.' `
    | Out-Null
Write-Host "    Task Scheduler entry registered (next fire: 3:00 AM tomorrow, WakeToRun enabled)"

# ----------------------------------------------------------------------
# 5b. Register the nightly semantic-dedup task (4:30am, OFFSET from the 3am dream)
# ----------------------------------------------------------------------
# semantic-dedup is a WSL python script (tier-sensitive cosine over the LIVE mem0_egemma_768
# collection). It runs offset from the dream so the dedup.lock mutual-exclusion never blocks the
# dream's consolidation. Every delete is preserved in the tier-ledger for restore. (Before 2026-06
# it had NO scheduled trigger AND queried the dead pre-egemma 'memories' collection -> 404 abort;
# both fixed: the collection is now env-driven and this task runs it nightly.)
Write-Host "==> [5b] Registering Task Scheduler entry: ClaudeCode-SemanticDedup-430am"
Unregister-ScheduledTask -TaskName $dedupTaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
$dedupCmd = "/home/$WslUser/apps/mem0-server/.venv/bin/python $RepoRootWsl/scripts/wsl/semantic-dedup.py >> /home/$WslUser/.mem0/dedup-cron.log 2>&1"
$dedupAction = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wsl.exe" -Argument ('-d ' + $Distro + ' -e bash -lc "' + $dedupCmd + '"')
$dedupTrigger = New-ScheduledTaskTrigger -Daily -At 4:30am
$dedupSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$dedupPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $dedupTaskName -Action $dedupAction -Trigger $dedupTrigger -Settings $dedupSettings -Principal $dedupPrincipal -Description 'Nightly semantic-dedup (tier-sensitive cosine) over mem0_egemma_768; 4:30am, offset from the 3am dream.' | Out-Null
Write-Host "    Semantic-dedup task registered (next fire: 4:30 AM)"
} # end brain-role gate (v1.16 §6.3)

Write-Host ""
Write-Host "==> Windows config complete." -ForegroundColor Green
exit 0
