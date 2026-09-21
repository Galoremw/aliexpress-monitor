@echo off
setlocal
cd /d "%~dp0"
set "OPEN_DASHBOARD=1"
if /i "%~1"=="--no-open" set "OPEN_DASHBOARD=0"

where docker >nul 2>nul
if errorlevel 1 (
  if exist "%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe" (
    set "PATH=%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin;%PATH%"
  ) else if exist "%ProgramFiles%\Docker\Docker\resources\bin\docker.exe" (
    set "PATH=%ProgramFiles%\Docker\Docker\resources\bin;%PATH%"
  ) else (
    echo Docker CLI was not found. Please start or reinstall Docker Desktop.
    if "%OPEN_DASHBOARD%"=="1" pause
    exit /b 1
  )
)

echo Waiting for Docker Desktop...
set /a docker_attempts=0
docker info >nul 2>nul
if errorlevel 1 (
  if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%ProgramFiles%\Docker\Docker\Docker Desktop.exe' -WindowStyle Hidden"
  ) else if exist "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe' -WindowStyle Hidden"
  )
)
:wait_for_docker
docker info >nul 2>nul
if not errorlevel 1 goto docker_ready
set /a docker_attempts+=1
if %docker_attempts% GEQ 45 (
  echo Docker Desktop is not ready. Please start Docker Desktop and try again.
  if "%OPEN_DASHBOARD%"=="1" pause
  exit /b 1
)
timeout /t 2 /nobreak >nul
goto wait_for_docker

:docker_ready
docker compose -p aliexpress-monitor up -d --build
if errorlevel 1 (
  echo Failed to start AliExpress Monitor.
  if "%OPEN_DASHBOARD%"=="1" pause
  exit /b 1
)

echo Waiting for the dashboard...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline=(Get-Date).AddSeconds(120); do { try { $response=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3000/ -TimeoutSec 3; if ($response.StatusCode -eq 200) { exit 0 } } catch { } Start-Sleep -Seconds 2 } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
  echo Services started but dashboard check timed out. Run: docker compose -p aliexpress-monitor ps
  if "%OPEN_DASHBOARD%"=="1" pause
  exit /b 1
)

if "%OPEN_DASHBOARD%"=="1" start "" "http://127.0.0.1:3000"
echo AliExpress Monitor is running at http://127.0.0.1:3000
endlocal
