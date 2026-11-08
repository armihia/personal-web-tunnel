[CmdletBinding()]
param([switch]$OpenBrowser, [switch]$EnableSharedTest)
$ErrorActionPreference = 'Stop'
$lock = $null
try {
    . (Join-Path $PSScriptRoot 'common\Tunnel.Common.ps1')
    Assert-TunnelWindows
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
        if ($OpenBrowser) { $arguments += ' -OpenBrowser' }
        if ($EnableSharedTest) { $arguments += ' -EnableSharedTest' }
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments -WindowStyle Hidden
        exit 0
    }
    $settings = Get-TunnelSettings -SharedTest:$EnableSharedTest
    if ($EnableSharedTest) {
        # 服务器完成一次分流升级后再启用；只停止本工具确认归属的后台。
        $global:LASTEXITCODE = 0
        & (Join-Path $PSScriptRoot 'stop-manager.ps1')
        if ($LASTEXITCODE) { throw '原管理器仍在运行，未切换协作模式。' }
        $settings.SharedTestEnabled = $true
        Write-TunnelText (Join-Path $PSScriptRoot 'settings.json') ($settings | ConvertTo-Json -Depth 6)
    }
    $port = 17890
    if ($settings.PSObject.Properties['ManagerPort']) { $port = $settings.ManagerPort }
    $url = "http://127.0.0.1:$port/"
    $pythonCandidates = @((Join-Path $PSScriptRoot 'client\python\python.exe'))
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $pythonCandidates += $pythonCommand.Source }
    $python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $python) { throw '未找到 Python。请按 README 安装 Python 3.12，并加入 PATH。' }
    $app = Join-Path $PSScriptRoot 'manager\app.py'
    $statePath = Join-Path $PSScriptRoot 'client\manager.state.json'
    $lock = Enter-TunnelLock (Join-Path $PSScriptRoot 'client\manager-start.lock')
    $ownedPython=$python
    if (Test-Path -LiteralPath $statePath) {
        $savedState=Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($savedState.ExecutablePath -in $pythonCandidates) { $ownedPython=$savedState.ExecutablePath }
    }
    $existing = Get-TunnelOwnedProcess -StatePath $statePath -Exe $ownedPython -ConfigPath $app
    if (-not $existing) {
        if (Test-TunnelTcp -Address '127.0.0.1' -Port $port -TimeoutSeconds 1) {
            throw "本机管理端口 $port 已被占用，请先关闭其它预览或在 settings.json 修改 ManagerPort。"
        }
        $runtime = Join-Path $PSScriptRoot 'client\manager-runtime'
        [IO.Directory]::CreateDirectory($runtime) | Out-Null
        $arguments = '"' + $app + '" --root "' + $PSScriptRoot + '"'
        $existing = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $runtime 'manager.stdout.log') -RedirectStandardError (Join-Path $runtime 'manager.stderr.log')
        Write-TunnelText $statePath ([ordered]@{
            Pid=$existing.Id; StartTimeTicks=$existing.StartTime.ToUniversalTime().Ticks.ToString()
            ExecutablePath=[IO.Path]::GetFullPath($python); ConfigPath=[IO.Path]::GetFullPath($app)
        } | ConvertTo-Json)
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while (-not (Test-TunnelTcp -Address '127.0.0.1' -Port $port -TimeoutSeconds 1)) {
        $existing.Refresh()
        if ($existing.HasExited) { throw "管理服务未启动，请查看 client\manager-runtime\manager.stderr.log。" }
        if ([DateTime]::UtcNow -gt $deadline) { throw '等待管理界面启动超时。' }
        Start-Sleep -Milliseconds 200
    }
    if ($OpenBrowser) { Start-Process -FilePath $url }
    Write-Host "[OK] 后台管理服务已启动：$url"
} catch {
    Write-Error $_ -ErrorAction Continue
    if ($OpenBrowser) {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show($_.Exception.Message,'网站穿透启动失败') | Out-Null
    }
    exit 1
} finally { if ($lock) { $lock.Dispose() } }
