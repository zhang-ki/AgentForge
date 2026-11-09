$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.agentforge\af012-venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'AF042_PENDING_DATABASE_INSPECTION.py'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'AgentForge virtual environment was not found.'
}
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw 'Pending database inspector was not found.'
}

& $python -X utf8 -B $runner
exit $LASTEXITCODE
