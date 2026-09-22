@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health -TimeoutSec 3 | Out-Null } catch { exit 1 }"
if errorlevel 1 call "%~dp0start.bat" --no-open
if errorlevel 1 exit /b 1

set "CHROME_EXE="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE (
  echo Google Chrome was not found.
  pause
  exit /b 1
)

set "PROFILE_DIR=%LOCALAPPDATA%\AliExpressMonitor\DianxiaomiProfile"
if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"
set "EXTENSION_DIR=%~dp0extension"
set "REMOTE_DEBUG_PORT=9223"

start "" "%CHROME_EXE%" --user-data-dir="%PROFILE_DIR%" --profile-directory="Default" "chrome://quit"
timeout /t 2 /nobreak >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$profile=$env:PROFILE_DIR; Get-CimInstance Win32_Process -Filter 'Name = ''chrome.exe''' | Where-Object { $_.CommandLine -and $_.CommandLine -like ('*' + $profile + '*') -and $_.CommandLine -notmatch '--type=' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul

start "AliExpress Monitor Dianxiaomi Worker Setup" "%CHROME_EXE%" --user-data-dir="%PROFILE_DIR%" --profile-directory="Default" --load-extension="%EXTENSION_DIR%" --remote-debugging-address=127.0.0.1 --remote-debugging-port=%REMOTE_DEBUG_PORT% --new-window "http://127.0.0.1:3000/?dianxiaomi_worker=1" "https://www.dianxiaomi.com/web/productCrawl/dataAcquisition" "chrome://extensions/"
echo.
echo Dedicated Dianxiaomi Chrome profile started with dashboard, Dianxiaomi, and extension management tabs.
echo First time: reload the unpacked extension if needed, sign in to the monitor extension and Dianxiaomi, then confirm the collection agreement.
pause
endlocal
