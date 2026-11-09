$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
$CredentialPath = Join-Path $ProjectRoot '.agentforge\launcher\postgres-key.dpapi'
$Python = Join-Path $ProjectRoot '.agentforge\af012-venv\Scripts\python.exe'
$secureAdmin = $null
$adminPointer = [IntPtr]::Zero
try {
    $listener = Get-NetTCPConnection -State Listen -LocalPort 5432 -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -eq '127.0.0.1' }
    if (-not $listener) {
        @{ status = 'BLOCKED'; failure_code = 'POSTGRESQL_LISTENER_UNAVAILABLE' } |
            ConvertTo-Json -Compress
        exit 2
    }
    if (-not (Test-Path -LiteralPath $CredentialPath -PathType Leaf)) {
        @{ status = 'BLOCKED'; failure_code = 'ENCRYPTED_APP_CREDENTIAL_MISSING' } |
            ConvertTo-Json -Compress
        exit 2
    }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        @{ status = 'BLOCKED'; failure_code = 'PROJECT_PYTHON_ENVIRONMENT_MISSING' } |
            ConvertTo-Json -Compress
        exit 2
    }

    . (Join-Path $ProjectRoot 'launchers\common.ps1')
    $appPassword = Get-ConversationDatabaseKey
    $env:AGENTFORGE_DB_PASSWORD = $appPassword
    $appPassword = $null

    $secureAdmin = Read-Host 'PostgreSQL administrator password for disposable database (hidden)' -AsSecureString
    if ($secureAdmin.Length -eq 0) { throw 'Administrator credential was empty.' }
    $adminPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureAdmin)
    $env:AGENTFORGE_PG_ADMIN_PASSWORD =
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($adminPointer)

    Set-Location -LiteralPath $ProjectRoot
    & $Python -X utf8 -B (Join-Path $ProjectRoot 'AF012_MIGRATION_HOST_ACCEPTANCE.py')
    exit $LASTEXITCODE
} catch {
    @{ status = 'failed'; failure_code = $_.Exception.GetType().Name } | ConvertTo-Json -Compress
    exit 1
} finally {
    if ($adminPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($adminPointer)
    }
    if ($secureAdmin) { $secureAdmin.Dispose() }
    $env:AGENTFORGE_DB_PASSWORD = $null
    $env:AGENTFORGE_PG_ADMIN_PASSWORD = $null
}
