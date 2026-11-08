[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
try {
    $root = Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Config.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    Assert-TunnelAdministrator
    $settings = Get-TunnelSettings
    $exe = Join-Path $PSScriptRoot 'frps.exe'
    $config = Join-Path $PSScriptRoot 'frps.toml'
    $task = Get-TunnelTask -Name $script:FrpsTaskName -Exe $exe -ConfigPath $config
    if (-not $task) { throw '尚未安装 FRPS 计划任务，请先运行 install-server.ps1。' }
    if (-not (Get-TunnelServerProcesses -Exe $exe -ConfigPath $config)) {
        $conflict = Get-TunnelListeners -Ports @($settings.FrpsBindPort,$settings.RemotePort)
        if ($conflict.Count -gt 0) { throw "FRP 端口冲突：$(Format-TunnelListeners $conflict)" }
        Assert-TunnelToken (Join-Path $PSScriptRoot 'token.txt')
        if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
            Write-TunnelText -Path $config -Text (New-TunnelServerConfig -Settings $settings -ServerDirectory $PSScriptRoot)
        }
        $existing=[IO.File]::ReadAllText($config)
        foreach ($required in @(
            '(?m)^proxyBindAddr\s*=\s*"127\.0\.0\.1"\s*$',
            '(?m)^transport\.tls\.force\s*=\s*true\s*$',
            '(?m)^auth\.method\s*=\s*"token"\s*$',
            '(?m)^auth\.tokenSource\.type\s*=\s*"file"\s*$'
        )) {
            if ($existing -notmatch $required) { throw '现有 FRPS 配置需要回环代理监听、强制 TLS 和令牌文件认证。' }
        }
        Invoke-TunnelNative -Exe $exe -Arguments ('verify -c "' + $config + '"') -Directory $PSScriptRoot | Out-Null
        Start-ScheduledTask -InputObject $task
    }
    Wait-TunnelListener -Port $settings.FrpsBindPort -Exe $exe -TimeoutSeconds $settings.ConnectTimeoutSeconds
    Write-Host "[OK] FRPS 正在监听 TCP $($settings.FrpsBindPort)。"
    $proxyDirectory = Join-Path $PSScriptRoot 'reverse-proxy'
    $caddyExe = Join-Path $proxyDirectory 'caddy.exe'
    $caddyConfig = Join-Path $proxyDirectory 'Caddyfile'
    $caddyTask = Get-TunnelTask -Name $script:CaddyTaskName -Exe $caddyExe -ConfigPath $caddyConfig
    if ($caddyTask) {
        if (-not (Get-TunnelServerProcesses -Exe $caddyExe -ConfigPath $caddyConfig)) {
            $conflict = Get-TunnelListeners -Ports @(80,443)
            if ($conflict.Count -gt 0) { throw "已有 Web 服务占用 80/443：$(Format-TunnelListeners $conflict)。FRPS 已启动，Caddy 未启动。" }
            Start-ScheduledTask -InputObject $caddyTask
        }
        Wait-TunnelListener -Port 443 -Exe $caddyExe -TimeoutSeconds $settings.ConnectTimeoutSeconds
        Write-Host '[OK] Caddy 正在监听 443；有效公网证书需另行检查。'
    }
} catch { Write-Error "服务器启动失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
