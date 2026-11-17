# Registers the Trading System scheduled tasks (PRD 1.3.3) + the GitHub auto-update task.
# Run as the user that stays logged on and runs MT5 (MT5 is a GUI app, it does not run in session 0):
#   powershell -ExecutionPolicy Bypass -File deploy\scripts\register_tasks.ps1 -Root C:\TradingSystem
#
# Uses Register-ScheduledTask -Xml (accepts the XML as a string; `schtasks /XML` only accepts UTF-16 files).
param(
    [string]$Root = "C:\TradingSystem",
    [string]$TasksDir = "$PSScriptRoot\..\tasks",
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [string]$Branch = "main",
    [int]$UpdateEveryMinutes = 5,
    [switch]$NoUpdateTask
)

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python of the virtual environment not found: $python" }

# TS_Sync is NOT registered: the EOD routine already syncs, and two routines at 19:30 would compete for MT5.
$tasks = @("TS_PreOpen", "TS_Watchdog", "TS_EOD", "TS_DeadMan", "TS_Backup")
if (-not $NoUpdateTask) { $tasks += "TS_Update" }

foreach ($t in $tasks) {
    $xmlPath = Join-Path $TasksDir "$t.xml"
    if (-not (Test-Path $xmlPath)) { Write-Warning "Missing $xmlPath"; continue }
    $xml = (Get-Content $xmlPath -Raw -Encoding UTF8)
    $xml = $xml.Replace("__ROOT__", $Root).Replace("__USER__", $User)
    $xml = $xml.Replace("__BRANCH__", $Branch).Replace("__INTERVAL__", [string]$UpdateEveryMinutes)
    Write-Host "Registering $t ..." -ForegroundColor Cyan
    Register-ScheduledTask -TaskName $t -Xml $xml -Force | Out-Null
}

Write-Host "Done. Check with: Get-ScheduledTask TS_* | Get-ScheduledTaskInfo" -ForegroundColor Green
