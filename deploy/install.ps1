<#
.SYNOPSIS
  Instalador do Trading System B3 para Windows (PRD 2.2 / Sprint 1 passo 0).

.DESCRIPTION
  1. Instala Git e Python 3.12 64-bit se faltarem (winget; sem winget, baixa os instaladores oficiais).
  2. Clona o repositório do GitHub em C:\TradingSystem (raiz curta, fora de OneDrive/Dropbox — PRD 1.3.2).
  3. Cria o .venv, instala o pacote e roda a suíte de testes (sem MT5).
  4. Cria config\settings.local.yaml (fora do git: login, provedor de dados, Telegram).
  5. Registra as tarefas do Agendador (PreOpen, Watchdog, EOD, DeadMan, Backup) e a tarefa TS_Update,
     que verifica o GitHub a cada N minutos e se atualiza sozinha (deploy/scripts/update.ps1).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -RepoUrl https://github.com/SEU_USUARIO/trading-b3.git

.EXAMPLE
  # Teste sem corretora: dados D1 gratuitos do Yahoo, modo paper
  powershell -ExecutionPolicy Bypass -File install.ps1 -RepoUrl https://github.com/SEU_USUARIO/trading-b3.git -Provider yahoo
#>
param(
    [Parameter(Mandatory = $true)][string]$RepoUrl,
    [string]$Root = "C:\TradingSystem",
    [string]$Branch = "main",
    [ValidateSet("yahoo", "mt5")][string]$Provider = "yahoo",
    [int]$UpdateEveryMinutes = 5,
    [int]$Mt5Login = 0,
    [string]$Mt5Server = "",
    [string]$TelegramChatId = "",
    [switch]$NoTasks,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$PythonVersion = "3.12.10"
$GitInstaller = "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe"

if ([Console]::IsOutputRedirected) { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false }  # GUI do Setup.exe

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Get-Secret([string]$envName, [string]$prompt) {
    # O Setup.exe passa segredos por variável de ambiente do processo filho (nunca na linha de comando).
    $v = [Environment]::GetEnvironmentVariable($envName, "Process")
    if ($v) { [Environment]::SetEnvironmentVariable($envName, $null, "Process"); return $v }
    if ([Console]::IsInputRedirected) { return $null }
    $sec = Read-Host $prompt -AsSecureString
    return [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
function Run([string]$exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "Falhou ($LASTEXITCODE): $exe $($argv -join ' ')" }
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Has-Winget { [bool](Get-Command winget -ErrorAction SilentlyContinue) }

# ---------------------------------------------------------------- 0. sanity
if ($Root -match "OneDrive|Dropbox|Google Drive") { throw "Use uma pasta fora de OneDrive/Dropbox (PRD 1.3.2): $Root" }
if ($Root.Length -gt 40) { Write-Warning "Raiz longa ($Root): risco com o limite de 260 caracteres do NTFS." }
if (-not [Environment]::Is64BitOperatingSystem) { throw "Windows 64-bit é obrigatório (MetaTrader5)." }

# ---------------------------------------------------------------- 1. Git
Step "Git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    if (Has-Winget) {
        Run winget @("install", "--id", "Git.Git", "-e", "--silent", "--accept-package-agreements", "--accept-source-agreements")
    } else {
        $tmp = Join-Path $env:TEMP "git-installer.exe"
        Invoke-WebRequest $GitInstaller -OutFile $tmp -UseBasicParsing
        Start-Process $tmp -ArgumentList "/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-", "/SUPPRESSMSGBOXES" -Wait
    }
    Refresh-Path
}
git --version

# ---------------------------------------------------------------- 2. Python 3.12 64-bit
Step "Python $PythonVersion (64-bit)"
function Find-Py312 {
    try { $p = (& py -3.12 -c "import sys;print(sys.executable)" 2>$null); if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() } } catch {}
    $cand = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $cand) { return $cand }
    return $null
}
$py = Find-Py312
if (-not $py) {
    if (Has-Winget) {
        Run winget @("install", "--id", "Python.Python.3.12", "-e", "--silent", "--accept-package-agreements", "--accept-source-agreements")
    } else {
        $tmp = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile $tmp -UseBasicParsing
        Start-Process $tmp -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=1", "Include_test=0" -Wait
    }
    Refresh-Path
    $py = Find-Py312
    if (-not $py) { throw "Python 3.12 não encontrado após a instalação." }
}
$bits = & $py -c "import struct;print(struct.calcsize('P')*8)"
if ($bits.Trim() -ne "64") { throw "Python precisa ser 64-bit (encontrado: $bits-bit em $py)" }
Write-Host "Python: $py"

# ---------------------------------------------------------------- 3. Repositório
Step "Repositório em $Root"
if (Test-Path (Join-Path $Root ".git")) {
    Run git @("-C", $Root, "fetch", "origin", $Branch)
    Run git @("-C", $Root, "checkout", $Branch)
    Run git @("-C", $Root, "reset", "--hard", "origin/$Branch")
} else {
    if ((Test-Path $Root) -and (Get-ChildItem $Root -Force | Measure-Object).Count -gt 0) {
        throw "$Root existe e não é um clone git. Mova o conteúdo ou escolha outro -Root."
    }
    # Repositório privado: o Git Credential Manager abre o login do GitHub uma única vez e guarda o token.
    Run git @("clone", "--branch", $Branch, $RepoUrl, $Root)
}
Set-Location $Root

# ---------------------------------------------------------------- 4. venv + pacote
Step "Ambiente virtual e dependências"
$vpy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { Run $py @("-m", "venv", (Join-Path $Root ".venv")) }
Run $vpy @("-m", "pip", "install", "--upgrade", "pip", "--quiet")
if (Test-Path (Join-Path $Root "requirements.lock")) {
    Run $vpy @("-m", "pip", "install", "-r", "requirements.lock", "--quiet")
}
Run $vpy @("-m", "pip", "install", "-e", ".[dev]", "--quiet")

# ---------------------------------------------------------------- 5. Configuração local
Step "config\settings.local.yaml"
New-Item -ItemType Directory -Force (Join-Path $Root "data"), (Join-Path $Root "logs"), (Join-Path $Root "reports") | Out-Null
$local = Join-Path $Root "config\settings.local.yaml"
if (-not (Test-Path $local)) {
    $yaml = @"
# Overrides desta máquina (fora do git; o auto-update não mexe neste arquivo).
execution:
  mode: "paper"          # só muda para "live" após o relatório Go/No-Go assinado (PRD 1.4)
data:
  provider: "$Provider"  # "yahoo" = D1 gratuito para testes; "mt5" = terminal da corretora
mt5:
  login: $Mt5Login
"@
    if ($Mt5Server) { $yaml += "`n  server: `"$Mt5Server`"" }
    if ($TelegramChatId) { $yaml += "`ntelegram:`n  enabled: true`n  chat_id: `"$TelegramChatId`"" }
    [IO.File]::WriteAllText($local, $yaml + "`n", (New-Object Text.UTF8Encoding $false))
    Write-Host "Criado $local"
} else {
    Write-Host "Mantido $local (já existia)"
}

if ($Mt5Login -gt 0) {
    $plain = Get-Secret "TS_MT5_PASSWORD" "Senha do MT5 para a conta $Mt5Login (vai para o Gerenciador de Credenciais do Windows)"
    if ($plain) {
        $plain | & $vpy -c "import sys,keyring; keyring.set_password('ts-mt5', '$Mt5Login', sys.stdin.read().strip())"
        Write-Host "Senha do MT5 guardada no Gerenciador de Credenciais (ts-mt5 / $Mt5Login)."
    } else { Write-Warning "Senha do MT5 não informada: guarde depois com keyring (ver README)." }
}
if ($TelegramChatId) {
    $plain = Get-Secret "TS_TELEGRAM_TOKEN" "Token do bot do Telegram (vai para o Gerenciador de Credenciais)"
    if ($plain) {
        $plain | & $vpy -c "import sys,keyring; keyring.set_password('ts-telegram','bot_token',sys.stdin.read().strip()); keyring.set_password('ts-telegram','chat_id','$TelegramChatId')"
        Write-Host "Token do Telegram guardado no Gerenciador de Credenciais."
    } else { Write-Warning "Token do Telegram não informado." }
}

# ---------------------------------------------------------------- 6. Testes e diagnóstico
if (-not $SkipTests) {
    Step "Testes (sem MT5)"
    Run $vpy @("-m", "pytest", "-q", "-p", "no:cacheprovider")
}
Step "Diagnóstico"
& $vpy -m ts doctor

# ---------------------------------------------------------------- 7. Tarefas do Agendador
if (-not $NoTasks) {
    Step "Tarefas do Agendador (incluindo auto-update a cada $UpdateEveryMinutes min)"
    & (Join-Path $Root "deploy\scripts\register_tasks.ps1") -Root $Root -Branch $Branch -UpdateEveryMinutes $UpdateEveryMinutes
}

Step "Pronto"
Write-Host @"
Raiz:        $Root
Provedor:    $Provider   (troque em config\settings.local.yaml)
Primeira carga de dados:   .venv\Scripts\python.exe -m ts sync --full
Backtest + G0:             .venv\Scripts\python.exe -m ts backtest --g0
Dashboard:                 .venv\Scripts\streamlit.exe run src\ts\app\dashboard.py
Log do auto-update:        logs\update-AAAA-MM-DD.log
"@ -ForegroundColor Green
