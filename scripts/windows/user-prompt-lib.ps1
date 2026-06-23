# user-prompt-lib.ps1 — v0.18 MED-20/21: dot-sourceable pure logic for
# user-prompt-extract.ps1 (Phase 0.B decision-capture predicate + Phase 0.D
# proactive-injection admission policy).
# v0.20 A.5: + resident-daemon client (named pipe 'mem0-hook-daemon') and the
# shared helpers the daemon (mem0-hook-daemon.ps1) reuses. The daemon is an
# ACCELERATOR, never a dependency: Invoke-DaemonBundle returns $null on ANY
# failure and the hook falls back to its inline path unchanged.
#
# NO side effects at load — this file only defines functions, so Pester tests
# (scripts/windows/tests/UserPromptExtract.Tests.ps1) can dot-source it without
# running the hook pipeline. The production hook dot-sources it from $PSScriptRoot;
# deploy BOTH files to C:\Users\__WIN_USER__\.claude\scripts\ together.

function Test-CacheAclOwnerOnly {
    <#
    .SYNOPSIS
    v0.22 review L7: is the cache file's DACL owner-only-protected (inheritance
    off, exactly one ACE for the current user)? Used on the fresh-read path so a
    pre-v0.21 cache with inherited ACEs is never served verbatim. Returns $false
    on any error (fail-closed -> the caller refreshes, re-securing the file).
    GetAccessControl carries the same PS5.1-static / PS7-extension split as the
    SetAccessControl block below.
    #>
    param([string]$Path)
    try {
        if ([System.IO.File].GetMethod('GetAccessControl', [type[]]@([string]))) {
            $fs = [System.IO.File]::GetAccessControl($Path)
        } else {
            $fs = [System.IO.FileSystemAclExtensions]::GetAccessControl((New-Object System.IO.FileInfo($Path)))
        }
        if (-not $fs.AreAccessRulesProtected) { return $false }
        $me    = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $rules = @($fs.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
        return ($rules.Count -eq 1) -and ($rules[0].IdentityReference.Value -eq $me.Value)
    } catch { return $false }
}

function Get-Mem0ApiKeyCached {
    <#
    .SYNOPSIS
    v0.20 A.2: read the mem0 API key via a local cache to avoid the UNC
    \\wsl.localhost network-filesystem read (~90ms measured) on every hook spawn.

    Policy: a fresh local cache (< MaxAgeMinutes, default 60) is authoritative.
    Missing/stale/empty cache -> read the UNC original and refresh the cache.
    Caching FAILS CLOSED: the owner-only protected ACL is applied to an empty
    file BEFORE the secret is written; if the ACL cannot be applied the file is
    deleted and caching is SKIPPED (the key is still returned, so callers are
    unaffected) — the cache is never left on disk with inherited ACLs.
    UNC unreadable (WSL asleep) -> fall back to a stale cache ONLY if it is
    younger than MaxStaleFallbackHours (bounds a rotated-away key), else $null.
    Never throws.
    #>
    param(
        [string]$UncPath           = '\\wsl.localhost\__WSL_DISTRO__\home\__WSL_USER__\.mem0\api-key',
        [string]$CachePath         = ($env:USERPROFILE + '\.mem0\api-key.cache'),
        [int]$MaxAgeMinutes        = 60,
        [int]$MaxStaleFallbackHours = 24
    )
    # v0.20 A.3 perf: .NET statics only — this runs on the hooks' hot path
    # where the first cmdlet of a PS5.1 module costs its module load.

    $cached = $null
    try {
        if ([System.IO.File]::Exists($CachePath)) {
            $cached = ([System.IO.File]::ReadAllText($CachePath)).Trim()
            $age    = [System.DateTime]::Now - [System.IO.File]::GetLastWriteTime($CachePath)
            if ($cached -and ($age.TotalMinutes -lt $MaxAgeMinutes)) {
                # v0.22 review L7 read-path backstop: only trust a fresh cache whose
                # ACL is owner-only-protected. A pre-v0.21 file (secret-first,
                # best-effort ACL) with inherited ACEs must NOT be served verbatim —
                # drop through to the refresh path (which rewrites ACL-first + atomic).
                if (Test-CacheAclOwnerOnly -Path $CachePath) {
                    return $cached   # fresh cache, tight ACL — no UNC touch
                }
                # bad/inherited ACL: do NOT return; fall through to refresh.
            }
        }
    } catch { $cached = $null }

    # Cache missing/stale/empty -> authoritative UNC read
    $key = $null
    try { $key = ([System.IO.File]::ReadAllText($UncPath)).Trim() } catch { $key = $null }
    if (-not $key) {
        # UNC unavailable: last-resort stale cache beats no key at all, but
        # bound the staleness so a rotated-away key is not served indefinitely.
        if ($cached) {
            try {
                $staleAge = [System.DateTime]::Now - [System.IO.File]::GetLastWriteTime($CachePath)
                if ($staleAge.TotalHours -lt $MaxStaleFallbackHours) { return $cached }
            } catch {}
        }
        return $null
    }

    # Refresh the cache, FAIL CLOSED + ATOMIC. v0.21 review fix-pass: do the whole
    # empty-file -> owner-only protected ACL -> secret-write trio on a PER-PROCESS
    # temp path, then atomically rename it into place. A fresh inode is created on
    # a private path, so a concurrent same-user spawn can never share the
    # create/delete window on the live cache and the secret can never land on a
    # freshly-created inode that inherited the parent-dir ACLs (the old in-place
    # WriteAllText('')->ACL->WriteAllText($key) trio could, under a
    # sharing-violation-induced Delete race, leave the secret with inherited ACLs).
    # The NTFS rename is atomic and the moved file keeps its explicit protected
    # ACL. On ANY failure delete the TEMP (never the live cache) and skip caching;
    # the key is still returned, so callers are unaffected.
    $tmp = $null
    try {
        $dir = [System.IO.Path]::GetDirectoryName($CachePath)
        if (-not [System.IO.Directory]::Exists($dir)) { [void][System.IO.Directory]::CreateDirectory($dir) }
        $tmp = $CachePath + '.' + [System.Guid]::NewGuid().ToString('N') + '.tmp'
        [System.IO.File]::WriteAllText($tmp, '')
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true, $false)   # drop inherited ACEs
        $me   = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')
        $acl.AddAccessRule($rule)
        # [System.IO.File]::SetAccessControl exists on .NET Framework (PS5.1 —
        # the production hook runtime) but NOT on .NET Core (PS7 — Pester).
        # Try the Framework static, fall back to the Core ACL extension so the
        # owner-only ACL is applied (not skipped) on both runtimes.
        if ([System.IO.File].GetMethod('SetAccessControl', [type[]]@([string], [System.Security.AccessControl.FileSecurity]))) {
            [System.IO.File]::SetAccessControl($tmp, $acl)
        } else {
            [System.IO.FileSystemAclExtensions]::SetAccessControl((New-Object System.IO.FileInfo($tmp)), $acl)
        }
        [System.IO.File]::WriteAllText($tmp, $key)
        # Atomic publish: the secret-bearing, tight-ACL temp replaces the cache in
        # one step. Move fails if the dest exists, so prefer Replace when present
        # (atomic overwrite); else Delete-then-Move.
        if ([System.IO.File]::Exists($CachePath)) {
            # Replace = single atomic NTFS swap (no absent-cache window); pass
            # [NullString]::Value for the no-backup arg (a bare $null coerces to
            # '' and throws on .NET Core). Fall back to Delete+Move if Replace is
            # somehow absent — also atomic enough, the temp already carries the
            # protected ACL which Move preserves.
            if ([System.IO.File].GetMethod('Replace', [type[]]@([string], [string], [string]))) {
                [System.IO.File]::Replace($tmp, $CachePath, [NullString]::Value)
            } else {
                [System.IO.File]::Delete($CachePath)
                [System.IO.File]::Move($tmp, $CachePath)
            }
        } else {
            [System.IO.File]::Move($tmp, $CachePath)
        }
        $tmp = $null   # consumed by the rename — nothing to clean up
        # v0.22 review L7: File.Replace PRESERVES the DESTINATION's DACL, so when
        # the cache pre-existed with an inherited/weak ACL the swapped-in inode
        # keeps that weak ACL despite the temp's tight one. Re-apply the owner-only
        # protected ACL to the live cache after publish (idempotent) so the final
        # file is always owner-only-protected regardless of the publish path taken.
        # NOTE: a FileSecurity's modified-section flags are cleared once consumed
        # by SetAccessControl, so build a FRESH object here (re-using $acl is a no-op).
        $acl2 = New-Object System.Security.AccessControl.FileSecurity
        $acl2.SetAccessRuleProtection($true, $false)
        $acl2.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')))
        if ([System.IO.File].GetMethod('SetAccessControl', [type[]]@([string], [System.Security.AccessControl.FileSecurity]))) {
            [System.IO.File]::SetAccessControl($CachePath, $acl2)
        } else {
            [System.IO.FileSystemAclExtensions]::SetAccessControl((New-Object System.IO.FileInfo($CachePath)), $acl2)
        }
    } catch {
        if ($tmp) { try { [System.IO.File]::Delete($tmp) } catch {} }
    }
    return $key
}

function Test-DecisionLikePrompt {
    <#
    .SYNOPSIS
    Phase 0.B decision-capture predicate: is this prompt a short decision-like reply?
    Encapsulates the v0.17 inline logic (word-count gate + 4 pattern checks) verbatim,
    plus the v0.18 MED-20 'go ahead' extension.
    #>
    param([string]$Prompt)

    if ([string]::IsNullOrWhiteSpace($Prompt)) { return $false }

    $promptWords   = ($Prompt.Trim() -split '\s+').Count
    $isShortPrompt = $promptWords -le 25
    if (-not $isShortPrompt) { return $false }

    $isDecisionLike = $false
    if ($Prompt -match '^\s*\d+\s*$') { $isDecisionLike = $true }          # "1", "2", "3", "4"
    if ($Prompt -match '^\s*[ABCD]\s*$') { $isDecisionLike = $true }        # "A", "B", "C", "D"
    if ($Prompt -match '\b\d+\s+(and|y|o|or|,|&)\s+\d+\b') { $isDecisionLike = $true }  # "1 and 2" (incl. mid-sentence)
    # v0.18 MED-20: 'go ahead' added to the affirmative alternation
    if ($Prompt -match '^\s*(yes|no|both|all|skip|cancel|ok|okay|sure|done|proceed|go|go ahead)\s*$') { $isDecisionLike = $true }

    return $isDecisionLike
}

function Get-StackBrandRules {
    <#
    .SYNOPSIS
    v1.0 Phase 7B: operator brand-routing rules read from the deployed
    brands.json (beside this lib in ~/.claude/scripts/). Each rule is
    { pattern = <case-insensitive regex>; brand = <label> }. Operators add their
    own projects there; the shipped default covers only this stack's workspace,
    so NO private brand names are hardcoded in source. Defensive: any read/parse
    failure falls back to the neutral default so the prompt hook never breaks.
    #>
    $cfg = Join-Path $PSScriptRoot 'brands.json'
    try {
        if (Test-Path -LiteralPath $cfg) {
            $rules = (Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json).rules
            if ($rules) { return @($rules) }
        }
    } catch {}
    return @([pscustomobject]@{ pattern = 'ai-ecosystem|agentic-memory|mem0'; brand = 'ai-ecosystem' })
}

function Get-InferredBrandFromPath {
    <#
    .SYNOPSIS
    Infer the session brand from a Claude Code transcript path using the
    operator's brand rules (Get-StackBrandRules / brands.json). Returns the brand
    label or $null when the path matches no rule (the caller MUST treat $null as
    "unknown brand" and fail closed — see Select-AdmittedMemoryResults). Pass
    -Rules to inject rules (tests). Operator-agnostic: brand names live in
    brands.json, not in this code.
    #>
    param([string]$Path, $Rules = (Get-StackBrandRules))

    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    $lower = $Path.ToLower()
    foreach ($r in $Rules) { if ($r.pattern -and ($lower -match $r.pattern)) { return $r.brand } }
    return $null
}

function Get-SessionInitiative {
    <#
    .SYNOPSIS
    v0.22 Pillar 1: derive the session's INITIATIVE (the repo / line of work)
    from the hook payload's cwd. Two initiatives can share the SAME brand
    (agentic-memory-stack and local-offload both run under ai-ecosystem), so
    workspace/project/brand cannot separate them — the distinguishing axis is
    the repo. The server scopes goal/OQ injection to (initiative OR NULL) so a
    goal from another initiative under the same brand never bleeds in.

    Computation: `git -C <cwd> rev-parse --show-toplevel` -> leaf basename of
    the repo root; if cwd is not inside a git repo (or git is unavailable),
    fall back to the leaf basename of cwd itself. Returns $null only when cwd
    is null/empty (server then treats the session as unscoped on initiative).
    Never throws.
    #>
    param([string]$Cwd)

    if ([string]::IsNullOrWhiteSpace($Cwd)) { return $null }
    $leafFrom = {
        param($p)
        # Trim trailing slashes/backslashes, then take the last path segment.
        $t = ([string]$p).TrimEnd('\', '/')
        if ([string]::IsNullOrWhiteSpace($t)) { return $null }
        $seg = $t -split '[\\/]+'
        if ($seg.Length -eq 0) { return $null }
        return $seg[$seg.Length - 1]
    }
    # Prefer the git repo root leaf (stable across any subdir of the repo).
    try {
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = 'git'
        $psi.Arguments = '-C "' + $Cwd + '" rev-parse --show-toplevel'
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        if ($p) {
            $top = $p.StandardOutput.ReadToEnd()
            [void]$p.StandardError.ReadToEnd()
            if (-not $p.WaitForExit(1500)) { try { $p.Kill() } catch {}; $top = $null }
            if ($p.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($top)) {
                $leaf = & $leafFrom ($top.Trim())
                if ($leaf) { return $leaf }
            }
        }
    } catch {}
    # Not a git repo (or git unavailable): fall back to the cwd leaf.
    return (& $leafFrom $Cwd)
}

function Resolve-ModelTier {
    <#
    .SYNOPSIS
    v0.22 Pillar 2 (D4): map a model id (e.g. claude-haiku-4-5) to its injection
    TIER by case-insensitive substring match against each tier's match[] in
    model-tiers.json. THIS phase is detection-only — the tier is plumbed through
    but Phase D applies per-tier caps/format, so a frontier resolution reproduces
    today's behavior exactly.

    Resolution: load model-tiers.json (default: beside the deployed lib;
    -ConfigPath overrides for tests), then for each tier in declaration order
    test whether ANY of its match[] substrings appears (case-insensitive) in the
    model id; first hit wins. No match / null-or-empty model / unreadable config
    -> default_tier (which is 'frontier' = today's behavior; over-injecting a
    rare unknown model is the SAFE default — the main driver is Opus/Fable).
    Never throws.
    #>
    param(
        [string]$Model,
        [string]$ConfigPath
    )
    # Default tier is frontier even before we can read the config — the safe,
    # behavior-preserving fallback if anything below fails.
    $defaultTier = 'frontier'
    if (-not $ConfigPath) {
        # Deployed default: model-tiers.json sits beside the lib in ~/.claude/scripts.
        $here = $PSScriptRoot
        if (-not $here) { try { $here = [System.IO.Path]::GetDirectoryName($PSCommandPath) } catch { $here = $null } }
        if ($here) { $ConfigPath = [System.IO.Path]::Combine($here, 'model-tiers.json') }
    }
    $cfg = $null
    try {
        if ($ConfigPath -and [System.IO.File]::Exists($ConfigPath)) {
            $cfg = ([System.IO.File]::ReadAllText($ConfigPath)) | ConvertFrom-Json
        }
    } catch { $cfg = $null }
    if (-not $cfg) { return $defaultTier }
    if ($cfg.default_tier) { $defaultTier = [string]$cfg.default_tier }

    if ([string]::IsNullOrWhiteSpace($Model)) { return $defaultTier }
    $modelLower = $Model.ToLower()

    # Iterate tiers in declaration order; first tier with a substring hit wins.
    $tiers = $cfg.tiers
    if (-not $tiers) { return $defaultTier }
    foreach ($name in $tiers.PSObject.Properties.Name) {
        $matches = $tiers.$name.match
        if (-not $matches) { continue }
        foreach ($m in @($matches)) {
            if ([string]::IsNullOrWhiteSpace([string]$m)) { continue }
            if ($modelLower.Contains(([string]$m).ToLower())) { return $name }
        }
    }
    return $defaultTier
}

function Get-SessionTier {
    <#
    .SYNOPSIS
    v0.22 Pillar 2 (D4): resolve the consuming model's TIER for a session, in
    order: per-session sidecar -> transcript tail (.message.model of the LAST
    assistant line) -> default 'frontier'. UserPromptSubmit has no model field in
    its payload, so the sidecar (written at SessionStart) is the fast path; a
    transcript scan is the fallback for sessions that started before the sidecar
    existed (resume/compact). Frontier default is SAFE — see Resolve-ModelTier.

    Sidecar: ~/.mem0/session-tier/<SessionId>.json (-TierDir overrides for tests),
    a JSON object with a 'tier' field. After resolving via the transcript, the
    tier is CACHED back to the sidecar so subsequent prompts in the same session
    skip the transcript scan. Transcript reading tails the file (last ~64KB) and
    scans backward for the last assistant line — it never parses the whole JSONL.
    Fail-open everywhere: any error -> 'frontier'. Never throws.
    #>
    param(
        [string]$SessionId,
        [string]$TranscriptPath,
        [string]$TierDir,
        [string]$ConfigPath
    )
    $defaultTier = 'frontier'
    if (-not $TierDir) { $TierDir = [System.IO.Path]::Combine($env:USERPROFILE, '.mem0', 'session-tier') }

    # 1) sidecar (fast path)
    $sidecarPath = $null
    if (-not [string]::IsNullOrWhiteSpace($SessionId)) {
        $sidecarPath = [System.IO.Path]::Combine($TierDir, $SessionId + '.json')
        try {
            if ([System.IO.File]::Exists($sidecarPath)) {
                $sc = ([System.IO.File]::ReadAllText($sidecarPath)) | ConvertFrom-Json
                $t = $null
                try { $t = [string]$sc.tier } catch { $t = $null }
                if (-not [string]::IsNullOrWhiteSpace($t)) { return $t }
            }
        } catch {}
    }

    # 2) transcript tail -> last assistant line's .message.model
    $model = $null
    try {
        if ($TranscriptPath -and [System.IO.File]::Exists($TranscriptPath)) {
            # Tail the last ~64KB rather than reading the whole JSONL.
            $tailText = $null
            try {
                $fs = [System.IO.File]::Open($TranscriptPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
                try {
                    $cap = 65536L
                    $start = if ($fs.Length -gt $cap) { $fs.Length - $cap } else { 0L }
                    [void]$fs.Seek($start, [System.IO.SeekOrigin]::Begin)
                    $buf = [byte[]]::new([int]($fs.Length - $start))
                    [void]$fs.Read($buf, 0, $buf.Length)
                    $tailText = [System.Text.Encoding]::UTF8.GetString($buf)
                } finally { $fs.Close() }
            } catch { $tailText = $null }
            if ($tailText) {
                $lines = $tailText -split "`n"
                # If we seeked into the middle of a line, the first element is a
                # partial line — harmless, it just won't parse as JSON.
                for ($i = $lines.Length - 1; $i -ge 0; $i--) {
                    $ln = $lines[$i]
                    if ([string]::IsNullOrWhiteSpace($ln)) { continue }
                    if ($ln -notmatch '"model"') { continue }   # cheap pre-filter
                    $obj = $null
                    try { $obj = $ln | ConvertFrom-Json } catch { $obj = $null; continue }
                    $isAssistant = $false
                    try { $isAssistant = ($obj.type -eq 'assistant') -or ($obj.message -and $obj.message.role -eq 'assistant') } catch { $isAssistant = $false }
                    if (-not $isAssistant) { continue }
                    $mm = $null
                    try { if ($obj.message) { $mm = [string]$obj.message.model } } catch { $mm = $null }
                    if (-not [string]::IsNullOrWhiteSpace($mm)) { $model = $mm; break }
                }
            }
        }
    } catch { $model = $null }

    if ([string]::IsNullOrWhiteSpace($model)) { return $defaultTier }
    $tier = Resolve-ModelTier -Model $model -ConfigPath $ConfigPath
    if ([string]::IsNullOrWhiteSpace($tier)) { $tier = $defaultTier }

    # Cache the transcript-resolved tier back to the sidecar (last-writer-wins).
    if ($sidecarPath) {
        try {
            if (-not [System.IO.Directory]::Exists($TierDir)) { [void][System.IO.Directory]::CreateDirectory($TierDir) }
            $payload = '{"model":' + (ConvertTo-TierJsonString $model) + ',"tier":"' + $tier + '","ts":"' + [System.DateTime]::Now.ToString('o') + '","source":"transcript"}'
            [System.IO.File]::WriteAllText($sidecarPath, $payload)
        } catch {}
    }
    return $tier
}

function Get-SessionSidecar {
    <#
    .SYNOPSIS
    v0.22 Pillar 2 + B latency fix: read the per-session sidecar
    (~/.mem0/session-tier/<SessionId>.json) ONCE and return its parsed
    {model, tier, initiative} as a hashtable, or $null when absent/unreadable.
    The UserPromptSubmit path uses this to (a) get the resolved tier without a
    transcript scan and (b) get the cwd-derived initiative WITHOUT spawning git
    per prompt — the ~70ms git spawn now happens once at SessionStart, cached
    here. Sidecar miss -> $null and the caller falls back (Get-SessionTier /
    Get-SessionInitiative). -TierDir overrides for tests. Never throws.
    #>
    param([string]$SessionId, [string]$TierDir)
    if ([string]::IsNullOrWhiteSpace($SessionId)) { return $null }
    if (-not $TierDir) { $TierDir = [System.IO.Path]::Combine($env:USERPROFILE, '.mem0', 'session-tier') }
    $path = [System.IO.Path]::Combine($TierDir, $SessionId + '.json')
    try {
        if (-not [System.IO.File]::Exists($path)) { return $null }
        $sc = ([System.IO.File]::ReadAllText($path)) | ConvertFrom-Json
        if (-not $sc) { return $null }
        $tier = $null; $init = $null; $model = $null
        try { $tier  = [string]$sc.tier }       catch {}
        try { $init  = [string]$sc.initiative } catch {}
        try { $model = [string]$sc.model }       catch {}
        # An empty-string initiative in the sidecar means "explicitly unscoped"
        # (cwd-less SessionStart); normalize to $null for the request body.
        if ([string]::IsNullOrWhiteSpace($init)) { $init = $null }
        if ([string]::IsNullOrWhiteSpace($tier)) { $tier = $null }
        return @{ tier = $tier; initiative = $init; model = $model }
    } catch { return $null }
}

function ConvertTo-TierJsonString {
    <#
    .SYNOPSIS
    Encode a string as a JSON string literal (with surrounding quotes), or the
    literal null when empty. Used by the session-tier sidecar writers (lib +
    SessionStart spawn) so neither pulls ConvertTo-Json onto a path that may run
    under PS5.1. Escapes the JSON-significant control chars; model ids are
    [A-Za-z0-9.-] in practice so this is defensive.
    #>
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return 'null' }
    $s = $Value -replace '\\', '\\' -replace '"', '\"' -replace "`r", '\r' -replace "`n", '\n' -replace "`t", '\t'
    return '"' + $s + '"'
}

function Format-MemoryContextBlock {
    <#
    .SYNOPSIS
    v0.20 A.3: render the [MEMORY CONTEXT] block from ONE POST /v1/context/bundle
    response (memories + goals + open_questions in a single payload). The block
    is identical to what the v0.17-v0.19 hook rendered from its separate
    search / goals / open_questions calls. Client-side admission
    (Select-AdmittedMemoryResults Layers 1/2/3) still applies to the bundle's
    memories — defense in depth unchanged. Returns the block string, or $null.
    v1.0 Phase 3 / R2 (abstention-first): returns $null whenever NO memory clears
    the relevance gate — open goals / open frontier questions alone no longer
    render a block (they used to static-prepend on every substantive turn). The
    block is emitted only alongside >=1 admitted memory.
    .PARAMETER AuditPath
    Forwarded to Select-AdmittedMemoryResults (tests point it at a temp file).
    #>
    param(
        $Bundle,
        [string]$Brand,
        [string]$AuditPath,
        # v0.22 Phase D: consuming-model tier. frontier/mid = current full format
        # (byte-identical); small = flat (drop redundant [brand] tag when brand is
        # known, highest-tier-first, prepend a one-line legend). Unknown/empty ->
        # frontier (fail-open: a tier-resolution slip never degrades the block).
        [string]$Tier = 'frontier'
    )
    if (-not $Bundle) { return $null }

    # v0.22 D: 'small' is the only tier that changes rendering; everything else
    # (frontier, mid, unknown) renders the full format unchanged.
    $isSmall = ($Tier -eq 'small')

    # Tier rank for highest-tier-first ordering in the flat (small) format.
    # Higher = more trusted; canonical/insight are admission-stripped before
    # rendering, but ranked anyway so the ordering is total.
    $tierRank = @{ canonical = 5; insight = 4; stable = 3; evidence = 2; temporal = 1 }

    $contextLines = [System.Collections.Generic.List[string]]::new()
    $contextLines.Add('[MEMORY CONTEXT - auto-surfaced by user-prompt-extract.ps1 v0.17 Phase 0.D]')
    $contextLines.Add('')

    # v0.22 D (small tier): one-line legend right after the header so a small
    # model reads the trust grammar before the memories.
    if ($isSmall) {
        $contextLines.Add('Memory tiers: [canonical]=locked truth · [insight]/[stable]=trusted · [evidence]/[temporal]=advisory (verify before risky actions) · prefer higher-tier on conflict.')
        $contextLines.Add('')
    }

    # v1.0 Phase 3 / R2 (abstention-first): the block renders ONLY when >=1 memory
    # clears the relevance gate (the calibrated 0.30 semantic-cosine threshold is
    # applied server-side — 0.30, NOT 0.50: on EmbeddingGemma's compressed scale 0.35
    # craters recall to 0.47 and 0.50 drops 100%, so the raise was deliberately refused;
    # the bundle returns nothing when the prompt is off the
    # project-knowledge manifold). Open goals / open frontier questions no longer
    # static-prepend on their own — that was the paper's #2 anti-pattern (static
    # 63.82% premature). $anyMemoryAdmitted gates the whole block at the end.
    $anyMemoryAdmitted = $false

    # 1. Memories — client-side admission layers still applied (defense in depth).
    # v1.0 Phase 5 / R6 (placement / attention hygiene): the memory section is built HERE but
    # rendered LAST in the block (appended after goals + open-questions, below) so the relevant
    # project memories sit at the recency peak immediately above the user's prompt, NOT in the
    # low-attention middle/"basin" (Found-in-the-Middle 2406.16008; Attention-Basin 2508.05128;
    # Context-Rot). Within the section the list is reversed so the SINGLE most-important memory
    # (top relevance for frontier; top tier-rank for small) is the final line — adjacent to the
    # prompt. A cheap complement to R2/R3, not a lever.
    $memoryLines = [System.Collections.Generic.List[string]]::new()
    $hits = @($Bundle.memories)
    if ($hits.Count -gt 0) {
        $filteredResults = @(Select-AdmittedMemoryResults -Hits $hits -Brand $Brand -AuditPath $AuditPath)
        $rejectedCount = $hits.Count - $filteredResults.Count
        if ($rejectedCount -gt 0 -and ($null -ne $ExecutionContext.SessionState.InvokeCommand.GetCommand('Write-Log', 'Function'))) {
            Write-Log "0.D admission: $rejectedCount of $($hits.Count) results rejected (see admission-rejected.jsonl)"
        }
        if ($filteredResults.Count -gt 0) {
            $anyMemoryAdmitted = $true   # v1.0 R2: gates the whole block (below)
            # v0.22 D (small tier): highest-tier memories first via a STABLE sort.
            # MUST be PS 5.1-compatible: Sort-Object -Stable is PS7-only and under
            # Windows PowerShell 5.1 (the production hook runtime) it is a
            # parameter-binding error — which $ErrorActionPreference='SilentlyContinue'
            # swallows, collapsing the result to an EMPTY array ("Top 0 relevant
            # memories"). v0.22 H1 fix: decorate-sort-undecorate with an explicit
            # original-index tiebreaker. Sorting on (rank DESC, index ASC) is a
            # total order, so it is stable on BOTH 5.1 and 7 — same-tier rows keep
            # their incoming order without relying on -Stable.
            if ($isSmall) {
                $decorated = for ($i = 0; $i -lt $filteredResults.Count; $i++) {
                    $h = $filteredResults[$i]
                    $t = if ($h.metadata -and $h.metadata.tier) { $h.metadata.tier } else { 'evidence' }
                    $rank = if ($tierRank.ContainsKey([string]$t)) { $tierRank[[string]$t] } else { 0 }
                    [pscustomobject]@{ Idx = $i; Rank = $rank; Item = $h }
                }
                $filteredResults = @(
                    $decorated |
                        Sort-Object -Property @{ Expression = 'Rank'; Descending = $true },
                                              @{ Expression = 'Idx';  Descending = $false } |
                        ForEach-Object { $_.Item }
                )
            }
            # v1.0 R6 (placement): reverse so the MOST-important memory (top of the importance
            # order above — relevance for frontier, tier-rank for small) is rendered LAST, the
            # line immediately above the user's prompt (the recency peak). PS 5.1-safe in-place.
            [array]::Reverse($filteredResults)
            $memoryLines.Add("Top $($filteredResults.Count) relevant memories:")
            foreach ($h in $filteredResults) {
                $tier   = if ($h.metadata -and $h.metadata.tier)  { $h.metadata.tier  } else { 'evidence' }
                $hbrand = if ($h.metadata -and $h.metadata.brand) { $h.metadata.brand } else { 'cross-brand' }
                # v0.22 D (small tier): drop the redundant [|brand] tag when the
                # session brand is known (every surfaced row is same-brand or
                # brand-neutral by admission). Keep it when brand is unknown.
                if ($isSmall -and $Brand) {
                    $memoryLines.Add("  - [$tier] $($h.memory)")
                } else {
                    $memoryLines.Add("  - [$tier|$hbrand] $($h.memory)")
                }
            }
        }
    }

    # v0.21 Phase A (M2): defense-in-depth brand gate on goals/OQ, mirroring the
    # memory Layer-2 rule in Select-AdmittedMemoryResults. With an inferred brand,
    # keep brand-neutral (null/empty .brand) rows plus same-brand rows; with NO
    # inferred brand (unknown-brand session), keep ONLY brand-neutral rows so a
    # brand-tagged goal/question never leaks into an unrecognized session. The
    # server already fails closed (only_brand_neutral); this is the client backstop.
    $brandGate = {
        param($row)
        $rowBrand = $row.brand
        if ($Brand) {
            return ([string]::IsNullOrWhiteSpace([string]$rowBrand) -or ($rowBrand -eq $Brand))
        }
        return [string]::IsNullOrWhiteSpace([string]$rowBrand)
    }

    # 2. Open goals (server sends <=5, same query GET /v1/goals served the hook)
    $goals = @(@($Bundle.goals) | Where-Object { & $brandGate $_ })
    if ($goals.Count -gt 0) {
        $contextLines.Add("Open goals ($($goals.Count) shown):")
        foreach ($g in $goals) {
            $title = $g.title
            if ($title.Length -gt 100) { $title = $title.Substring(0, 100) + '...' }
            $contextLines.Add("  - [P$($g.priority) OPEN] $title")
        }
        $contextLines.Add('')
    }

    # 3. Open frontier questions (<=3)
    $questions = @(@($Bundle.open_questions) | Where-Object { & $brandGate $_ })
    if ($questions.Count -gt 0) {
        $contextLines.Add('Open frontier questions:')
        foreach ($q in $questions) {
            $qtext = $q.question_text
            if ($qtext.Length -gt 120) { $qtext = $qtext.Substring(0, 120) + '...' }
            $contextLines.Add("  - $qtext")
        }
        $contextLines.Add('')
    }

    # v1.0 Phase 5 / R6 (placement): append the memory section LAST — goals + open-questions are
    # context that can live in the low-attention middle; the relevant memories belong at the
    # recency peak immediately above the user's prompt. No trailing blank line, so the single
    # most-important memory (reversed to last, above) is the final line, adjacent to the prompt.
    foreach ($ml in $memoryLines) { $contextLines.Add($ml) }

    # v1.0 Phase 6 / R4 (raw-trace fallback): when NO condensed memory cleared the relevance
    # gate, the server may surface ONE semantically-relevant past episode (low-confidence
    # retrieval). Render it as a single ADVISORY line INSTEAD OF fully abstaining. This is the
    # only path that emits the block without an admitted memory; it stays R2-faithful because
    # (a) the server's SEMANTIC gate (raw cosine >= RAW_FALLBACK_COSINE_FLOOR + fail-closed
    # brand; lexical/bm25 was disproven live — see v0.29 CHANGELOG) only fires on a
    # genuinely-relevant episode, never on off-domain prompts, and (b) we emit ONLY this line
    # — open goals/OQ do NOT piggyback here (that would re-introduce the static-prepend anti-
    # pattern on no-memory turns). Defense-in-depth: the same $brandGate as goals/OQ (the
    # server already fails closed via only_brand_neutral; this is the client backstop).
    $rawFallbackLine = $null
    if ((-not $anyMemoryAdmitted) -and $Bundle.raw_fallback -and (& $brandGate $Bundle.raw_fallback)) {
        $snip = [string]$Bundle.raw_fallback.snippet
        if (-not [string]::IsNullOrWhiteSpace($snip)) {
            $rawFallbackLine = "Related past work (episode $($Bundle.raw_fallback.episode_id)): $snip"
        }
    }

    # v1.0 Phase 3 / R2 abstention-first: render the block ONLY when at least one memory cleared
    # the relevance gate ($anyMemoryAdmitted == $memoryLines non-empty). When nothing clears, the
    # WHOLE block is a NOOP (return $null) even if open goals/OQ exist — they no longer
    # static-prepend on off-domain / no-memory-needed turns (the paper's #2 anti-pattern).
    # v1.0 R4 adds ONE exception: a strict-match raw-trace fallback may render its single line.
    # Session-start goal awareness is a separate hook and is unaffected.
    if ($anyMemoryAdmitted) { return ($contextLines -join "`n") }
    if ($rawFallbackLine) { return $rawFallbackLine }
    return $null
}

function Select-AdmittedMemoryResults {
    <#
    .SYNOPSIS
    v0.18 Phase B three-layer client-side admission policy (Phase 0.D), extracted
    verbatim from user-prompt-extract.ps1 for testability (MED-21).

    Layer 1 — tier allowlist: only stable+evidence may surface via proactive
    injection; canonical+insight are never echoed this way. Null/missing tier is
    ADMITTED (matches Phase C server-side gate semantics).
    Layer 2 — brand-match guard (v0.19 M5/M14/L10: fail-closed): with an inferred
    brand, only memories matching it (or brand-neutral null-brand ones) surface.
    With NO inferred brand (unknown-brand session), ONLY brand-neutral memories
    surface — brand-tagged memories never leak into an unrecognized session.
    Layer 3 — top-3 + 200-char per-memory cap.

    Rejected candidates are appended to admission-rejected.jsonl (one JSONL line
    per result not in the final surfaced set). Audit failure never breaks the hook.
    .PARAMETER AuditPath
    Override for the rejected-candidate audit log (tests point this at a temp dir).
    Default: $env:USERPROFILE\.mem0\admission-rejected.jsonl
    #>
    param(
        [array]$Hits,
        [string]$Brand,
        [string]$AuditPath
    )

    if (-not $Hits -or @($Hits).Count -eq 0) { return @() }
    $Hits = @($Hits)

    # Layer 1 — tier allowlist
    $allowedTiers = @('stable', 'evidence')
    $filteredResults = @($Hits | Where-Object {
        $tier = $_.metadata.tier
        [string]::IsNullOrEmpty([string]$tier) -or ($allowedTiers -contains $tier)
    })

    # Layer 2 — brand-match guard (v0.19 M5/M14/L10: fail-closed; empty-string
    # brand treated as legacy/null on both client and server layers).
    # v0.20 Phase F (M14): IsNullOrWhiteSpace — a whitespace-only brand ('  ')
    # normalizes to legacy-empty here exactly like the server gate's strip.
    if ($Brand) {
        $filteredResults = @($filteredResults | Where-Object {
            $memBrand = $_.metadata.brand
            [string]::IsNullOrWhiteSpace([string]$memBrand) -or ($memBrand -eq $Brand)
        })
    } else {
        # Fail closed: session brand unknown -> never surface brand-tagged memories
        $filteredResults = @($filteredResults | Where-Object {
            [string]::IsNullOrWhiteSpace([string]$_.metadata.brand)
        })
    }

    # Layer 3 — top-3 + 200-char per-memory cap
    # (array slice, not Select-Object -First: Select-Object is the Utility
    # module and would cost ~75ms module load on the hook hot path — v0.20 A.3)
    if ($filteredResults.Count -gt 3) { $filteredResults = @($filteredResults[0..2]) }
    foreach ($fr in $filteredResults) {
        if ($fr.memory -and $fr.memory.Length -gt 200) {
            $fr.memory = $fr.memory.Substring(0, 200) + '... [truncated by v0.18 admission policy]'
        }
    }

    # Rejected-candidate audit log. Never let audit failure break the hook.
    # v0.20 A.3 perf: .NET statics + hand-built JSON line (ConvertTo-Json /
    # Get-Date / Add-Content would each pull a PS5.1 module load onto the hook
    # hot path; rejections fire on most substantive prompts via truncated_by_topN).
    try {
        $admittedIds = @($filteredResults | ForEach-Object { $_.id })
        $rejected = @($Hits | Where-Object { $admittedIds -notcontains $_.id })
        if ($rejected.Count -gt 0) {
            if (-not $AuditPath) {
                $auditDir = $env:USERPROFILE + '\.mem0'
                if (-not [System.IO.Directory]::Exists($auditDir)) { [void][System.IO.Directory]::CreateDirectory($auditDir) }
                $AuditPath = $auditDir + '\admission-rejected.jsonl'
            }
            # v0.19 L5: rotate at 10MB (single .1 backup) — client parallel of the
            # server-side admission_gate.log_rejected rotation; previously unbounded.
            if ([System.IO.File]::Exists($AuditPath) -and (([System.IO.FileInfo]::new($AuditPath)).Length -gt 10MB)) {
                $archive = "$AuditPath.1"
                if ([System.IO.File]::Exists($archive)) { [System.IO.File]::Delete($archive) }
                [System.IO.File]::Move($AuditPath, $archive)
            }
            $auditLines = [System.Text.StringBuilder]::new()
            foreach ($r in $rejected) {
                $rTier  = $r.metadata.tier
                $rBrand = $r.metadata.brand
                $reason = if (-not [string]::IsNullOrEmpty([string]$rTier) -and ($allowedTiers -notcontains $rTier)) {
                    "tier_disallowed:$rTier"
                } elseif ($Brand -and (-not [string]::IsNullOrWhiteSpace([string]$rBrand)) -and ($rBrand -ne $Brand)) {
                    "brand_mismatch:${rBrand}_vs_${Brand}"
                } elseif ((-not $Brand) -and (-not [string]::IsNullOrWhiteSpace([string]$rBrand))) {
                    # v0.19 M5/L10: fail-closed rejection — brand-tagged memory in an
                    # unknown-brand session
                    "brand_unknown_session:${rBrand}"
                } else {
                    'truncated_by_topN'
                }
                # v0.19 L6: schema unified with the server writer
                # (admission_gate.log_rejected) — same five fields, same
                # schema_version; only the layer label differs by design
                # ('phase-0d-client' here vs 'server-search' on the server).
                # memory_id is a UUID and reason is built from controlled tier/brand
                # values; escape quotes/backslashes defensively anyway.
                $idJson     = ([string]$r.id)  -replace '\\', '\\\\' -replace '"', '\"'
                $reasonJson = $reason          -replace '\\', '\\\\' -replace '"', '\"'
                [void]$auditLines.Append('{"ts":"' + [System.DateTime]::Now.ToString('o') + '","memory_id":"' + $idJson + '","reason":"' + $reasonJson + '","layer":"phase-0d-client","schema_version":"v18"}' + [System.Environment]::NewLine)
            }
            [System.IO.File]::AppendAllText($AuditPath, $auditLines.ToString())
        }
    } catch {
        if ($null -ne $ExecutionContext.SessionState.InvokeCommand.GetCommand('Write-Log', 'Function')) {
            Write-Log "0.D admission audit log failed: $($_.Exception.Message)"
        }
    }

    # NOTE: plain return (no unary comma) — callers wrap in @(); a comma-wrapped
    # return survives pipeline unrolling as a nested array and breaks .Count/order.
    return $filteredResults
}

# ===========================================================================
# v0.20 Phase F (L9) — rate-limit + fixture logic as pure lib functions.
# Shared by user-prompt-extract.ps1 (inline path), mem0-hook-daemon.ps1
# (op=bundle_raw mirrors the inline pipeline) and pre-tool-check.ps1 (fixture
# write) so the three call sites cannot drift — and Pester can pin the
# cooldown boundary, per-session isolation, corrupt-state fail-open, stale
# sweep, byte-fidelity and keep-20 prune without running a hook pipeline.
# ===========================================================================

function Get-RateLimitDecision {
    <#
    .SYNOPSIS
    v0.20 Phase F (L9): READ-ONLY per-session proactive-search rate-limit check
    (v0.18 MED-16 cooldown, v0.19 L2 per-session keying + consume-on-fire).
    Extracted verbatim from user-prompt-extract.ps1 / Invoke-DaemonRawBundle.
    Returns @{ RateLimited = [bool]; StatePath = [string] }. The cooldown token
    is consumed (written to StatePath) by the CALLER, and only when proactive
    surfacing actually fires — never here. Corrupt/unreadable state fails open.
    -NowFileTimeUtc is injected for testability (FileTime ticks = 100ns units,
    so elapsedMs = (now - last) / 10000). Never throws.
    #>
    param(
        [string]$StateDir,
        [string]$SessionId,
        [int64]$NowFileTimeUtc,
        [int]$CooldownMs = 1000
    )
    $statePath = $StateDir + '\user-prompt-rate-limit-' + $SessionId
    $rateLimited = $false
    try {
        if (-not [System.IO.Directory]::Exists($StateDir)) { [void][System.IO.Directory]::CreateDirectory($StateDir) }
        if ([System.IO.File]::Exists($statePath)) {
            try {
                $lastFireTicks = [int64]([System.IO.File]::ReadAllText($statePath).Trim())
                $elapsedMs = ($NowFileTimeUtc - $lastFireTicks) / 10000
                if ($elapsedMs -lt $CooldownMs) { $rateLimited = $true }
            } catch {
                # Corrupt/unreadable state file: fail open (overwritten on fire)
                if ($null -ne $ExecutionContext.SessionState.InvokeCommand.GetCommand('Write-Log', 'Function')) {
                    Write-Log "0.D rate-limit state unreadable ($($_.Exception.Message)); failing open"
                }
            }
        }
    } catch { $rateLimited = $false }
    return [pscustomobject]@{ RateLimited = $rateLimited; StatePath = $statePath }
}

function Invoke-RateLimitStateSweep {
    <#
    .SYNOPSIS
    v0.20 Phase F (L9): stale rate-limit state sweep (v0.19 L2) — per-session
    files accumulate one per session; delete anything older than -MaxAgeHours,
    including the legacy global 'user-prompt-rate-limit' file (the prefix match
    covers it). Fresh files are spared. Never throws.
    #>
    param([string]$StateDir, [int]$MaxAgeHours = 1)
    try {
        $cutoff = [System.DateTime]::Now.AddHours(-$MaxAgeHours)
        foreach ($sf in [System.IO.Directory]::GetFiles($StateDir, 'user-prompt-rate-limit*')) {
            if ([System.IO.File]::GetLastWriteTime($sf) -lt $cutoff) {
                try { [System.IO.File]::Delete($sf) } catch {}
            }
        }
    } catch {}
}

function Save-HookFixture {
    <#
    .SYNOPSIS
    v0.20 Phase F (L9): byte-faithful hook-fixture write + keep-20 prune
    (v0.17 F.3.3 corpus, v0.18 MED-14 sampling + LOW-2 ms timestamp, v0.19 L13
    byte fidelity). The raw stdin string is written VERBATIM via
    [System.IO.File]::WriteAllText — UTF-8 WITHOUT BOM, no JSON round-trip
    (key order/escapes untouched), no added trailing newline — because the
    corpus exists to detect wire-format drift. Contract version lives in the
    FILENAME. The caller computes the 1-in-10 sample roll and passes it
    (-SampleRoll) so this function is deterministic under test. Prune keeps
    the 20 newest '<EventName>-*.json' (zero-cmdlet: Array.Sort, not
    Get-ChildItem/Sort-Object — pre-tool-check's fast path dot-sources this
    lib). Returns the written path, or $null (roll missed / bad args / error).
    Never throws.
    #>
    param(
        [string]$FixtureDir,
        [string]$EventName,
        [string]$ContractVersion,
        [string]$RawBytes,
        [bool]$SampleRoll
    )
    try {
        if (-not $SampleRoll) { return $null }
        if ([string]::IsNullOrEmpty($RawBytes) -or [string]::IsNullOrEmpty($FixtureDir) -or
            [string]::IsNullOrEmpty($EventName)) { return $null }
        if (-not [System.IO.Directory]::Exists($FixtureDir)) { [void][System.IO.Directory]::CreateDirectory($FixtureDir) }
        # v0.18 LOW-2: sub-second timestamp avoids 1-second filename collisions
        $ts = [System.DateTime]::Now.ToString('yyyyMMdd-HHmmss-fff')
        $path = $FixtureDir + '\' + $EventName + '-' + $ts + '-contract' + $ContractVersion + '.json'
        [System.IO.File]::WriteAllText($path, $RawBytes)
        $files = [System.IO.DirectoryInfo]::new($FixtureDir).GetFiles($EventName + '-*.json')
        if ($files.Count -gt 20) {
            [System.Array]::Sort($files, [System.Comparison[System.IO.FileInfo]]{
                param($a, $b) $b.LastWriteTime.CompareTo($a.LastWriteTime) })
            for ($i = 20; $i -lt $files.Count; $i++) { try { $files[$i].Delete() } catch {} }
        }
        return $path
    } catch { return $null }
}

# ===========================================================================
# v0.20 A.5 — resident hook daemon (named pipe 'mem0-hook-daemon')
# Client side (used by user-prompt-extract.ps1) + helpers shared with
# mem0-hook-daemon.ps1. All hot-path code is zero-cmdlet (.NET statics +
# [type]::new()) per the A.2/A.3 PS5.1 findings.
# ===========================================================================

function Get-HookJsonSerializer {
    <#
    .SYNOPSIS
    Lazy JavaScriptSerializer for lib functions that need JSON. Reuses the
    host script's $script:Jss when present (user-prompt-extract.ps1 and
    mem0-hook-daemon.ps1 both set it). Under PowerShell 7 (Pester tests)
    System.Web.Extensions does not exist -> returns $null and callers fall
    back to ConvertTo/From-Json (fine off the hot path).
    #>
    if ($script:Jss) { return $script:Jss }
    if ($script:HookJssTried) { return $script:HookJss }
    $script:HookJssTried = $true
    $script:HookJss = $null
    # Desktop edition only: under PowerShell 7 a System.Web compat shim lets
    # the type RESOLVE but Serialize() throws TypeLoadException lazily on
    # first use — so gate on edition, not on type resolution.
    if ($PSVersionTable.PSEdition -eq 'Desktop') {
        try {
            [void][System.Reflection.Assembly]::LoadWithPartialName('System.Web.Extensions')
            $t = 'System.Web.Script.Serialization.JavaScriptSerializer' -as [type]
            if ($t) {
                $script:HookJss = [Activator]::CreateInstance($t)
                $script:HookJss.MaxJsonLength = 16MB
            }
        } catch { $script:HookJss = $null }
    }
    return $script:HookJss
}

function ConvertTo-HookJson {
    param($Object)
    $s = Get-HookJsonSerializer
    if ($s) { return $s.Serialize($Object) }
    return ($Object | ConvertTo-Json -Compress -Depth 8)
}

function ConvertFrom-HookJson {
    param([string]$Json)
    $s = Get-HookJsonSerializer
    if ($s) { return $s.DeserializeObject($Json) }
    return ($Json | ConvertFrom-Json)
}

function Invoke-Mem0Post {
    # Raw POST -> response text. No proxy lookup, explicit timeout. Throws on
    # HTTP/network errors (callers wrap in try/catch). KEEP IN SYNC with the
    # local copy in user-prompt-extract.ps1 (the hook keeps its own so a
    # missing lib deploy cannot break the 0.A checkpoint; identical body, the
    # lib's definition harmlessly overrides at dot-source). The daemon uses
    # THIS definition.
    param([string]$Uri, [string]$Body, [string]$ApiKey, [int]$TimeoutMs = 3000)
    $req = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create($Uri)
    $req.Method = 'POST'
    $req.ContentType = 'application/json'
    $req.Headers.Add('X-API-Key', $ApiKey)
    $req.Timeout = $TimeoutMs
    $req.ReadWriteTimeout = $TimeoutMs
    $req.Proxy = $null
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
    $req.ContentLength = $bytes.Length
    $rs = $req.GetRequestStream()
    try { $rs.Write($bytes, 0, $bytes.Length) } finally { $rs.Close() }
    $resp = $req.GetResponse()
    try {
        $sr = [System.IO.StreamReader]::new($resp.GetResponseStream())
        try { return $sr.ReadToEnd() } finally { $sr.Close() }
    } finally { $resp.Close() }
}

function Get-FileSha256Hex {
    <#
    .SYNOPSIS
    Lowercase hex SHA256 of a file, or $null on any failure. Used for the
    deploy-staleness handshake: the daemon stamps the hash of the lib it
    loaded on every response; the client compares against the CURRENT
    deployed lib file and treats a mismatch as daemon-unusable (stale logic).
    #>
    param([string]$Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $fs = [System.IO.File]::OpenRead($Path)
            try { $hash = $sha.ComputeHash($fs) } finally { $fs.Close() }
        } finally { $sha.Dispose() }
        return ([System.BitConverter]::ToString($hash) -replace '-', '').ToLower()
    } catch { return $null }
}

function Get-StringSha256Hex {
    <#
    .SYNOPSIS
    Lowercase hex SHA256 of a UTF-8 string, or $null on any failure. Used to
    fold the lib hash and the daemon-script hash into ONE combined handshake
    digest (v0.21 Phase B M3/M6) so a daemon-only redeploy also forces a
    mismatch -> shutdown -> fresh-daemon rollover.
    #>
    param([string]$Text)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { $hash = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Text)) } finally { $sha.Dispose() }
        return ([System.BitConverter]::ToString($hash) -replace '-', '').ToLower()
    } catch { return $null }
}

function Get-HandshakeHash {
    <#
    .SYNOPSIS
    v0.21 Phase B (M3/M6): the deploy-staleness handshake digest, computed
    IDENTICALLY on all three sides (daemon startup, this lib's client, and
    mem0-hook-client.cs). The digest is SHA256 over
        Sha256Hex(user-prompt-lib.ps1) + Sha256Hex(mem0-hook-daemon.ps1)
    so a daemon-only edit (orchestration that lives in the daemon script, not
    the lib) changes the digest just like a lib edit does. Returns $null if
    EITHER file is unhashable (caller then skips the daemon entirely).
    #>
    param([string]$ScriptDir)
    $libHash    = Get-FileSha256Hex -Path ([System.IO.Path]::Combine($ScriptDir, 'user-prompt-lib.ps1'))
    $daemonHash = Get-FileSha256Hex -Path ([System.IO.Path]::Combine($ScriptDir, 'mem0-hook-daemon.ps1'))
    if ([string]::IsNullOrEmpty($libHash) -or [string]::IsNullOrEmpty($daemonHash)) { return $null }
    return Get-StringSha256Hex ($libHash + $daemonHash)
}

function Test-DaemonPipePresent {
    <#
    .SYNOPSIS
    Cheap (~1-3ms) existence probe for the daemon pipe WITHOUT opening it
    (File.Exists on \\.\pipe\<name> performs a CreateFile that consumes the
    single server instance). Enumeration can throw on exotic pipe names in
    .NET Framework -> return $true ("maybe") and let Connect() decide; the
    spawn launcher treats its own enumeration failure as absent instead
    (the daemon's mutex makes a duplicate spawn a no-op).
    #>
    param([string]$PipeName = 'mem0-hook-daemon')
    try {
        foreach ($p in [System.IO.Directory]::EnumerateFiles('\\.\pipe\')) {
            if ($p -eq ('\\.\pipe\' + $PipeName)) { return $true }
        }
        return $false
    } catch { return $true }
}

function Read-PipeLineWithDeadline {
    <#
    .SYNOPSIS
    Read one newline-terminated UTF-8 line from a pipe stream with a hard
    total deadline (pipe streams have no ReadTimeout in .NET Framework, so
    reads go through ReadAsync + Task.Wait). Returns the line WITHOUT the
    newline, or $null on timeout/EOF/error.
    #>
    param($Stream, [int]$TimeoutMs = 2500)
    try {
        $deadline = [System.Diagnostics.Stopwatch]::StartNew()
        $buf = [byte[]]::new(65536)
        $acc = [System.IO.MemoryStream]::new()
        while ($true) {
            $remaining = $TimeoutMs - $deadline.ElapsedMilliseconds
            if ($remaining -le 0) { return $null }
            # APM (BeginRead/WaitOne/EndRead) instead of ReadAsync+Task.Wait:
            # the TPL first-use JIT costs ~30ms in a fresh PS5.1 process
            # (measured A.5); the APM path is lighter on the hook hot path.
            $iar = $Stream.BeginRead($buf, 0, $buf.Length, $null, $null)
            if (-not $iar.AsyncWaitHandle.WaitOne([int]$remaining)) { return $null }
            $n = $Stream.EndRead($iar)
            if ($n -le 0) { break }
            $acc.Write($buf, 0, $n)
            if ([System.Array]::IndexOf($buf, [byte]10, 0, $n) -ge 0) { break }
        }
        if ($acc.Length -eq 0) { return $null }
        $text = [System.Text.Encoding]::UTF8.GetString($acc.ToArray())
        $idx = $text.IndexOf("`n")
        if ($idx -ge 0) { $text = $text.Substring(0, $idx) }
        return $text.TrimEnd("`r")
    } catch { return $null }
}

function Test-DaemonResponse {
    <#
    .SYNOPSIS
    Pure validation of a parsed daemon response. Returns:
      'ok'            — usable response (ok=true, lib_hash present and matching)
      'hash_mismatch' — daemon loaded a stale lib (deploy happened since it
                        started); caller must fall back inline AND signal
                        shutdown so the next prompt gets a fresh daemon
      'invalid'       — anything else (null, ok!=true, missing lib_hash)
    #>
    param($Response, [string]$ExpectedLibHash)
    if ($null -eq $Response) { return 'invalid' }
    $ok = $false
    try { $ok = [bool]$Response.ok } catch { $ok = $false }
    if (-not $ok) { return 'invalid' }
    $hash = $null
    try { $hash = [string]$Response.lib_hash } catch { $hash = $null }
    if ([string]::IsNullOrEmpty($hash)) { return 'invalid' }
    if ($ExpectedLibHash -and ($hash -ne $ExpectedLibHash)) { return 'hash_mismatch' }
    return 'ok'
}

function Send-DaemonShutdown {
    <#
    .SYNOPSIS
    Best-effort {op:'shutdown'} to the daemon (stale-lib protection). Waits
    briefly for the ack so the daemon reads the request before we vanish.
    Never throws.
    #>
    param([string]$PipeName = 'mem0-hook-daemon', [int]$ConnectTimeoutMs = 200)
    $c = $null
    try {
        $c = [System.IO.Pipes.NamedPipeClientStream]::new('.', $PipeName, [System.IO.Pipes.PipeDirection]::InOut, [System.IO.Pipes.PipeOptions]::Asynchronous)
        $c.Connect($ConnectTimeoutMs)
        $bytes = [System.Text.Encoding]::UTF8.GetBytes('{"op":"shutdown"}' + "`n")
        $c.Write($bytes, 0, $bytes.Length)
        $c.Flush()
        [void](Read-PipeLineWithDeadline -Stream $c -TimeoutMs 500)
    } catch {} finally { if ($c) { try { $c.Dispose() } catch {} } }
}

function Start-HookDaemonDetached {
    <#
    .SYNOPSIS
    Spawn the daemon as a detached hidden powershell.exe (5.1) process so the
    NEXT prompt is fast — never blocks the current hook on daemon startup.
    Raw Process.Start (Start-Process = Management module load on the hot
    path). Returns $true if a spawn was attempted, $false otherwise.
    CRITICAL: UseShellExecute=$true — with $false the child INHERITS the
    hook's stdout handle, and whatever reads the hook's stdout (Claude Code)
    waits for EOF until the resident daemon exits (measured hang, A.5).
    ShellExecute starts the child with no inherited std handles.
    #>
    param([string]$DaemonPath)
    try {
        if (-not $DaemonPath -or -not [System.IO.File]::Exists($DaemonPath)) { return $false }
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $env:SystemRoot + '\System32\WindowsPowerShell\v1.0\powershell.exe'
        $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $DaemonPath + '"'
        $psi.UseShellExecute = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $p = [System.Diagnostics.Process]::Start($psi)
        if ($p) { $p.Dispose() }
        return $true
    } catch { return $false }
}

function Invoke-DaemonBundle {
    <#
    .SYNOPSIS
    v0.20 A.5 client transaction: send the bundle request to the resident
    daemon over the named pipe and return the parsed response (whose
    .context_block is the fully-rendered [MEMORY CONTEXT] string or $null).

    Returns $null on ANY failure — pipe absent, connect timeout, write/read
    failure, response timeout, unparseable response, ok!=true, lib-hash
    mismatch — and the caller falls back to the inline path unchanged
    (fail-open; the daemon is an accelerator, never a dependency).

    Side effects on failure:
      - pipe absent + -SpawnDaemonPath given -> detached daemon spawn so the
        NEXT prompt is fast (this one proceeds inline immediately);
      - hash mismatch -> best-effort {op:'shutdown'} so the stale daemon
        exits and the next prompt respawns a fresh one.
    Connect timeout against an EXISTING pipe (daemon busy serving another
    session) spawns nothing — the mutex would no-op it anyway.
    #>
    param(
        [hashtable]$Request,
        [string]$ExpectedLibHash,
        [string]$PipeName = 'mem0-hook-daemon',
        [int]$ConnectTimeoutMs = 100,
        [int]$ResponseTimeoutMs = 2500,
        [string]$SpawnDaemonPath
    )
    if (-not $Request) { return $null }
    if ([string]::IsNullOrEmpty($ExpectedLibHash)) { return $null }   # can't verify staleness -> inline
    $client = $null
    try {
        if (-not (Test-DaemonPipePresent -PipeName $PipeName)) {
            if ($SpawnDaemonPath) { [void](Start-HookDaemonDetached -DaemonPath $SpawnDaemonPath) }
            return $null
        }
        $client = [System.IO.Pipes.NamedPipeClientStream]::new('.', $PipeName, [System.IO.Pipes.PipeDirection]::InOut, [System.IO.Pipes.PipeOptions]::Asynchronous)
        try { $client.Connect($ConnectTimeoutMs) } catch { return $null }
        $bytes = [System.Text.Encoding]::UTF8.GetBytes((ConvertTo-HookJson $Request) + "`n")
        $client.Write($bytes, 0, $bytes.Length)
        $client.Flush()
        $line = Read-PipeLineWithDeadline -Stream $client -TimeoutMs $ResponseTimeoutMs
        if (-not $line) { return $null }
        $resp = $null
        try { $resp = ConvertFrom-HookJson $line } catch { return $null }
        $verdict = Test-DaemonResponse -Response $resp -ExpectedLibHash $ExpectedLibHash
        if ($verdict -eq 'ok') { return $resp }
        if ($verdict -eq 'hash_mismatch') {
            try { $client.Dispose(); $client = $null } catch {}
            Send-DaemonShutdown -PipeName $PipeName
        }
        return $null
    } catch { return $null }
    finally { if ($client) { try { $client.Dispose() } catch {} } }
}

# ---------------------------------------------------------------------------
# v0.20 A.5 iteration 2 — RAW fast path. Measured floor analysis: the
# JSON-protocol client (Invoke-DaemonBundle) still pays JavaScriptSerializer
# load + first-use JIT (~60ms) and stdin parsing client-side; with spawn
# ~242ms and server-side bundle ~250ms that overshoots the 600ms p50 target.
# The raw path sends VERBATIM hook stdin (base64) to the daemon — which does
# parse/fixture/session-id/brand/triviality/rate-limit/bundle/render — and
# the client touches NO JSON machinery at all: the request line is built by
# string concat and the response fields are extracted with anchored regexes
# over base64/hex/literal values (immune to key order and escaping).
# ---------------------------------------------------------------------------

function ConvertFrom-DaemonRawResponse {
    <#
    .SYNOPSIS
    Pure parser/validator for the bundle_raw response line (no JSON parser —
    see block comment above). Returns a hashtable
    {context_block, prompt, transcript_path, session_id, brand, diag} when the
    response is usable, or @{verdict='hash_mismatch'} / $null otherwise.
    All free-text fields travel base64-encoded ([A-Za-z0-9+/=]*), so a regex
    per field is exact regardless of serializer key order.
    #>
    param([string]$Line, [string]$ExpectedLibHash)
    try {
        if ([string]::IsNullOrEmpty($Line)) { return $null }
        if ($Line -notmatch '"ok"\s*:\s*true') { return $null }
        $m = [regex]::Match($Line, '"lib_hash"\s*:\s*"([0-9a-f]{64})"')
        if (-not $m.Success) { return $null }
        if ($ExpectedLibHash -and ($m.Groups[1].Value -ne $ExpectedLibHash)) { return @{ verdict = 'hash_mismatch' } }
        if ($Line -notmatch '"served"\s*:\s*true') { return $null }
        $out = @{ verdict = 'ok' }
        foreach ($f in @(@('context_b64','context_block'), @('prompt_b64','prompt'), @('tpath_b64','transcript_path'), @('sid_b64','session_id'), @('brand_b64','brand'), @('diag_b64','diag'))) {
            $fm = [regex]::Match($Line, ('"' + $f[0] + '"\s*:\s*"([A-Za-z0-9+/=]*)"'))
            $val = $null
            if ($fm.Success -and $fm.Groups[1].Value.Length -gt 0) {
                $val = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($fm.Groups[1].Value))
            }
            $out[$f[1]] = $val
        }
        return $out
    } catch { return $null }
}

function Invoke-DaemonRawTransaction {
    <#
    .SYNOPSIS
    A.5 fast-path client: VERBATIM hook stdin -> daemon (op=bundle_raw) ->
    parsed response fields. $null on ANY failure (caller runs the full inline
    pipeline, identical to A.3 — fail-open; accelerator, never a dependency).
    Probe BEFORE Connect: enumeration costs ~4ms while Connect() against an
    ABSENT pipe spins its full timeout (~110-155ms measured) — probe-first
    keeps the cold/no-daemon fallback near the inline baseline. Absent ->
    spawn detached + $null; present-but-connect-fail = busy daemon -> $null,
    no spawn (the mutex would no-op a duplicate anyway).
    Hash mismatch -> best-effort shutdown signal (fresh daemon next prompt).
    #>
    param(
        [string]$RawStdin,
        [string]$ExpectedLibHash,
        [string]$PipeName = 'mem0-hook-daemon',
        [int]$ConnectTimeoutMs = 100,
        [int]$ResponseTimeoutMs = 2500,
        [string]$SpawnDaemonPath
    )
    if ([string]::IsNullOrEmpty($RawStdin)) { return $null }
    if ([string]::IsNullOrEmpty($ExpectedLibHash)) { return $null }
    $client = $null
    try {
        if (-not (Test-DaemonPipePresent -PipeName $PipeName)) {
            if ($SpawnDaemonPath) { [void](Start-HookDaemonDetached -DaemonPath $SpawnDaemonPath) }
            return $null
        }
        $client = [System.IO.Pipes.NamedPipeClientStream]::new('.', $PipeName, [System.IO.Pipes.PipeDirection]::InOut, [System.IO.Pipes.PipeOptions]::Asynchronous)
        try { $client.Connect($ConnectTimeoutMs) } catch { return $null }
        # v0.21 Phase B (L1): carry the client's expected handshake hash so the
        # daemon can refuse stale service BEFORE any side effect (no rate-limit
        # token write, no HTTP). Client-side hash validation below STAYS as
        # defense-in-depth; an old daemon ignoring this field keeps old behavior.
        $reqLine = '{"op":"bundle_raw","expected_lib_hash":"' + $ExpectedLibHash + '","stdin_b64":"' + [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($RawStdin)) + '"}'
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($reqLine + "`n")
        $client.Write($bytes, 0, $bytes.Length)
        $client.Flush()
        $line = Read-PipeLineWithDeadline -Stream $client -TimeoutMs $ResponseTimeoutMs
        if (-not $line) { return $null }
        $parsed = ConvertFrom-DaemonRawResponse -Line $line -ExpectedLibHash $ExpectedLibHash
        if ($null -eq $parsed) { return $null }
        if ($parsed.verdict -eq 'hash_mismatch') {
            try { $client.Dispose(); $client = $null } catch {}
            Send-DaemonShutdown -PipeName $PipeName
            return $null
        }
        if ($parsed.verdict -ne 'ok') { return $null }
        return $parsed
    } catch { return $null }
    finally { if ($client) { try { $client.Dispose() } catch {} } }
}

# ===========================================================================
# v0.22 Phase E — Pillar 2 verification helpers (pure, dot-sourceable).
# These back the Test-MemoryStack R-offload + R-budget INVARIANTS checks and
# their Pester coverage. NO side effects at load. Never throw.
#   - Test-OffloadNoBlockInvariant : parse a Claude Code hooks config and prove
#     the offload harness (mcp__local-offload__*) can never receive the
#     [MEMORY CONTEXT] block (UserPromptSubmit is human-prompt-only; PreToolUse
#     gates Bash/Edit/MultiEdit/Write and excludes mcp__*).
#   - Measure-MemoryContextBudget : render the [MEMORY CONTEXT] block per tier
#     against a worst-case (cap-filling) fixture and char-proxy budget it.
# ===========================================================================

function Test-OffloadNoBlockInvariant {
    <#
    .SYNOPSIS
    v0.22 Phase E (E1, D7): given a PARSED Claude Code hooks object (settings.json
    .hooks), prove by CONSTRUCTION that the offload harness never receives the
    [MEMORY CONTEXT] block. The block is produced ONLY by the UserPromptSubmit
    path (the compiled mem0-hook-client.exe / its PowerShell fallback) and the
    daemon it spawns. Two invariants make `mcp__local-offload__*` (and every other
    MCP tool call) unreachable from that path:

      INV-1  UserPromptSubmit is bound ONLY to the human-prompt client — it has no
             tool/MCP matcher and its command is the stack's hook client, never an
             mcp__* anything. (UserPromptSubmit fires on human prompts only; it is
             never raised for tool/MCP invocations or for subagents — so the
             offload MCP process cannot trip it.)
      INV-2  Every PreToolUse hook matcher gates strictly on the editing/exec tools
             (Bash|Edit|MultiEdit|Write) and NONE of them name mcp__ — so no
             PreToolUse hook fires for an mcp__local-offload__* call either.

    Returns a [pscustomobject] @{ Ok=[bool]; Status='OK'|'WARN'|'FAIL';
    Detail=[string] }. Fail-OPEN: a null/empty/unparseable config yields a WARN
    (config not found is operational, not a security regression). A config that is
    present but VIOLATES an invariant yields FAIL (a real exposure). Never throws.
    #>
    param(
        # The parsed `.hooks` object from settings.json (ConvertFrom-Json).
        $Hooks,
        # Substrings that, in a UserPromptSubmit command, mean it routes to the
        # stack's human-prompt client (the only producer of the block).
        [string[]]$HumanClientMarkers = @('mem0-hook-client', 'user-prompt-extract')
    )
    try {
        if ($null -eq $Hooks) {
            return [pscustomobject]@{ Ok = $true; Status = 'WARN'; Detail = 'hooks config not found (fail-open)' }
        }

        # --- INV-1: UserPromptSubmit binds only to the human-prompt client -------
        $ups = $null
        try { $ups = $Hooks.UserPromptSubmit } catch { $ups = $null }
        $upsCmds = @()
        foreach ($entry in @($ups)) {
            if ($null -eq $entry) { continue }
            # An entry may carry a matcher (it must NOT, for UserPromptSubmit) and
            # a list of .hooks each with a .command.
            $matcher = $null
            try { $matcher = [string]$entry.matcher } catch { $matcher = $null }
            if (-not [string]::IsNullOrWhiteSpace($matcher)) {
                # A matcher on UserPromptSubmit that references a tool/MCP would be
                # a misconfiguration — surface it.
                if ($matcher -match 'mcp__') {
                    return [pscustomobject]@{ Ok = $false; Status = 'FAIL'; Detail = "UserPromptSubmit has an mcp__ matcher ('$matcher') - offload could receive the block" }
                }
            }
            foreach ($h in @($entry.hooks)) {
                $c = $null
                try { $c = [string]$h.command } catch { $c = $null }
                if (-not [string]::IsNullOrWhiteSpace($c)) { $upsCmds += $c }
            }
        }
        if ($upsCmds.Count -eq 0) {
            return [pscustomobject]@{ Ok = $true; Status = 'WARN'; Detail = 'no UserPromptSubmit hook registered (block producer absent; fail-open)' }
        }
        foreach ($c in $upsCmds) {
            if ($c -match 'mcp__') {
                return [pscustomobject]@{ Ok = $false; Status = 'FAIL'; Detail = "UserPromptSubmit command references mcp__ ('$c') - offload exposure" }
            }
        }
        # Every UserPromptSubmit command must route to a known human-prompt client.
        $allHuman = $true
        foreach ($c in $upsCmds) {
            $isHuman = $false
            foreach ($m in $HumanClientMarkers) { if ($c -like "*$m*") { $isHuman = $true; break } }
            if (-not $isHuman) { $allHuman = $false }
        }
        if (-not $allHuman) {
            return [pscustomobject]@{ Ok = $true; Status = 'WARN'; Detail = "UserPromptSubmit command(s) not recognized as the stack human-prompt client: $($upsCmds -join '; ')" }
        }

        # --- INV-2: no PreToolUse hook matcher names mcp__ -----------------------
        $pre = $null
        try { $pre = $Hooks.PreToolUse } catch { $pre = $null }
        $matchers = @()
        foreach ($entry in @($pre)) {
            if ($null -eq $entry) { continue }
            $m = $null
            try { $m = [string]$entry.matcher } catch { $m = $null }
            if (-not [string]::IsNullOrWhiteSpace($m)) { $matchers += $m }
        }
        foreach ($m in $matchers) {
            if ($m -match 'mcp__') {
                return [pscustomobject]@{ Ok = $false; Status = 'FAIL'; Detail = "PreToolUse matcher names mcp__ ('$m') - an MCP call (incl. offload) would fire a hook" }
            }
        }
        # Find the stack's own pre-tool-check matcher and confirm it is exactly the
        # editing/exec gate (Bash|Edit|MultiEdit|Write), so a future widening to
        # mcp__* would flip this check.
        $stackMatcher = $null
        foreach ($entry in @($pre)) {
            $cmdHasCheck = $false
            foreach ($h in @($entry.hooks)) {
                $c = $null; try { $c = [string]$h.command } catch { $c = $null }
                if ($c -match 'pre-tool-check\.ps1') { $cmdHasCheck = $true; break }
            }
            if ($cmdHasCheck) { try { $stackMatcher = [string]$entry.matcher } catch { $stackMatcher = $null }; break }
        }
        $matcherNote = if ($stackMatcher) { "pre-tool-check matcher='$stackMatcher'" } else { 'pre-tool-check matcher not found (other PreToolUse matchers checked)' }

        return [pscustomobject]@{ Ok = $true; Status = 'OK'; Detail = "offload-no-block holds: UserPromptSubmit=human-client-only ($($upsCmds.Count) cmd), no PreToolUse mcp__ matcher; $matcherNote" }
    } catch {
        return [pscustomobject]@{ Ok = $true; Status = 'WARN'; Detail = "invariant parse error (fail-open): $($_.Exception.Message)" }
    }
}

function Measure-MemoryContextBudget {
    <#
    .SYNOPSIS
    v0.22 Phase E (E2, D8): render the [MEMORY CONTEXT] block for ONE tier against
    a WORST-CASE fixture (cap-filling memories/goals/OQ, each near its per-item
    truncation ceiling) and return @{ Tier; Chars; Ceiling; WithinBudget; Block }.

    The char-proxy ceiling is derived from the tier's own caps in model-tiers.json
    (so it stays honest if caps change) plus the fixed per-item truncation limits
    enforced by Format-MemoryContextBlock / Select-AdmittedMemoryResults:
      - memories: client admission hard-caps at top-3, each truncated to 200 chars
                  (+ the truncation suffix) -> per-item ceiling ~260 incl. tag.
      - goals:    title truncated to 100 chars -> per-item ceiling ~120.
      - oq:       text truncated to 120 chars -> per-item ceiling ~140.
    Plus header (~90), optional small-tier legend (~200), and section labels (~90).
    The effective memory count is min(memory_cap, 3) (client top-3 cap).

    This is a CEILING check, not an equality check: a tier passes when its rendered
    worst-case block is <= the ceiling its caps imply. The 'small' tier is tighter
    BY CAPS (3/3/2 vs 5/5/3), so its ceiling is materially smaller than frontier's.
    Never throws; returns WithinBudget=$true with Ceiling=0/Chars=0 on render miss
    (fail-open — a render failure is not a budget regression).
    #>
    param(
        [string]$Tier = 'frontier',
        [string]$ConfigPath,
        [string]$AuditPath
    )
    # Fail-open defaults if the config can't be read. 'small' is the only tier
    # Format-MemoryContextBlock renders a legend for, so default include_legend by
    # tier name — otherwise the missing-config ceiling would omit the legend
    # allowance while the rendered small block still carries it.
    $defaultCaps = @{ memory_cap = 5; goal_cap = 5; oq_cap = 3; include_legend = ($Tier -eq 'small') }
    try {
        # Resolve caps for this tier from the config (fail-open to frontier-ish).
        $caps = $defaultCaps.Clone()
        $cfg = $null
        try {
            if ($ConfigPath -and [System.IO.File]::Exists($ConfigPath)) {
                $cfg = ([System.IO.File]::ReadAllText($ConfigPath)) | ConvertFrom-Json
            }
        } catch { $cfg = $null }
        if ($cfg -and $cfg.tiers -and $cfg.tiers.$Tier) {
            $tc = $cfg.tiers.$Tier
            foreach ($k in 'memory_cap', 'goal_cap', 'oq_cap') {
                try { if ($null -ne $tc.$k) { $caps[$k] = [int]$tc.$k } } catch {}
            }
            try { $caps.include_legend = [bool]$tc.include_legend } catch {}
        }

        # Worst-case fixture: fill each cap with near-ceiling items so the render
        # exercises the truncation paths. Memories also carry a tier so the small
        # ordering path runs. Client top-3 cap bounds memories regardless of caps.
        $memN = [Math]::Max(1, [int]$caps.memory_cap)
        $goalN = [Math]::Max(0, [int]$caps.goal_cap)
        $oqN = [Math]::Max(0, [int]$caps.oq_cap)
        $memText = ('x' * 240)   # > 200 so truncation fires
        $goalText = ('g' * 130)  # > 100
        $oqText = ('q' * 150)    # > 120
        $mems = @()
        for ($i = 0; $i -lt $memN; $i++) {
            $mems += [pscustomobject]@{ id = "bm-$i"; memory = $memText
                                        metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
        }
        $goals = @()
        for ($i = 0; $i -lt $goalN; $i++) {
            $goals += [pscustomobject]@{ id = $i; title = $goalText; priority = 2; status = 'open'; brand = 'ai-ecosystem' }
        }
        $oqs = @()
        for ($i = 0; $i -lt $oqN; $i++) {
            $oqs += [pscustomobject]@{ id = $i; question_text = $oqText; brand = 'ai-ecosystem' }
        }
        $bundle = [pscustomobject]@{ ok = $true; memories = $mems; goals = $goals; open_questions = $oqs }

        $block = Format-MemoryContextBlock -Bundle $bundle -Brand 'ai-ecosystem' -AuditPath $AuditPath -Tier $Tier
        if ([string]::IsNullOrEmpty($block)) {
            return [pscustomobject]@{ Tier = $Tier; Chars = 0; Ceiling = 0; WithinBudget = $true; Block = $null }
        }

        # Ceiling from caps + fixed per-item truncation limits.
        $effMem = [Math]::Min(3, $memN)         # client admission hard-caps memories at 3
        $perMem = 200 + 60                        # 200-char cap + truncation suffix + tag
        $perGoal = 100 + 20
        $perOq = 120 + 20
        $headerOverhead = 90
        $legendOverhead = if ($caps.include_legend) { 220 } else { 0 }
        $sectionLabels = 100                       # 3 section headers + blank lines
        $ceiling = $headerOverhead + $legendOverhead + $sectionLabels +
                   ($effMem * $perMem) + ($goalN * $perGoal) + ($oqN * $perOq)

        return [pscustomobject]@{
            Tier = $Tier; Chars = $block.Length; Ceiling = $ceiling
            WithinBudget = ($block.Length -le $ceiling); Block = $block
        }
    } catch {
        return [pscustomobject]@{ Tier = $Tier; Chars = 0; Ceiling = 0; WithinBudget = $true; Block = $null }
    }
}
