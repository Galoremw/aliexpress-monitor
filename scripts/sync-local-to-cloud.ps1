[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CloudDatabaseUrl,
    [switch]$ConfirmOverwrite,
    [string]$DumpPath = (Join-Path $env:TEMP "aliexpress-monitor-local.sql"),
    [string]$SourceDatabaseUrl = ""
)

$ErrorActionPreference = "Stop"
if (-not $ConfirmOverwrite) {
    throw "这是覆盖式同步。确认云端数据库可以被本地数据替换后，重新执行并追加 -ConfirmOverwrite。"
}

function Read-DotEnvValue([string]$name) {
    $envPath = Join-Path $PSScriptRoot "..\.env"
    if (-not (Test-Path -LiteralPath $envPath)) { return $null }
    $line = Get-Content -LiteralPath $envPath | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -First 1
    if (-not $line) { return $null }
    return ($line -replace "^\s*$name\s*=\s*", "").Trim().Trim('"').Trim("'")
}

if (-not $SourceDatabaseUrl) {
    $SourceDatabaseUrl = Read-DotEnvValue "DATABASE_URL"
}
if (-not $SourceDatabaseUrl) {
    $SourceDatabaseUrl = "postgresql://monitor:monitor-local-only@localhost:5432/monitor"
}

function Normalize-PsqlUrl([string]$url) {
    if ($url.StartsWith("postgresql+psycopg://")) {
        return "postgresql://" + $url.Substring("postgresql+psycopg://".Length)
    }
    if ($url.StartsWith("postgres+psycopg://")) {
        return "postgresql://" + $url.Substring("postgres+psycopg://".Length)
    }
    if ($url.StartsWith("postgres://")) {
        return "postgresql://" + $url.Substring("postgres://".Length)
    }
    return $url
}

$SourceDatabaseUrl = Normalize-PsqlUrl $SourceDatabaseUrl
$CloudDatabaseUrl = Normalize-PsqlUrl $CloudDatabaseUrl

Write-Host "导出本地监控数据到 $DumpPath ..."
docker run --rm postgres:17-alpine pg_dump $SourceDatabaseUrl --schema=public --clean --if-exists --no-owner --no-privileges --exclude-table-data=public.auth_sessions | Set-Content -LiteralPath $DumpPath -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw "本地数据库导出失败。" }

Write-Host "导入云端 PostgreSQL..."
Get-Content -LiteralPath $DumpPath -Raw | docker run --rm -i postgres:17-alpine psql $CloudDatabaseUrl -v ON_ERROR_STOP=1
if ($LASTEXITCODE -ne 0) { throw "云端数据库导入失败；本地导出文件已保留：$DumpPath" }

Remove-Item -LiteralPath $DumpPath -Force
Write-Host "同步完成。云端数据已替换为本地数据；首次登录使用本地管理员账号。"
