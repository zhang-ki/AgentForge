$ErrorActionPreference = 'Stop'
try {
    $python = Join-Path $PSScriptRoot '.agentforge\af012-venv\Scripts\python.exe'
    if ($env:CODEX_SANDBOX_NETWORK_DISABLED -eq '1') {
        & $python -X utf8 -B (Join-Path $PSScriptRoot 'AF011_1_JOINT_HOST.py')
        exit $LASTEXITCODE
    }
    . "$PSScriptRoot\launchers\common.ps1"
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.agentforge\launcher\postgres-key.dpapi') -PathType Leaf)) {
        throw 'Existing local database credential not found; configure AgentForge first.'
    }
    $env:AGENTFORGE_DB_PASSWORD = Get-ConversationDatabaseKey
    $env:AGENTFORGE_LLAMA_API_KEY = Get-LauncherKey $false
    $env:AGENTFORGE_EMBEDDING_BASE_URL = 'http://127.0.0.1:58067'
    $env:AGENTFORGE_EMBEDDING_MODEL_ID = 'Qwen/Qwen3-Embedding-0.6B-GGUF@370f27d7550e0def9b39c1f16d3fbaa13aa67728:Q8_0#06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439'
    $env:AGENTFORGE_EMBEDDING_API_MODEL = 'qwen3-embedding-0.6b-q8_0-agentforge'
    $env:AGENTFORGE_EMBEDDING_DIMENSION = '1024'
    & $python -X utf8 -B (Join-Path $PSScriptRoot 'AF011_1_JOINT_HOST.py')
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:AGENTFORGE_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_LLAMA_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_MODEL_ID -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_API_MODEL -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_DIMENSION -ErrorAction SilentlyContinue
}
