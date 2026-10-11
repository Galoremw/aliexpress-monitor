$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$healthUrls = @("http://127.0.0.1:8000/health", "http://127.0.0.1:3000/health")

function Test-MonitorHealth {
    foreach ($url in $healthUrls) {
        try { Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 3 | Out-Null }
        catch { return $false }
    }
    return $true
}

if (-not (Test-MonitorHealth)) {
    $startScript = Join-Path $projectRoot "start.bat"
    & $startScript --no-open
    $healthy = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if (Test-MonitorHealth) {
            $healthy = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $healthy) { throw "AliExpress Monitor backend or frontend did not become healthy." }
}

$chromeCandidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
$chromeExe = $chromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $chromeExe) { throw "Google Chrome was not found." }

$workerUrl = "http://127.0.0.1:3000/?dianxiaomi_worker=1&dianxiaomi_local=1"
Start-Process -FilePath $chromeExe -ArgumentList @("--new-tab", $workerUrl)
