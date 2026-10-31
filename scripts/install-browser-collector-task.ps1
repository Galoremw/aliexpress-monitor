$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $projectRoot "start-browser-collector.bat"
$taskName = "AliExpress Monitor Daily Collection"
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Collector launcher was not found: $launcher"
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/d /c `"`"$launcher`"`""
$trigger = New-ScheduledTaskTrigger -Daily -At "15:15"
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
    -Description "Ensure AliExpress Monitor is running and launch its separate BrowserCollectorProfile for collection." `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Scheduled task created: $taskName"
Write-Host "Daily collection uses the separate BrowserCollectorProfile."
Write-Host "The task does not close or reuse your everyday Chrome profile."
