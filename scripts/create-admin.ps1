[CmdletBinding()]
param(
    [string]$Username = ""
)

$ErrorActionPreference = "Stop"
if (-not $Username) { $Username = Read-Host "Admin username" }
$securePassword = Read-Host "Admin password" -AsSecureString
$password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
)
try {
    Write-Host "Building Backend image and applying database migrations..."
    docker compose build backend
    if ($LASTEXITCODE -ne 0) { throw "Backend image build failed." }
    docker compose run --rm --no-deps backend alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Database migration failed." }
    docker compose run --rm --no-deps `
        -e "AUTH_REQUIRED=true" `
        -e "AUTH_ADMIN_USERNAME=$Username" `
        -e "AUTH_ADMIN_PASSWORD=$password" `
        backend python -c "from app.core.auth import ensure_bootstrap_admin; from app.db.session import get_session_factory; db=get_session_factory()(); ensure_bootstrap_admin(db); db.close(); print('Admin account created or already exists')"
    if ($LASTEXITCODE -ne 0) { throw "Admin account creation failed. Check the database connection." }
    Write-Host "Restarting authenticated services..."
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) { throw "Authenticated services failed to start." }
    Write-Host "Admin account is ready. Authenticated monitor is starting."
}
finally {
    $password = $null
}
