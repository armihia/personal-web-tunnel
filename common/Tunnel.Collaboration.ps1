# 为协作模式生成预览配置；调用方负责校验、备份和回滚。
function Open-TunnelLegacyGuard {
    param([int]$Port)
    $socket=New-Object Net.Sockets.Socket([Net.Sockets.AddressFamily]::InterNetwork,[Net.Sockets.SocketType]::Stream,[Net.Sockets.ProtocolType]::Tcp)
    try {
        $socket.ExclusiveAddressUse=$true
        $socket.Bind((New-Object Net.IPEndPoint([Net.IPAddress]::Loopback,$Port)))
        return $socket
    } catch {
        $socket.Dispose()
        throw '旧普通入口正在被连接或占用，未修改服务器。'
    }
}

function Enable-TunnelCollaborationFrps {
    param([string]$Text, [int]$FirstPort=30000, [int]$LastPort=30255)
    if ($Text -notmatch '(?m)^proxyBindAddr\s*=\s*"127\.0\.0\.1"\s*$') {
        throw 'FRPS 代理端口没有限制为本机回环，未修改。'
    }
    $entries=[regex]::Matches($Text,'(?m)^allowPorts\s*=\s*\[(?<ports>[^\r\n]*)\][ \t]*\r?$')
    if ($entries.Count -ne 1) { throw '无法确认唯一的 FRPS 允许端口列表。' }
    $entry=$entries[0]; $ports=$entry.Groups['ports'].Value
    if ($ports -match ('\{\s*start\s*=\s*'+$FirstPort+'\s*,\s*end\s*=\s*'+$LastPort+'\s*\}')) { return $Text }
    if (-not $ports.Trim()) { throw 'FRPS 允许端口列表为空，未修改。' }
    $ranges=[regex]::Matches($ports,'\{\s*start\s*=\s*(\d+)\s*,\s*end\s*=\s*(\d+)\s*\}')
    foreach ($range in $ranges) {
        $start=[int]$range.Groups[1].Value; $end=[int]$range.Groups[2].Value
        if ($start -le $FirstPort -and $end -ge $LastPort) { return $Text }
        if ($start -le $LastPort -and $end -ge $FirstPort) { throw '现有 FRPS 端口范围与协作范围部分重叠，请人工核对。' }
    }
    $singles=[regex]::Matches($ports,'\{\s*single\s*=\s*(\d+)\s*\}')
    foreach ($single in $singles) {
        $port=[int]$single.Groups[1].Value
        if ($port -ge $FirstPort -and $port -le $LastPort) { throw '现有单独允许端口与协作范围重叠，请人工核对。' }
    }
    $replacement='allowPorts = ['+$ports.Trim()+', { start = '+$FirstPort+', end = '+$LastPort+' }]'
    return $Text.Substring(0,$entry.Index)+$replacement+$Text.Substring($entry.Index+$entry.Length)
}

function New-TunnelCollaborationDispatch {
    param([int]$RegistryPort=18083)
    return @"
route {
    request_header -X-Personal-Tunnel-Port
    forward_auth 127.0.0.1:$RegistryPort {
        uri /lookup
        header_up X-Forwarded-Host {host}
        copy_headers X-Personal-Tunnel-Port
    }
    reverse_proxy 127.0.0.1:{http.request.header.X-Personal-Tunnel-Port} {
        header_up -X-Personal-Tunnel-Port
    }
}
"@
}

function Replace-TunnelCollaborationProxy {
    param([string]$Block, [int]$LegacyPort, [int]$RegistryPort)
    $pattern='(?m)^[ \t]*reverse_proxy[ \t]+127\.0\.0\.1:'+$LegacyPort+'[ \t]*\r?$'
    $matches=[regex]::Matches($Block,$pattern)
    if ($matches.Count -ne 1) { throw '无法确认唯一的原普通映射代理，未覆盖。' }
    $match=$matches[0]
    return $Block.Substring(0,$match.Index)+(New-TunnelCollaborationDispatch $RegistryPort)+$Block.Substring($match.Index+$match.Length)
}

function Enable-TunnelCollaborationCaddy {
    param([string]$Text, [string]$PublicDomain='demo.example.com',
          [string]$BaseDomain='example.com', [int]$LegacyPort=18081,
          [int]$TestPort=18080, [int]$RegistryPort=18083)
    if ($Text -match '(?i)__personal_web_tunnel/registry|X-Personal-Tunnel-Port') {
        throw 'Caddy 配置已有协作路由，请先核对，未覆盖。'
    }
    if ($Text -match '(?m)^\s*import(?:\s|$)') { throw 'Caddy 配置包含导入项，请人工核对后再合并协作路由。' }
    $site=Get-TunnelCaddySiteBlock $Text $PublicDomain
    if ($site.Text -match '(?i)__personal_web_tunnel') { throw '公网主站已使用协作保留路径，请先人工核对。' }
    if ($site.Text -notmatch '(?m)^\s*@shared_test\s+path\s+/test\s+/test/\*\s*$' -or
        $site.Text -notmatch ('(?m)^\s*reverse_proxy\s+127\.0\.0\.1:'+$TestPort+'\s*$')) {
        throw '未确认原 /test 兼容路由，未修改。'
    }
    $changed=Replace-TunnelCollaborationProxy $site.Text $LegacyPort $RegistryPort
    $opening=$changed.IndexOf('{')
    $api=@"

    @personal_registry path /__personal_web_tunnel/registry/health /__personal_web_tunnel/registry/lease/*
    handle @personal_registry {
        uri strip_prefix /__personal_web_tunnel/registry
        reverse_proxy 127.0.0.1:$RegistryPort
    }
"@
    $changed=$changed.Insert($opening+1,$api)
    $Text=$Text.Substring(0,$site.Start)+$changed+$Text.Substring($site.Closing+1)
    # 单独添加过的一级子域名站点也使用同一登记服务。
    $domainPattern='(?mi)^[ \t]*(?:https://)?(?<domain>[a-z0-9][a-z0-9-]*\.'+[regex]::Escape($BaseDomain)+')[ \t]*\{[ \t]*\r?$'
    $domains=@([regex]::Matches($Text,$domainPattern) | ForEach-Object { $_.Groups['domain'].Value } | Where-Object { $_ -ne $PublicDomain } | Select-Object -Unique)
    foreach ($domain in $domains) {
        $sub=Get-TunnelCaddySiteBlock $Text $domain
        if ($sub.Text -notmatch ('127\.0\.0\.1:'+$LegacyPort+'(?:\s|$)')) { continue }
        if ($sub.Text -match '(?m)^\s*(?:handle|handle_path|route|import)\b') {
            throw "域名 $domain 已有复杂路由，请人工核对，未覆盖。"
        }
        $subChanged=Replace-TunnelCollaborationProxy $sub.Text $LegacyPort $RegistryPort
        $Text=$Text.Substring(0,$sub.Start)+$subChanged+$Text.Substring($sub.Closing+1)
    }
    $askOld='http://127.0.0.1:'+$LegacyPort+'/__personal_web_tunnel/certificate-permission'
    $askNew='http://127.0.0.1:'+$RegistryPort+'/certificate-permission'
    if ($Text -match '(?m)^\s*on_demand_tls\s*\{') {
        if (([regex]::Matches($Text,[regex]::Escape($askOld))).Count -ne 1) {
            throw '已有按需证书配置与工具不匹配，请人工核对。'
        }
        $Text=$Text.Replace($askOld,$askNew)
        $catchall=Get-TunnelCaddySiteBlock $Text 'https://'
        $newCatchall=Replace-TunnelCollaborationProxy $catchall.Text $LegacyPort $RegistryPort
        $Text=$Text.Substring(0,$catchall.Start)+$newCatchall+$Text.Substring($catchall.Closing+1)
    } else {
        $global=[regex]::Match($Text,'\A\s*\{')
        if (-not $global.Success -or $Text -match '(?m)^\s*(?:https://\s*\{|:443\s*\{)') {
            throw '无法安全添加按需 HTTPS 配置，请人工合并。'
        }
        $opening=$Text.IndexOf('{',$global.Index)
        $Text=$Text.Insert($opening+1,"`r`n    on_demand_tls {`r`n        ask $askNew`r`n    }")
        $dispatch=New-TunnelCollaborationDispatch $RegistryPort
        $addition=@"

https:// {
    tls {
        on_demand
        issuer acme {
            disable_http_challenge
        }
    }
    @personal_subdomain host *.$BaseDomain
    handle @personal_subdomain {
        $dispatch
    }
    handle {
        respond "Not Found" 404
    }
}
"@
        $Text=$Text.TrimEnd()+"`r`n"+$addition+"`r`n"
    }
    if ($Text -match ('127\.0\.0\.1:'+$LegacyPort+'(?:\s|$)')) {
        throw '其他站点仍引用旧普通入口，请先人工核对。'
    }
    if ($Text -match '(?m)^\s*log_credentials(?:\s|$)') {
        throw '当前 Caddy 开启了凭据日志，不能安全公开协作登记接口。'
    }
    return $Text
}
