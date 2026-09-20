@echo off
setlocal
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
  echo Docker Desktop is not available. Please start Docker Desktop first.
  pause
  exit /b 1
)

docker compose up -d --build
if errorlevel 1 (
  echo Failed to start AliExpress Monitor.
  pause
  exit /b 1
)

echo Waiting for the frontend...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline=(Get-Date).AddSeconds(90); do { try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3000/health -TimeoutSec 3 | Out-Null; exit 0 } catch { Start-Sleep -Seconds 2 } } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
  echo Services started but frontend health check timed out. Run: docker compose ps
  pause
  exit /b 1
)

start "" "http://127.0.0.1:3000"
echo AliExpress Monitor is running at http://127.0.0.1:3000
endlocal
