$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.agentforge\af012-venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'AF033_AGENT_LOOP_HOST_ACCEPTANCE.py'
$runnerExitCode = 2

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'AgentForge virtual environment was not found.'
    }
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
        throw 'Phase 3.3 host runner was not found.'
    }

    $qwenReady = $false
    $postgresReady = $false
    try {
        $qwenHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:56572/health' -Method Get -TimeoutSec 3
        $qwenReady = ($qwenHealth.status -eq 'ok')
    } catch { $qwenReady = $false }
    try {
        $postgresReady = Test-NetConnection -ComputerName '127.0.0.1' -Port 5432 `
            -InformationLevel Quiet -WarningAction SilentlyContinue
    } catch { $postgresReady = $false }

    if ($env:CODEX_SANDBOX_NETWORK_DISABLED -eq '1') {
        Write-Host 'The current environment blocks local network access; credentials will not be read.'
    } elseif ($qwenReady -and $postgresReady) {
        . (Join-Path $PSScriptRoot 'launchers\common.ps1')
        $env:AGENTFORGE_DB_PASSWORD = Get-ConversationDatabaseKey
        $env:AGENTFORGE_LLAMA_API_KEY = Get-LauncherKey $false
        Write-Host 'Qwen and PostgreSQL are ready. The runner will create isolated disposable resources.'
    } else {
        Write-Host 'A required local service is unavailable; the runner will record BLOCKED without reading credentials.'
    }

    & $python -X utf8 -B $runner
    $runnerExitCode = $LASTEXITCODE
} catch {
    Write-Host ("Host runner could not start: " + $_.Exception.GetType().Name)
    $runnerExitCode = 2
} finally {
    Remove-Item Env:AGENTFORGE_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_LLAMA_API_KEY -ErrorAction SilentlyContinue
}

exit $runnerExitCode
