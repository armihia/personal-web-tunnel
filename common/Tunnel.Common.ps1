# 所有入口共享配置、下载、探测和进程识别，兼容 Windows PowerShell 5.1。
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:TunnelRoot = Split-Path -Parent $PSScriptRoot

function Assert-TunnelWindows {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw '此工具只支持 Windows。'
    }
    $architecture = $env:PROCESSOR_ARCHITECTURE
    if ($architecture -ne 'AMD64' -or -not [Environment]::Is64BitProcess) {
        throw '请使用 Windows x64 的 64 位 PowerShell；当前发布包为 windows_amd64。'
    }
    if ($PSVersionTable.PSVersion -lt [version]'5.1') {
        throw '需要 Windows PowerShell 5.1 或 PowerShell 7。'
    }
}

function Assert-TunnelAdministrator {
    Assert-TunnelWindows
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '请在 Windows 服务器上以管理员身份运行 PowerShell。'
    }
}

function Get-TunnelSettings {
    param([string]$Path = (Join-Path $script:TunnelRoot 'settings.json'), [string]$ServerAddress,
          [switch]$SharedTest)
    $config = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $config.PSObject.Properties['SharedTestEnabled']) {
        $config | Add-Member -NotePropertyName SharedTestEnabled -NotePropertyValue $false
    }
    if (-not $config.PSObject.Properties['SharedMainPort']) {
        $config | Add-Member -NotePropertyName SharedMainPort -NotePropertyValue 18081
    }
    if ($SharedTest) { $config.SharedTestEnabled = $true }
    if ($config.SharedTestEnabled -isnot [bool]) { throw 'SharedTestEnabled 必须为布尔值。' }
    foreach ($key in @('PublicDomain','ServerAddress','FrpsBindPort','RemotePort','LocalHost','LocalPort',
                       'FrpVersion','CaddyVersion','ProbeTimeoutSeconds','ConnectTimeoutSeconds')) {
        if (-not $config.PSObject.Properties[$key]) { throw "配置缺少字段：$key" }
    }
    if ($PSBoundParameters.ContainsKey('ServerAddress')) { $config.ServerAddress = $ServerAddress }
    $domainPattern = '^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$'
    if ($config.PublicDomain -notmatch $domainPattern) { throw 'PublicDomain 必须为纯域名，不含 https:// 或路径。' }
    $parsedIP = $null
    if ($config.ServerAddress -notmatch $domainPattern -and
        -not [Net.IPAddress]::TryParse([string]$config.ServerAddress, [ref]$parsedIP)) {
        throw 'ServerAddress 必须为服务器域名或公网 IP。'
    }
    foreach ($key in @('FrpsBindPort','RemotePort','LocalPort')) {
        $value = $config.$key
        if ($value -isnot [int] -and $value -isnot [long]) { throw "$key 必须为整数。" }
        if ($value -lt 1 -or $value -gt 65535) { throw "$key 必须在 1 到 65535 之间。" }
    }
    if ($config.FrpsBindPort -eq $config.RemotePort -or
        $config.FrpsBindPort -in @(80,443) -or $config.RemotePort -in @(80,443)) {
        throw 'FRP 控制端口、代理端口必须不同，且不能占用 80/443。'
    }
    if ($config.LocalHost -ne '127.0.0.1') { throw 'LocalHost 必须为 127.0.0.1。' }
    if (($config.SharedMainPort -isnot [int] -and $config.SharedMainPort -isnot [long]) -or
        $config.SharedMainPort -lt 1024 -or $config.SharedMainPort -gt 65535 -or
        ($config.SharedTestEnabled -and $config.SharedMainPort -in @($config.RemotePort,$config.FrpsBindPort))) {
        throw 'SharedMainPort 必须是独立的 1024～65535 端口。'
    }
    if ($config.FrpVersion -ne '0.71.0') { throw '此版本工具要求服务端、客户端统一使用 FRP 0.71.0。' }
    if ($config.CaddyVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'CaddyVersion 必须为完整版本号。' }
    foreach ($key in @('ProbeTimeoutSeconds','ConnectTimeoutSeconds')) {
        if (($config.$key -isnot [int] -and $config.$key -isnot [long]) -or $config.$key -lt 1 -or $config.$key -gt 60) {
            throw "$key 必须为 1 到 60 的整数秒。"
        }
    }
    return $config
}

function Write-TunnelText {
    param([string]$Path, [string]$Text)
    $parent = Split-Path -Parent ([IO.Path]::GetFullPath($Path))
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function ConvertTo-TomlString {
    param([string]$Value)
    # JSON 字符串的转义规则可用于本项目的 TOML 基本字符串。
    return (ConvertTo-Json -InputObject $Value -Compress)
}

function Test-TunnelTcp {
    param([string]$Address, [int]$Port, [int]$TimeoutSeconds = 3)
    $socket = New-Object Net.Sockets.TcpClient
    try {
        $operation = $socket.ConnectAsync($Address, $Port)
        if (-not $operation.Wait($TimeoutSeconds * 1000)) { return $false }
        return $socket.Connected
    } catch { return $false }
    finally { $socket.Dispose() }
}

function Test-TunnelHttp {
    param([string]$Url, [int]$TimeoutSeconds = 3)
    # HttpClient 的总超时覆盖 DNS、TLS、响应头和读取，禁止无限重定向。
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $http = New-Object Net.Http.HttpClient($handler)
    $http.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
    $response = $null
    try {
        $operation = $http.GetAsync($Url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead)
        if (-not $operation.Wait($TimeoutSeconds * 1000)) {
            $http.CancelPendingRequests()
            return [pscustomobject]@{ Up = $false; Code = 0; Detail = "HTTP 探测超过 $TimeoutSeconds 秒" }
        }
        $response = $operation.GetAwaiter().GetResult()
        $code = [int]$response.StatusCode
        return [pscustomobject]@{ Up = ($code -ge 200 -and $code -lt 400); Code = $code; Detail = "HTTP $code" }
    } catch {
        return [pscustomobject]@{ Up = $false; Code = 0; Detail = $_.Exception.GetBaseException().Message }
    } finally {
        if ($response) { $response.Dispose() }
        $http.Dispose()
        $handler.Dispose()
    }
}

function Invoke-TunnelNative {
    param([string]$Exe, [string]$Arguments, [string]$Directory, [int]$TimeoutSeconds = 30)
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Exe
    $info.Arguments = $Arguments
    $info.WorkingDirectory = $Directory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { throw "无法启动：$Exe" }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            $process.WaitForExit()
            throw "命令执行超时：$Exe $Arguments"
        }
        $output = ($stdout.GetAwaiter().GetResult() + $stderr.GetAwaiter().GetResult()).Trim()
        if ($process.ExitCode -ne 0) { throw "命令失败（退出码 $($process.ExitCode)）：$output" }
        return $output
    } finally { $process.Dispose() }
}

function Install-TunnelBinary {
    param([ValidateSet('frpc','frps','caddy')][string]$Name, [string]$Directory, $Settings)
    $exe = Join-Path $Directory "$Name.exe"
    $version = $Settings.FrpVersion
    $versionArg = '-v'
    if ($Name -eq 'caddy') { $version = $Settings.CaddyVersion; $versionArg = 'version' }
    if (Test-Path -LiteralPath $exe) {
        $reported = Invoke-TunnelNative -Exe $exe -Arguments $versionArg -Directory $Directory
        if ($reported -notmatch ('^v?' + [regex]::Escape($version) + '(?:\s|$)')) {
            throw "$exe 版本不符，要求 $version，实际 $reported。请先停止进程，再从官方发行包更换。"
        }
        return $exe
    }
    [IO.Directory]::CreateDirectory($Directory) | Out-Null
    $downloadRoot = Join-Path $script:TunnelRoot '.downloads'
    $work = Join-Path $downloadRoot ([Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($work) | Out-Null
    try {
        if ($Name -eq 'caddy') {
            $archiveName = "caddy_${version}_windows_amd64.zip"
            $url = "https://github.com/caddyserver/caddy/releases/download/v$version/$archiveName"
            $relativeExe = 'caddy.exe'
        } else {
            $archiveName = "frp_${version}_windows_amd64.zip"
            $url = "https://github.com/fatedier/frp/releases/download/v$version/$archiveName"
            $relativeExe = "frp_${version}_windows_amd64\$Name.exe"
        }
        Write-Host "[下载] $url"
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $zip = Join-Path $work $archiveName
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 120
        $expanded = Join-Path $work 'expanded'
        Expand-Archive -LiteralPath $zip -DestinationPath $expanded
        $candidate = Join-Path $expanded $relativeExe
        if (-not (Test-Path -LiteralPath $candidate)) { throw "官方发行包中缺少 $relativeExe。" }
        $reported = Invoke-TunnelNative -Exe $candidate -Arguments $versionArg -Directory $expanded
        if ($reported -notmatch ('^v?' + [regex]::Escape($version) + '(?:\s|$)')) { throw '下载的二进制版本不符。' }
        Copy-Item -LiteralPath $candidate -Destination $exe
        return $exe
    } catch { throw "无法安装 $Name：$($_.Exception.Message)。可手动从上述官方 Release 下载到 $exe。" }
    finally {
        # 递归清理前核实绝对路径始终位于本项目的下载目录。
        $absoluteWork = [IO.Path]::GetFullPath($work)
        $allowed = [IO.Path]::GetFullPath($downloadRoot) + [IO.Path]::DirectorySeparatorChar
        if ($absoluteWork.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $absoluteWork)) {
            Remove-Item -LiteralPath $absoluteWork -Recurse -Force
        }
    }
}

function Assert-TunnelToken {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw "缺少 $Path，请安全复制服务器生成的 token.txt 到此位置。" }
    $token = [IO.File]::ReadAllText($Path).Trim()
    if ($token -notmatch '^[a-fA-F0-9]{64}$') {
        throw 'token.txt 应保存由服务器安装脚本生成的 64 位十六进制随机 token。'
    }
    # FRP tokenSource 按文件内容读取，消除编辑器附加的换行和 BOM。
    Write-TunnelText -Path $Path -Text $token
}

function Enter-TunnelLock {
    param([string]$Path)
    try { return [IO.File]::Open($Path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw '另一个发布、停止或配置操作正在执行，请稍后重试。' }
}

function Get-TunnelOwnedProcess {
    param([string]$StatePath, [string]$Exe, [string]$ConfigPath)
    if (-not (Test-Path -LiteralPath $StatePath)) { return $null }
    $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($key in @('Pid','StartTimeTicks','ExecutablePath','ConfigPath')) {
        if (-not $state.PSObject.Properties[$key]) { throw "进程记录损坏：$StatePath" }
    }
    # 搬动项目会保留旧路径；先确认记录是否仍对应活进程，再检查目录归属。
    $process = Get-Process -Id $state.Pid -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    if ($process.StartTime.ToUniversalTime().Ticks.ToString() -ne [string]$state.StartTimeTicks -or
        $process.Path -ne $state.ExecutablePath) { return $null }
    if ($state.ExecutablePath -ne [IO.Path]::GetFullPath($Exe) -or
        $state.ConfigPath -ne [IO.Path]::GetFullPath($ConfigPath)) {
        throw '进程记录指向其它目录，且原进程仍在运行。请先停止原目录的服务，再从新目录启动；不会操作其它目录的进程。'
    }
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $($state.Pid)"
    if (-not $cim -or -not $cim.CommandLine -or
        $cim.CommandLine.IndexOf($state.ConfigPath, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw '无法确认该进程使用本项目配置，拒绝结束进程。'
    }
    return $process
}

function Get-TunnelListeners {
    param([int[]]$Ports)
    return ,@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -in $Ports })
}

function Format-TunnelListeners {
    param($Listeners)
    return (($Listeners | ForEach-Object {
        $owner = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
        $name = '未知进程'
        if ($owner) { $name = $owner.ProcessName }
        "$($_.LocalAddress):$($_.LocalPort) PID=$($_.OwningProcess) $name"
    }) -join '; ')
}
