<#
.SYNOPSIS
  Auto-update a partir do GitHub (executado pela tarefa TS_Update a cada N minutos e no logon).

.DESCRIPTION
  Fluxo seguro (nunca deixa o sistema numa versão quebrada):
    1. Fora das janelas críticas (08:30-09:15 e 19:00-21:00 em dias úteis) — a menos que -Force.
    2. Pega o MESMO lock das rotinas (data\ts.lock): nenhuma rotina roda durante a atualização, e a
       atualização não começa no meio de um EOD.
    3. git fetch; se origin/<branch> == HEAD, sai em silêncio.
    4. Recusa atualizar se houver arquivo versionado modificado localmente (avisa no Telegram).
    5. git reset --hard origin/<branch>; reinstala dependências só se pyproject.toml/requirements.lock mudaram.
    6. Roda a suíte de testes. Se falhar: ROLLBACK para o commit anterior e alerta.
    7. Re-registra as tarefas se deploy/ mudou; alerta se config/strategy.yaml mudou (PRD 1.4.4: mudança
       de parâmetro reinicia a contagem do paper).
#>
param(
    [string]$Root = "C:\TradingSystem",
    [string]$Branch = "main",
    [switch]$Force,
    [switch]$SkipTests
)

$ErrorActionPreference = "Continue"
Set-Location $Root
$vpy = Join-Path $Root ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force (Join-Path $Root "logs"), (Join-Path $Root "data") | Out-Null
$log = Join-Path $Root ("logs\update-{0:yyyy-MM-dd}.log" -f (Get-Date))

function Log($m) { $line = "{0:s} {1}" -f (Get-Date), $m; Add-Content -Path $log -Value $line -Encoding UTF8; Write-Host $line }
function Notify($m) { & $vpy -m ts notify $m *> $null }
function Invoke-Git([string[]]$argv) {
    # git.exe explícito: uma função chamada "Git" chamaria a si mesma (nomes não diferenciam maiúsculas)
    $out = & git.exe @argv 2>&1
    if ($LASTEXITCODE -ne 0) { throw "git $($argv -join ' ') falhou: $out" }
    return $out
}

# ---------------------------------------------------------------- 1. janelas críticas
$now = Get-Date
$weekday = $now.DayOfWeek -notin @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)
$t = $now.TimeOfDay
$critical = $weekday -and (($t -ge [TimeSpan]"08:30" -and $t -le [TimeSpan]"09:15") -or ($t -ge [TimeSpan]"19:00" -and $t -le [TimeSpan]"21:00"))
if ($critical -and -not $Force) { exit 0 }

# ---------------------------------------------------------------- 2. lock compartilhado com as rotinas Python
$lockPath = Join-Path $Root "data\ts.lock"
$fs = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
try { $fs.Lock(0, 1) } catch { $fs.Close(); exit 4 }   # rotina em execução: tenta na próxima rodada

try {
    # ------------------------------------------------------------ 3. há commit novo?
    Invoke-Git @("fetch", "--quiet", "origin", $Branch) | Out-Null
    $old = (Invoke-Git @("rev-parse", "HEAD")).Trim()
    $new = (Invoke-Git @("rev-parse", "origin/$Branch")).Trim()
    if ($old -eq $new) { exit 0 }
    $rejectedFile = Join-Path $Root "data\update_rejected.txt"
    if ((Test-Path $rejectedFile) -and ((Get-Content $rejectedFile -Raw).Trim() -eq $new) -and -not $Force) {
        exit 0   # este commit já falhou nos testes; espera um commit novo (evita alerta a cada rodada)
    }

    $msg = (Invoke-Git @("log", "-1", "--format=%h %s", $new)).Trim()
    Log "Novo commit: $msg (atual $($old.Substring(0,7)))"

    # ------------------------------------------------------------ 4. alterações locais?
    # Arquivos de DADOS que o próprio sistema grava nesta máquina (carteira do IBrX baixada da B3, proventos do
    # Yahoo) não bloqueiam: a cópia local vai para data\backups\local_data\<data-hora>\ e vale a do GitHub.
    # Qualquer outro arquivo versionado alterado continua bloqueando (proteção contra edição manual).
    $autoData = '^(config/universe/ibx100_[^/]+\.csv|config/corporate_actions\.csv)$'
    $dirty = @(Invoke-Git @("status", "--porcelain", "--untracked-files=no") | Where-Object { $_ })
    $dataFiles = @($dirty | ForEach-Object { $_.Substring(3).Trim('"') } | Where-Object { $_ -match $autoData })
    if ($dataFiles.Count -gt 0) {
        $bk = Join-Path $Root ("data\backups\local_data\{0:yyyyMMdd-HHmmss}" -f (Get-Date))
        foreach ($f in $dataFiles) {
            $dest = Join-Path $bk $f
            New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
            Copy-Item -LiteralPath (Join-Path $Root $f) -Destination $dest -Force
            Invoke-Git @("checkout", "--", $f) | Out-Null
        }
        Log "Arquivos de dados locais substituídos pela versão do GitHub (backup em $bk): $($dataFiles -join ', ')"
        # Fora da checagem: o reset --hard abaixo grava a versão nova deles de qualquer forma (mesmo quando o
        # checkout acima não "limpa" o status, como num arquivo com quebras de linha mistas).
        $dirty = @(Invoke-Git @("status", "--porcelain", "--untracked-files=no") | Where-Object { $_ } |
                   Where-Object { $_.Substring(3).Trim('"') -notmatch $autoData })
    }
    if ($dirty) {
        Log "ABORTADO: arquivos versionados modificados nesta máquina:`n$dirty"
        Notify "⚠️ UPDATE abortado: há arquivos versionados modificados localmente em $Root (use config\settings.local.yaml para ajustes da máquina)."
        exit 1
    }

    $changed = @(Invoke-Git @("diff", "--name-only", $old, $new))
    $depsChanged = $changed -match '^(pyproject\.toml|requirements\.lock)$'

    # ------------------------------------------------------------ 5. aplica
    Invoke-Git @("reset", "--hard", $new) | Out-Null
    if ($depsChanged) {
        Log "Dependências mudaram: reinstalando"
        & $vpy -m pip install -e ".[dev]" --quiet 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
        if ($LASTEXITCODE -ne 0) {
            # ex.: painel (Streamlit) aberto prendendo arquivos do .venv — volta à versão anterior
            Log "pip install falhou em $msg — rollback para $($old.Substring(0,7))"
            Invoke-Git @("reset", "--hard", $old) | Out-Null
            & $vpy -m pip install -e ".[dev]" --quiet 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
            Notify "❌ UPDATE: falha ao instalar dependências de $msg (feche o painel e tente de novo). Mantida a versão anterior."
            exit 3
        }
    }

    # ------------------------------------------------------------ 6. testes + rollback
    if (-not $SkipTests) {
        & $vpy -m pytest -q -x -p no:cacheprovider 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
        if ($LASTEXITCODE -ne 0) {
            Log "TESTES FALHARAM em $msg — rollback para $($old.Substring(0,7))"
            Invoke-Git @("reset", "--hard", $old) | Out-Null
            Set-Content -Path $rejectedFile -Value $new -Encoding ascii
            if ($depsChanged) { & $vpy -m pip install -e ".[dev]" --quiet 2>&1 | Out-File -FilePath $log -Append -Encoding utf8 }
            Notify "❌ UPDATE rejeitado: testes falharam no commit $msg. Sistema mantido em $($old.Substring(0,7))."
            exit 2
        }
    }

    # ------------------------------------------------------------ 7. pós-atualização
    if ($changed -match '^deploy/(tasks/|scripts/register_tasks\.ps1)') {
        Log "Tarefas do Agendador mudaram: re-registrando"
        & (Join-Path $Root "deploy\scripts\register_tasks.ps1") -Root $Root -Branch $Branch 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    }
    $warn = ""
    if ($changed -contains "config/strategy.yaml") {
        $warn = " ⚠️ strategy.yaml mudou: pela regra 1.4.4 a contagem do paper reinicia."
    }
    if (Test-Path $rejectedFile) { Remove-Item $rejectedFile -Force }
    # O webhook do Cérebro roda o dia todo: sem reiniciar ele continuaria com o código antigo.
    if ($changed -match '^src/') {
        try {
            $svc = Get-ScheduledTask -TaskName "TS_Cerebro" -ErrorAction Stop
            Stop-ScheduledTask -TaskName "TS_Cerebro" -ErrorAction SilentlyContinue
            Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
                Where-Object { $_.CommandLine -match 'cerebro serve' } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            if ($svc.State -ne "Disabled") { Start-ScheduledTask -TaskName "TS_Cerebro" }
            Log "Webhook do Cérebro reiniciado com a versão nova"
        } catch { }   # tarefa não instalada: nada a fazer
    }
    Log "Atualizado para $msg"
    Notify "🔄 Atualizado para $msg ($($changed.Count) arquivos).$warn"
}
catch {
    Log "ERRO: $_"
    Notify "❌ UPDATE falhou: $_"
    exit 3
}
finally {
    try { $fs.Unlock(0, 1) } catch {}
    $fs.Close()
}
exit 0
