param(
    [string]$RegistryRoot = "HKCU:\Software\Classes\aliexpress-monitor-collector"
)

$ErrorActionPreference = "Stop"
$launcher = Join-Path $PSScriptRoot "open-browser-collector-chrome.ps1"
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Browser collector launcher was not found: $launcher"
}

$commandKey = Join-Path $RegistryRoot "shell\open\command"
New-Item -Path $commandKey -Force | Out-Null
Set-Item -Path $RegistryRoot -Value "URL:AliExpress Monitor Collector" -Force
New-ItemProperty -Path $RegistryRoot -Name "URL Protocol" -Value "" -PropertyType String -Force | Out-Null

$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$handler = "`"$powershell`" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" `"%1`""
Set-Item -Path $commandKey -Value $handler
