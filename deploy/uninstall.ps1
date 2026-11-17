<#
.SYNOPSIS
  Desinstala o Trading System B3: remove as tarefas do Agendador (TS_*).
  Por padrão MANTÉM a pasta (dados, banco do paper, logs). Use -RemoveFiles para apagar tudo.
#>
param(
    [string]$Root = "C:\TradingSystem",
    [switch]$RemoveFiles
)
$ErrorActionPreference = "Continue"
if ([Console]::IsOutputRedirected) { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false }

Write-Host "==> Removendo tarefas do Agendador"
foreach ($t in Get-ScheduledTask -TaskName "TS_*" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
    Write-Host "  removida: $($t.TaskName)"
}

if ($RemoveFiles) {
    if (-not (Test-Path (Join-Path $Root ".git"))) {
        Write-Warning "$Root não parece uma instalação do Trading System (sem .git). Nada apagado."
        exit 1
    }
    Write-Host "==> Apagando $Root (inclui banco do paper, dados e logs)"
    Remove-Item -Recurse -Force $Root
}
Write-Host "Concluído."
