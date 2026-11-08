[CmdletBinding()]
param([ValidateSet('Auto','Existing')][string]$HttpsMode = 'Auto')
$ErrorActionPreference = 'Stop'
try {
    $root = Split-Path -Parent $PSScriptRoot
    . (Join-Path $root 'common\Tunnel.Common.ps1')
    . (Join-Path $root 'common\Tunnel.Config.ps1')
    . (Join-Path $root 'common\Tunnel.Server.ps1')
    Assert-TunnelAdministrator
    $settings = Get-TunnelSettings
    $conflict = Get-TunnelListeners -Ports @($settings.FrpsBindPort,$settings.RemotePort)
    if ($conflict.Count -gt 0) { throw "FRP 端口被占用：$(Format-TunnelListeners $conflict)。请修改 settings.json 或先停止本项目服务器。" }
    $proxyDirectory = Join-Path $PSScriptRoot 'reverse-proxy'
    $generated = Join-Path $proxyDirectory 'generated'
    New-TunnelProxyExamples -Settings $settings -Directory $generated
    $webListeners = Get-TunnelListeners -Ports @(80,443)
    $installCaddy = ($HttpsMode -eq 'Auto' -and $webListeners.Count -eq 0)
    $frpsExe = Join-Path $PSScriptRoot 'frps.exe'
    $frpsConfig = Join-Path $PSScriptRoot 'frps.toml'
    Get-TunnelTask -Name $script:FrpsTaskName -Exe $frpsExe -ConfigPath $frpsConfig | Out-Null
    if ($installCaddy) {
        Get-TunnelTask -Name $script:CaddyTaskName -Exe (Join-Path $proxyDirectory 'caddy.exe') -ConfigPath (Join-Path $proxyDirectory 'Caddyfile') | Out-Null
    }
    $frpsExe = Install-TunnelBinary -Name frps -Directory $PSScriptRoot -Settings $settings
    $tokenPath = Join-Path $PSScriptRoot 'token.txt'
    if (-not (Test-Path -LiteralPath $tokenPath)) {
        $bytes = New-Object byte[] 32
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $random.GetBytes($bytes) } finally { $random.Dispose() }
        $token = ([BitConverter]::ToString($bytes)).Replace('-','').ToLowerInvariant()
        Write-TunnelText -Path $tokenPath -Text $token
        Write-Host "[首次生成] 认证令牌已写入 server\token.txt。"
        Write-Host '请安全复制 server\token.txt 到个人电脑的 client\token.txt；再次安装不会显示或更换 token。'
    }
    Assert-TunnelToken $tokenPath
    # 密钥仅允许管理员和 SYSTEM 访问；不修改上级目录的 ACL。
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($sidText in @('S-1-5-18','S-1-5-32-544')) {
        $sid = New-Object Security.Principal.SecurityIdentifier($sidText)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $tokenPath -AclObject $acl
    Write-TunnelText -Path $frpsConfig -Text (New-TunnelServerConfig -Settings $settings -ServerDirectory $PSScriptRoot)
    Invoke-TunnelNative -Exe $frpsExe -Arguments ('verify -c "' + $frpsConfig + '"') -Directory $PSScriptRoot | Out-Null
    $caddyExe = $null
    if ($installCaddy) {
        $caddyExe = Install-TunnelBinary -Name caddy -Directory $proxyDirectory -Settings $settings
        $caddyConfig = Join-Path $proxyDirectory 'Caddyfile'
        if (-not (Test-Path -LiteralPath $caddyConfig)) {
            Write-TunnelText -Path $caddyConfig -Text (New-TunnelCaddyConfig -Settings $settings -Directory $proxyDirectory)
        }
        # adapt 不申请证书；实际证书申请在 Caddy 启动后由其完成。
        Invoke-TunnelNative -Exe $caddyExe -Arguments ('adapt --config "' + $caddyConfig + '" --adapter caddyfile') -Directory $proxyDirectory | Out-Null
    }
    Set-TunnelFirewall -Name 'PersonalWebTunnel-FRPS-In' -Ports @($settings.FrpsBindPort) -Exe $frpsExe
    Register-TunnelTask -Name $script:FrpsTaskName -Exe $frpsExe -Arguments ('-c "' + $frpsConfig + '"') -ConfigPath $frpsConfig -Directory $PSScriptRoot
    if ($installCaddy) {
        # 下载期间可能有其它 Web 服务启动，再检测一次。
        $webListeners = Get-TunnelListeners -Ports @(80,443)
        if ($webListeners.Count -gt 0) { throw "80/443 已被占用：$(Format-TunnelListeners $webListeners)。Caddy 未启动，请合并 $generated 中的配置。" }
        Set-TunnelFirewall -Name 'PersonalWebTunnel-HTTPS-In' -Ports @(80,443) -Exe $caddyExe
        Register-TunnelTask -Name $script:CaddyTaskName -Exe $caddyExe -Arguments ('run --config "' + $caddyConfig + '" --adapter caddyfile') -ConfigPath $caddyConfig -Directory $proxyDirectory
    } else {
        Write-Host "[待配置] 已有 Web 服务或选择 Existing 模式。$(Format-TunnelListeners $webListeners)"
        Write-Host "[待配置] 将 $generated 中适用的片段合并到现有 HTTPS 网站；现有配置未被修改。"
    }
    $global:LASTEXITCODE = 0
    & (Join-Path $PSScriptRoot 'start-server.ps1')
    if ($LASTEXITCODE -ne 0) { throw '服务器启动未成功，请检查上方错误和日志。' }
    Write-Host "[OK] FRPS 已安装，计划任务在系统启动时自动恢复。"
    Write-Host "[待确认] 在服务器防火墙和云安全组中开放 TCP 80、443、$($settings.FrpsBindPort)，不要开放 $($settings.RemotePort)。"
    Write-Host "[待确认] $($settings.PublicDomain) 的 DNS 指向本服务器，公网 HTTPS 地址：https://$($settings.PublicDomain)"
    Write-Host '安装成功只表示本机程序启动；证书、公网连通和网络切换仍需按 README 验收。'
} catch { Write-Error "服务器安装失败：$($_.Exception.Message)" -ErrorAction Continue; exit 1 }
