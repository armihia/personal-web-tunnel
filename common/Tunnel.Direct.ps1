# 仅为指定服务器建立临时主机路由，保留已有路由和 Clash 配置。
function New-TunnelDirectRoute {
    param([Net.IPAddress]$ServerIP, [uint32]$InterfaceIndex = 0)
    if ($ServerIP.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        throw '直连模式需要服务器的真实 IPv4 地址。'
    }
    $ip = $ServerIP.ToString()
    $bytes = $ServerIP.GetAddressBytes()
    if ($bytes[0] -eq 0 -or $bytes[0] -eq 127 -or $bytes[0] -ge 224 -or
        ($bytes[0] -eq 198 -and $bytes[1] -in @(18,19))) {
        throw '请填写自有服务器的公网 IPv4 地址，回环和 Fake-IP 地址不可用于连接服务器。'
    }
    $gateway = Get-TunnelDirectGateway -InterfaceIndex $InterfaceIndex
    $prefix = "$ip/32"
    $existing = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore -ErrorAction Stop |
        Where-Object DestinationPrefix -EQ $prefix)
    $created = $null
    try {
        if ($existing.Count -gt 0) {
            $matching = @($existing | Where-Object {
                $_.InterfaceIndex -eq $gateway.InterfaceIndex -and $_.NextHop -eq $gateway.NextHop
            })
            if ($matching.Count -ne $existing.Count) {
                throw "已有 $prefix 路由指向其它出口，请先检查该路由；脚本不会覆盖。"
            }
        } else {
            $created = New-NetRoute -DestinationPrefix $prefix -InterfaceIndex $gateway.InterfaceIndex `
                -NextHop $gateway.NextHop -RouteMetric 1 -PolicyStore ActiveStore -ErrorAction Stop
        }
        # 核对 Windows 实际选路，避免只凭添加成功就声称已绕过 TUN。
        $selected = @(Find-NetRoute -RemoteIPAddress $ip -ErrorAction Stop |
            Where-Object { $_.PSObject.Properties['DestinationPrefix'] })
        if ($selected.Count -ne 1 -or $selected[0].InterfaceIndex -ne $gateway.InterfaceIndex -or
            $selected[0].NextHop -ne $gateway.NextHop) {
            throw 'Windows 没有选择预期的物理网卡路由，停止本次直连发布。'
        }
        return [pscustomobject]@{
            ServerIP = $ip
            InterfaceIndex = $gateway.InterfaceIndex
            InterfaceAlias = $gateway.InterfaceAlias
            NextHop = $gateway.NextHop
            CreatedRoute = $created
        }
    } catch {
        if ($created) { $created | Remove-NetRoute -Confirm:$false -ErrorAction Stop }
        throw
    }
}

function Get-TunnelDirectGateway {
    param([uint32]$InterfaceIndex = 0)
    $adapters = @(Get-NetAdapter -Physical -ErrorAction Stop | Where-Object {
        $_.Status -eq 'Up' -and ($InterfaceIndex -eq 0 -or $_.InterfaceIndex -eq $InterfaceIndex)
    })
    $indices = @($adapters | ForEach-Object { $_.InterfaceIndex })
    $defaults = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore -ErrorAction Stop |
        Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' -and
            $_.InterfaceIndex -in $indices -and $_.NextHop -ne '0.0.0.0' })
    if ($defaults.Count -eq 0) { throw '未找到已连接物理网卡的 IPv4 默认网关，请检查 Wi-Fi 或网线。' }
    return ($defaults | Sort-Object @{ Expression = {
        $interface = Get-NetIPInterface -AddressFamily IPv4 -InterfaceIndex $_.InterfaceIndex -ErrorAction Stop
        [int]$_.RouteMetric + [int]$interface.InterfaceMetric
    } }, InterfaceIndex | Select-Object -First 1)
}

function Remove-TunnelDirectRoute {
    param($RouteState)
    # 复用的已有路由不归本次发布所有，只有本次创建的路由才清理。
    if ($RouteState -and $RouteState.CreatedRoute) {
        try {
            $RouteState.CreatedRoute | Remove-NetRoute -Confirm:$false -ErrorAction Stop
        } catch {
            # 网卡断开或重启可能已经移除了 ActiveStore 路由；缺失时视为已清理。
            $created = $RouteState.CreatedRoute
            $remaining = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore -ErrorAction Stop | Where-Object {
                $_.DestinationPrefix -eq $created.DestinationPrefix -and
                $_.InterfaceIndex -eq $created.InterfaceIndex -and $_.NextHop -eq $created.NextHop
            })
            if ($remaining.Count -gt 0) { throw }
        }
    }
}
