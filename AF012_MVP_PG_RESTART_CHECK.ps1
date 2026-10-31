$ErrorActionPreference = 'Stop'
try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this check from an elevated PowerShell window. It restarts only postgresql-x64-18.'
    }
    . "$PSScriptRoot\launchers\common.ps1"
    $env:AGENTFORGE_DB_PASSWORD = Get-ConversationDatabaseKey
    $python = Join-Path $PSScriptRoot '.agentforge\af012-venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'AF-012 isolated Python environment is missing.' }
    $checkpoint = Join-Path $PSScriptRoot ('.agentforge\data\af012-mvp-restart-' + [Guid]::NewGuid().ToString('N') + '.json')
    Write-Host 'Preparing disposable session, message, approved-memory and outbox records.'
    & $python -X utf8 -B (Join-Path $PSScriptRoot 'AF012_MVP_PG_RESTART_CHECK.py') prepare $checkpoint
    if ($LASTEXITCODE -ne 0) { throw 'The restart check could not prepare; PostgreSQL was not restarted.' }
    try {
        Restart-Service -Name 'postgresql-x64-18' -ErrorAction Stop
    } catch {
        & $python -X utf8 -B (Join-Path $PSScriptRoot 'AF012_MVP_PG_RESTART_CHECK.py') verify $checkpoint
        throw 'PostgreSQL restart failed or was not permitted; the verification result records the outcome.'
    }
    & $python -X utf8 -B (Join-Path $PSScriptRoot 'AF012_MVP_PG_RESTART_CHECK.py') verify $checkpoint
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:AGENTFORGE_DB_PASSWORD -ErrorAction SilentlyContinue
}
