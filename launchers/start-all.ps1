$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"
Set-Location -LiteralPath $ProjectRoot

$ChatPort = 56572
$EmbeddingPort = 58067
$ChatModel = 'qwen3.5-4b'
$EmbeddingModel = 'qwen3-embedding-0.6b-q8_0-agentforge'
$ReadyTimeoutSeconds = 300

function Read-LocalResponse {
    param([System.Net.HttpWebResponse]$Response)
    $reader = $null
    try {
        $reader = New-Object System.IO.StreamReader($Response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $body = $reader.ReadToEnd()
        if ($body.Length -gt 16384) { throw 'Local readiness response exceeded the 16 KiB safety limit.' }
        return $body
    } finally {
        if ($reader) { $reader.Dispose() }
    }
}

function Invoke-LocalGet {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [string]$BearerToken
    )
    $request = [System.Net.HttpWebRequest]::Create($Uri)
    $request.Method = 'GET'
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 2000
    $request.ReadWriteTimeout = 2000
    $request.KeepAlive = $false
    if ($BearerToken) { $request.Headers['Authorization'] = "Bearer $BearerToken" }

    $response = $null
    try {
        $response = [System.Net.HttpWebResponse]$request.GetResponse()
        $body = Read-LocalResponse $response
        return [pscustomobject]@{
            StatusCode = [int]$response.StatusCode
            Body = $body
            ContentType = [string]$response.ContentType
            TransportStatus = $null
        }
    } catch [System.Net.WebException] {
        $webError = $_.Exception
        if ($webError.Response -is [System.Net.HttpWebResponse]) {
            $response = [System.Net.HttpWebResponse]$webError.Response
            $body = Read-LocalResponse $response
            return [pscustomobject]@{
                StatusCode = [int]$response.StatusCode
                Body = $body
                ContentType = [string]$response.ContentType
                TransportStatus = $null
                SocketErrorCode = $null
            }
        }
        $socketErrorCode = $null
        $inner = $webError
        while ($inner) {
            if ($inner -is [System.Net.Sockets.SocketException]) {
                $socketErrorCode = [int]$inner.SocketErrorCode
                break
            }
            $inner = $inner.InnerException
        }
        return [pscustomobject]@{
            StatusCode = $null
            Body = ''
            ContentType = ''
            TransportStatus = [string]$webError.Status
            SocketErrorCode = $socketErrorCode
        }
    } finally {
        if ($response) { $response.Dispose() }
    }
}

function Get-HealthState {
    param([Parameter(Mandatory = $true)]$Response)
    if ($Response.TransportStatus) {
        if ($Response.SocketErrorCode -eq [int][System.Net.Sockets.SocketError]::ConnectionRefused) {
            return 'not_listening'
        }
        if ($Response.TransportStatus -eq 'Timeout') { return 'request_timeout' }
        $socketLabel = if ($null -eq $Response.SocketErrorCode) { 'unavailable' } else { [string]$Response.SocketErrorCode }
        return "transport_error (WebException.Status=$($Response.TransportStatus); SocketErrorCode=$socketLabel)"
    }
    if ($Response.ContentType -notmatch '^application/json(?:\s*;|$)') { return 'unknown_response' }
    try { $payload = $Response.Body | ConvertFrom-Json -ErrorAction Stop }
    catch { return 'unknown_response' }

    if ($Response.StatusCode -eq 200 -and $payload.status -ceq 'ok') { return 'ready' }
    if ($Response.StatusCode -eq 503 -and $payload.error.message -ceq 'Loading model') { return 'loading' }
    return 'unexpected_response'
}

function Get-LocalListenerState {
    param([Parameter(Mandatory = $true)][int]$Port)
    try {
        $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
        $matches = @($listeners | Where-Object { $_.Port -eq $Port })
    } catch {
        throw "无法安全读取本机端口 $Port 的监听状态；为避免启动重复或错误服务，已停止。"
    }
    if ($matches.Count -eq 0) { return 'none' }
    $addresses = @($matches | ForEach-Object { $_.Address.ToString() } | Sort-Object -Unique)
    if ($addresses.Count -eq 1 -and $addresses[0] -eq '127.0.0.1') { return 'loopback' }
    return 'non_loopback_or_mixed'
}

function Start-ModelWindow {
    param([Parameter(Mandatory = $true)][string]$LauncherPath)
    if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
        throw "模型启动文件不存在：$LauncherPath"
    }
    $arguments = '/c "' + $LauncherPath + '"'
    return Start-Process -FilePath $env:ComSpec -ArgumentList $arguments -WorkingDirectory $ProjectRoot -PassThru
}

function Wait-ForModelReady {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][int]$Port,
        [System.Diagnostics.Process]$LauncherProcess,
        [switch]$AllowExistingListener
    )
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $lastState = 'unknown'
    while ($timer.Elapsed.TotalSeconds -lt $ReadyTimeoutSeconds) {
        if ($LauncherProcess -and $LauncherProcess.HasExited) {
            throw "$Label 启动窗口已退出（退出码 $($LauncherProcess.ExitCode)），未通过模型健康检查。"
        }
        $response = Invoke-LocalGet -Uri "http://127.0.0.1:$Port/health"
        $state = Get-HealthState $response
        if ($state -eq 'ready') {
            if ((Get-LocalListenerState $Port) -ne 'loopback') {
                throw "$Label 已响应，但监听地址未能确认仅为 127.0.0.1。"
            }
            Write-Host "$Label 已就绪（HTTP 200，status=ok；等待 $([math]::Round($timer.Elapsed.TotalSeconds, 1)) 秒）。"
            return
        }
        if ($state -eq 'loading') {
            if ((Get-LocalListenerState $Port) -ne 'loopback') {
                throw "$Label 正在响应加载状态，但监听地址未能确认仅为 127.0.0.1。"
            }
            if ($lastState -ne 'loading') { Write-Host "$Label 正在加载模型，请稍候……" }
        } elseif ($state -eq 'not_listening' -and $LauncherProcess) {
            if ((Get-LocalListenerState $Port) -ne 'none') {
                throw "$Label 的连接被拒绝，但监听表显示端口已占用；不会启动重复服务。"
            }
        } elseif ($state -eq 'request_timeout' -and ($LauncherProcess -or $AllowExistingListener)) {
            $listenerState = Get-LocalListenerState $Port
            if ($listenerState -eq 'non_loopback_or_mixed') {
                throw "$Label 超时且端口不是仅绑定 127.0.0.1；不会继续连接。"
            }
            if ($listenerState -eq 'none' -and -not $LauncherProcess) {
                throw "$Label 超时且已有监听已消失；不会自动重启服务。"
            }
            # 这里只进行有界的健康状态轮询，不发送聊天或嵌入请求。
        } elseif ($state -eq 'not_listening' -and $AllowExistingListener) {
            throw "$Label 的已有服务停止监听；没有自动重启或终止任何进程。"
        } elseif ($state -eq 'not_listening') {
            throw "$Label 的已有服务在等待期间停止监听；没有自动重启或终止任何进程。"
        } else {
            throw "$Label 健康检查未满足安全就绪合同（状态：$state，HTTP：$($response.StatusCode)）。已停止后续启动。"
        }
        $lastState = $state
        Start-Sleep -Seconds 1
    }
    throw "$Label 在 $ReadyTimeoutSeconds 秒内没有就绪；不会自动重启服务。"
}

function Confirm-ModelIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$ExpectedModel,
        [string]$BearerToken
    )
    $response = Invoke-LocalGet -Uri "http://127.0.0.1:$Port/v1/models" -BearerToken $BearerToken
    if ($response.StatusCode -ne 200) {
        throw "$Label 模型身份检查失败（HTTP $($response.StatusCode)）；未启动 AgentForge。"
    }
    if ($response.ContentType -notmatch '^application/json(?:\s*;|$)') {
        throw "$Label 的模型身份响应不是 JSON；未启动 AgentForge。"
    }
    try { $payload = $response.Body | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "$Label 的模型列表不是合法 JSON；未启动 AgentForge。" }
    $ids = @($payload.data | ForEach-Object { [string]$_.id })
    if ($ids -notcontains $ExpectedModel) {
        throw "$Label 服务未报告预期模型别名 '$ExpectedModel'；未启动 AgentForge。"
    }
    Write-Host "$Label 模型身份已确认。"
}

function Ensure-Model {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$LauncherPath,
        [Parameter(Mandatory = $true)][string]$ExpectedModel,
        [string]$BearerToken,
        [switch]$SkipIdentityCheck
    )
    $response = Invoke-LocalGet -Uri "http://127.0.0.1:$Port/health"
    $state = Get-HealthState $response
    $launcherProcess = $null
    $listenerState = Get-LocalListenerState $Port
    $allowExistingListener = $false
    if ($state -eq 'not_listening' -and $listenerState -eq 'none') {
        Write-Host "$Label 尚未运行，正在打开其本地启动窗口。"
        $launcherProcess = Start-ModelWindow $LauncherPath
    } elseif ($state -eq 'ready') {
        if ($listenerState -ne 'loopback') { throw "$Label 已响应，但端口监听状态不是仅绑定 127.0.0.1。" }
        Write-Host "$Label 已在运行，将复用现有服务。"
    } elseif ($state -eq 'loading') {
        if ($listenerState -ne 'loopback') { throw "$Label 正在加载，但端口监听状态不是仅绑定 127.0.0.1。" }
        Write-Host "$Label 已在加载，将等待它就绪；不会再启动第二份服务。"
        $allowExistingListener = $true
    } elseif ($state -eq 'request_timeout' -and $listenerState -eq 'none') {
        Write-Host "$Label 端口没有监听者，正在打开其本地启动窗口。"
        $launcherProcess = Start-ModelWindow $LauncherPath
    } elseif ($state -eq 'request_timeout' -and $listenerState -eq 'loopback') {
        Write-Host "$Label 的回环端口已监听但 Health 暂无响应；仅继续有界 Health 检查，不会启动重复服务。"
        $allowExistingListener = $true
    } elseif ($listenerState -eq 'non_loopback_or_mixed') {
        throw "$Label 端口 $Port 存在非 127.0.0.1 或混合地址监听；为避免连接错误服务，不会连接或启动替代进程。"
    } else {
        throw "$Label 端口响应异常（状态：$state，HTTP：$($response.StatusCode)，监听状态：$listenerState）；为避免连接错误服务，没有启动任何替代进程。"
    }

    Wait-ForModelReady -Label $Label -Port $Port -LauncherProcess $launcherProcess -AllowExistingListener:$allowExistingListener
    if (-not $SkipIdentityCheck) {
        Confirm-ModelIdentity -Label $Label -Port $Port -ExpectedModel $ExpectedModel -BearerToken $BearerToken
    }
}

try {
    $postgres = Get-Service -Name 'postgresql-x64-18' -ErrorAction Stop
    if ($postgres.Status -ne 'Running') {
        throw '本机 PostgreSQL 服务未运行。请先在 Windows 服务中启动 postgresql-x64-18；此启动器不会更改服务状态或请求管理员权限。'
    }
    Write-Host 'PostgreSQL 已运行。'

    Ensure-Model -Label '聊天模型' -Port $ChatPort `
        -LauncherPath (Join-Path $ProjectRoot 'Start-Qwen.cmd') `
        -ExpectedModel $ChatModel -SkipIdentityCheck
    $chatKey = Get-LauncherKey $false
    Confirm-ModelIdentity -Label '聊天模型认证' -Port $ChatPort -ExpectedModel $ChatModel -BearerToken $chatKey

    Ensure-Model -Label 'Embedding 模型' -Port $EmbeddingPort `
        -LauncherPath (Join-Path $ProjectRoot 'Start-Qwen-Embedding.cmd') `
        -ExpectedModel $EmbeddingModel

    Write-Host '依赖已就绪，正在本窗口启动 AgentForge。模型窗口会继续运行。'
    & (Join-Path $PSScriptRoot 'start-agentforge.ps1')
} catch {
    Write-Host "启动未完成：$($_.Exception.Message)"
    exit 1
}
