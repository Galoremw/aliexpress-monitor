[CmdletBinding()]
param(
    [ValidateSet('InitializeKey', 'Backup', 'VerifyLatest', 'Decrypt')]
    [string]$Mode = 'Backup',
    [string]$ArchivePath,
    [string]$RecoveryKeyPath,
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$userDocuments = [Environment]::GetFolderPath([Environment+SpecialFolder]::MyDocuments)
if (-not $localAppData -or -not $userDocuments) { throw 'Could not resolve the current user profile folders.' }
$appRoot = Join-Path $localAppData 'AliExpressMonitor'
$keyDir = Join-Path $appRoot 'keys'
$keyFile = Join-Path $appRoot 'backup-key.dpapi'
$defaultRecoveryKey = Join-Path $keyDir 'recovery-key.txt'
$localBackupDir = Join-Path $appRoot 'backups'
$quarkUploadDir = Join-Path $userDocuments 'QuarkAutoBackup\AliExpressMonitor'
$workDir = Join-Path $appRoot 'work'
$header = [byte[]](0x41, 0x45, 0x4D, 0x42, 0x01)
$entropy = [Text.Encoding]::UTF8.GetBytes('AliExpressMonitor AES-256-GCM key v1')
$docker = (Get-Command docker.exe -ErrorAction Stop).Source

Add-Type -AssemblyName System.Security.Cryptography.ProtectedData

function Set-PrivateDirectory([string]$Path) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    $identity = (whoami).Trim()
    & icacls.exe $Path /inheritance:r /grant:r `
        "$($identity):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not secure directory: $Path" }
}

function Get-PostgresContainerId {
    Push-Location $projectRoot
    try {
        $id = (& $docker compose ps -q postgres | Select-Object -First 1).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $id) { throw 'Local Docker PostgreSQL is not running.' }
        return $id
    }
    finally { Pop-Location }
}

function Invoke-DockerText([string[]]$Arguments) {
    $result = @(& $docker @Arguments)
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed: $($Arguments[0])" }
    return $result
}

function Get-PlainDump([string]$ContainerId) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $docker
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in @(
        'exec', $ContainerId, 'pg_dump', '--username=monitor', '--dbname=monitor',
        '--format=custom', '--schema=public', '--exclude-table-data=public.auth_sessions',
        '--no-owner', '--no-privileges'
    )) { [void]$start.ArgumentList.Add($argument) }

    $process = [Diagnostics.Process]::Start($start)
    $errorTask = $process.StandardError.ReadToEndAsync()
    $stream = [IO.MemoryStream]::new()
    $process.StandardOutput.BaseStream.CopyTo($stream)
    $process.WaitForExit()
    $stderr = $errorTask.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0) { throw 'pg_dump failed; no backup was published.' }
    $bytes = $stream.ToArray()
    if ($bytes.Length -lt 5 -or [Text.Encoding]::ASCII.GetString($bytes, 0, 5) -ne 'PGDMP') {
        throw 'pg_dump returned an invalid custom archive.'
    }
    return ,$bytes
}

function Get-BackupKey([string]$RecoveryFile) {
    if ($RecoveryFile) {
        $encoded = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $RecoveryFile).Path).Trim()
        $key = [Convert]::FromBase64String($encoded)
    }
    else {
        if (-not (Test-Path -LiteralPath $keyFile)) { throw 'Backup encryption key is not initialized.' }
        $protected = [IO.File]::ReadAllBytes($keyFile)
        $key = [Security.Cryptography.ProtectedData]::Unprotect(
            $protected, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser
        )
    }
    if ($key.Length -ne 32) { throw 'Backup key must contain exactly 256 bits.' }
    return ,$key
}

function Protect-Archive([byte[]]$Plain, [byte[]]$Key) {
    $nonce = [byte[]]::new(12)
    [Security.Cryptography.RandomNumberGenerator]::Fill($nonce)
    $cipher = [byte[]]::new($Plain.Length)
    $tag = [byte[]]::new(16)
    $aes = [Security.Cryptography.AesGcm]::new($Key, 16)
    try { $aes.Encrypt($nonce, $Plain, $cipher, $tag, $header) }
    finally { $aes.Dispose() }

    $payload = [byte[]]::new($header.Length + $nonce.Length + $tag.Length + $cipher.Length)
    [Array]::Copy($header, 0, $payload, 0, $header.Length)
    [Array]::Copy($nonce, 0, $payload, $header.Length, $nonce.Length)
    [Array]::Copy($tag, 0, $payload, $header.Length + $nonce.Length, $tag.Length)
    [Array]::Copy($cipher, 0, $payload, $header.Length + $nonce.Length + $tag.Length, $cipher.Length)
    return ,$payload
}

function Unprotect-Archive([string]$Path, [byte[]]$Key) {
    $payload = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $Path).Path)
    $prefixLength = $header.Length + 12 + 16
    if ($payload.Length -le $prefixLength) { throw 'Encrypted archive is truncated.' }
    for ($i = 0; $i -lt $header.Length; $i++) {
        if ($payload[$i] -ne $header[$i]) { throw 'Unknown encrypted archive format.' }
    }
    $nonce = [byte[]]::new(12)
    $tag = [byte[]]::new(16)
    $cipher = [byte[]]::new($payload.Length - $prefixLength)
    [Array]::Copy($payload, $header.Length, $nonce, 0, $nonce.Length)
    [Array]::Copy($payload, $header.Length + $nonce.Length, $tag, 0, $tag.Length)
    [Array]::Copy($payload, $prefixLength, $cipher, 0, $cipher.Length)
    $plain = [byte[]]::new($cipher.Length)
    $aes = [Security.Cryptography.AesGcm]::new($Key, 16)
    try { $aes.Decrypt($nonce, $cipher, $tag, $plain, $header) }
    finally { $aes.Dispose() }
    if ($plain.Length -lt 5 -or [Text.Encoding]::ASCII.GetString($plain, 0, 5) -ne 'PGDMP') {
        throw 'Decrypted content is not a PostgreSQL custom archive.'
    }
    return ,$plain
}

function Get-DatabaseSummary([string]$ContainerId, [string]$Database) {
    $sql = @'
SELECT 'alembic=' || version_num FROM alembic_version;
SELECT 'users=' || count(*) FROM users;
SELECT 'stores=' || count(*) FROM stores;
SELECT 'products=' || count(*) FROM products;
SELECT 'product_snapshots=' || count(*) FROM product_snapshots;
SELECT 'store_snapshots=' || count(*) FROM store_snapshots;
SELECT 'product_daily_metrics=' || count(*) FROM product_daily_metrics;
SELECT 'store_daily_metrics=' || count(*) FROM store_daily_metrics;
SELECT 'collection_attempts=' || count(*) FROM collection_attempts;
SELECT 'manual_collection_tasks=' || count(*) FROM manual_collection_tasks;
SELECT 'browser_collection_runs=' || count(*) FROM browser_collection_runs;
SELECT 'browser_collection_items=' || count(*) FROM browser_collection_items;
SELECT 'product_reviews=' || count(*) FROM product_reviews;
SELECT 'review_collection_tasks=' || count(*) FROM review_collection_tasks;
SELECT 'dianxiaomi_handoffs=' || count(*) FROM dianxiaomi_handoffs;
SELECT 'auth_sessions=' || count(*) FROM auth_sessions;
SELECT 'last_run=' || id || ':' || status || ':' || coalesce(paused_reason, '') FROM browser_collection_runs ORDER BY id DESC LIMIT 1;
SELECT 'last_item_' || status || '=' || count(*) FROM browser_collection_items WHERE run_id = (SELECT max(id) FROM browser_collection_runs) GROUP BY status ORDER BY status;
'@
    $lines = @(& $docker exec $ContainerId psql -X -A -t -U monitor -d $Database -c $sql)
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect database $Database." }
    return ,@($lines | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Sort-Object)
}

function Restore-ArchiveToTemporaryDatabase([byte[]]$Plain, [string]$SourceContainer) {
    $networkJson = (& $docker inspect --format '{{json .NetworkSettings.Networks}}' $SourceContainer | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0) { throw 'Could not identify the Docker database network.' }
    $network = $networkJson.PSObject.Properties.Name | Select-Object -First 1
    if (-not $network) { throw 'Docker database network is unavailable.' }

    $name = 'aem-backup-verify-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
    $runArgs = @(
        'run', '--rm', '-d', '--name', $name, '--network', $network,
        '--tmpfs', '/var/lib/postgresql/data:rw,size=536870912',
        '-e', 'POSTGRES_DB=monitor_check', '-e', 'POSTGRES_USER=monitor',
        '-e', 'POSTGRES_PASSWORD=verify-only-not-real', 'postgres:17-alpine'
    )
    $null = Invoke-DockerText $runArgs
    try {
        $ready = $false
        for ($i = 0; $i -lt 60; $i++) {
            & $docker exec $name pg_isready -U monitor -d monitor_check *> $null
            if ($LASTEXITCODE -eq 0) { $ready = $true; break }
            Start-Sleep -Seconds 1
        }
        if (-not $ready) { throw 'Temporary PostgreSQL did not become ready.' }

        $start = [Diagnostics.ProcessStartInfo]::new()
        $start.FileName = $docker
        $start.UseShellExecute = $false
        $start.RedirectStandardInput = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        foreach ($argument in @(
            'exec', '-i', $name, 'pg_restore', '--clean', '--if-exists', '--exit-on-error',
            '--no-owner', '--no-privileges', '-U', 'monitor', '-d', 'monitor_check'
        )) { [void]$start.ArgumentList.Add($argument) }

        $process = [Diagnostics.Process]::Start($start)
        $errorTask = $process.StandardError.ReadToEndAsync()
        $plainStream = $process.StandardInput.BaseStream
        $plainStream.Write($Plain, 0, $Plain.Length)
        $plainStream.Close()
        $null = $process.StandardOutput.ReadToEnd()
        $process.WaitForExit()
        $restoreError = $errorTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw "Temporary pg_restore failed: $restoreError" }

        $sourceSummary = Get-DatabaseSummary $SourceContainer 'monitor'
        $restoredSummary = Get-DatabaseSummary $name 'monitor_check'
        $diff = @(Compare-Object -ReferenceObject $sourceSummary -DifferenceObject $restoredSummary)
        if ($diff.Count -gt 0) { throw 'Restored backup data does not match the live database.' }
        return $restoredSummary
    }
    finally {
        & $docker stop --time 3 $name *> $null
    }
}

try {
    switch ($Mode) {
        'InitializeKey' {
            Set-PrivateDirectory $keyDir
            if (Test-Path -LiteralPath $keyFile) { throw 'A DPAPI key already exists; refusing to replace it.' }
            if (Test-Path -LiteralPath $defaultRecoveryKey) { throw 'A recovery-key file already exists; refusing to replace it.' }
            $key = [byte[]]::new(32)
            [Security.Cryptography.RandomNumberGenerator]::Fill($key)
            $protected = [Security.Cryptography.ProtectedData]::Protect(
                $key, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser
            )
            [IO.File]::WriteAllBytes($keyFile, $protected)
            [IO.File]::WriteAllText($defaultRecoveryKey, [Convert]::ToBase64String($key), [Text.UTF8Encoding]::new($false))
            [Array]::Clear($key, 0, $key.Length)
            Write-Output "key_initialized recovery_key_file=$defaultRecoveryKey"
        }

        'Backup' {
            Set-PrivateDirectory $localBackupDir
            Set-PrivateDirectory $quarkUploadDir
            Set-PrivateDirectory $workDir
            $key = Get-BackupKey ''
            $container = Get-PostgresContainerId
            $plain = Get-PlainDump $container
            $payload = Protect-Archive $plain $key
            $stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
            $name = "aliexpress-monitor-$stamp.aemb"
            $workFile = Join-Path $workDir $name
            $localFile = Join-Path $localBackupDir $name
            $quarkFile = Join-Path $quarkUploadDir $name
            if ((Test-Path -LiteralPath $localFile) -or (Test-Path -LiteralPath $quarkFile)) {
                throw 'A backup with this timestamp already exists; refusing to overwrite.'
            }
            [IO.File]::WriteAllBytes($workFile, $payload)
            [IO.File]::Move($workFile, $localFile)
            [IO.File]::Copy($localFile, $quarkFile, $false)
            $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $localFile).Hash

            $archives = @(Get-ChildItem -LiteralPath $localBackupDir -File -Filter 'aliexpress-monitor-*.aemb' |
                Sort-Object LastWriteTimeUtc -Descending)
            foreach ($old in ($archives | Select-Object -Skip 12)) {
                if ($old.DirectoryName -eq $localBackupDir -and $old.Name -match '^aliexpress-monitor-[0-9-]+_[0-9]+\.aemb$') {
                    Remove-Item -LiteralPath $old.FullName -Force
                }
            }
            [Array]::Clear($plain, 0, $plain.Length)
            [Array]::Clear($key, 0, $key.Length)
            [Array]::Clear($payload, 0, $payload.Length)
            Write-Output ([pscustomobject]@{status='backup_created';local_file=$localFile;quark_upload_file=$quarkFile;bytes=(Get-Item -LiteralPath $localFile).Length;sha256=$hash;local_retention=12;quark_copies_deleted='no'} | ConvertTo-Json -Compress)
        }

        'VerifyLatest' {
            Set-PrivateDirectory $workDir
            if (-not $ArchivePath) {
                $latest = Get-ChildItem -LiteralPath $localBackupDir -File -Filter 'aliexpress-monitor-*.aemb' |
                    Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
                if (-not $latest) { throw 'No encrypted backup exists to verify.' }
                $ArchivePath = $latest.FullName
            }
            $key = Get-BackupKey $RecoveryKeyPath
            $plain = Unprotect-Archive $ArchivePath $key
            $container = Get-PostgresContainerId
            $summary = Restore-ArchiveToTemporaryDatabase $plain $container
            [Array]::Clear($plain, 0, $plain.Length)
            [Array]::Clear($key, 0, $key.Length)
            Write-Output ([pscustomobject]@{status='restore_verified';archive=(Resolve-Path -LiteralPath $ArchivePath).Path;database_summary=$summary} | ConvertTo-Json -Compress -Depth 5)
        }

        'Decrypt' {
            if (-not $ArchivePath -or -not $RecoveryKeyPath -or -not $OutputPath) {
                throw 'Decrypt requires -ArchivePath, -RecoveryKeyPath, and -OutputPath.'
            }
            $resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
            if (Test-Path -LiteralPath $resolvedOutput) {
                throw 'OutputPath already exists; refusing to overwrite a file.'
            }
            if ($resolvedOutput.StartsWith([IO.Path]::GetFullPath($quarkUploadDir), [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Plaintext restore files must not be written into the Quark upload directory.'
            }
            $key = Get-BackupKey $RecoveryKeyPath
            $plain = Unprotect-Archive $ArchivePath $key
            [IO.File]::WriteAllBytes($resolvedOutput, $plain)
            [Array]::Clear($plain, 0, $plain.Length)
            [Array]::Clear($key, 0, $key.Length)
            Write-Output "decrypted_archive=$resolvedOutput"
        }
    }
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
