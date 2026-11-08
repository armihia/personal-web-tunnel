[CmdletBinding()]
param([string]$CaddyExe, [string]$CaddyConfig, [switch]$Apply)
$ErrorActionPreference = 'Stop'
try {
    $root = Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Config.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    . (Join-Path $root 'common\Tunnel.SharedTest.ps1')
    Assert-TunnelAdministrator
    $settings = Get-TunnelSettings -SharedTest
    $frpsExe = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'frps.exe')).Path
    $frpsConfig = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'frps.toml')).Path
    $task = Get-TunnelTask -Name $script:FrpsTaskName -Exe $frpsExe -ConfigPath $frpsConfig
    if (-not $task) { throw '原 FRPS 计划任务不存在，请先配置服务器自启动。' }
    $context=Get-TunnelCaddyContext $CaddyExe $CaddyConfig
    $CaddyExe=$context.Exe; $CaddyConfig=$context.Config
    $oldFrps = [IO.File]::ReadAllText($frpsConfig)
    $oldCaddy = [IO.File]::ReadAllText($CaddyConfig)
    $reloadArguments=$context.ReloadArguments
    $newFrps = Enable-TunnelSharedFrps $oldFrps $settings
    $newCaddy = Enable-TunnelSharedCaddy $oldCaddy $settings
    $previewFrps = $frpsConfig + '.shared-test.preview'
    $previewCaddy = $CaddyConfig + '.shared-test.preview'
    Write-TunnelText $previewFrps $newFrps
    Write-TunnelText $previewCaddy $newCaddy
    Invoke-TunnelNative $frpsExe ('verify -c "' + $previewFrps + '"') $PSScriptRoot | Out-Null
    Invoke-TunnelNative $CaddyExe ('validate --config "' + $previewCaddy + '" --adapter caddyfile') (Split-Path -Parent $CaddyConfig) | Out-Null
    Write-Host "[预览] /test 与 /test/* → 原入口 $($settings.RemotePort)；其它路径 → $($settings.SharedMainPort)。"
    Write-Host "[预览] Caddy 配置：$previewCaddy"
    if (-not $Apply) { Write-Host '校验通过。加 -Apply 应用；FRPS 重启会让现有隧道短暂重连。'; exit 0 }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $settingsPath = Join-Path $root 'settings.json'
    $backups = @{}
    foreach ($path in @($frpsConfig,$CaddyConfig,$settingsPath)) {
        $backup=$path+'.before-shared-test-'+$stamp
        Copy-Item -LiteralPath $path -Destination $backup
        $backups[$path]=$backup
    }
    $frpsRestarted=$false; $caddyReloadAttempted=$false
    try {
        Write-TunnelText $frpsConfig $newFrps
        Write-TunnelText $CaddyConfig $newCaddy
        Write-TunnelText $settingsPath ($settings | ConvertTo-Json -Depth 6)
        $caddyReloadAttempted=$true
        Invoke-TunnelNative $CaddyExe $reloadArguments (Split-Path -Parent $CaddyConfig) | Out-Null
        if ($oldFrps -ne $newFrps) {
            $frpsRestarted=$true
            Stop-TunnelTask $script:FrpsTaskName $frpsExe $frpsConfig
            Start-ScheduledTask -InputObject $task
            Wait-TunnelListener $settings.FrpsBindPort $frpsExe $settings.ConnectTimeoutSeconds
        }
    } catch {
        $failure=$_
        foreach ($path in $backups.Keys) { Copy-Item -LiteralPath $backups[$path] -Destination $path -Force }
        if ($caddyReloadAttempted) {
            try { Invoke-TunnelNative $CaddyExe $reloadArguments (Split-Path -Parent $CaddyConfig) | Out-Null }
            catch { Write-Warning '原 Caddy 配置已恢复到磁盘，但在线重载失败，请检查 Caddy。' }
        }
        if ($frpsRestarted) {
            Stop-TunnelTask $script:FrpsTaskName $frpsExe $frpsConfig
            Start-ScheduledTask -InputObject $task
        }
        throw $failure
    }
    Write-Host '[OK] 协作分流已应用；原密钥、TLS 和自启动任务继续使用。'
    Write-Host '[下一步] 在自己的电脑双击“启用协作模式.cmd”；对方代码无需更新。'
} catch { Write-Error "配置协作分流失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
