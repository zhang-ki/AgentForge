param(
    [ValidateSet('general_direct', 'repository_auto', 'hybrid_knowledge', 'single_tool',
        'multi_tool_dependency', 'observation_replanning', 'tool_failure_recovery',
        'approval_pause_resume', 'long_tool_output', 'long_answer', 'multi_turn',
        'streaming_lifecycle', 'tool_timeout', 'tool_cancel')]
    [string] $Scenario,
    [ValidateSet('llama-server', 'deepseek')]
    [string] $Provider = 'llama-server',
    [string] $Model,
    [switch] $ApproveSyntheticActions
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.agentforge\af012-venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'AF042_UNIFIED_AGENT_HOST_ACCEPTANCE.py'
$runnerExitCode = 2
$stopBeforeRunner = $false
$approvalGranted = $false
$processEnvironment = [Environment]::GetEnvironmentVariables('Process')
$hadDbPassword = $processEnvironment.Contains('AGENTFORGE_DB_PASSWORD')
$originalDbPassword = [Environment]::GetEnvironmentVariable('AGENTFORGE_DB_PASSWORD', 'Process')
$hadLlamaApiKey = $processEnvironment.Contains('AGENTFORGE_LLAMA_API_KEY')
$originalLlamaApiKey = [Environment]::GetEnvironmentVariable('AGENTFORGE_LLAMA_API_KEY', 'Process')
$safeStartupMessages = @(
    'AgentForge virtual environment was not found.',
    'Phase 4.2 unified host runner was not found.',
    'Select a model ID from the latest DeepSeek model list with -Model.',
    'Synthetic action approval is only valid when the selected matrix includes approval_pause_resume.'
)

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'AgentForge virtual environment was not found.'
    }
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
        throw 'Phase 4.2 unified host runner was not found.'
    }
    if ($Provider -eq 'deepseek' -and -not $Model) {
        throw 'Select a model ID from the latest DeepSeek model list with -Model.'
    }
    if ($ApproveSyntheticActions -and $Scenario -and $Scenario -ne 'approval_pause_resume') {
        throw 'Synthetic action approval is only valid when the selected matrix includes approval_pause_resume.'
    }
    $modelReady = ($Provider -eq 'deepseek')
    $qwenReady = $false
    $postgresReady = $false
    if ($env:CODEX_SANDBOX_NETWORK_DISABLED -ne '1') {
        if ($Provider -eq 'llama-server') {
            try {
                $health = Invoke-RestMethod -Uri 'http://127.0.0.1:56572/health' -Method Get -TimeoutSec 3
                $qwenReady = ($health.status -eq 'ok')
                $modelReady = $qwenReady
            } catch { $qwenReady = $false; $modelReady = $false }
        }
        try {
            $postgresReady = Test-NetConnection -ComputerName '127.0.0.1' -Port 5432 `
                -InformationLevel Quiet -WarningAction SilentlyContinue
        } catch { $postgresReady = $false }
    }

    if ($env:CODEX_SANDBOX_NETWORK_DISABLED -eq '1') {
        Write-Host 'The current environment blocks local network access; no credentials will be read.'
    } elseif ($modelReady -and $postgresReady) {
        $approvalScenarioIncluded = (-not $Scenario -or $Scenario -eq 'approval_pause_resume')
        if ($approvalScenarioIncluded) {
            Write-Host 'Synthetic approval action: set only the in-memory flag approved_action_ran=true after the approval checkpoint; no file, database, or network mutation is performed by this action.'
            if ($ApproveSyntheticActions) {
                $approvalPrompt = 'You requested the synthetic approval path. Type APPROVE SYNTHETIC ACTION to authorize it, or DENY SYNTHETIC ACTION to stop.'
            } else {
                $approvalPrompt = 'Type APPROVE SYNTHETIC ACTION to authorize the in-memory action, or DENY SYNTHETIC ACTION to stop.'
            }
            $approvalDecision = Read-Host $approvalPrompt
            if ($approvalDecision -ceq 'APPROVE SYNTHETIC ACTION') {
                $approvalGranted = $true
            } else {
                if ($approvalDecision -cne 'DENY SYNTHETIC ACTION') {
                    Write-Host 'No exact approval or denial was entered; the host runner will not start.'
                } else {
                    Write-Host 'Synthetic action denied; the host runner will not start.'
                }
                $stopBeforeRunner = $true
                $runnerExitCode = 3
            }
        }

        if (-not $stopBeforeRunner) {
            . (Join-Path $PSScriptRoot 'launchers\common.ps1')
            $env:AGENTFORGE_DB_PASSWORD = Get-ConversationDatabaseKey
            if ($Provider -eq 'llama-server') {
                $env:AGENTFORGE_LLAMA_API_KEY = Get-LauncherKey $false
                Write-Host 'Qwen and PostgreSQL are ready. Only disposable database, Chroma, and synthetic repository data will be used.'
            } else {
                Write-Host 'PostgreSQL is ready. DeepSeek will require explicit confirmation before any cloud request; only synthetic data is used.'
            }
        }
    } else {
        Write-Host 'A required local service is unavailable; acceptance will be recorded as BLOCKED without reading credentials.'
    }

    $runnerArgs = @()
    $runnerArgs += @('--provider', $Provider)
    if ($Model) { $runnerArgs += @('--model', $Model) }
    if ($Scenario) { $runnerArgs += @('--scenario', $Scenario) }
    if ($approvalGranted) { $runnerArgs += '--approve-synthetic-actions' }
    if (-not $stopBeforeRunner) {
        & $python -X utf8 -B $runner @runnerArgs
        $runnerExitCode = $LASTEXITCODE
    }
} catch {
    if ($safeStartupMessages -contains $_.Exception.Message) {
        Write-Host ("Host runner could not start: " + $_.Exception.Message)
    } else {
        Write-Host ("Host runner could not start: " + $_.Exception.GetType().Name)
    }
    $runnerExitCode = 2
} finally {
    if ($hadDbPassword) {
        [Environment]::SetEnvironmentVariable('AGENTFORGE_DB_PASSWORD', $originalDbPassword, 'Process')
    } else {
        Remove-Item Env:AGENTFORGE_DB_PASSWORD -ErrorAction SilentlyContinue
    }
    if ($hadLlamaApiKey) {
        [Environment]::SetEnvironmentVariable('AGENTFORGE_LLAMA_API_KEY', $originalLlamaApiKey, 'Process')
    } else {
        Remove-Item Env:AGENTFORGE_LLAMA_API_KEY -ErrorAction SilentlyContinue
    }
}

exit $runnerExitCode
