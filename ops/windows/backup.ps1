<#
.SYNOPSIS
    Automated PostgreSQL database backup for News Radar.
.DESCRIPTION
    Creates a binary custom-format archive inside the PostgreSQL container, validates it,
    copies it byte-for-byte to the host, writes a SHA256 sidecar, and applies retention.
#>

[CmdletBinding()]
param (
    [string]$BackupDir = (Join-Path $env:ProgramData "NewsRadar\backups\db"),
    [int]$RetentionDays = 14
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
Set-Location $ProjectRoot

$TargetDir = [System.IO.Path]::GetFullPath($BackupDir)
if ($TargetDir -eq [System.IO.Path]::GetPathRoot($TargetDir)) {
    throw "BackupDir must not be a filesystem root."
}
if (-not (Test-Path $TargetDir)) {
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
}

$Timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$BackupFileName = "backup_$Timestamp.dump"
$BackupPath = Join-Path $TargetDir $BackupFileName
$PartialPath = "$BackupPath.partial"
$ShaPath = "$BackupPath.sha256"
$ContainerDumpPath = "/tmp/news_radar_backup.dump"

Write-Host "[$((Get-Date).ToString('o'))] Starting database backup to $BackupPath..." -ForegroundColor Cyan

try {
    $ContainerId = (& docker compose ps -q postgres).Trim()
    if (-not $ContainerId) {
        throw "PostgreSQL container is not running."
    }

    # Never redirect pg_dump binary output through PowerShell: older versions widen it to UTF-16.
    & docker compose exec -T postgres sh -ec 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/news_radar_backup.dump'
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump failed with exit code $LASTEXITCODE."
    }

    & docker compose exec -T postgres pg_restore --list $ContainerDumpPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "pg_restore rejected the newly created archive."
    }

    & docker cp "${ContainerId}:$ContainerDumpPath" $PartialPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $PartialPath) -or (Get-Item $PartialPath).Length -eq 0) {
        throw "docker cp failed or created an empty backup."
    }
    Move-Item -LiteralPath $PartialPath -Destination $BackupPath -Force

    $HashResult = Get-FileHash -Path $BackupPath -Algorithm SHA256
    "$($HashResult.Hash) *$BackupFileName" | Set-Content -Path $ShaPath -Encoding ascii

    $SizeKB = [math]::Round((Get-Item $BackupPath).Length / 1KB, 2)
    Write-Host "[$((Get-Date).ToString('o'))] Backup complete ($SizeKB KB). SHA256: $($HashResult.Hash)" -ForegroundColor Green
}
catch {
    if (Test-Path $PartialPath) {
        Remove-Item -LiteralPath $PartialPath -Force
    }
    Write-Error "Backup failed: $_"
    exit 1
}
finally {
    & docker compose exec -T postgres rm -f $ContainerDumpPath 2>$null
}

Write-Host "[$((Get-Date).ToString('o'))] Enforcing $RetentionDays-day retention policy..."
$CutoffDate = (Get-Date).AddDays(-$RetentionDays)
Get-ChildItem -LiteralPath $TargetDir -Filter "backup_*.dump*" |
    Where-Object { $_.LastWriteTime -lt $CutoffDate } |
    ForEach-Object {
        Write-Host "Removing expired backup: $($_.Name)" -ForegroundColor Yellow
        Remove-Item -LiteralPath $_.FullName -Force
    }

Write-Host "[$((Get-Date).ToString('o'))] Backup process completed successfully." -ForegroundColor Green