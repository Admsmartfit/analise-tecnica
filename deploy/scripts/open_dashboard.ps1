<#
.SYNOPSIS
  Abre o terminal do Trading System (atalho "Trading System B3") numa janela de programa.
  Se o terminal já estiver aberto, abre outra janela na mesma instância.
  -Classic abre o painel antigo (Streamlit) no navegador.
#>
param([string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path, [switch]$Classic)

$pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = Join-Path $Root ".venv\Scripts\python.exe" }

if (-not $Classic) {
    Start-Process -FilePath $pyw -WorkingDirectory $Root -ArgumentList @("-m", "ts", "ui")
    exit 0
}

$port = 8501
$url = "http://localhost:$port"
function Test-Port { try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $port); $c.Close(); $true } catch { $false } }
if (-not (Test-Port)) {
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    Start-Process -FilePath $py -WorkingDirectory $Root -WindowStyle Hidden -ArgumentList @(
        "-m", "streamlit", "run", "src\ts\app\dashboard.py",
        "--server.headless", "true", "--server.port", "$port", "--browser.gatherUsageStats", "false"
    )
    for ($i = 0; $i -lt 60 -and -not (Test-Port); $i++) { Start-Sleep -Milliseconds 500 }
}
Start-Process $url
