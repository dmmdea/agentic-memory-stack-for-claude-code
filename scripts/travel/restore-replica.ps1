#Requires -PSEdition Core
<#
.SYNOPSIS
  Restore the newest your-machine snapshot into the laptop's LOCAL mem0 + Qdrant as a read-only replica.

.NOTES
  Called by travel-mode.ps1 on. Idempotent — re-running refreshes the replica to a newer snapshot.

  Qdrant is restored via the SNAPSHOT UPLOAD API, not by copying the storage directory. A raw
  directory copy is version-coupled and silently corrupts across Qdrant versions; the upload API
  is the supported path.

  Embedding space: a snapshot's vectors only mean something to the model and prompt template that made
  them. The set's manifest names its profile (embed_profile; a set from before profiles has none, which
  is the default space) and the collection it holds. The restore refuses unless the WSL distro is
  configured for that profile (~/.mem0/stack.env MEM0_EMBED_PROFILE) AND the local llama-swap serves the
  profile's alias. Both come from mem0-server/embedder_profile.py as deployed in the distro; nothing here
  names a collection or model. To move a PC replica to another profile, run `install.ps1 -EmbedProfile <profile>`
  (or, inside WSL, `MEM0_SET_EMBED_PROFILE=<profile> bash install/1-wsl-services.sh <wsluser> <winuser>
  <distro>`), which records the profile and stages its model files; serve its alias on the local llama-swap;
  and restore a set made in that profile (travel-mode.ps1 on -DryRun, while online, seeds the cache with one).
  The native-Linux replica installer is not a Windows path: inside a WSL distro it would overwrite the
  receipt's distro and user names and register a second (Linux) watcher.

  Media memories (1.35.0 sets): when the manifest's files.media names a media-<ts>.tar in the backup
  directory it is extracted into the distro's media directory (the mem0 unit's MEM0_MEDIA_DIR when a drop-in
  sets one, else the login shell's, else ~/.mem0/media) additively; a failed extraction is a WARNING, with
  tar's own message, because the memories keep their captions. The tar is listed first and is not extracted
  when it holds anything but regular files and directories, or a name that is absolute or has a '..'
  component (a warning too): our backups hold nothing else.
  After mem0 starts, /health/deep must report the restored set's profile and collection.
#>
param(
    # No default ON PURPOSE: the old 'P:\memory-backups\your-machine' default pointed at pCloud's STREAMING
    # drive, which cannot serve a restore offline. travel-mode.ps1 always passes the resolved
    # (local-first) dir; a direct caller must choose deliberately.
    [Parameter(Mandatory)][string]$BackupDir,
    [Parameter(Mandatory)][string]$Stamp,
    [string]$Distro = $(if ($env:MEM0_WSL_DISTRO) { $env:MEM0_WSL_DISTRO } else { 'Ubuntu' }),
    # '' = the set's own collection (manifest collections.memories), else the profile's. A name given here is the
    # operator's: it is not compared with the collection the server binds (the restore prints a note instead).
    [string]$Collection = ''
)
$ErrorActionPreference = 'Stop'
function Wsl([string]$cmd) { wsl.exe -d $Distro -e bash -lc $cmd }

$epi  = "$BackupDir\episodic-$Stamp.db"
$hist = "$BackupDir\history-$Stamp.db"
$snap = "$BackupDir\qdrant-$Stamp.snapshot"
foreach ($f in @($epi, $hist, $snap)) { if (-not (Test-Path $f)) { throw "missing backup artifact: $f" } }

# Preconditions the hard way: jq is required by the snapshot chain (AMS issue #17), and a
# missing embedder means the replica can answer nothing.
if (-not (Wsl "command -v jq >/dev/null && echo ok")) { throw "jq is not installed in WSL ($Distro). Run: sudo apt-get install -y jq" }

# --- the set's embedding space, resolved by the profile module inside the distro ---
$manifestFile = "$BackupDir\manifest-$Stamp.json"
$mProfile = ''; $mCollection = ''; $mMedia = ''
if (Test-Path $manifestFile) {
    try {
        $mj = Get-Content -Raw $manifestFile | ConvertFrom-Json
        if ($mj.embed_profile) { $mProfile = "$($mj.embed_profile)".Trim() }
        if ($mj.collections -and $mj.collections.memories) { $mCollection = "$($mj.collections.memories)".Trim() }
        if ($mj.files -and $mj.files.media -is [string]) { $mMedia = $mj.files.media.Trim() }
    } catch { Write-Warning "manifest $manifestFile did not parse ($_); treating the set as made before embedding profiles" }
}
# The media tar's name comes from the manifest and ends up in a shell command: only a plain file name is used.
if ($mMedia -and $mMedia -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*\.tar$') {
    Write-Warning "manifest $manifestFile names an odd media file '$mMedia'; not restoring media for set $Stamp"
    $mMedia = ''
}
if ($mProfile -eq 'unknown') { throw "set $Stamp was written without a resolvable embedding profile (embed_profile: unknown); fix the Brain's backup first" }
$epScript = @'
import json, os, sys
sys.path.insert(0, os.path.expanduser("~/apps/mem0-server"))
import embedder_profile as ep
p = ep.get(ep.LEGACY_PROFILE if sys.argv[1] == "-" else sys.argv[1])
print(json.dumps({"profile": p.name, "local": ep.active().name, "alias": ep.embed_model(p),
                  "collection": ep.collection("memories", p), "base_url": ep.base_url()}))
'@
$epJson = $epScript | wsl.exe -d $Distro -e python3 - $(if ($mProfile) { $mProfile } else { '-' })
if ($LASTEXITCODE -ne 0 -or -not $epJson) {
    throw "embedder_profile.py could not be read in WSL ($Distro) (is the server deployed to ~/apps/mem0-server, and does MEM0_EMBED_PROFILE name a known profile?): $epJson"
}
$space = "$epJson" | ConvertFrom-Json
if ($space.profile -ne $space.local) {
    throw "set $Stamp is in embedding profile '$($space.profile)' but WSL ($Distro) is configured for '$($space.local)' (~/.mem0/stack.env MEM0_EMBED_PROFILE). Restoring would load vectors this replica embeds every query against in another space. Run install.ps1 -EmbedProfile $($space.profile) (or, inside WSL, MEM0_SET_EMBED_PROFILE=$($space.profile) bash install/1-wsl-services.sh <wsluser> <winuser> <distro>), which records the profile and stages the model files its alias needs. Serve '$($space.alias)' on the local llama-swap :11436 (install/llama-swap-setup.md), then re-run this restore."
}
if (-not (Wsl "curl -sf -m 5 '$($space.base_url)/models' | grep -q '`"$($space.alias)`"' && echo ok")) {
    throw "The local embedder ($($space.base_url)) does not serve '$($space.alias)', the alias of embedding profile '$($space.profile)'. The replica needs it locally or recall returns nothing (EmbeddingGemma-2 needs llama.cpp b11452 or later)."
}
$collectionGiven = [bool]$Collection   # an explicit -Collection is the operator's own name for the restore
if (-not $Collection) {
    $Collection = if ($mCollection) { $mCollection } else { "$($space.collection)" }
    if ($Collection -ne "$($space.collection)") {
        throw "set $Stamp holds collection '$Collection' but this replica's server binds '$($space.collection)' for profile '$($space.profile)' (MEM0_QDRANT_COLLECTION in ~/.mem0/stack.env). Align the two, or pass -Collection to restore under a name you will bind yourself."
    }
}
Write-Host "    embedding profile $($space.profile): alias '$($space.alias)' served locally; restoring into collection '$Collection'"

Write-Host "    stopping local mem0; starting qdrant (needed for the snapshot upload)"
Wsl "systemctl --user stop mem0.service 2>/dev/null; systemctl --user start qdrant.service 2>/dev/null; true" | Out-Null

# --- SQLite ledgers: straight copy (mem0 is stopped) ---
Write-Host "    restoring episodic + history ledgers"
$epiW  = "$(Wsl "wslpath '$($epi -replace '\\','/')'")".Trim()
$histW = "$(Wsl "wslpath '$($hist -replace '\\','/')'")".Trim()
$snapW = "$(Wsl "wslpath '$($snap -replace '\\','/')'")".Trim()
# v1.23.1: every artifact must be READABLE FROM WSL, not merely present on Windows. A streaming
# or virtual drive (pCloud's P:) passes Test-Path above but is not mounted in WSL: wslpath
# printed nothing, `cp ''` failed inside a piped command, the snapshot upload sent an empty file,
# and this script then announced the OLD collection's count as "restored". Copy such a set to a
# local drive first (travel-mode.ps1 keeps a local cache for exactly this reason).
foreach ($pair in @(@($epi, $epiW), @($hist, $histW), @($snap, $snapW))) {
    if (-not $pair[1] -or -not (Wsl "test -r '$($pair[1])' && echo ok")) {
        throw "backup artifact not readable from WSL ($Distro): $($pair[0]) -> '$($pair[1])'. A streaming/virtual drive (e.g. pCloud P:) is not mounted in WSL - copy the set to a local drive (D:\memory-backups\<host>) and pass that -BackupDir."
    }
}
$cpOut = Wsl "mkdir -p ~/.mem0 && rm -f ~/.mem0/episodic.db-shm ~/.mem0/episodic.db-wal ~/.mem0/history.db-shm ~/.mem0/history.db-wal && cp '$epiW' ~/.mem0/episodic.db && cp '$histW' ~/.mem0/history.db && echo ok"
if ("$cpOut" -notmatch '(?m)^ok\s*$') { throw "ledger copy into WSL failed: $cpOut" }

# --- Qdrant collection: snapshot UPLOAD (version-safe) ---
Write-Host "    restoring Qdrant collection '$Collection' via snapshot upload"
# Bounded wait (2 min) — an `until` loop with no cap hangs travel-mode 'on' forever if qdrant is broken
Wsl 'for i in $(seq 1 60); do curl -sf -m 3 http://127.0.0.1:6333/healthz >/dev/null && exit 0; sleep 2; done; echo QDRANT_TIMEOUT; exit 1' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "local qdrant did not come up within 2 minutes — check: wsl -d $Distro systemctl --user status qdrant.service" }
$out = Wsl "curl -s -m 900 -X POST 'http://127.0.0.1:6333/collections/$Collection/snapshots/upload?priority=snapshot' -H 'Content-Type: multipart/form-data' -F 'snapshot=@$snapW'"
if ($out -notmatch '"status"\s*:\s*"ok"') { throw "Qdrant snapshot upload failed: $out" }

# --- media memories' files (1.35.0 sets list them as files.media): additive, and only a WARNING when they fail ---
# The memories keep their captions and vectors without the files; what is lost is opening the file. The names
# are content addresses, so an existing name already holds the same bytes: --skip-old-files never rewrites one.
# A set's tar is looked at before it is extracted. stack-backup.sh tars the media directory, so a set holds regular
# files and directories under relative names and nothing else, while the backup directory is a synced folder and
# tar extracts whatever it is given: a link entry plants a link that points outside the media directory (GNU tar
# 1.35 will not write a file through it, but the link is created and the server could later read through it), and
# an absolute or '..' name has no place in a tar of one directory. Any other entry refuses the whole tar. tar's
# own diagnostics (lines starting 'tar:') are not entries.
function Get-MediaTarProblem([string]$tarW) {
    $list = @(Wsl "tar -tvf '$tarW' 2>&1")
    if ($LASTEXITCODE -ne 0) {
        return "tar could not list it ($(($list | ForEach-Object { "$_".Trim() } | Where-Object { $_ }) -join ' '))"
    }
    foreach ($entry in $list) {
        $line = "$entry".TrimEnd("`r")
        if (-not $line.Trim() -or $line -match '^tar: ') { continue }
        if ($line.Substring(0, 1) -cnotin '-', 'd') { return "it holds an entry that is not a regular file or directory: $line" }
        # 'mode owner/group size date time name'; a line this does not fit is not trusted
        if ($line -match '^\S+\s+\S+\s+\S+\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+(?<name>.+)$') { $name = $Matches['name'] }
        else { return "tar listed an entry this script cannot read: $line" }
        if ($name -match '^/|(^|/)\.\.(/|$)') { return "it holds an entry with an absolute or '..' name: $line" }
    }
    return ''
}
if ($mMedia) {
    $mediaTar = Join-Path $BackupDir $mMedia
    if (-not (Test-Path -LiteralPath $mediaTar)) {
        Write-Warning "the set lists $mMedia but it is not in $BackupDir; media memories will answer without their files"
    } else {
        $mediaW = "$(Wsl "wslpath '$($mediaTar -replace '\\','/')'")".Trim()
        if (-not $mediaW -or -not (Wsl "test -r '$mediaW' && echo ok")) {
            Write-Warning "$mMedia is not readable from WSL ($Distro) ($mediaTar); media memories will answer without their files. Copy the set to a local drive."
        } elseif ($mediaProblem = Get-MediaTarProblem $mediaW) {
            Write-Warning "not extracting $mMedia into the distro's media directory ($mediaProblem); the set's media memories keep their captions but their files are missing"
        } else {
            # The server (mem0-server/media.py) reads MEM0_MEDIA_DIR from its own process environment and nothing
            # else: no unit file and no stack.env line sets it, so a relocated directory can only live in an
            # Environment= line of a mem0.service drop-in. Ask systemd for the unit's environment, then this login
            # shell's (what restore-replica.sh and stack-backup.sh read), else the server's default. The group's
            # stderr rides along on stdout, or a failed tar would leave the warning below with no reason in it.
            $mediaCmd = (@'
{ d=$(systemctl --user show mem0.service -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^MEM0_MEDIA_DIR=//p' | head -n1 | tr -d '\r')
[ -n "$d" ] || d="${MEM0_MEDIA_DIR:-$HOME/.mem0/media}"
mkdir -p "$d" && tar -C "$d" --skip-old-files --no-same-owner -xf '__TAR__' && echo "media-ok $(find "$d" -type f ! -name '*.tmp' | wc -l)"
} 2>&1
'@ -replace '\r?\n', '; ').Replace('__TAR__', $mediaW)
            # Joined by newlines, like $deepRaw below: a bare "$(...)" joins the lines with a space, and a line
            # before media-ok (tar's warning, a login-shell banner) would then keep ^media-ok from matching at all.
            $mediaOut = (@(Wsl $mediaCmd) -join "`n")
            if ($mediaOut -match '(?m)^media-ok\s+(\d+)') {
                Write-Host "    media: $($Matches[1]) file(s) in the distro's media directory (extracted additively from $mMedia)"
            } else {
                Write-Warning "could not extract $mMedia into the distro's media directory ($mediaOut); the set's media memories keep their captions but their files are missing"
            }
        }
    }
}

Write-Host "    starting mem0"
Wsl "systemctl --user start mem0.service && sleep 4" | Out-Null

# --- Verify: the replica must actually answer ---
$health = Wsl "curl -sf -m 10 http://127.0.0.1:18791/health"
if ($health -notmatch '"ok"\s*:\s*true') { throw "replica mem0 did not come up healthy: $health" }
# ...and bound to the profile and collection that were just restored (restore-replica.sh checks the same two
# fields). A server reports both on /health/deep; one that predates the profile report says neither and is not
# second-guessed. -s without -f: a degraded /health/deep answers 503 with its JSON, and the binding is still readable.
$deepRaw = (@(Wsl "curl -s -m 120 http://127.0.0.1:18791/health/deep") -join "`n")
$deep = $null
try { $deep = $deepRaw | ConvertFrom-Json -ErrorAction Stop } catch { $deep = $null }
if ($null -eq $deep) {
    Write-Warning "replica /health/deep did not answer readable JSON; the profile and collection it is bound to are unverified"
} else {
    $boundProfile = ''; $boundCollection = ''
    if ($deep.embed_profile -and $deep.embed_profile.profile) { $boundProfile = "$($deep.embed_profile.profile)".Trim() }
    if ($deep.collection) { $boundCollection = "$($deep.collection)".Trim() }
    if ($boundProfile -and $boundProfile -ne "$($space.profile)") {
        throw "replica mem0 is bound to embedding profile '$boundProfile', but the restored set is '$($space.profile)' (set $Stamp)"
    }
    if ($collectionGiven) {
        # Restoring under a name of the operator's own is the documented way to bind it themselves afterwards, so
        # the server cannot be bound to it yet and this would throw after the upload. Say so instead of comparing.
        Write-Host "    note: restored under the -Collection name '$Collection'; the server's binding ($(if ($boundCollection) { "'$boundCollection'" } else { 'not reported' })) was not checked against it, so bind it before relying on this replica"
    } elseif ($boundCollection -and $boundCollection -ne $Collection) {
        throw "replica mem0 is bound to collection '$boundCollection', but the set was restored into '$Collection' (set $Stamp)"
    }
}
$pts = Wsl "curl -sf -m 10 'http://127.0.0.1:6333/collections/$Collection' | jq -r '.result.points_count'"
# v1.23.1: "restored" means the set's manifest count, not whatever collection happens to exist.
$manifest = "$BackupDir\manifest-$Stamp.json"
if (Test-Path $manifest) {
    $expected = $null
    try { $m = Get-Content -Raw $manifest | ConvertFrom-Json; $expected = $m.counts.qdrant_points; if ($null -eq $expected) { $expected = $m.qdrant_points } } catch { $expected = $null }
    if ($null -ne $expected -and ([int]"$expected".Trim() -ne [int]"$pts".Trim())) {
        throw "replica point count $($pts.Trim()) != manifest qdrant_points $expected for set $Stamp - the snapshot upload did not replace the collection"
    }
}
Write-Host "    replica live: $($pts.Trim()) memories restored (set $Stamp)" -ForegroundColor Green
