<#
.SYNOPSIS
    Registers News Radar scheduled tasks in Windows Task Scheduler.
.DESCRIPTION
    Creates:
    1. NewsRadar-DailyBackup: Runs backup.ps1 every day at 02:00.
    2. NewsRadar-MonthlyRestoreDrill: Runs restore-drill.ps1 on 1st of month at 03:00.
    3. NewsRadar-Watchdog: Runs health-check.ps1 every 5 minutes.
    4. NewsRadar-Startup: Runs startup.ps1 at user logon.
#>

[CmdletBinding()]
param (
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

$Tasks = @(
    @{
        Name = "NewsRadar-DailyBackup"
        Script = "$ScriptDir\backup.ps1"
        Trigger = { New-ScheduledTaskTrigger -Daily -At "02:00" }
        Description = "Daily automated PostgreSQL backup for News Radar"
    },
    @{
        Name = "NewsRadar-MonthlyRestoreDrill"
        Script = "$ScriptDir\restore-drill.ps1"
        Trigger = { New-ScheduledTaskTrigger -Daily -At "03:00" }
        Description = "Monthly automated backup restore drill for News Radar"
    },
    @{
        Name = "NewsRadar-Watchdog"
        Script = "$ScriptDir\health-check.ps1"
        Trigger = { New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) }
        Description = "5-minute health watchdog for News Radar containers"
    },
    @{
        Name = "NewsRadar-Startup"
        Script = "$ScriptDir\startup.ps1"
        Trigger = { New-ScheduledTaskTrigger -AtLogOn }
        Description = "Launches News Radar Docker stack upon Windows login"
    }
)

if ($Unregister) {
    foreach ($T in $Tasks) {
        Write-Host "Unregistering scheduled task: $($T.Name)..." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $T.Name -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Host "All News Radar tasks unregistered." -ForegroundColor Green
    exit 0
}

foreach ($T in $Tasks) {
    Write-Host "Registering scheduled task: $($T.Name)..." -ForegroundColor Cyan
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$($T.Script)`""
    $TriggerObj = & $T.Trigger
    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

    Register-ScheduledTask -TaskName $T.Name -Action $Action -Trigger $TriggerObj -Principal $Principal -Settings $Settings -Description $T.Description -Force | Out-Null
    Write-Host "  Registered $($T.Name) successfully." -ForegroundColor Green
}

Write-Host "[$((Get-Date).ToString('o'))] All scheduled tasks registered." -ForegroundColor Green
