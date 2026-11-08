[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$StartupConfig)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'common\Tunnel.Common.ps1')
. (Join-Path $root 'common\Tunnel.Server.ps1')
try {
    Assert-TunnelAdministrator
    $settings = Get-Content -LiteralPath $StartupConfig -Raw -Encoding UTF8 | ConvertFrom-Json
    $settings.Exe = (Resolve-Path -LiteralPath $settings.Exe).Path
    $settings.Config = (Resolve-Path -LiteralPath $settings.Config).Path
    $logPath = Join-Path $PSScriptRoot 'https-startup.log'
    $missingSince = $null
    while ($true) {
        $listeners = Get-TunnelListeners -Ports @(443)
        $processes = @(Get-TunnelServerProcesses -Exe $settings.Exe -ConfigPath $settings.Config)
        if ($listeners.Count -gt 0) {
            $missingSince = $null
            $ids = @($processes | ForEach-Object ProcessId)
            if (@($listeners | Where-Object { $_.OwningProcess -notin $ids }).Count -gt 0) {
                throw '443 被其它配置或程序占用，HTTPS 保障任务不会停止或覆盖它。'
            }
        } elseif ($processes.Count -eq 0) {
            # 用原用户的数据目录保存证书，避免 SYSTEM 切换到另一份证书存储。
            $launch = New-Object Diagnostics.ProcessStartInfo
            $launch.FileName = $settings.Exe
            $launch.Arguments = 'run --config "' + $settings.Config + '" --adapter caddyfile'
            $launch.WorkingDirectory = Split-Path -Parent $settings.Config
            $launch.UseShellExecute = $false
            $launch.CreateNoWindow = $true
            $launch.EnvironmentVariables['XDG_DATA_HOME'] = $settings.DataHome
            $launch.EnvironmentVariables['XDG_CONFIG_HOME'] = $settings.DataHome
            $child = [Diagnostics.Process]::Start($launch)
            [IO.File]::AppendAllText($logPath,"$(Get-Date -Format s) 恢复 Caddy PID=$($child.Id)`r`n",(New-Object Text.UTF8Encoding($false)))
            Start-Sleep -Seconds 2
            $child.Refresh()
            if ($child.HasExited -and $child.ExitCode -ne 0) { throw "Caddy 恢复失败，错误码 $($child.ExitCode)。" }
            $child.Dispose()
        } else {
            if (-not $missingSince) { $missingSince = [DateTime]::UtcNow }
            if (([DateTime]::UtcNow - $missingSince).TotalSeconds -gt 20) {
                throw '原 Caddy 进程存在但持续未监听 443，请检查原配置和日志；保障任务没有结束该进程。'
            }
        }
        Start-Sleep -Seconds 5
    }
} catch {
    Write-Error "HTTPS 启动保障失败：$($_.Exception.Message)" -ErrorAction Continue
    exit 1
}
