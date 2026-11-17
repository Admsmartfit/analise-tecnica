# Registers the Trading System scheduled tasks (PRD 1.3.3).
# Run in an elevated PowerShell on the trading machine, as the user that is logged on and runs MT5:
#   powershell -ExecutionPolicy Bypass -File deploy\scripts\register_tasks.ps1 -Root C:\TradingSystem
#
# Uses Register-ScheduledTask -Xml (accepts the XML as a string, so the file encoding does not matter;
# `schtasks /XML` only accepts UTF-16 files). __ROOT__ and __USER__ placeholders are filled in here.
param(
    [string]$Root = "C:\TradingSystem",
    [string]$TasksDir = "$PSScriptRoot\..\tasks",
    [string]$User = "$env:USERDOMAIN\$env:USERNAME"
)

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python of the virtual environment not found: $python" }

# TS_Sync is NOT registered: the EOD routine already syncs, and two routines at 19:30 would compete for MT5.
$tasks = @("TS_PreOpen", "TS_Watchdog", "TS_EOD", "TS_DeadMan", "TS_Backup")

foreach ($t in $tasks) {
    $xmlPath = Join-Path $TasksDir "$t.xml"
    if (-not (Test-Path $xmlPath)) { Write-Warning "Missing $xmlPath"; continue }
    $xml = (Get-Content $xmlPath -Raw -Encoding UTF8).Replace("__ROOT__", $Root).Replace("__USER__", $User)
    Write-Host "Registering $t ..." -ForegroundColor Cyan
    Register-ScheduledTask -TaskName $t -Xml $xml -User $User -Force | Out-Null
}

Write-Host "Done. Check with: Get-ScheduledTask TS_* | Get-ScheduledTaskInfo" -ForegroundColor Green
