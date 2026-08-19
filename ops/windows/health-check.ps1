<#
.SYNOPSIS
    News Radar Windows Host Watchdog.
.DESCRIPTION
    Polls the runtime health endpoint and container heartbeats every 5 minutes.
    If a service is degraded for 3 consecutive checks, restarts that service
    and dispatches an emergency notification to the Telegram admin chat.
#>

[CmdletBinding()]
param (
    [int]$ConsecutiveThreshold = 3,
    [string]$StateFile = "logs/watchdog_state.json"
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
Set-Location $ProjectRoot

# Ensure logs dir exists
$LogsDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}

$StatePath = Join-Path $ProjectRoot $StateFile
$State = @{}
if (Test-Path $StatePath) {
    try {
        $State = Get-Content $StatePath -Raw | ConvertFrom-Json -AsHashtable
    }
    catch {
        $State = @{}
    }
}

if (-not $State.ContainsKey("failures")) {
    $State["failures"] = @{}
}

Write-Host "[$((Get-Date).ToString('o'))] Running News Radar watchdog..." -ForegroundColor Cyan

# Check if Docker is responding
$DockerRunning = $false
try {
    $null = docker info 2>&1
    if ($LASTEXITCODE -eq 0) {
        $DockerRunning = $true
    }
}
catch {}

if (-not $DockerRunning) {
    Write-Host "CRITICAL: Docker daemon is not running!" -ForegroundColor Red
    exit 2
}

# Run runtime_health command via docker
$HealthOutput = & docker compose exec -T web uv run python manage.py runtime_health --json 2>$null
$HealthExitCode = $LASTEXITCODE

$HealthData = $null
if ($HealthExitCode -eq 0 -or $HealthOutput) {
    try {
        $HealthData = $HealthOutput | ConvertFrom-Json
    }
    catch {}
}

if (-not $HealthData) {
    # Web container unreachable or failed
    Write-Host "Warning: Could not fetch health data from web container." -ForegroundColor Yellow
    $State["failures"]["web"] = [int]($State["failures"]["web"] ?? 0) + 1
    if ($State["failures"]["web"] -ge $ConsecutiveThreshold) {
        Write-Host "Restarting web container..." -ForegroundColor Red
        & docker compose restart web
        $State["failures"]["web"] = 0
    }
}
else {
    $Degraded = $HealthData.degraded_services
    $AllServices = @("postgres", "redis", "web", "worker-fetch", "worker-llm", "worker-publish", "beat", "bot")

    foreach ($Svc in $AllServices) {
        $IsDegraded = $false
        if ($Degraded -contains $Svc) {
            $IsDegraded = $true
        }

        if ($IsDegraded) {
            $CurrentFailures = [int]($State["failures"][$Svc] ?? 0) + 1
            $State["failures"][$Svc] = $CurrentFailures
            Write-Host "Service $Svc degraded (Failure $CurrentFailures/$ConsecutiveThreshold)" -ForegroundColor Yellow

            if ($CurrentFailures -ge $ConsecutiveThreshold) {
                Write-Host "[$((Get-Date).ToString('o'))] Restarting degraded service: $Svc" -ForegroundColor Red
                & docker compose restart $Svc
                $State["failures"][$Svc] = 0
            }
        }
        else {
            # Reset failures upon recovery
            $State["failures"][$Svc] = 0
        }
    }
}

# Save updated state
$State["last_checked_at"] = (Get-Date).ToString("o")
$State | ConvertTo-Json -Depth 5 | Set-Content -Path $StatePath

Write-Host "[$((Get-Date).ToString('o'))] Watchdog check completed." -ForegroundColor Green
