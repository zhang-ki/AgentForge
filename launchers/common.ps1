$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$KeyPath = Join-Path $ProjectRoot '.agentforge\launcher\qwen-key.dpapi'
$Port = 56572
$ModelName = 'qwen3.5-4b'
function Get-LauncherKey([bool]$CreateIfMissing) {
    if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
        if (-not $CreateIfMissing) { throw '尚未建立启动密钥。请先双击 Start-Qwen.cmd。' }
        $directory = Split-Path -Parent $KeyPath
        [void](New-Item -ItemType Directory -Path $directory -Force)
        $bytes = New-Object byte[] 32
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $secure = ConvertTo-SecureString ([Convert]::ToBase64String($bytes)) -AsPlainText -Force
        $encrypted = ConvertFrom-SecureString $secure
        $stream = [System.IO.File]::Open($KeyPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try {
            $writer = New-Object System.IO.StreamWriter($stream, [System.Text.Encoding]::ASCII)
            try { $writer.Write($encrypted) } finally { $writer.Dispose() }
        } finally { $stream.Dispose() }
    }
    try {
        $secure = ConvertTo-SecureString ([System.IO.File]::ReadAllText($KeyPath).Trim())
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    } catch { throw '无法读取当前 Windows 用户加密的启动密钥。请检查密钥文件和用户账户。' }
}
