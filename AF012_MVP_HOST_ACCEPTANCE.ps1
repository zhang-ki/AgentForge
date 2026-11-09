$ErrorActionPreference = 'Stop'
try {
    if (-not $env:AF012_TEST_DATABASE -or -not $env:AF012_TEST_CHROMA_PATH) {
        Write-Host 'This is an isolated child runner. Use AF012_DUAL_CAPABILITY_HOST_ACCEPTANCE.ps1; no credential or service was accessed.'
        exit 2
    }    . "$PSScriptRoot\launchers\common.ps1"
    $env:AGENTFORGE_DB_PASSWORD = Get-ConversationDatabaseKey
    $env:AGENTFORGE_LLAMA_API_KEY = Get-LauncherKey $false
    $env:AGENTFORGE_EMBEDDING_BASE_URL = 'http://127.0.0.1:58067'
    $env:AGENTFORGE_EMBEDDING_MODEL_ID = 'Qwen/Qwen3-Embedding-0.6B-GGUF@370f27d7550e0def9b39c1f16d3fbaa13aa67728:Q8_0#06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439'
    $env:AGENTFORGE_EMBEDDING_API_MODEL = 'qwen3-embedding-0.6b-q8_0-agentforge'
    $env:AGENTFORGE_EMBEDDING_DIMENSION = '1024'
    $python = Join-Path $PSScriptRoot '.agentforge\af012-venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw '找不到 AF-012 独立 Python 环境。' }
    Write-Host '将只连接已运行的本机 PostgreSQL、聊天模型与 Embedding 模型；此脚本不会启动或停止服务。'
    Write-Host '真实模型请求不重试，PostgreSQL 与 Chroma 测试数据使用唯一临时标识并在结束时清理。'
    & $python -X utf8 -B (Join-Path $PSScriptRoot 'AF012_MVP_HOST_ACCEPTANCE.py')
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:AGENTFORGE_DB_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_LLAMA_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_MODEL_ID -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_API_MODEL -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_EMBEDDING_DIMENSION -ErrorAction SilentlyContinue
}
