[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$lock = $null
try {
    . (Join-Path (Split-Path -Parent $PSScriptRoot) 'common\Tunnel.Common.ps1')
    Assert-TunnelWindows
    $lock = Enter-TunnelLock (Join-Path $PSScriptRoot 'frpc.lock')
    $statePath = Join-Path $PSScriptRoot 'frpc.state.json'
    $process = Get-TunnelOwnedProcess -StatePath $statePath -Exe (Join-Path $PSScriptRoot 'frpc.exe') -ConfigPath (Join-Path $PSScriptRoot 'frpc.runtime.toml')
    if ($process) { $process.Kill(); $process.WaitForExit(); $process.Dispose() }
    Remove-Item -LiteralPath $statePath,(Join-Path $PSScriptRoot 'frpc.pid') -Force -ErrorAction SilentlyContinue
    Write-Host '[OK] 公网隧道已停止。'
} catch { Write-Error "停止失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
finally { if ($lock) { $lock.Dispose() } }
