try {
    . "$PSScriptRoot\common.ps1"
    Set-Location -LiteralPath $ProjectRoot
    $Python = Join-Path $ProjectRoot '.agentforge\af012-venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw '找不到 AF-012 项目环境，请先完成数据库依赖安装。' }
    $env:AGENTFORGE_LLAMA_API_KEY = Get-LauncherKey $false
    $env:AGENTFORGE_DB_PASSWORD = Get-ConversationDatabaseKey
    if (-not $env:AGENTFORGE_EMBEDDING_BASE_URL) {
        $env:AGENTFORGE_EMBEDDING_BASE_URL = 'http://127.0.0.1:58067'
    }
    if (-not $env:AGENTFORGE_EMBEDDING_MODEL_ID) {
        $env:AGENTFORGE_EMBEDDING_MODEL_ID = 'Qwen/Qwen3-Embedding-0.6B-GGUF@370f27d7550e0def9b39c1f16d3fbaa13aa67728:Q8_0#06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439'
    }
    if (-not $env:AGENTFORGE_EMBEDDING_API_MODEL) {
        $env:AGENTFORGE_EMBEDDING_API_MODEL = 'qwen3-embedding-0.6b-q8_0-agentforge'
    }
    if (-not $env:AGENTFORGE_EMBEDDING_DIMENSION) {
        $env:AGENTFORGE_EMBEDDING_DIMENSION = '1024'
    }
    Write-Host '请保持 PostgreSQL 和千问服务窗口运行。'
    & $Python -X utf8 -B -m agentforge session migrate
    if ($LASTEXITCODE -ne 0) { throw '数据库连接或迁移失败；未启动会话。' }
    & $Python -X utf8 -B -m agentforge.conversation_console
    if ($LASTEXITCODE -ne 0) { throw '会话启动失败，请查看上方说明。' }
} catch { Write-Host "启动失败：$($_.Exception.Message)"; exit 1 }
finally { Remove-Item Env:AGENTFORGE_DB_PASSWORD -ErrorAction SilentlyContinue }
