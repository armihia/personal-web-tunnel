[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
try {
    $root = Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    Assert-TunnelAdministrator
    $settings = Get-TunnelSettings
    foreach ($entry in @(
        @{ Name=$script:FrpsTaskName; Exe=(Join-Path $PSScriptRoot 'frps.exe'); Config=(Join-Path $PSScriptRoot 'frps.toml') },
        @{ Name=$script:CaddyTaskName; Exe=(Join-Path $PSScriptRoot 'reverse-proxy\caddy.exe'); Config=(Join-Path $PSScriptRoot 'reverse-proxy\Caddyfile') },
        @{ Name='PersonalWebTunnel-HTTPS-Restore'; Exe=(Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'); Config=(Join-Path $PSScriptRoot 'https-startup.state.json') }
    )) {
        $task = Get-TunnelTask -Name $entry.Name -Exe $entry.Exe -ConfigPath $entry.Config
        $status = '未注册本项目计划任务'; if ($task) { $status = [string]$task.State }
        Write-Host "$($entry.Name) : $status"
        if ($task) {
            $boot = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' }).Count -gt 0
            Write-Host "  系统启动触发：$boot；运行身份：$($task.Principal.UserId)"
            $info = $task | Get-ScheduledTaskInfo
            $resultText = [string]$info.LastTaskResult
            if ($info.LastTaskResult -eq 267009) { $resultText = '267009（任务正在运行）' }
            Write-Host "  上次运行：$($info.LastRunTime)；结果：$resultText"
        }
    }
    $proxyPorts = @($settings.RemotePort)
    if ($settings.SharedTestEnabled) { $proxyPorts += $settings.SharedMainPort }
    $listeners = Get-TunnelListeners -Ports (@($settings.FrpsBindPort,80,443) + $proxyPorts)
    Write-Host "监听端口：$(Format-TunnelListeners $listeners)"
    foreach ($ownerId in @($listeners | Where-Object LocalPort -eq 443 | Select-Object -ExpandProperty OwningProcess -Unique)) {
        $owner = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
        if ($owner -and $owner.ProcessName -eq 'caddy') {
            Write-Host "HTTPS 入口：Caddy 已运行（PID $ownerId）；本项目计划任务状态不代表其它 Caddy 实例的安装状态。"
        }
    }
    $unsafe = @($listeners | Where-Object { $_.LocalPort -in $proxyPorts -and $_.LocalAddress -ne '127.0.0.1' })
    if ($unsafe.Count -gt 0) { throw "代理端口不在回环地址：$(Format-TunnelListeners $unsafe)，请立即核查。" }
    foreach ($proxyPort in $proxyPorts) {
        if (-not @($listeners | Where-Object LocalPort -eq $proxyPort).Count) {
            Write-Host "代理端口 $proxyPort 尚未监听：对应客户端没有发布时属于正常状态。"
        }
    }
    $public = Test-TunnelHttp -Url "https://$($settings.PublicDomain)" -TimeoutSeconds $settings.ProbeTimeoutSeconds
    Write-Host "公网 HTTPS：$($public.Detail)"
} catch { Write-Error "服务器状态查询失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
