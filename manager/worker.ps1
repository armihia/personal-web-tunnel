[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidateSet('Router','Tunnel','TestTunnel','MappingTunnel','Route')][string]$Mode,
      [Parameter(Mandatory=$true)][string]$RuntimeDirectory,
      [Parameter(Mandatory=$true)][string]$Instance,
      [Parameter(Mandatory=$true)][int]$ParentPid,
      [Parameter(Mandatory=$true)][string]$ParentStartTicks,
      [Net.IPAddress]$ServerIP, [int]$RouterPort = 17891, [int]$AdminPort = 17892,
      [int]$RemotePort = 0, [string]$ProxyName = '', [string]$LogStem = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'common\Tunnel.Common.ps1')
. (Join-Path $root 'common\Tunnel.Config.ps1')
. (Join-Path $root 'common\Tunnel.Server.ps1')
. (Join-Path $root 'common\Tunnel.Direct.ps1')
$readyPath = Join-Path $RuntimeDirectory "$Instance.ready.json"
$stopPath = Join-Path $RuntimeDirectory "$Instance.stop"
$child = $null
$routeState = $null

function Test-ManagerParent {
    $parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    return ($parent -and $parent.StartTime.ToUniversalTime().Ticks.ToString() -eq $ParentStartTicks)
}
function Write-WorkerReady {
    param($Value)
    $temporary = $readyPath + '.tmp'
    Write-TunnelText $temporary ($Value | ConvertTo-Json -Compress)
    Move-Item -LiteralPath $temporary -Destination $readyPath -Force
}
try {
    Assert-TunnelWindows
    if ($Instance -notmatch '^[a-zA-Z0-9-]+$' -or -not (Test-ManagerParent)) { throw '管理服务实例无效。' }
    $settings = Get-TunnelSettings
    if ($Mode -eq 'Route') {
        while ((Test-ManagerParent) -and -not (Test-Path -LiteralPath $stopPath)) {
            try {
                $gateway = Get-TunnelDirectGateway
                if ($routeState -and ($routeState.InterfaceIndex -ne $gateway.InterfaceIndex -or $routeState.NextHop -ne $gateway.NextHop)) {
                    Remove-TunnelDirectRoute $routeState
                    $routeState = $null
                }
                if (-not $routeState) { $routeState = New-TunnelDirectRoute -ServerIP $ServerIP }
                $selected = @(Find-NetRoute -RemoteIPAddress $ServerIP.ToString() | Where-Object { $_.PSObject.Properties['DestinationPrefix'] })
                if ($selected.Count -ne 1 -or $selected[0].InterfaceIndex -ne $routeState.InterfaceIndex) {
                    Remove-TunnelDirectRoute $routeState
                    $routeState = $null
                    throw '物理网卡直连路由暂不可用，正在重试。'
                }
                Write-WorkerReady @{ status='ready'; gateway=$routeState.NextHop; interface=$routeState.InterfaceAlias }
            } catch {
                Write-WorkerReady @{ status='waiting'; error=$_.Exception.Message }
            }
            for ($i=0; $i -lt 10 -and (Test-ManagerParent) -and -not (Test-Path -LiteralPath $stopPath); $i++) {
                Start-Sleep -Seconds 1
            }
        }
    } else {
        $clientDirectory = Join-Path $root 'client'
        if ($Mode -eq 'Router') {
            $exe = Install-TunnelBinary -Name caddy -Directory $clientDirectory -Settings $settings
            $config = Join-Path $RuntimeDirectory 'Caddyfile'
            if ((Get-TunnelListeners -Ports @($RouterPort,$AdminPort)).Count -gt 0) { throw '本机路由端口已被其它程序占用。' }
            $arguments = 'run --config "' + $config + '" --adapter caddyfile'
        } else {
            $exe = Install-TunnelBinary -Name frpc -Directory $clientDirectory -Settings $settings
            $tokenPath = Join-Path $clientDirectory 'token.txt'
            if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) { throw '缺少客户端令牌，请安全复制服务器令牌文件。' }
            $token=[IO.File]::ReadAllText($tokenPath)
            if ($token -cnotmatch '^[a-fA-F0-9]{64}$') { throw '客户端令牌格式不正确，请重新启动管理器检查。' }
            $settings.ServerAddress = $ServerIP.ToString()
            if ($Mode -eq 'MappingTunnel') {
                if ($LogStem -notmatch '^mapping-[a-zA-Z0-9_-]{1,64}$' -or -not $RemotePort -or -not $ProxyName) { throw '协作映射参数不正确。' }
                $stem = $LogStem
            } else { $stem = if ($Mode -eq 'TestTunnel') { 'frpc-test' } else { 'frpc' } }
            $config = Join-Path $RuntimeDirectory ($stem + '.toml')
            $frpcLog = Join-Path $RuntimeDirectory ($stem + '.log')
            if (Test-Path -LiteralPath $frpcLog) {
                Move-Item -LiteralPath $frpcLog -Destination (Join-Path $RuntimeDirectory ($stem + '-previous.log')) -Force
            }
            $text = New-TunnelClientConfig $settings $RouterPort $tokenPath $frpcLog -RemotePort $RemotePort -ProxyName $ProxyName
            Write-TunnelText $config ($text.Replace('loginFailExit = true','loginFailExit = false'))
            Invoke-TunnelNative -Exe $exe -Arguments ('verify -c "' + $config + '"') -Directory $clientDirectory | Out-Null
            $arguments = '-c "' + $config + '"'
        }
        $launch = New-Object Diagnostics.ProcessStartInfo
        $launch.FileName = $exe
        $launch.Arguments = $arguments
        $launch.WorkingDirectory = $clientDirectory
        $launch.UseShellExecute = $false
        $launch.CreateNoWindow = $true
        if ($Mode -in @('Tunnel','TestTunnel','MappingTunnel')) { $launch.EnvironmentVariables['http_proxy'] = '' }
        $child = [Diagnostics.Process]::Start($launch)
        if ($Mode -eq 'Router') {
            Wait-TunnelListener -Port $RouterPort -Exe $exe -TimeoutSeconds 10
            $listeners = Get-TunnelListeners -Ports @($RouterPort,$AdminPort)
            $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
            if ($owners.Count -ne 1 -or $owners[0] -ne $child.Id) { throw '路由端口归属不符，停止启动。' }
        }
        Write-WorkerReady @{ status='ready'; pid=$child.Id; startTicks=$child.StartTime.ToUniversalTime().Ticks.ToString() }
        while ((Test-ManagerParent) -and -not (Test-Path -LiteralPath $stopPath) -and -not $child.HasExited) {
            Start-Sleep -Milliseconds 500
            $child.Refresh()
        }
        if ($child.HasExited -and $child.ExitCode -ne 0) { throw "子进程退出，错误码 $($child.ExitCode)。" }
    }
} catch {
    Write-WorkerReady @{ status='error'; error=$_.Exception.Message }
    Write-Error $_ -ErrorAction Continue
    exit 1
} finally {
    if ($child) {
        if (-not $child.HasExited) { $child.Kill(); $child.WaitForExit() }
        $child.Dispose()
    }
    if ($routeState) { Remove-TunnelDirectRoute $routeState }
}
