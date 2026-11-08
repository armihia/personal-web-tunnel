[CmdletBinding()]
param([Parameter(Mandatory=$true)][Net.IPAddress]$ServerIP,
      [ValidateRange(1,65535)][int]$LocalPort = 0, [uint32]$InterfaceIndex = 0)
$ErrorActionPreference = 'Stop'
$routeState = $null
$exitCode = 0
try {
    . (Join-Path $PSScriptRoot 'common\Tunnel.Common.ps1')
    . (Join-Path $PSScriptRoot 'common\Tunnel.Direct.ps1')
    Assert-TunnelWindows
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '直连模式需要添加临时 Windows 路由，请在本机以管理员身份打开 PowerShell 后运行。'
    }
    $settings = Get-TunnelSettings
    if ($LocalPort -eq 0) { $LocalPort = $settings.LocalPort }
    if (-not (Test-TunnelTcp -Address '127.0.0.1' -Port $LocalPort -TimeoutSeconds $settings.ProbeTimeoutSeconds)) {
        throw "本地网站未监听 127.0.0.1:$LocalPort，请先启动网站。"
    }
    $routeState = New-TunnelDirectRoute -ServerIP $ServerIP -InterfaceIndex $InterfaceIndex
    Write-Host "[直连] $($routeState.ServerIP)/32 → $($routeState.NextHop) → $($routeState.InterfaceAlias)"
    Write-Host '[直连] 发布期间到此服务器 IP 的连接走物理网卡，其它目标仍按现有网络配置处理。'
    if (-not (Test-TunnelTcp -Address $routeState.ServerIP -Port $settings.FrpsBindPort -TimeoutSeconds $settings.ProbeTimeoutSeconds)) {
        throw "物理网卡直连 $($routeState.ServerIP):$($settings.FrpsBindPort) 失败，请检查服务器安全组、Windows 防火墙和监听。"
    }
    $global:LASTEXITCODE = 0
    & (Join-Path $PSScriptRoot 'client\publish.ps1') -LocalPort $LocalPort -ServerAddress $routeState.ServerIP -DirectConnection
    $exitCode = $LASTEXITCODE
} catch {
    Write-Error "直连发布失败：$($_.Exception.Message)" -ErrorAction Continue
    $exitCode = 1
} finally {
    if ($routeState) {
        try {
            Remove-TunnelDirectRoute -RouteState $routeState
            if ($routeState.CreatedRoute) { Write-Host '[直连] 已移除本次创建的临时路由。' }
        } catch {
            Write-Error "临时路由清理失败：$($_.Exception.Message)" -ErrorAction Continue
            Write-Host "请在管理员 PowerShell 检查路由：Get-NetRoute -DestinationPrefix '$($routeState.ServerIP)/32'"
            $exitCode = 1
        }
    }
}
exit $exitCode
