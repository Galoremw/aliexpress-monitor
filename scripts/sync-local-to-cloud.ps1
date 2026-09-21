[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CloudDatabaseUrl,
    [switch]$ConfirmOverwrite,
    [string]$DumpPath = (Join-Path $env:TEMP "aliexpress-monitor-local.sql")
)

$ErrorActionPreference = "Stop"
if (-not $ConfirmOverwrite) {
    throw "这是覆盖式同步。确认云端数据库可以被本地数据替换后，重新执行并追加 -ConfirmOverwrite。"
}

Write-Host "检查本地 PostgreSQL 容器..."
docker compose ps postgres | Out-Host
if ($LASTEXITCODE -ne 0) { throw "无法访问本地 PostgreSQL，请先执行 docker compose up -d postgres。" }

Write-Host "导出本地监控数据到 $DumpPath ..."
docker compose exec -T postgres pg_dump -U monitor -d monitor --clean --if-exists --no-owner --no-privileges --exclude-table-data=auth_sessions | Set-Content -LiteralPath $DumpPath -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw "本地数据库导出失败。" }

Write-Host "导入云端 PostgreSQL..."
Get-Content -LiteralPath $DumpPath -Raw | docker run --rm -i postgres:17-alpine psql $CloudDatabaseUrl -v ON_ERROR_STOP=1
if ($LASTEXITCODE -ne 0) { throw "云端数据库导入失败；本地导出文件已保留：$DumpPath" }

Remove-Item -LiteralPath $DumpPath -Force
Write-Host "同步完成。云端数据已替换为本地数据；首次登录使用本地管理员账号。"
