function New-TunnelClientConfig {
    param($Settings, [int]$LocalPort, [string]$TokenPath, [string]$LogPath,
          [int]$RemotePort = 0, [string]$ProxyName = '')
    if (-not $RemotePort) {
        $RemotePort = if ($Settings.SharedTestEnabled) { $Settings.SharedMainPort } else { $Settings.RemotePort }
    }
    if (-not $ProxyName) {
        $ProxyName = if ($Settings.SharedTestEnabled) { 'personal-main-web' } else { 'personal-temp-web' }
    }
    $proxy = ConvertTo-TomlString $ProxyName
    $server = ConvertTo-TomlString $Settings.ServerAddress
    $token = ConvertTo-TomlString ([IO.Path]::GetFullPath($TokenPath))
    $log = ConvertTo-TomlString ([IO.Path]::GetFullPath($LogPath))
    return @"
serverAddr = $server
serverPort = $($Settings.FrpsBindPort)
loginFailExit = true
auth.method = "token"
auth.tokenSource.type = "file"
auth.tokenSource.file.path = $token
transport.tls.enable = true
transport.dialServerTimeout = $($Settings.ProbeTimeoutSeconds)
log.to = $log
log.level = "info"
log.maxDays = 7
log.disablePrintColor = true

[[proxies]]
name = $proxy
type = "tcp"
localIP = "127.0.0.1"
localPort = $LocalPort
remotePort = $RemotePort
"@
}

function New-TunnelServerConfig {
    param($Settings, [string]$ServerDirectory)
    $token = ConvertTo-TomlString (Join-Path $ServerDirectory 'token.txt')
    $log = ConvertTo-TomlString (Join-Path $ServerDirectory 'frps.log')
    $allowed = "{ single = $($Settings.RemotePort) }"
    if ($Settings.SharedTestEnabled) { $allowed += ", { single = $($Settings.SharedMainPort) }" }
    return @"
bindAddr = "0.0.0.0"
bindPort = $($Settings.FrpsBindPort)
proxyBindAddr = "127.0.0.1"
allowPorts = [$allowed]
transport.tls.force = true
auth.method = "token"
auth.tokenSource.type = "file"
auth.tokenSource.file.path = $token
log.to = $log
log.level = "info"
log.maxDays = 7
"@
}

function New-TunnelPublicRoutes {
    param($Settings)
    if (-not $Settings.SharedTestEnabled) { return "    reverse_proxy 127.0.0.1:$($Settings.RemotePort)" }
    return @"
    @shared_test path /test /test/*
    handle @shared_test {
        reverse_proxy 127.0.0.1:$($Settings.RemotePort)
    }
    handle {
        reverse_proxy 127.0.0.1:$($Settings.SharedMainPort)
    }
"@
}

function New-TunnelCaddyConfig {
    param($Settings, [string]$Directory)
    $accessLog = ConvertTo-TomlString ((Join-Path $Directory 'access.log').Replace('\','/'))
    $serviceLog = ConvertTo-TomlString ((Join-Path $Directory 'caddy.log').Replace('\','/'))
    return @"
{
    admin 127.0.0.1:2019
    log default {
        output file $serviceLog {
            roll_size 10MiB
            roll_keep 7
        }
    }
}
$($Settings.PublicDomain) {
$(New-TunnelPublicRoutes $Settings)
    log {
        output file $accessLog {
            roll_size 10MiB
            roll_keep 7
        }
    }
}
"@
}

function New-TunnelProxyExamples {
    param($Settings, [string]$Directory)
    [IO.Directory]::CreateDirectory($Directory) | Out-Null
    $domain = $Settings.PublicDomain
    $port = $Settings.RemotePort
    Write-TunnelText -Path (Join-Path $Directory 'Caddyfile.snippet') -Text @"
$domain {
$(New-TunnelPublicRoutes $Settings)
}
"@
    # 协作模式的后缀共享使用 Caddy；不要生成仍指向单一入口的其它代理片段。
    if ($Settings.SharedTestEnabled) { return }
    Write-TunnelText -Path (Join-Path $Directory 'nginx.conf.snippet') -Text @"
# 合并到现有 http 配置；证书路径必须替换为服务器的有效证书。
server {
    listen 80;
    server_name $domain;
    return 301 https://`$host`$request_uri;
}
server {
    listen 443 ssl;
    server_name $domain;
    ssl_certificate C:/path/to/fullchain.pem;
    ssl_certificate_key C:/path/to/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:$port;
        proxy_http_version 1.1;
        proxy_set_header Host `$host;
        proxy_set_header X-Real-IP `$remote_addr;
        proxy_set_header X-Forwarded-For `$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto `$scheme;
        proxy_set_header Upgrade `$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
"@
    Write-TunnelText -Path (Join-Path $Directory 'IIS-web.config.snippet') -Text @"
<?xml version="1.0" encoding="utf-8"?>
<!-- 在 IIS 现有配置中合并，不要直接覆盖 web.config。需安装 URL Rewrite 和 ARR。 -->
<configuration>
  <system.webServer>
    <rewrite><rules>
      <rule name="PersonalWebTunnel" stopProcessing="true">
        <match url="(.*)" />
        <conditions><add input="{HTTP_HOST}" pattern="^$([regex]::Escape($domain))$" /></conditions>
        <action type="Rewrite" url="http://127.0.0.1:$port/{R:1}" />
      </rule>
    </rules></rewrite>
  </system.webServer>
</configuration>
"@
}
