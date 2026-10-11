$ErrorActionPreference = 'Stop'

$resolvedLocalAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$appRoot = Join-Path $resolvedLocalAppData 'AliExpressMonitor'
$keyFile = Join-Path $appRoot 'backup-key.dpapi'
$logDir = Join-Path $appRoot 'logs'
$backupScript = Join-Path $PSScriptRoot 'backup-local-postgres.ps1'
$pwsh = Join-Path $PSHOME 'pwsh.exe'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$identity = (whoami).Trim()

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
& icacls.exe $logDir /inheritance:r /grant:r `
    "$($identity):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not secure the scheduled-backup log directory.' }

$logPath = Join-Path $logDir ("backup-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd_HHmmss'))
$lines = [System.Collections.Generic.List[string]]::new()
$exitCode = 1

try {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $pwsh
    $start.WorkingDirectory = $projectRoot
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $backupScript, '-Mode', 'Backup')) {
        [void]$start.ArgumentList.Add($argument)
    }

    $process = [Diagnostics.Process]::Start($start)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    $exitCode = $process.ExitCode

    $lines.Add("started_at=$([DateTimeOffset]::Now.ToString('o'))")
    $lines.Add("user=$identity")
    $lines.Add("user_sid=$([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)")
    $interactiveSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-4')
    $lines.Add("interactive_group_present=$([Security.Principal.WindowsIdentity]::GetCurrent().Groups.Contains($interactiveSid))")
    $lines.Add("local_app_data=$resolvedLocalAppData")
    $lines.Add("app_root_exists=$(Test-Path -LiteralPath $appRoot)")
    $lines.Add("app_root_children=$(([IO.Directory]::GetDirectories($appRoot) | ForEach-Object { Split-Path -Leaf $_ }) -join ',')")
    $lines.Add("key_dir_exists=$(Test-Path -LiteralPath (Split-Path -Parent $keyFile))")
    $lines.Add("key_file_exists=$([IO.File]::Exists($keyFile))")
    try {
        $visibleDirs = [IO.Directory]::GetDirectories($appRoot)
        $lines.Add("key_dir_visible=$($visibleDirs -contains (Split-Path -Parent $keyFile))")
    }
    catch { $lines.Add("key_dir_access_error=$($_.Exception.GetType().Name):$($_.Exception.Message)") }
    try {
        $keyProbe = [IO.File]::OpenRead($keyFile)
        $keyProbe.Dispose()
        $lines.Add('key_file_readable=true')
    }
    catch {
        $inner = $_.Exception.InnerException
        $detail = if ($inner) { "$($inner.GetType().Name):$($inner.Message)" } else { "$($_.Exception.GetType().Name):$($_.Exception.Message)" }
        $lines.Add("key_file_access_error=$detail")
    }
    $lines.Add("pwsh=$pwsh")
    $lines.Add("docker_cli=$((Get-Command docker.exe -ErrorAction SilentlyContinue).Source)")
    $lines.Add("exit_code=$exitCode")
    $lines.Add('stdout:')
    $lines.Add($stdout.TrimEnd())
    $lines.Add('stderr:')
    $lines.Add($stderr.TrimEnd())
}
catch {
    $lines.Add("runner_error=$($_.Exception.Message)")
    $exitCode = 1
}
finally {
    [IO.File]::WriteAllLines($logPath, $lines, [Text.UTF8Encoding]::new($false))
}

if ($exitCode -ne 0) { exit $exitCode }
