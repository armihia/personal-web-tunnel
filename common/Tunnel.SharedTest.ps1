# 只修改原有入口的分流和允许端口，保留认证、TLS 与其它站点配置。
function Enable-TunnelSharedFrps {
    param([string]$Text, $Settings)
    if ($Text -notmatch '(?m)^proxyBindAddr\s*=\s*"127\.0\.0\.1"\s*$') {
        throw '原 FRPS 必须只在 127.0.0.1 监听代理端口，未修改配置。'
    }
    $entries = [regex]::Matches($Text, '(?m)^allowPorts\s*=\s*\[(?<ports>[^\r\n]*)\][ \t]*\r?$')
    if ($entries.Count -ne 1) { throw '无法确认原 allowPorts 单行配置，请手工合并允许端口。' }
    $entry = $entries[0]
    $ports = $entry.Groups['ports'].Value
    if ($ports -match ('\{\s*single\s*=\s*' + $Settings.SharedMainPort + '\s*\}')) { return $Text }
    if (-not $ports.Trim()) { throw '原允许端口列表为空，未修改配置。' }
    $replacement = 'allowPorts = [' + $ports.Trim() + ', { single = ' + $Settings.SharedMainPort + ' }]'
    return $Text.Substring(0,$entry.Index) + $replacement + $Text.Substring($entry.Index+$entry.Length)
}

function Get-TunnelCaddySiteBlock {
    param([string]$Text, [string]$Domain)
    $pattern = '(?m)^[ \t]*(?:https://)?' + [regex]::Escape($Domain) + '[ \t]*\{[ \t]*\r?$'
    $sites = [regex]::Matches($Text,$pattern)
    if ($sites.Count -ne 1) { throw '未找到唯一的目标域名单独站点块，请手工合并分流。' }
    $start = $sites[0].Index
    $opening = $Text.IndexOf('{',$start)
    $depth = 0; $quoted = $false; $escaped = $false; $comment = $false; $closing = -1
    for ($i=$opening; $i -lt $Text.Length; $i++) {
        $character = $Text[$i]
        if ($comment) { if ($character -eq "`n") { $comment=$false }; continue }
        if ($escaped) { $escaped=$false; continue }
        if ($quoted -and $character -eq '\') { $escaped=$true; continue }
        if ($character -eq '"') { $quoted=-not $quoted; continue }
        if ($quoted) { continue }
        if ($character -eq '#') { $comment=$true; continue }
        if ($character -eq '{') { $depth++ }
        if ($character -eq '}') { $depth--; if ($depth -eq 0) { $closing=$i; break } }
    }
    if ($closing -lt 0) { throw '目标站点块未闭合，未修改配置。' }
    return [pscustomobject]@{ Start=$start; Opening=$opening; Closing=$closing; Text=$Text.Substring($start,$closing-$start+1) }
}

function Enable-TunnelSharedCaddy {
    param([string]$Text, $Settings)
    $site = Get-TunnelCaddySiteBlock $Text $Settings.PublicDomain
    $block=$site.Text
    if ($block -match '(?m)^\s*@shared_test\s+path\s+/test\s+/test/\*\s*$' -and
        $block.Contains("reverse_proxy 127.0.0.1:$($Settings.SharedMainPort)")) { return $Text }
    if ($block -match '(?m)^\s*(?:handle|handle_path|route|import)\b') {
        throw '目标站点已有自定义路径分流，请手工合并，未覆盖原规则。'
    }
    $proxyPattern = '(?m)^[ \t]*reverse_proxy[ \t]+127\.0\.0\.1:' + $Settings.RemotePort + '[ \t]*\r?$'
    $proxies = [regex]::Matches($block,$proxyPattern)
    if ($proxies.Count -ne 1 -or [regex]::Matches($block,'(?m)^\s*reverse_proxy\b').Count -ne 1) {
        throw '目标站点不是原来的单一回环代理，请手工合并，未覆盖原规则。'
    }
    $proxy = $proxies[0]
    $replacement = New-TunnelPublicRoutes $Settings
    $changed = $block.Substring(0,$proxy.Index) + $replacement + $block.Substring($proxy.Index+$proxy.Length)
    return $Text.Substring(0,$site.Start) + $changed + $Text.Substring($site.Closing+1)
}
