<#
.SYNOPSIS
    Atomic deployment script for News Radar on Windows.
.DESCRIPTION
    1. Creates a pre-deploy database backup.
    2. Builds Docker images.
    3. Runs database migrations.
    4. Recreates and starts all containers.
    5. Runs health checks. Triggers automatic rollback if health checks fail.
#>

[CmdletBinding()]
param (
    [switch]$SkipBackup,
    [int]$HealthTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
Set-Location $ProjectRoot

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   NEWS RADAR DEPLOYMENT PIPELINE         " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. Pre-deploy backup
if (-not $SkipBackup) {
    Write-Host "==> Step 1: Taking pre-deploy backup..." -ForegroundColor Yellow
    & "$ScriptDir\backup.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Pre-deploy backup failed. Aborting deploy."
        exit 1
    }
}

$CurrentCommit = (git rev-parse HEAD).Trim()
Write-Host "==> Current commit: $CurrentCommit" -ForegroundColor Gray

try {
    # 2. Build Docker images
    Write-Host "==> Step 2: Building Docker images..." -ForegroundColor Yellow
    & docker compose build
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose build failed."
    }

    # 3. Apply database migrations
    Write-Host "==> Step 3: Running database migrations..." -ForegroundColor Yellow
    & docker compose run --rm web uv run python manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) {
        throw "Database migration failed."
    }

    # 4. Recreate containers
    Write-Host "==> Step 4: Recreating and starting containers..." -ForegroundColor Yellow
    & docker compose up -d
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose up -d failed."
    }

    # 5. Health verification
    Write-Host "==> Step 5: Verifying container health (timeout: ${HealthTimeoutSeconds}s)..." -ForegroundColor Yellow
    $Deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
    $HealthPassed = $false

    while ((Get-Date) -lt $Deadline) {
        Start-Sleep -Seconds 3
        try {
            $Response = Invoke-RestMethod -Uri "http://127.0.0.1:8000/healthz/" -TimeoutSec 3 -ErrorAction SilentlyContinue
            if ($Response.status -eq "ok") {
                $ReadyResponse = Invoke-RestMethod -Uri "http://127.0.0.1:8000/readyz/" -TimeoutSec 3 -ErrorAction SilentlyContinue
                if ($ReadyResponse.status -eq "ok") {
                    $HealthPassed = $true
                    break
                }
            }
        }
        catch {
            Write-Host "  Waiting for web endpoint..." -ForegroundColor DarkGray
        }
    }

    if (-not $HealthPassed) {
        throw "Health verification timed out after ${HealthTimeoutSeconds}s."
    }

    Write-Host "[$((Get-Date).ToString('o'))] Deployment succeeded! All services healthy." -ForegroundColor Green
}
catch {
    Write-Host "[$((Get-Date).ToString('o'))] DEPLOYMENT FAILED: $_" -ForegroundColor Red
    Write-Host "Initiating rollback..." -ForegroundColor Magenta
    & "$ScriptDir\rollback.ps1" -TargetCommit $CurrentCommit
    exit 1
}
