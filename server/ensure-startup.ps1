[CmdletBinding()]
param([string]$CaddyExe, [string]$CaddyConfig, [string]$CaddyDataHome)
$ErrorActionPreference = 'Stop'
try {
    $root = Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    Assert-TunnelAdministrator
    $settings = Get-TunnelSettings
    $frpsExe = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'frps.exe')).Path
    $frpsConfig = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'frps.toml')).Path
    Get-TunnelTask -Name $script:FrpsTaskName -Exe $frpsExe -ConfigPath $frpsConfig | Out-Null
    Invoke-TunnelNative -Exe $frpsExe -Arguments ('verify -c "' + $frpsConfig + '"') -Directory $PSScriptRoot | Out-Null

    $caddyProcess = $null
    foreach ($connection in (Get-TunnelListeners -Ports @(443))) {
        $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)"
        if ($candidate.Name -eq 'caddy.exe') { $caddyProcess = $candidate; break }
    }
    if (-not $CaddyExe -and $caddyProcess) { $CaddyExe = $caddyProcess.ExecutablePath }
    if (-not $CaddyConfig -and $caddyProcess -and $caddyProcess.CommandLine -match '--config\s+(?:"([^"]+)"|(\S+))') {
        $CaddyConfig = $Matches[1]
        if (-not $CaddyConfig) { $CaddyConfig = $Matches[2] }
    }
    if (-not $CaddyExe -or -not $CaddyConfig) {
        throw '未能从 443 的 Caddy 进程确认配置，请提供 -CaddyExe 和 -CaddyConfig 的完整路径。'
    }
    $CaddyExe = (Resolve-Path -LiteralPath $CaddyExe).Path
    $CaddyConfig = (Resolve-Path -LiteralPath $CaddyConfig).Path
    Invoke-TunnelNative -Exe $CaddyExe -Arguments ('validate --config "' + $CaddyConfig + '" --adapter caddyfile') -Directory (Split-Path -Parent $CaddyConfig) | Out-Null
    if (-not $CaddyDataHome) {
        if ($env:XDG_DATA_HOME) { $CaddyDataHome = $env:XDG_DATA_HOME }
        elseif ($caddyProcess) {
            $owner = Invoke-CimMethod -InputObject $caddyProcess -MethodName GetOwnerSid
            $profile = Get-CimInstance Win32_UserProfile | Where-Object SID -EQ $owner.Sid
            if ($profile) { $CaddyDataHome = Join-Path $profile.LocalPath 'AppData\Roaming' }
            elseif ($owner.Sid -eq 'S-1-5-18') { $CaddyDataHome = Join-Path $env:SystemRoot 'System32\config\systemprofile\AppData\Roaming' }
            elseif ($owner.Sid -eq 'S-1-5-19') { $CaddyDataHome = Join-Path $env:SystemRoot 'ServiceProfiles\LocalService\AppData\Roaming' }
            elseif ($owner.Sid -eq 'S-1-5-20') { $CaddyDataHome = Join-Path $env:SystemRoot 'ServiceProfiles\NetworkService\AppData\Roaming' }
        }
        if (-not $CaddyDataHome) { $CaddyDataHome = $env:APPDATA }
    }
    $CaddyDataHome = [IO.Path]::GetFullPath($CaddyDataHome)
    if (-not (Test-Path -LiteralPath (Join-Path $CaddyDataHome 'Caddy'))) {
        throw "没有找到原证书目录 $(Join-Path $CaddyDataHome 'Caddy')。若使用自定义存储，请提供原来的 -CaddyDataHome。"
    }
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $startupConfig = Join-Path $PSScriptRoot 'https-startup.state.json'
    $taskName = 'PersonalWebTunnel-HTTPS-Restore'
    Get-TunnelTask -Name $taskName -Exe $powershell -ConfigPath $startupConfig | Out-Null
    Write-TunnelText $startupConfig ([ordered]@{ Exe=$CaddyExe; Config=$CaddyConfig; DataHome=$CaddyDataHome } | ConvertTo-Json)
    Register-TunnelTask -Name $script:FrpsTaskName -Exe $frpsExe -Arguments ('-c "' + $frpsConfig + '"') -ConfigPath $frpsConfig -Directory $PSScriptRoot
    Set-TunnelFirewall -Name 'PersonalWebTunnel-FRPS-In' -Ports @($settings.FrpsBindPort) -Exe $frpsExe
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'restore-https.ps1') + '" -StartupConfig "' + $startupConfig + '"'
    Register-TunnelTask -Name $taskName -Exe $powershell -Arguments $arguments -ConfigPath $startupConfig -Directory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT30S'
    Set-ScheduledTask -TaskName $taskName -TaskPath '\' -Trigger $trigger | Out-Null
    Start-ScheduledTask -TaskName $taskName
    $global:LASTEXITCODE = 0
    & (Join-Path $PSScriptRoot 'start-server.ps1')
    if ($LASTEXITCODE) { throw 'FRPS 启动未成功。' }
    Write-Host '[OK] FRPS：系统启动后以 SYSTEM 自动运行；失败自动重试。'
    Write-Host '[OK] HTTPS：系统启动 30 秒后检查原 Caddy；已有实例会复用，缺失时恢复。'
    Write-Host "[OK] 复用 Caddy 配置：$CaddyConfig"
    Write-Host "[OK] 复用证书目录：$(Join-Path $CaddyDataHome 'Caddy')"
    Write-Host '[待验证] 实际重启服务器后运行 status-server.ps1，再从本机核对 HTTPS 网站。'
} catch { Write-Error "配置自启动失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
