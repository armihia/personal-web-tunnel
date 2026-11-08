[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
try {
    $root=Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    Assert-TunnelAdministrator
    $task=Get-ScheduledTask -TaskName 'PersonalWebTunnel-Collaboration' -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host '协作登记服务：未注册（服务器仍使用旧版协作方式）。'; exit 0 }
    $scriptPath=Join-Path $PSScriptRoot 'collaboration_registry.py'
    if (@($task).Count -ne 1 -or @($task.Actions).Count -ne 1 -or
        -not $task.Actions[0].Arguments -or
        $task.Actions[0].Arguments.IndexOf($scriptPath,[StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw '协作登记任务归属无法确认，请核对任务计划程序。'
    }
    if ($task.Actions[0].Arguments -notmatch '--port\s+(\d{1,5})') { throw '无法从协作任务确认端口。' }
    $port=[int]$Matches[1]
    $boot=@($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' }).Count -gt 0
    $info=$task | Get-ScheduledTaskInfo
    $resultText=[string]$info.LastTaskResult
    if ($info.LastTaskResult -eq 267009) { $resultText='267009（任务正在运行）' }
    Write-Host "协作登记服务：$($task.State)；系统启动触发：$boot；运行身份：$($task.Principal.UserId)"
    Write-Host "上次运行：$($info.LastRunTime)；结果：$resultText"
    $processes=@(Get-TunnelServerProcesses -Exe $task.Actions[0].Execute -ConfigPath $scriptPath)
    $listeners=Get-TunnelListeners -Ports @($port)
    $ids=@($processes | ForEach-Object ProcessId)
    $owned=@($listeners | Where-Object { $_.LocalAddress -eq '127.0.0.1' -and $_.OwningProcess -in $ids })
    $unsafe=@($listeners | Where-Object { $_.LocalAddress -ne '127.0.0.1' })
    if ($unsafe.Count) { throw '协作登记端口存在非回环监听，请核查。' }
    if ($owned.Count) { Write-Host "登记端口：127.0.0.1:$port（归属已确认）" }
    else { Write-Host "登记端口：$port 尚未由本项目进程监听，请检查任务结果。" }
} catch { Write-Error "查询协作状态失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
