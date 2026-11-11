$ErrorActionPreference = 'Stop'
$UtilityModuleManifest = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1'
if (-not (Test-Path -LiteralPath $UtilityModuleManifest -PathType Leaf)) {
    throw "Required PowerShell utility module manifest is missing: $UtilityModuleManifest"
}
Import-Module -Name $UtilityModuleManifest -ErrorAction Stop
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ModelRoot = if ($env:AGENTFORGE_MODEL_ROOT) {
    [System.IO.Path]::GetFullPath($env:AGENTFORGE_MODEL_ROOT)
} else {
    Join-Path (Split-Path -Parent $ProjectRoot) 'models'
}
$Runtime = Join-Path $ModelRoot 'qwen3.5-4b\.runtime\llama-b11424'
$Exe = Join-Path $Runtime 'llama-server.exe'
$Impl = Join-Path $Runtime 'llama-server-impl.dll'
$Model = Join-Path $ModelRoot 'qwen3-embedding-0.6b-gguf\Qwen3-Embedding-0.6B-Q8_0.gguf'
$Port = 58067
$ApiModelAlias = 'qwen3-embedding-0.6b-q8_0-agentforge'
$Expected = @{}
$Expected[$Exe] = 'F8DF891A5E4D10FA8CFC0F86C8D99617A06EDD18D046E18B59EC3354F17FC12D'
$Expected[$Impl] = 'F020D94DF5E91840F2C9626B86F9AFE414C16D17ED5E1F41FE9CB614D3CDCDCB'
$Expected[$Model] = '06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439'
foreach ($Path in @($Exe, $Impl, $Model)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Pinned local embedding asset is missing: $Path"
    }
    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($Actual -ne $Expected[$Path]) {
        throw "Pinned local embedding asset hash mismatch: $Path"
    }
}
try {
    $Listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    if (@($Listeners | Where-Object { $_.Address.ToString() -eq '127.0.0.1' -and $_.Port -eq $Port }).Count -gt 0) {
        throw "Port $Port is already occupied. This launcher will not stop an unknown service."
    }
} catch { throw "Cannot safely verify the local embedding port: $($_.Exception.Message)" }

$env:AGENTFORGE_EMBEDDING_BASE_URL = "http://127.0.0.1:$Port"
$env:AGENTFORGE_EMBEDDING_MODEL_ID = 'Qwen/Qwen3-Embedding-0.6B-GGUF@370f27d7550e0def9b39c1f16d3fbaa13aa67728:Q8_0#06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439'
$env:AGENTFORGE_EMBEDDING_API_MODEL = $ApiModelAlias
$env:AGENTFORGE_EMBEDDING_DIMENSION = '1024'
Write-Host 'Starting the pinned local Qwen embedding model on 127.0.0.1:58067.'
Write-Host 'Keep this window open. Start AgentForge separately after model loaded.'
& $Exe --model $Model --alias $ApiModelAlias --host 127.0.0.1 --port $Port `
    --ctx-size 4096 --n-gpu-layers 0 --embedding --pooling last --embd-normalize 2 --no-webui
if ($LASTEXITCODE -ne 0) { Write-Host "Embedding service exited with code $LASTEXITCODE" }
