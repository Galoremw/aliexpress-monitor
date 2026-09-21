@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-browser-collector-task.ps1"
if errorlevel 1 (
  echo Browser collector setup failed.
  pause
  exit /b 1
)
echo Browser collector setup completed.
pause
endlocal
