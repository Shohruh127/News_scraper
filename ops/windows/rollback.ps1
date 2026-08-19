<#
.SYNOPSIS
    Emergency rollback script for News Radar on Windows.
.DESCRIPTION
    Rolls back the codebase to a previous stable state, rebuilds images,
    optionally restores the previous DB dump, and restarts services.
#>

[CmdletBinding()]
param (
    [string]$TargetCommit,
    [switch]$RestoreLatestBackup
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
Set-Location $ProjectRoot

Write-Host "==========================================" -ForegroundColor Magenta
Write-Host "   NEWS RADAR EMERGENCY ROLLBACK         " -ForegroundColor Magenta
Write-Host "==========================================" -ForegroundColor Magenta

# 1. Rollback Git state if TargetCommit provided
if ($TargetCommit) {
    Write-Host "==> Checking out target commit $TargetCommit..." -ForegroundColor Yellow
    git checkout $TargetCommit
}

# 2. Rebuild images
Write-Host "==> Rebuilding Docker images..." -ForegroundColor Yellow
& docker compose build

# 3. Optional DB Restore
if ($RestoreLatestBackup) {
    $LatestDump = Get-ChildItem -Path "backups/db" -Filter "backup_*.dump" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($LatestDump) {
        Write-Host "==> Restoring database from $($LatestDump.Name)..." -ForegroundColor Yellow
        Get-Content -Path $LatestDump.FullName -Raw -Encoding Byte | & docker compose exec -T postgres pg_restore -U news_radar -d news_radar --clean --if-exists --no-owner
    }
}

# 4. Restart containers
Write-Host "==> Restarting containers..." -ForegroundColor Yellow
& docker compose up -d

# 5. Verify health
Write-Host "==> Verifying health after rollback..." -ForegroundColor Yellow
Start-Sleep -Seconds 5
try {
    $Response = Invoke-RestMethod -Uri "http://127.0.0.1:8000/healthz/" -TimeoutSec 5
    if ($Response.status -eq "ok") {
        Write-Host "[$((Get-Date).ToString('o'))] Rollback completed and services are healthy." -ForegroundColor Green
    }
}
catch {
    Write-Host "[$((Get-Date).ToString('o'))] Warning: Healthcheck after rollback returned: $_" -ForegroundColor Red
}
