@echo off
setlocal
cd /d "%~dp0"
docker compose -p aliexpress-monitor down
if errorlevel 1 pause
endlocal
