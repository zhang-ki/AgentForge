$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.agentforge\af012-venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'AF012_MULTIMODEL_HOST_ACCEPTANCE.py'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Error 'Project Python environment was not found. Create the documented AgentForge virtual environment first.'
    exit 2
}
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    Write-Error 'The AgentForge multi-model acceptance runner is missing.'
    exit 2
}

Write-Host 'This bounded check never starts model services and sends only synthetic source text to DeepSeek.'
Write-Host 'It asks before any cloud request, makes no retries, and does not test or modify formal database records.'
& $python -X utf8 -B $runner
exit $LASTEXITCODE
