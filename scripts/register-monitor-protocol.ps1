$ErrorActionPreference = "Stop"

$launcher = Join-Path $PSScriptRoot "open-dedicated-chrome.ps1"
$protocolKey = "HKCU:\Software\Classes\aliexpress-monitor"
$commandKey = Join-Path $protocolKey "shell\open\command"
New-Item -Path $commandKey -Force | Out-Null
Set-Item -Path $protocolKey -Value "URL:AliExpress Monitor" -Force
New-ItemProperty -Path $protocolKey -Name "URL Protocol" -Value "" -PropertyType String -Force | Out-Null
$handler = "`"$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`""
Set-Item -Path $commandKey -Value $handler
