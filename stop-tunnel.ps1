[CmdletBinding()]
param([switch]$ShowResult)
$ErrorActionPreference = 'Stop'
try {
    . (Join-Path $PSScriptRoot 'common\Tunnel.Common.ps1')
    Assert-TunnelWindows
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
        if ($ShowResult) { $arguments += ' -ShowResult' }
        $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -ArgumentList $arguments -PassThru -Wait
        exit $elevated.ExitCode
    }
    # 管理器负责释放两个入口和自身创建的直连路由，必须先等它退出。
    $global:LASTEXITCODE = 0
    & (Join-Path $PSScriptRoot 'stop-manager.ps1')
    if ($LASTEXITCODE) { throw '管理器未完全停止，请按报错处理后重新运行。' }
    # 兼顾以前直接运行 publish.ps1 或 publish-direct.ps1 的用法。
    $global:LASTEXITCODE = 0
    & (Join-Path $PSScriptRoot 'client\stop-publish.ps1')
    if ($LASTEXITCODE) { throw '旧发布连接未停止，请按报错处理后重新运行。' }
    Write-Host '[OK] 本机穿透服务已停止；映射配置保留。'
    if ($ShowResult) {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show(
            '本机穿透服务已停止，映射配置保留。需要时双击“启动管理器.cmd”恢复。',
            '网站穿透', [Windows.Forms.MessageBoxButtons]::OK,
            [Windows.Forms.MessageBoxIcon]::Information) | Out-Null
    }
    exit 0
} catch {
    Write-Error "停止穿透失败：$($_.Exception.Message)" -ErrorAction Continue
    if ($ShowResult) {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show($_.Exception.Message, '停止穿透失败',
            [Windows.Forms.MessageBoxButtons]::OK, [Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    }
    exit 1
}
