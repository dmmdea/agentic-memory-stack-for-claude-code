# 0-prereqs.ps1 - verify Windows + WSL + auth prerequisites
# Returns nonzero if anything is missing. Tells you exactly what's wrong.

# v1.0 Phase 7A: operator-agnostic. The WSL distro is resolved by the orchestrator
# and passed in; if run standalone it auto-detects the default distro.
param([string]$Distro = '')

$ErrorActionPreference = 'Continue'
if (-not $Distro) {
    $prevEnc = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::Unicode
        $Distro = (wsl.exe -l -q | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
    } finally { [Console]::OutputEncoding = $prevEnc }
}
$fails = @()

function Check {
    param([string]$Name, [scriptblock]$Test, [string]$FixHint)
    Write-Host -NoNewline "  $Name ... "
    try {
        $result = & $Test
        if ($result) {
            Write-Host "OK" -ForegroundColor Green
        } else {
            Write-Host "MISSING" -ForegroundColor Red
            $script:fails += "$Name : $FixHint"
        }
    } catch {
        Write-Host "FAIL ($_)" -ForegroundColor Red
        $script:fails += "$Name : $FixHint"
    }
}

Write-Host "Checking Windows-side prerequisites..."

Check "PowerShell 5.1+" { $PSVersionTable.PSVersion.Major -ge 5 } "Upgrade to Windows 10+/11 (built-in)"
Check "WSL2 installed" { (wsl.exe --status 2>&1) -match 'WSL' } "Run: wsl --install (admin PowerShell)"
Check "WSL distro resolved ($Distro)" { [bool]$Distro } "No WSL distro found. Install one (wsl --install -d Ubuntu) or pass -Distro <name> (see: wsl -l -q)"
Check "WSL is running" { $null -ne (wsl.exe -d $Distro -e echo ok 2>&1 | Select-String 'ok') } "WSL distro not started: wsl -d $Distro"
Check "WSL mirrored networking" {
    $wslconf = "$env:USERPROFILE\.wslconfig"
    if (Test-Path $wslconf) { (Get-Content $wslconf -Raw) -match 'networkingMode\s*=\s*mirrored' } else { $false }
} "Add to $env:USERPROFILE\.wslconfig:`n  [wsl2]`n  networkingMode = mirrored`nThen: wsl --shutdown"

Check "claude.cmd (Claude Code CLI)" { Test-Path "$env:USERPROFILE\AppData\Roaming\npm\claude.cmd" } "Install Claude Code: npm i -g @anthropic-ai/claude-code"
Check "codex.cmd (Codex CLI)" { Test-Path "$env:USERPROFILE\AppData\Roaming\npm\codex.cmd" } "Install Codex: npm i -g @openai/codex"
Check "Codex authenticated (ChatGPT subscription)" {
    if (-not (Test-Path "$env:USERPROFILE\.codex\auth.json")) { return $false }
    $auth = Get-Content "$env:USERPROFILE\.codex\auth.json" -Raw | ConvertFrom-Json
    $auth.auth_mode -eq 'chatgpt' -and $auth.tokens -and $auth.tokens.access_token
} "Run: codex login   (pick 'Sign in with ChatGPT')"

Check "git CLI" { Get-Command git -ErrorAction SilentlyContinue } "Install Git for Windows: winget install Git.Git"

Write-Host ""
Write-Host "Checking WSL-side prerequisites..."

Check "Python 3.12+ in WSL" { (wsl.exe -d $Distro -e python3 --version 2>&1) -match 'Python 3\.(1[2-9]|[2-9])' } "wsl -d $Distro -e sudo apt install -y python3 python3-pip python3-venv"
Check "curl in WSL" { wsl.exe -d $Distro -e which curl 2>&1 | Out-Null; $LASTEXITCODE -eq 0 } "wsl: sudo apt install -y curl"
# v0.22: Ollama is decommissioned from mem0's path (the embedder is EmbeddingGemma on
# llama-swap :11436). llama-swap (+ its llama.cpp build >= b6384 for gemma-embedding)
# is the single local inference stack. We check llama-swap is reachable rather than
# requiring Ollama. WARN-level: the installer stages the egemma GGUF and prints the
# llama-swap model entry to add if it isn't serving yet.
Check "llama-swap :11436 reachable in WSL" {
    wsl.exe -d $Distro -e bash -lc "curl -sf -m 5 http://127.0.0.1:11436/v1/models >/dev/null" 2>&1 | Out-Null
    $LASTEXITCODE -eq 0
} "Start llama-swap (single local inference stack on :11436) serving the EmbeddingGemma embedder + bge-reranker. Full step-by-step guide: install/llama-swap-setup.md (build llama.cpp >= b6384, download the two GGUFs, config + systemd unit + verify)."
Check "Node 22+ in WSL" {
    $v = wsl.exe -d $Distro -e bash -lc "node --version 2>/dev/null"
    if (-not $v) { return $false }
    $v = ($v -as [string]).Trim().TrimStart('v').Split('.')[0]
    [int]$v -ge 22
} "wsl: install via nvm:  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash && source ~/.nvm/nvm.sh && nvm install 22"

Check "systemd in WSL" { (wsl.exe -d $Distro -e ps -p 1 -o comm= 2>&1) -match 'systemd' } "Add to /etc/wsl.conf inside WSL:`n  [boot]`n  systemd=true`nThen: wsl --shutdown"

Check "Claude /login (Max OAuth)" { Test-Path "$env:USERPROFILE\.claude\.credentials.json" } "Run: claude /login   (in any directory)"

Write-Host ""
if ($fails.Count -gt 0) {
    Write-Host "Prerequisites FAILED ($($fails.Count) issues):" -ForegroundColor Red
    foreach ($f in $fails) { Write-Host "  - $f" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "Fix the issues above and re-run .\install.ps1" -ForegroundColor Yellow
    exit 1
}

Write-Host "All prerequisites OK." -ForegroundColor Green
exit 0
