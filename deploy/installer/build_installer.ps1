<#
.SYNOPSIS
  Gera dist\TradingSystemB3-Setup.exe usando o compilador C# do .NET Framework 4.x (já vem no Windows 10/11).
  Nada para instalar. Uso:  powershell -ExecutionPolicy Bypass -File deploy\installer\build_installer.ps1
#>
param([string]$Out = "", [string]$RepoUrl = "")

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$repo = Resolve-Path (Join-Path $here "..\..")
if (-not $Out) { $Out = Join-Path $repo "dist\TradingSystemB3-Setup.exe" }

$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe" }
if (-not (Test-Path $csc)) { throw "csc.exe do .NET Framework 4 não encontrado." }

# Os scripts embutidos precisam de BOM UTF-8: o PowerShell 5.1 lê .ps1 sem BOM como ANSI (quebra acentos).
$obj = Join-Path $here "obj"
New-Item -ItemType Directory -Force $obj, (Split-Path $Out) | Out-Null
$bom = New-Object Text.UTF8Encoding $true
foreach ($name in "install.ps1", "uninstall.ps1") {
    $text = [IO.File]::ReadAllText((Join-Path $repo "deploy\$name"), [Text.Encoding]::UTF8)
    [IO.File]::WriteAllText((Join-Path $obj $name), $text, $bom)
}

# URL padrão embutida no EXE (remote origin deste clone), para instalar com um clique.
if (-not $RepoUrl) {
    $RepoUrl = (& git -C $repo remote get-url origin 2>$null)
    if (-not $RepoUrl) { $RepoUrl = "https://github.com/Admsmartfit/analise-tecnica.git" }
}
$commit = (& git -C $repo rev-parse --short HEAD 2>$null); if (-not $commit) { $commit = "dev" }
$buildInfo = Join-Path $obj "BuildInfo.cs"
[IO.File]::WriteAllText($buildInfo, "namespace TradingSystemSetup { static class BuildInfo { public const string DefaultRepo = @`"$($RepoUrl.Trim())`"; public const string Commit = `"$commit`"; } }", $bom)

$cscArgs = @(
    "/nologo", "/target:winexe", "/optimize+", "/platform:anycpu",
    "/out:$Out",
    "/win32manifest:$(Join-Path $here 'app.manifest')",
    "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
    "/resource:$(Join-Path $obj 'install.ps1'),install.ps1",
    "/resource:$(Join-Path $obj 'uninstall.ps1'),uninstall.ps1",
    (Join-Path $here "Setup.cs"), $buildInfo
)
& $csc @cscArgs
if ($LASTEXITCODE -ne 0) { throw "Compilação falhou ($LASTEXITCODE)" }

$h = (Get-FileHash $Out -Algorithm SHA256).Hash
Write-Host "Gerado: $Out" -ForegroundColor Green
Write-Host "SHA-256: $h"
