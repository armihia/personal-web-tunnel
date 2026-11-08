[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Domain,
    [ValidateRange(1024,65535)][int]$ProxyPort=18081,
    [string]$CaddyExe, [string]$CaddyConfig, [switch]$Apply
)
$ErrorActionPreference='Stop'
try {
    $root=Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    . (Join-Path $root 'common\Tunnel.SharedTest.ps1')
    Assert-TunnelAdministrator
    $settings=Get-TunnelSettings
    $baseDomain=[string]$settings.ServerAddress
    $Domain=$Domain.Trim().ToLowerInvariant().TrimEnd('.')
    if ($Domain -notmatch ('^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.'+[regex]::Escape($baseDomain)+'$') -or $Domain -eq $settings.PublicDomain) {
        throw '请填写 ServerAddress 下的一级子域名，并避开 PublicDomain。'
    }
    $context=Get-TunnelCaddyContext $CaddyExe $CaddyConfig
    $old=[IO.File]::ReadAllText($context.Config)
    $demo=Get-TunnelCaddySiteBlock $old $settings.PublicDomain
    if ($demo.Text -notmatch ('(?m)^\s*reverse_proxy\s+127\.0\.0\.1:'+$ProxyPort+'(?:\s|$)')) {
        throw '现有 demo 站点没有确认使用该代理端口，请核对服务器实际配置。'
    }
    if ($old.IndexOf($Domain,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
        throw '当前配置已出现此域名，请人工核对现有配置，未覆盖。'
    }
    $addition=@"

$Domain {
    tls {
        issuer acme {
            disable_http_challenge
        }
    }
    reverse_proxy 127.0.0.1:$ProxyPort
}
"@
    $updated=$old.TrimEnd()+"`r`n"+$addition+"`r`n"
    $candidate=$context.Config+'.subdomain.preview'
    Write-TunnelText $candidate $updated
    $directory=Split-Path -Parent $context.Config
    Invoke-TunnelNative $context.Exe ('validate --config "'+$candidate+'" --adapter caddyfile') $directory | Out-Null
    if (-not $Apply) { Write-Host "[预览] 已校验 $candidate；确认域名解析后使用 -Apply。"; exit 0 }
    $backup=$context.Config+'.before-subdomain-'+(Get-Date -Format 'yyyyMMdd-HHmmss-fff')
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
    Write-Host "[OK] 已添加 $Domain，转发至本机代理端口 $ProxyPort。"
    Write-Host "[备份] $backup"
    Write-Host '[待验证] 确认 DNS 已指向服务器，并访问新 HTTPS 域名；首次申请证书可能需要时间。'
} catch { Write-Error "配置独立域名失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
