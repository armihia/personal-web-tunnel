[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
try {
    $root = Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    Assert-TunnelAdministrator
    Stop-TunnelTask -Name $script:FrpsTaskName -Exe (Join-Path $PSScriptRoot 'frps.exe') -ConfigPath (Join-Path $PSScriptRoot 'frps.toml')
    $proxy = Join-Path $PSScriptRoot 'reverse-proxy'
    Stop-TunnelTask -Name $script:CaddyTaskName -Exe (Join-Path $proxy 'caddy.exe') -ConfigPath (Join-Path $proxy 'Caddyfile')
    Write-Host '[OK] 本项目服务器进程已停止；下次系统启动仍会自动运行。'
} catch { Write-Error "停止服务器失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
