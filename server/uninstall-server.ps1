[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
try {
    $root = Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    Assert-TunnelAdministrator
    $entries = @(
        @{ Name=$script:FrpsTaskName; Exe=(Join-Path $PSScriptRoot 'frps.exe'); Config=(Join-Path $PSScriptRoot 'frps.toml') },
        @{ Name=$script:CaddyTaskName; Exe=(Join-Path $PSScriptRoot 'reverse-proxy\caddy.exe'); Config=(Join-Path $PSScriptRoot 'reverse-proxy\Caddyfile') },
        @{ Name='PersonalWebTunnel-HTTPS-Restore'; Exe=(Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'); Config=(Join-Path $PSScriptRoot 'https-startup.state.json') }
    )
    # 先验证所有归属，再执行卸载，避免发现任务冲突时已停止其它部分。
    foreach ($entry in $entries) { Get-TunnelTask -Name $entry.Name -Exe $entry.Exe -ConfigPath $entry.Config | Out-Null }
    $rules = @()
    foreach ($entry in @(
        @{ Name='PersonalWebTunnel-FRPS-In'; Exe=$entries[0].Exe },
        @{ Name='PersonalWebTunnel-HTTPS-In'; Exe=$entries[1].Exe }
    )) {
        $rule = Get-NetFirewallRule -ErrorAction Stop | Where-Object Name -eq $entry.Name
        if ($rule) {
            $application = $rule | Get-NetFirewallApplicationFilter
            if ($rule.Group -ne $script:FirewallGroup -or $application.Program -ne $entry.Exe) { throw "规则 $($entry.Name) 归属不符，拒绝卸载。" }
            $rules += $rule
        }
    }
    foreach ($entry in $entries) {
        Stop-TunnelTask -Name $entry.Name -Exe $entry.Exe -ConfigPath $entry.Config
        $task = Get-TunnelTask -Name $entry.Name -Exe $entry.Exe -ConfigPath $entry.Config
        if ($task) { $task | Unregister-ScheduledTask -Confirm:$false }
    }
    foreach ($rule in $rules) { $rule | Remove-NetFirewallRule }
    Write-Host '[OK] 本项目计划任务和防火墙规则已移除，程序、配置、token 和日志均保留。'
} catch { Write-Error "服务器卸载失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
