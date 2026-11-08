# 仅管理本项目创建的计划任务和防火墙规则。
$script:FrpsTaskName = 'PersonalWebTunnel-FRPS'
$script:CaddyTaskName = 'PersonalWebTunnel-Caddy'
$script:FirewallGroup = 'PersonalWebTunnel'

function Get-TunnelTask {
    param([string]$Name, [string]$Exe, [string]$ConfigPath)
    $task = Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq $Name }
    if (-not $task) { return $null }
    if (@($task).Count -ne 1 -or @($task.Actions).Count -ne 1 -or
        $task.TaskPath -ne '\' -or $task.Actions[0].Execute -ne $Exe -or
        $task.Actions[0].Arguments.IndexOf($ConfigPath, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "计划任务 $Name 已被其它配置使用，拒绝覆盖或停止。"
    }
    return $task
}

function Register-TunnelTask {
    param([string]$Name, [string]$Exe, [string]$Arguments, [string]$ConfigPath, [string]$Directory)
    Get-TunnelTask -Name $Name -Exe $Exe -ConfigPath $ConfigPath | Out-Null
    $action = New-ScheduledTaskAction -Execute $Exe -Argument $Arguments -WorkingDirectory $Directory
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $options = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $Name -TaskPath '\' -Action $action -Trigger $trigger -Principal $principal -Settings $options -Description '个人临时网站穿透工具；仅管理本项目进程。' -Force | Out-Null
}

function Set-TunnelFirewall {
    param([string]$Name, [int[]]$Ports, [string]$Exe)
    $rule = Get-NetFirewallRule -ErrorAction Stop | Where-Object { $_.Name -eq $Name }
    if ($rule) {
        $application = $rule | Get-NetFirewallApplicationFilter
        if ($rule.Group -ne $script:FirewallGroup -or $application.Program -ne $Exe) {
            throw "防火墙规则 $Name 属于其它程序，拒绝覆盖。"
        }
        $rule | Set-NetFirewallRule -Enabled True -Direction Inbound -Action Allow -Profile Any
        $rule | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Ports
    } else {
        New-NetFirewallRule -Name $Name -DisplayName $Name -Group $script:FirewallGroup -Direction Inbound -Action Allow -Profile Any -Protocol TCP -LocalPort $Ports -Program $Exe | Out-Null
    }
}

function Get-TunnelServerProcesses {
    param([string]$Exe, [string]$ConfigPath)
    $name = [IO.Path]::GetFileName($Exe)
    return @(Get-CimInstance Win32_Process -Filter "Name = '$name'" -ErrorAction Stop | Where-Object {
        $_.ExecutablePath -eq $Exe -and $_.CommandLine -and
        $_.CommandLine.IndexOf($ConfigPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
    })
}

function Stop-TunnelTask {
    param([string]$Name, [string]$Exe, [string]$ConfigPath)
    $task = Get-TunnelTask -Name $Name -Exe $Exe -ConfigPath $ConfigPath
    if ($task) { Stop-ScheduledTask -InputObject $task -ErrorAction Stop }
    foreach ($item in (Get-TunnelServerProcesses -Exe $Exe -ConfigPath $ConfigPath)) {
        $process = Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue
        if ($process -and $process.Path -eq $Exe -and
            [Math]::Abs(($process.StartTime.ToUniversalTime() - $item.CreationDate.ToUniversalTime()).TotalMilliseconds) -lt 1) {
            $process.Kill()
            $process.WaitForExit()
            $process.Dispose()
        }
    }
}

function Wait-TunnelListener {
    param([int]$Port, [string]$Exe, [int]$TimeoutSeconds = 20)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        foreach ($connection in (Get-TunnelListeners -Ports @($Port))) {
            $owner = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
            if ($owner -and $owner.Path -eq $Exe) { return }
        }
        Start-Sleep -Milliseconds 300
    }
    throw "等待本项目进程监听 $Port 超时，请检查服务器日志和计划任务状态。"
}

function Get-TunnelCaddyContext {
    param([string]$CaddyExe, [string]$CaddyConfig)
    $owners=@()
    foreach ($connection in (Get-TunnelListeners -Ports @(443))) {
        $owners += Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)"
    }
    $owners=@($owners | Sort-Object ProcessId -Unique)
    if ($owners.Count -ne 1 -or $owners[0].Name -ne 'caddy.exe') {
        throw '未确认唯一的现有 Caddy HTTPS 进程，未修改服务。'
    }
    $owner=$owners[0]
    if (-not $CaddyExe) { $CaddyExe=$owner.ExecutablePath }
    if (-not $CaddyConfig -and $owner.CommandLine -match '--config\s+(?:"([^"]+)"|(\S+))') {
        $CaddyConfig=$Matches[1]; if (-not $CaddyConfig) { $CaddyConfig=$Matches[2] }
    }
    $CaddyExe=(Resolve-Path -LiteralPath $CaddyExe).Path
    $CaddyConfig=(Resolve-Path -LiteralPath $CaddyConfig).Path
    if ($CaddyExe -ne $owner.ExecutablePath -or
        $owner.CommandLine.IndexOf($CaddyConfig,[StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw '提供的 Caddy 路径不属于现有 HTTPS 进程，未修改服务。'
    }
    $adminAddress='localhost:2019'
    $text=[IO.File]::ReadAllText($CaddyConfig)
    $adminLines=[regex]::Matches($text,'(?m)^[ \t]*admin[ \t]+([^\s{#]+)')
    if ($adminLines.Count -gt 1) { throw '无法确认唯一的 Caddy 管理地址，未修改服务。' }
    if ($adminLines.Count -eq 1) { $adminAddress=$adminLines[0].Groups[1].Value }
    if ($adminAddress -notmatch '^(?:localhost|127\.0\.0\.1|\[::1\]):(\d{1,5})$') {
        throw 'Caddy 未启用本机管理地址，无法安全在线重载；请手工合并分流。'
    }
    $adminPort=[int]$Matches[1]
    $adminOwners=@(Get-TunnelListeners -Ports @($adminPort) | Where-Object {
        $_.LocalPort -eq $adminPort -and $_.OwningProcess -eq $owner.ProcessId
    })
    if (-not $adminOwners.Count) { throw 'Caddy 管理端口不属于当前 HTTPS 进程，未修改服务。' }
    return [pscustomobject]@{ Exe=$CaddyExe; Config=$CaddyConfig; AdminAddress=$adminAddress;
        ReloadArguments=('reload --address "'+$adminAddress+'" --config "'+$CaddyConfig+'" --adapter caddyfile') }
}
