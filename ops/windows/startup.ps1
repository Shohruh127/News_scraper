<#
.SYNOPSIS
    News Radar Host Startup Script for Windows.
.DESCRIPTION
    Launches Docker Desktop if not already active, waits for daemon readiness,
    and starts the complete News Radar stack with `docker compose up -d`.
#>

[CmdletBinding()]
param (
    [int]$DockerWaitTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
Set-Location $ProjectRoot

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   NEWS RADAR HOST STARTUP SEQUENCE       " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. Check Docker status
$DockerReady = $false
try {
    $null = docker info 2>&1
    if ($LASTEXITCODE -eq 0) {
        $DockerReady = $true
    }
}
catch {}

if (-not $DockerReady) {
    Write-Host "Starting Docker Desktop..." -ForegroundColor Yellow
    $DockerDesktopPath = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $DockerDesktopPath) {
        Start-Process -FilePath $DockerDesktopPath
    }
    else {
        Write-Warning "Docker Desktop executable not found at standard path: $DockerDesktopPath"
    }

    Write-Host "Waiting for Docker daemon to respond (timeout: ${DockerWaitTimeoutSeconds}s)..."
    $Deadline = (Get-Date).AddSeconds($DockerWaitTimeoutSeconds)
    while ((Get-Date) -lt $Deadline) {
        Start-Sleep -Seconds 5
        try {
            $null = docker info 2>&1
            if ($LASTEXITCODE -eq 0) {
                $DockerReady = $true
                break
            }
        }
        catch {}
    }
}

if (-not $DockerReady) {
    Write-Error "Docker daemon failed to start within ${DockerWaitTimeoutSeconds}s. Manual Windows user login or service check required."
    exit 1
}

Write-Host "Docker daemon is ready." -ForegroundColor Green

# 2. Start stack
Write-Host "Starting News Radar services..." -ForegroundColor Cyan
& docker compose up -d

Write-Host "[$((Get-Date).ToString('o'))] News Radar stack started successfully." -ForegroundColor Green
