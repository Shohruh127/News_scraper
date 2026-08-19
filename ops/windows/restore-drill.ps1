<#
.SYNOPSIS
    Restores the latest backup into an isolated database and compares source row counts.
#>

[CmdletBinding()]
param (
    [string]$BackupDir = (Join-Path $env:ProgramData "NewsRadar\backups\db"),
    [string]$DrillDbName = "news_radar_restore_drill"
)

$ErrorActionPreference = "Stop"
if ($DrillDbName -notmatch '^[a-zA-Z0-9_]+$') {
    throw "DrillDbName contains unsupported characters."
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
Set-Location $ProjectRoot

$TargetDir = [System.IO.Path]::GetFullPath($BackupDir)
if ($TargetDir -eq [System.IO.Path]::GetPathRoot($TargetDir)) {
    throw "BackupDir must not be a filesystem root."
}
$LatestDump = Get-ChildItem -LiteralPath $TargetDir -Filter "backup_*.dump" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $LatestDump) {
    Write-Error "No backup files found in $TargetDir."
    exit 1
}

$ShaFile = "$($LatestDump.FullName).sha256"
if (-not (Test-Path $ShaFile)) {
    Write-Error "Checksum sidecar is missing: $ShaFile"
    exit 1
}
$ExpectedHash = (Get-Content $ShaFile).Trim().Split(" ")[0]
$ActualHash = (Get-FileHash -Path $LatestDump.FullName -Algorithm SHA256).Hash
if ($ExpectedHash.ToUpper() -ne $ActualHash.ToUpper()) {
    Write-Error "SHA256 checksum mismatch. Expected $ExpectedHash, got $ActualHash."
    exit 1
}

$ContainerId = (& docker compose ps -q postgres).Trim()
if (-not $ContainerId) {
    Write-Error "PostgreSQL container is not running."
    exit 1
}
$ContainerDumpPath = "/tmp/news_radar_restore_drill.dump"
$SourceDb = (& docker compose exec -T postgres sh -ec 'printf "%s" "$POSTGRES_DB"').Trim()

function Get-TableCount {
    param ([string]$Database, [string]$Table)
    $Query = "SELECT count(*) FROM $Table;"
    $Value = (& docker compose exec -T postgres sh -ec 'psql -U "$POSTGRES_USER" -d "$1" -t -A -c "$2"' -- $Database $Query).Trim()
    if ($LASTEXITCODE -ne 0 -or $Value -notmatch '^\d+$') {
        throw "Could not count $Table in $Database."
    }
    return [int64]$Value
}

Write-Host "[$((Get-Date).ToString('o'))] Restore drill: $($LatestDump.FullName)" -ForegroundColor Cyan

try {
    & docker cp $LatestDump.FullName "${ContainerId}:$ContainerDumpPath"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to copy backup into PostgreSQL container."
    }

    & docker compose exec -T postgres sh -ec 'dropdb -U "$POSTGRES_USER" --if-exists --force "$1"' -- $DrillDbName 2>$null
    & docker compose exec -T postgres sh -ec 'createdb -U "$POSTGRES_USER" "$1"' -- $DrillDbName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create disposable database $DrillDbName."
    }

    & docker compose exec -T postgres sh -ec 'pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --no-privileges "$2"' -- $DrillDbName $ContainerDumpPath
    if ($LASTEXITCODE -ne 0) {
        throw "pg_restore failed for $DrillDbName."
    }

    $Tables = @("digest_article", "digest_digest", "digest_digestitem", "digest_source")
    foreach ($Table in $Tables) {
        $SourceCount = Get-TableCount -Database $SourceDb -Table $Table
        $DrillCount = Get-TableCount -Database $DrillDbName -Table $Table
        Write-Host "  $Table : source=$SourceCount restored=$DrillCount"
        if ($SourceCount -ne $DrillCount) {
            throw "Row-count mismatch for $Table."
        }
    }

    Write-Host "[$((Get-Date).ToString('o'))] Restore drill succeeded." -ForegroundColor Green
}
catch {
    Write-Error "Restore drill failed: $_"
    exit 1
}
finally {
    & docker compose exec -T postgres sh -ec 'dropdb -U "$POSTGRES_USER" --if-exists --force "$1"' -- $DrillDbName 2>$null
    & docker compose exec -T postgres rm -f $ContainerDumpPath 2>$null
}