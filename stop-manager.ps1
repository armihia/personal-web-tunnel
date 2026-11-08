[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common\Tunnel.Common.ps1')
try {
    Assert-TunnelWindows
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"')
        exit 0
    }
    $statePath = Join-Path $PSScriptRoot 'client\manager.state.json'
    $pythonCandidates = @((Join-Path $PSScriptRoot 'client\python\python.exe'))
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $pythonCandidates += $pythonCommand.Source }
    $ownedPython=$pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (Test-Path -LiteralPath $statePath) {
        $savedState=Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($savedState.ExecutablePath -in $pythonCandidates) { $ownedPython=$savedState.ExecutablePath }
    }
    $process = Get-TunnelOwnedProcess -StatePath $statePath -Exe $ownedPython -ConfigPath (Join-Path $PSScriptRoot 'manager\app.py')
    if ($process) {
        $settings = Get-TunnelSettings
        $port = 17890
        if ($settings.PSObject.Properties['ManagerPort']) { $port = $settings.ManagerPort }
        Add-Type -AssemblyName System.Net.Http
        $handler = New-Object Net.Http.HttpClientHandler
        $handler.UseProxy = $false
        $http = New-Object Net.Http.HttpClient($handler)
        $http.Timeout = [TimeSpan]::FromSeconds(5)
        try {
            $origin = "http://127.0.0.1:$port"
            $session = $http.GetStringAsync("$origin/api/session").GetAwaiter().GetResult() | ConvertFrom-Json
            $http.DefaultRequestHeaders.Add('X-Manager-Token',$session.token)
            $http.DefaultRequestHeaders.Add('Origin',$origin)
            $body = New-Object Net.Http.StringContent('{}',[Text.Encoding]::UTF8,'application/json')
            $response = $http.PostAsync("$origin/api/shutdown",$body).GetAwaiter().GetResult()
            $response.EnsureSuccessStatusCode() | Out-Null
            $response.Dispose(); $body.Dispose()
        } finally { $http.Dispose(); $handler.Dispose() }
        if (-not $process.WaitForExit(30000)) { throw '后台仍在清理，请稍后再试；没有强制结束进程。' }
    }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    Write-Host '[OK] 后台服务已停止，映射列表保留。'
} catch { Write-Error $_ -ErrorAction Continue; exit 1 }
