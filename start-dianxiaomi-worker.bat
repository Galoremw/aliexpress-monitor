@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-local-dianxiaomi-worker.ps1"
if errorlevel 1 exit /b 1
echo Dianxiaomi queue enabled in local Chrome. Routine collection runs in a collapsed background tab group.
endlocal
