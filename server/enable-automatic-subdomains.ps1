[CmdletBinding()]
param([ValidateRange(1024,65535)][int]$ProxyPort=18081,
      [string]$CaddyExe, [string]$CaddyConfig, [switch]$Apply)
$ErrorActionPreference='Stop'
try {
    $root=Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    . (Join-Path $root 'common\Tunnel.SharedTest.ps1')
    Assert-TunnelAdministrator
    $settings=Get-TunnelSettings
    $baseDomain=[string]$settings.ServerAddress
    $context=Get-TunnelCaddyContext $CaddyExe $CaddyConfig
    $old=[IO.File]::ReadAllText($context.Config)
    $demo=Get-TunnelCaddySiteBlock $old $settings.PublicDomain
    if ($demo.Text -notmatch ('(?m)^\s*reverse_proxy\s+127\.0\.0\.1:'+$ProxyPort+'(?:\s|$)')) {
        throw '未在现有 demo 站点确认代理端口；未修改配置。'
    }
    if ($old -match '(?im)^\s*(?:on_demand_tls|https://\s*\{|:443\s*\{)') {
        throw '当前已有按需证书或 HTTPS 通配入口，请先人工核对，未覆盖原配置。'
    }
    # 避免错误修改复杂的全局块；复杂配置需人工合并。
    $global=[regex]::Match($old,'\A\s*\{')
    if (-not $global.Success) { throw '无法安全识别 Caddy 全局选项块，请人工合并。' }
    $opening=$old.IndexOf('{',$global.Index)
    $option="`r`n    on_demand_tls {`r`n        ask http://127.0.0.1:$ProxyPort/__personal_web_tunnel/certificate-permission`r`n    }"
    $updated=$old.Insert($opening+1,$option)
    $addition=@"

https:// {
    tls {
        on_demand
        issuer acme {
            disable_http_challenge
        }
    }
    @personal_subdomain host *.$baseDomain
    handle @personal_subdomain {
        reverse_proxy 127.0.0.1:$ProxyPort
    }
    handle {
        respond "Not Found" 404
    }
}
"@
    $updated=$updated.TrimEnd()+"`r`n"+$addition+"`r`n"
    $candidate=$context.Config+'.automatic-subdomains.preview'
    Write-TunnelText $candidate $updated
    $directory=Split-Path -Parent $context.Config
    Invoke-TunnelNative $context.Exe ('validate --config "'+$candidate+'" --adapter caddyfile') $directory | Out-Null
    if (-not $Apply) { Write-Host "[预览] 已校验 $candidate；请先设置 DNS 泛解析并确认本机已更新，再加 -Apply。"; exit 0 }
    $backup=$context.Config+'.before-automatic-subdomains-'+(Get-Date -Format 'yyyyMMdd-HHmmss-fff')
    Copy-Item -LiteralPath $context.Config -Destination $backup
    try {
        Write-TunnelText $context.Config $updated
        Invoke-TunnelNative $context.Exe $context.ReloadArguments $directory | Out-Null
    } catch {
        $failure=$_
        Copy-Item -LiteralPath $backup -Destination $context.Config -Force
        try { Invoke-TunnelNative $context.Exe $context.ReloadArguments $directory | Out-Null }
        catch { Write-Warning '原配置已恢复到磁盘，但在线重载失败，请检查 Caddy。' }
        throw $failure
    }
    Write-Host "[OK] 已启用按需证书；仅本机管理器已启用的独立域名可以申请。"
    Write-Host "[备份] $backup"
    Write-Host '[待验证] 完成 DNS 泛解析后，启动本机映射并从公网访问。'
} catch { Write-Error "配置自动子域名失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
