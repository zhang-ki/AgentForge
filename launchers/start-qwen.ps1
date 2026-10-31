try {
    . "$PSScriptRoot\common.ps1"
    $modelRoot = if ($env:AGENTFORGE_MODEL_ROOT) {
        [System.IO.Path]::GetFullPath($env:AGENTFORGE_MODEL_ROOT)
    } else {
        Join-Path (Split-Path -Parent $ProjectRoot) 'models'
    }
    $chatRoot = Join-Path $modelRoot 'qwen3.5-4b'
    $exe = Join-Path $chatRoot '.runtime\llama-b11424\llama-server.exe'
    $model = Join-Path $chatRoot 'Qwen3.5-4B-Q4_K_M.gguf'
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "找不到运行程序：$exe" }
    if (-not (Test-Path -LiteralPath $model -PathType Leaf)) { throw "找不到模型文件：$model" }
    try {
        $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
        if (@($listeners | Where-Object { $_.Port -eq $Port }).Count -gt 0) {
            throw "端口 $Port 已被占用。请先确认并关闭原有服务，本启动器不会终止其他进程。"
        }
    } catch { throw "无法安全确认端口状态：$($_.Exception.Message)" }
    $env:LLAMA_API_KEY = Get-LauncherKey $true
    Write-Host "正在加载本地千问模型，首次加载可能需要几分钟。请保持此窗口打开。"
    Write-Host "等待出现 model loaded 后，双击 Start-AgentForge.cmd。按 Ctrl+C 停止服务。"
    & $exe --model $model --alias $ModelName --host 127.0.0.1 --port $Port --ctx-size 4096 --n-gpu-layers 0 --no-webui --cors-origins "http://127.0.0.1:$Port" --no-cors-credentials
    if ($LASTEXITCODE -ne 0) { Write-Host "模型服务已退出，退出码：$LASTEXITCODE" }
} catch { Write-Host "启动失败：$($_.Exception.Message)"; exit 1 }
