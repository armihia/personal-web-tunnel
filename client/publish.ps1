[CmdletBinding()]
param([ValidateRange(1,65535)][int]$LocalPort = 0, [switch]$Background,
      [ValidateNotNullOrEmpty()][string]$ServerAddress, [switch]$DirectConnection)
$ErrorActionPreference = 'Stop'
$lock = $null
$process = $null
$keepRunning = $false
$ownsState = $false
$statePath = Join-Path $PSScriptRoot 'frpc.state.json'
$pidPath = Join-Path $PSScriptRoot 'frpc.pid'
try {
    . (Join-Path (Split-Path -Parent $PSScriptRoot) 'common\Tunnel.Common.ps1')
    . (Join-Path (Split-Path -Parent $PSScriptRoot) 'common\Tunnel.Config.ps1')
    Assert-TunnelWindows
    $settingsArgs = @{}
    if ($PSBoundParameters.ContainsKey('ServerAddress')) { $settingsArgs.ServerAddress = $ServerAddress }
    $settings = Get-TunnelSettings @settingsArgs
    if ($LocalPort -eq 0) { $LocalPort = $settings.LocalPort }
    $exe = Join-Path $PSScriptRoot 'frpc.exe'
    $runtime = Join-Path $PSScriptRoot 'frpc.runtime.toml'
    $lock = Enter-TunnelLock (Join-Path $PSScriptRoot 'frpc.lock')
    $existing = Get-TunnelOwnedProcess -StatePath $statePath -Exe $exe -ConfigPath $runtime
    if ($existing) { throw "本项目已经发布（PID $($existing.Id)），请先运行 stop-publish.ps1。" }
    if (-not (Test-TunnelTcp -Address '127.0.0.1' -Port $LocalPort -TimeoutSeconds $settings.ProbeTimeoutSeconds)) {
        throw "本地网站未监听 127.0.0.1:$LocalPort，请先启动网站。"
    }
    $tokenPath = Join-Path $PSScriptRoot 'token.txt'
    Assert-TunnelToken $tokenPath
    $exe = Install-TunnelBinary -Name frpc -Directory $PSScriptRoot -Settings $settings
    $logPath = Join-Path $PSScriptRoot ("logs\frpc-{0}-{1}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'), $PID)
    [IO.Directory]::CreateDirectory((Split-Path -Parent $logPath)) | Out-Null
    Write-TunnelText -Path $runtime -Text (New-TunnelClientConfig -Settings $settings -LocalPort $LocalPort -TokenPath $tokenPath -LogPath $logPath)
    Invoke-TunnelNative -Exe $exe -Arguments ('verify -c "' + $runtime + '"') -Directory $PSScriptRoot | Out-Null
    if ($DirectConnection) {
        # FRPC 默认读取 http_proxy；只覆盖子进程环境，保留父终端及其它功能的代理。
        $launch = New-Object Diagnostics.ProcessStartInfo
        $launch.FileName = $exe
        $launch.Arguments = '-c "' + $runtime + '"'
        $launch.WorkingDirectory = $PSScriptRoot
        $launch.UseShellExecute = $false
        $launch.CreateNoWindow = $true
        $launch.EnvironmentVariables['http_proxy'] = ''
        $process = [Diagnostics.Process]::Start($launch)
    } else {
        $process = Start-Process -FilePath $exe -ArgumentList ('-c "' + $runtime + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    }
    $state = [ordered]@{
        Pid = $process.Id
        StartTimeTicks = $process.StartTime.ToUniversalTime().Ticks.ToString()
        ExecutablePath = [IO.Path]::GetFullPath($exe)
        ConfigPath = [IO.Path]::GetFullPath($runtime)
        LocalPort = $LocalPort
        ServerAddress = $settings.ServerAddress
        LogPath = $logPath
    }
    $ownsState = $true
    Write-TunnelText -Path $statePath -Text ($state | ConvertTo-Json)
    Write-TunnelText -Path $pidPath -Text ([string]$process.Id)
    $deadline = [DateTime]::UtcNow.AddSeconds($settings.ConnectTimeoutSeconds)
    $connected = $false
    $proxyName = if ($settings.SharedTestEnabled) { 'personal-main-web' } else { 'personal-temp-web' }
    $proxyPattern = '\[' + [regex]::Escape($proxyName) + '\]'
    while ([DateTime]::UtcNow -lt $deadline) {
        $process.Refresh()
        $log = ''
        if (Test-Path -LiteralPath $logPath) { $log = Get-Content -LiteralPath $logPath -Raw -Encoding UTF8 }
        if ($process.HasExited) { throw "FRPC 已退出。日志：$log" }
        if ($log -match ($proxyPattern + ' start proxy success')) { $connected = $true; break }
        if ($log -match ($proxyPattern + ' start error:')) { throw "代理注册失败。日志：$log" }
        Start-Sleep -Milliseconds 200
    }
    if (-not $connected) { throw "等待隧道注册超过 $($settings.ConnectTimeoutSeconds) 秒。检查 TCP $($settings.FrpsBindPort)、token 和服务器日志。客户端日志：$logPath" }
    Write-Host "[OK] 本地网站：http://127.0.0.1:$LocalPort"
    Write-Host "[OK] FRP 服务器：$($settings.ServerAddress):$($settings.FrpsBindPort)"
    Write-Host "[OK] 公网地址：https://$($settings.PublicDomain)"
    Write-Host '[OK] 隧道状态：CONNECTED（代理注册成功）'
    Write-Host "[日志] $logPath"
    $lock.Dispose()
    $lock = $null
    if ($Background) {
        $keepRunning = $true
        Write-Host '[OK] 已在后台发布，运行 stop-publish.ps1 停止。'
    } else {
        Write-Host '按 Ctrl+C 停止发布；也可在另一个 PowerShell 运行 stop-publish.ps1。'
        $lastLine = ''
        while (-not $process.HasExited) {
            if (Test-Path -LiteralPath $logPath) {
                $line = Get-Content -LiteralPath $logPath -Tail 1 -Encoding UTF8
                if ($line -and $line -ne $lastLine) { Write-Host $line; $lastLine = $line }
            }
            Start-Sleep -Milliseconds 250
            $process.Refresh()
        }
        # 主动 stop-publish 删除状态文件；其它异常退出保留非零退出码。
        if ((Test-Path -LiteralPath $statePath) -and $process.ExitCode -ne 0) {
            throw "FRPC 意外退出，退出码 $($process.ExitCode)。查看 $logPath"
        }
    }
} catch {
    Write-Error "发布失败：$($_.Exception.Message)" -ErrorAction Continue
    exit 1
} finally {
    if ($process -and -not $keepRunning) {
        if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
        if ($ownsState -and [IO.File]::Exists($statePath)) {
            # 旧前台结束时，新发布可能已启动；只有 PID 与启动时间都相同才清理。
            $current = [IO.File]::ReadAllText($statePath) | ConvertFrom-Json
            if ($current.Pid -eq $process.Id -and [string]$current.StartTimeTicks -eq [string]$state.StartTimeTicks) {
                [IO.File]::Delete($statePath)
                [IO.File]::Delete($pidPath)
            }
        }
        $process.Dispose()
    }
    if ($lock) { $lock.Dispose() }
}
