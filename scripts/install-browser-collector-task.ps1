$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $projectRoot "start-browser-collector.bat"
$profileRoot = Join-Path $env:LOCALAPPDATA "AliExpressMonitor\ChromeProfile"
$taskName = "AliExpress Monitor Daily Collection"

$chromeCandidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

if (-not $chromeCandidates) {
    throw "Google Chrome was not found. Install Chrome before configuring the collector."
}
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Collector launcher was not found: $launcher"
}

New-Item -ItemType Directory -Path $profileRoot -Force | Out-Null

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/d /c `"`"$launcher`"`""
$trigger = New-ScheduledTaskTrigger -Daily -At "02:00"
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Description "Collect public AliExpress store and product observations for AliExpress Monitor." `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

$chrome = [string]($chromeCandidates | Select-Object -First 1)
$chromeArguments = @(
    "--user-data-dir=$profileRoot",
    "--profile-directory=Default",
    "--new-window",
    "chrome://extensions"
)
try {
    Start-Process -FilePath $chrome -ArgumentList $chromeArguments
} catch {
    Write-Warning "The scheduled task was created, but Chrome could not be opened automatically: $($_.Exception.Message)"
}

Write-Host "Scheduled task created: $taskName"
Write-Host "Dedicated Chrome profile: $profileRoot"
Write-Host "Load the unpacked extension from: $(Join-Path $projectRoot 'extension')"
Write-Host "Then sign in to AliExpress normally in this dedicated Chrome profile."
