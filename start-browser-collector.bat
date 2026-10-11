@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3000/health -TimeoutSec 3 | Out-Null } catch { exit 1 }"
if errorlevel 1 call "%~dp0start.bat" --no-open
if errorlevel 1 exit /b 1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\open-browser-collector-chrome.ps1"
if errorlevel 1 exit /b 1
endlocal
