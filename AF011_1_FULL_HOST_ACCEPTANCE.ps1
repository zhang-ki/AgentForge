$ErrorActionPreference = 'Stop'
try {
    $python = Join-Path $PSScriptRoot '.agentforge\af012-venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'PROJECT_PYTHON_NOT_FOUND' }
    if ($env:CODEX_SANDBOX_NETWORK_DISABLED -eq '1') {
        & $python -X utf8 -B (Join-Path $PSScriptRoot 'AF011_1_FULL_HOST_ACCEPTANCE.py')
        exit $LASTEXITCODE
    }
    . "$PSScriptRoot\launchers\common.ps1"
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.agentforge\launcher\postgres-key.dpapi') -PathType Leaf)) {
        throw 'DATABASE_CREDENTIAL_NOT_CONFIGURED'
    }
    $env:AGENTFORGE_DB_PASSWORD = Get-ConversationDatabaseKey
    $env:AGENTFORGE_LLAMA_API_KEY = Get-LauncherKey $false
    $env:AGENTFORGE_EMBEDDING_BASE_URL = 'http://127.0.0.1:58067'
    $env:AGENTFORGE_EMBEDDING_MODEL_ID = 'Qwen/Qwen3-Embedding-0.6B-GGUF@370f27d7550e0def9b39c1f16d3fbaa13aa67728:Q8_0#06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439'
    $env:AGENTFORGE_EMBEDDING_API_MODEL = 'qwen3-embedding-0.6b-q8_0-agentforge'
    $env:AGENTFORGE_EMBEDDING_DIMENSION = '1024'
    & $python -X utf8 -B (Join-Path $PSScriptRoot 'AF011_1_FULL_HOST_ACCEPTANCE.py')
    exit $LASTEXITCODE
} catch {
    $id = [guid]::NewGuid().ToString('N')
    $path = Join-Path $PSScriptRoot ("AF011_1_FULL_HOST_RESULT_" + $id + '.json')
    $value = @{ status='BLOCKED'; stage='launcher_preflight'; failure_code='LAUNCHER_PREFLIGHT_FAILED'; error_type=$_.Exception.GetType().Name; model_requests=0 }
    $value | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $path -Encoding UTF8
    @('# AgentForge 1.1 host acceptance', '', 'Status: BLOCKED', 'Stage: launcher_preflight', 'No model requests were sent.') | Set-Content -LiteralPath ([IO.Path]::ChangeExtension($path, '.md')) -Encoding UTF8
    Write-Host ("BLOCKED: launcher preflight failed. Result: " + $path)
    exit 2
} finally {
    Remove-Item Env:AGENTFORGE_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_LLAMA_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_MODEL_ID -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_API_MODEL -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_DIMENSION -ErrorAction SilentlyContinue
}
