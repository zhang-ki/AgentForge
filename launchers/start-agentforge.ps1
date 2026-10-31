try {
    . "$PSScriptRoot\common.ps1"
    Set-Location -LiteralPath $ProjectRoot
    if (-not (Get-Command python.exe -ErrorAction SilentlyContinue)) { throw '找不到 Python，请安装 Python 3.10 或以上并加入 PATH。' }
    $env:AGENTFORGE_LLAMA_API_KEY = Get-LauncherKey $false
    Write-Host 'AgentForge 本地代码问答。请先保持千问服务窗口运行。'
    Write-Host '使用当前已有索引；输入 exit 退出。每个问题独立提交，不保留聊天上下文。'
    Write-Host '示例：What does function refresh_access_token return for session_id? Cite the exact source.'
    while ($true) {
        $query = Read-Host '请输入问题'
        if ($null -eq $query -or $query.Trim() -eq 'exit') { break }
        if ([string]::IsNullOrWhiteSpace($query)) { continue }
        & python.exe -X utf8 -B -m agentforge ask $query --provider llama-server --base-url "http://127.0.0.1:$Port/v1/chat/completions" --model $ModelName --read-timeout 120
        if ($LASTEXITCODE -ne 0) { Write-Host "请求失败（退出码 $LASTEXITCODE）。确认服务已加载，并查看上方错误。" }
        Write-Host ''
    }
} catch { Write-Host "启动失败：$($_.Exception.Message)"; exit 1 }
