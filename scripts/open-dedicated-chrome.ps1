param([string]$ProtocolUrl)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$healthUrl = "http://127.0.0.1:8000/health"

try {
    Invoke-WebRequest -UseBasicParsing $healthUrl -TimeoutSec 3 | Out-Null
} catch {
    $startScript = Join-Path $projectRoot "start.bat"
    & $startScript --no-open
    $healthy = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            Invoke-WebRequest -UseBasicParsing $healthUrl -TimeoutSec 3 | Out-Null
            $healthy = $true
            break
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $healthy) { throw "AliExpress Monitor backend did not become healthy." }
}

$chromeCandidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
$chromeExe = $chromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $chromeExe) { throw "Google Chrome was not found." }

$profile = Join-Path $env:LOCALAPPDATA "AliExpressMonitor\DianxiaomiProfile"
$extension = Join-Path $projectRoot "extension"
New-Item -ItemType Directory -Path $profile -Force | Out-Null
$workerUrl = "http://127.0.0.1:3000/?dianxiaomi_worker=1&browser_collection=1"
$runningProfileProcesses = @(
    Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) -and $_.CommandLine -notmatch "--type=" }
)
if ($runningProfileProcesses.Count) {
    # Reuse the running worker without stealing focus from the user's active Chrome window.
    exit 0
}
$arguments = @(
    "--user-data-dir=`"$profile`"",
    "--profile-directory=Default",
    "--load-extension=`"$extension`"",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=9223",
    "--start-minimized",
    $workerUrl
)
$chromeProcess = Start-Process -FilePath $chromeExe -ArgumentList $arguments -WindowStyle Minimized -PassThru
$workerStarted = $false
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    $workerProcesses = @(
        Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) -and $_.CommandLine -notmatch "--type=" }
    )
    if ($workerProcesses.Count) {
        $workerStarted = $true
        break
    }
    $chromeProcess.Refresh()
    if ($chromeProcess.HasExited) { break }
    Start-Sleep -Milliseconds 500
}
if (-not $workerStarted) {
    $exitCode = if ($chromeProcess.HasExited) { $chromeProcess.ExitCode } else { "unknown" }
    throw "Dedicated Chrome did not stay running (launcher exit code: $exitCode). Check Chrome profile startup and permissions."
}
