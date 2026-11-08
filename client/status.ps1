[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
try {
    . (Join-Path (Split-Path -Parent $PSScriptRoot) 'common\Tunnel.Common.ps1')
    Assert-TunnelWindows
    $settings = Get-TunnelSettings
    $statePath = Join-Path $PSScriptRoot 'frpc.state.json'
    $localPort = $settings.LocalPort
    $process = Get-TunnelOwnedProcess -StatePath $statePath -Exe (Join-Path $PSScriptRoot 'frpc.exe') -ConfigPath (Join-Path $PSScriptRoot 'frpc.runtime.toml')
    if (Test-Path -LiteralPath $statePath) {
        $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $localPort = $state.LocalPort
        if ($state.PSObject.Properties['ServerAddress']) { $settings.ServerAddress = $state.ServerAddress }
    } else {
        $runtime = Join-Path $PSScriptRoot 'frpc.runtime.toml'
        if (Test-Path -LiteralPath $runtime) {
            $text = Get-Content -LiteralPath $runtime -Raw -Encoding UTF8
            if ($text -match '(?m)^localPort\s*=\s*(\d+)\s*$') { $localPort = [int]$Matches[1] }
            if ($text -match '(?m)^serverAddr\s*=\s*("[^\r\n]+")\s*$') {
                $settings.ServerAddress = $Matches[1] | ConvertFrom-Json
            }
        }
    }
    $localUp = Test-TunnelTcp -Address '127.0.0.1' -Port $localPort -TimeoutSeconds $settings.ProbeTimeoutSeconds
    $serverUp = Test-TunnelTcp -Address $settings.ServerAddress -Port $settings.FrpsBindPort -TimeoutSeconds $settings.ProbeTimeoutSeconds
    $url = "https://$($settings.PublicDomain)"
    $public = Test-TunnelHttp -Url $url -TimeoutSeconds $settings.ProbeTimeoutSeconds
    $localText = 'DOWN'; if ($localUp) { $localText = 'UP' }
    $processText = 'STOPPED'; if ($process) { $processText = "RUNNING（PID $($process.Id)）" }
    $serverText = 'NO'; if ($serverUp) { $serverText = 'YES' }
    $publicText = 'DOWN'; if ($public.Up) { $publicText = 'UP' }
    Write-Host "本地网站     : $localText（127.0.0.1:$localPort）"
    Write-Host "FRPC 进程    : $processText"
    Write-Host "FRPS 可连接  : $serverText（$($settings.ServerAddress):$($settings.FrpsBindPort)）"
    Write-Host "公网 HTTP    : $publicText（$($public.Detail)）"
    Write-Host "公网地址     : $url"
    Write-Host '公网 HTTP 结果只说明域名响应；请用本地网站特有内容核对实际转发。'
} catch { Write-Error "状态查询失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
