[CmdletBinding()]
param([string]$CaddyExe, [string]$CaddyConfig, [string]$PythonExe,
      [ValidateRange(1024,65535)][int]$RegistryPort=18083, [switch]$Apply)
$ErrorActionPreference='Stop'
$legacyGuard=$null
$installLock=$null
try {
    $root=Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    . (Join-Path $root 'common\Tunnel.SharedTest.ps1')
    . (Join-Path $root 'common\Tunnel.Collaboration.ps1')
    Assert-TunnelAdministrator
    $installLock=Enter-TunnelLock (Join-Path $PSScriptRoot 'collaboration-install.lock')
    $settings=Get-TunnelSettings -SharedTest
    if ($RegistryPort -in @($settings.FrpsBindPort,$settings.RemotePort,$settings.SharedMainPort,80,443) -or
        ($RegistryPort -ge 30000 -and $RegistryPort -le 30255)) { throw '协作登记端口与现有端口或协作范围重复。' }
    foreach ($existingPort in @($settings.FrpsBindPort,$settings.RemotePort,$settings.SharedMainPort)) {
        if ($existingPort -ge 30000 -and $existingPort -le 30255) { throw '现有 FRP 端口与协作范围重叠，未修改。' }
    }
    $baseDomain=[string]$settings.ServerAddress
    if ($baseDomain -notmatch '^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,63}$' -or
        -not $settings.PublicDomain.EndsWith('.'+$baseDomain,[StringComparison]::OrdinalIgnoreCase)) {
        throw '协作模式需要 ServerAddress 为根域名，PublicDomain 为其子域名。'
    }
    if (-not $PythonExe) {
        $bundledPython=Join-Path $PSScriptRoot 'collaboration-python\python.exe'
        if (Test-Path -LiteralPath $bundledPython -PathType Leaf) { $PythonExe=$bundledPython }
        else { $pythonCommand=Get-Command python.exe -ErrorAction Stop; $PythonExe=$pythonCommand.Source }
    }
    $PythonExe=(Resolve-Path -LiteralPath $PythonExe).Path
    & $PythonExe -c 'import sqlite3, ssl, sys; assert sys.version_info >= (3, 10)' 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '服务器需要可独立运行的 Python 3.10 或更新版本，含 sqlite3。' }
    $registryScript=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'collaboration_registry.py')).Path
    $frpsExe=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'frps.exe')).Path
    $frpsConfig=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'frps.toml')).Path
    $frpsTask=Get-TunnelTask -Name $script:FrpsTaskName -Exe $frpsExe -ConfigPath $frpsConfig
    if (-not $frpsTask) { throw '原 FRPS 自启动任务不存在，未修改。' }
    $context=Get-TunnelCaddyContext $CaddyExe $CaddyConfig
    $oldFrps=[IO.File]::ReadAllText($frpsConfig)
    $oldCaddy=[IO.File]::ReadAllText($context.Config)
    $tokenMatch=[regex]::Match($oldFrps,'(?m)^auth\.tokenSource\.file\.path\s*=\s*(?<literal>"(?:\\.|[^"\\])*")\s*$')
    if (-not $tokenMatch.Success) { throw '无法从原 FRPS 配置确认令牌文件，未修改。' }
    $tokenPath=[string](ConvertFrom-Json $tokenMatch.Groups['literal'].Value)
    if (-not [IO.Path]::IsPathRooted($tokenPath)) { $tokenPath=Join-Path (Split-Path -Parent $frpsConfig) $tokenPath }
    $tokenPath=(Resolve-Path -LiteralPath $tokenPath).Path
    $token=[IO.File]::ReadAllText($tokenPath).Trim()
    if ($token -cnotmatch '^[a-fA-F0-9]{64}$') { throw '原令牌文件格式不正确，未修改。' }
    # 仅迁移已经明确分开的旧协作入口，避免把旧版单入口程序错误限制到 /test。
    $newFrps=Enable-TunnelCollaborationFrps $oldFrps
    $newCaddy=Enable-TunnelCollaborationCaddy $oldCaddy $settings.PublicDomain $settings.ServerAddress $settings.SharedMainPort $settings.RemotePort $RegistryPort
    $frpsPreview=$frpsConfig+'.collaboration.preview'
    $caddyPreview=$context.Config+'.collaboration.preview'
    Write-TunnelText $frpsPreview $newFrps
    Write-TunnelText $caddyPreview $newCaddy
    Invoke-TunnelNative $frpsExe ('verify -c "'+$frpsPreview+'"') $PSScriptRoot | Out-Null
    Invoke-TunnelNative $context.Exe ('validate --config "'+$caddyPreview+'" --adapter caddyfile') (Split-Path -Parent $context.Config) | Out-Null
    Write-Host "[预览] /test 保留原 $($settings.RemotePort) 入口；其他映射由本机登记服务逐条分流。"
    Write-Host "[预览] FRPS：$frpsPreview；Caddy：$caddyPreview"
    if (-not $Apply) { Write-Host '[预览] 校验通过。应用时 FRPS 会短暂重启，原 /test 客户端需要自动重连。'; exit 0 }
    $legacyListeners=Get-TunnelListeners -Ports @($settings.SharedMainPort)
    if ($legacyListeners.Count -gt 0) {
        throw "旧普通入口 $($settings.SharedMainPort) 正在被使用；请先让使用该入口的旧版客户端断开，未修改服务器。"
    }
    $legacyGuard=Open-TunnelLegacyGuard $settings.SharedMainPort
    $registryListeners=Get-TunnelListeners -Ports @($RegistryPort)
    if ($registryListeners.Count -gt 0) { throw '协作登记端口已被占用，未修改。' }
    $registryTaskName='PersonalWebTunnel-Collaboration'
    if (Get-ScheduledTask -TaskName $registryTaskName -ErrorAction SilentlyContinue) { throw '协作登记任务已存在，请核对，未覆盖。' }
    $database=Join-Path $PSScriptRoot 'collaboration-leases.sqlite3'
    $arguments='-X utf8 "'+$registryScript+'" --token-file "'+$tokenPath+'" --database "'+$database+'" --base-domain "'+$settings.ServerAddress+'" --public-domain "'+$settings.PublicDomain+'" --port '+$RegistryPort+' --test-port '+$settings.RemotePort+' --legacy-port '+$settings.SharedMainPort
    $stamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $frpsBackup=$frpsConfig+'.before-collaboration-'+$stamp
    $caddyBackup=$context.Config+'.before-collaboration-'+$stamp
    Copy-Item -LiteralPath $frpsConfig -Destination $frpsBackup
    Copy-Item -LiteralPath $context.Config -Destination $caddyBackup
    $registered=$false; $frpsChanged=$false; $caddyChanged=$false
    try {
        $registered=$true
        Register-TunnelTask -Name $registryTaskName -Exe $PythonExe -Arguments $arguments -ConfigPath $registryScript -Directory $PSScriptRoot
        Start-ScheduledTask -TaskName $registryTaskName
        try {
            Wait-TunnelListener -Port $RegistryPort -Exe $PythonExe -TimeoutSeconds ([Math]::Max(60,[int]$settings.ConnectTimeoutSeconds))
        } catch {
            $taskInfo=Get-ScheduledTaskInfo -TaskName $registryTaskName -ErrorAction SilentlyContinue
            $taskResult=if ($taskInfo) { [string]$taskInfo.LastTaskResult } else { '无法读取' }
            throw "协作登记进程启动超时。计划任务结果 $taskResult，请查看 server\collaboration-registry.log。"
        }
        if (@(Get-TunnelServerProcesses -Exe $PythonExe -ConfigPath $registryScript).Count -ne 1) { throw '无法确认协作登记进程归属。' }
        Add-Type -AssemblyName System.Net.Http
        $handler=New-Object Net.Http.HttpClientHandler
        $handler.UseProxy=$false
        $http=New-Object Net.Http.HttpClient($handler)
        $http.Timeout=[TimeSpan]::FromSeconds(5)
        try {
            $response=$http.GetStringAsync("http://127.0.0.1:$RegistryPort/health").GetAwaiter().GetResult() | ConvertFrom-Json
            if (-not $response.ok -or $response.service -ne 'personal-web-tunnel-coordination-v1') { throw '协作登记服务健康检查失败。' }
        } finally { $http.Dispose(); $handler.Dispose() }
        if ([IO.File]::ReadAllText($frpsConfig) -ne $oldFrps -or [IO.File]::ReadAllText($context.Config) -ne $oldCaddy) {
            throw '服务器配置在迁移期间发生变化，未覆盖新配置。'
        }
        $frpsChanged=$true
        Write-TunnelText $frpsConfig $newFrps
        Stop-TunnelTask $script:FrpsTaskName $frpsExe $frpsConfig
        Start-ScheduledTask -InputObject $frpsTask
        Wait-TunnelListener $settings.FrpsBindPort $frpsExe $settings.ConnectTimeoutSeconds
        if ([IO.File]::ReadAllText($context.Config) -ne $oldCaddy) { throw 'Caddy 配置在迁移期间发生变化，未覆盖新配置。' }
        $caddyChanged=$true
        Write-TunnelText $context.Config $newCaddy
        Invoke-TunnelNative $context.Exe $context.ReloadArguments (Split-Path -Parent $context.Config) | Out-Null
    } catch {
        $failure=$_
        if ($caddyChanged) {
            Copy-Item -LiteralPath $caddyBackup -Destination $context.Config -Force
            try { Invoke-TunnelNative $context.Exe $context.ReloadArguments (Split-Path -Parent $context.Config) | Out-Null }
            catch { Write-Warning 'Caddy 配置已恢复到磁盘，但在线重载失败，请检查现有服务。' }
        }
        if ($frpsChanged) {
            Copy-Item -LiteralPath $frpsBackup -Destination $frpsConfig -Force
            try { Stop-TunnelTask $script:FrpsTaskName $frpsExe $frpsConfig; Start-ScheduledTask -InputObject $frpsTask }
            catch { Write-Warning 'FRPS 配置已恢复到磁盘，但服务重启失败，请检查现有服务。' }
        }
        if ($registered) {
            try {
                $ownedTask=Get-TunnelTask -Name $registryTaskName -Exe $PythonExe -ConfigPath $registryScript
                if ($ownedTask) { Stop-TunnelTask $registryTaskName $PythonExe $registryScript; Unregister-ScheduledTask -TaskName $registryTaskName -Confirm:$false }
            }
            catch { Write-Warning '协作登记任务清理失败，请检查任务计划程序。' }
        }
        throw $failure
    }
    Write-Host '[OK] 协作登记已启用并设置为系统启动时运行。'
    Write-Host "[备份] $frpsBackup"
    Write-Host "[备份] $caddyBackup"
    Write-Host '[待验证] 更新客户端后验证两台电脑、旧 /test、Tomcat 和微信助手。'
} catch { Write-Error "启用逐映射协作失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
finally { if ($legacyGuard) { $legacyGuard.Dispose() }; if ($installLock) { $installLock.Dispose() } }
