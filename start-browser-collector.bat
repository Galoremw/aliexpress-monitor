@echo off
setlocal
cd /d "%~dp0"

call "%~dp0start.bat" --no-open
if errorlevel 1 exit /b 1

set "CHROME_EXE="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if not defined CHROME_EXE (
  echo Google Chrome was not found.
  exit /b 1
)

set "PROFILE_DIR=%LOCALAPPDATA%\AliExpressMonitor\ChromeProfile"
if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"

start "AliExpress Monitor Collector" "%CHROME_EXE%" --user-data-dir="%PROFILE_DIR%" --profile-directory="Default" --new-window "http://127.0.0.1:3000/?browser_collection=1"
endlocal
