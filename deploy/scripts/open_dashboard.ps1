<#
.SYNOPSIS
  Abre o painel do Trading System (atalho "Trading System B3"). Se o painel já estiver rodando,
  só abre o navegador; senão inicia o Streamlit em segundo plano e abre quando estiver pronto.
#>
param([string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path, [int]$Port = 8501)

$url = "http://localhost:$Port"
function Test-Port { try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $c.Close(); $true } catch { $false } }

if (-not (Test-Port)) {
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    # headless: sem prompt de e-mail do Streamlit na primeira execução (travaria com a janela oculta)
    Start-Process -FilePath $py -WorkingDirectory $Root -WindowStyle Hidden -ArgumentList @(
        "-m", "streamlit", "run", "src\ts\app\dashboard.py",
        "--server.headless", "true", "--server.port", "$Port", "--browser.gatherUsageStats", "false"
    )
    for ($i = 0; $i -lt 60 -and -not (Test-Port); $i++) { Start-Sleep -Milliseconds 500 }
}
Start-Process $url
