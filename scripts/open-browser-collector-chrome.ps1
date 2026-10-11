$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$extensionPath = Join-Path $projectRoot "extension"
$profilePath = Join-Path $env:LOCALAPPDATA "AliExpressMonitor\BrowserCollectorProfile"
$dashboardUrl = "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated"

if (-not (Test-Path -LiteralPath (Join-Path $extensionPath "manifest.json"))) {
    throw "Chrome extension was not found: $extensionPath"
}

$chromeCandidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
$chromePath = $chromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $chromePath) {
    $chromeCommand = Get-Command "chrome.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($chromeCommand) { $chromePath = $chromeCommand.Source }
}
if (-not $chromePath) {
    throw "Google Chrome was not found in its standard installation paths or PATH."
}

$profileArgumentPattern = "(?i)--user-data-dir=(?:`"$([regex]::Escape($profilePath))`"|$([regex]::Escape($profilePath)))(?=\s|$)"
$profileProcesses = Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $profileArgumentPattern }
if ($profileProcesses) {
    Write-Host "The dedicated browser collector profile is already running."
    exit 0
}

$chromeArguments = @(
    "--user-data-dir=`"$profilePath`"",
    "--profile-directory=Default",
    "--load-extension=`"$extensionPath`"",
    "--no-first-run",
    "--no-default-browser-check",
    "--start-minimized",
    $dashboardUrl
)
Start-Process -FilePath $chromePath -ArgumentList $chromeArguments
Write-Host "Started the isolated browser collector profile."
