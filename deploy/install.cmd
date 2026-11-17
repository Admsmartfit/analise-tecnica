@echo off
rem Duplo clique: instala/atualiza o Trading System B3. Uso: install.cmd https://github.com/USUARIO/REPO.git [yahoo^|mt5]
set REPO=%~1
if "%REPO%"=="" set /p REPO=URL do repositorio GitHub: 
set PROV=%~2
if "%PROV%"=="" set PROV=yahoo
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -RepoUrl "%REPO%" -Provider %PROV%
pause
