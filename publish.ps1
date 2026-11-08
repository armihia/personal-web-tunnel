[CmdletBinding()]
param([ValidateRange(1,65535)][int]$LocalPort = 0, [switch]$Background,
      [ValidateNotNullOrEmpty()][string]$ServerAddress)
$forward = @{ Background = $Background }
if ($PSBoundParameters.ContainsKey('LocalPort')) { $forward.LocalPort = $LocalPort }
if ($PSBoundParameters.ContainsKey('ServerAddress')) { $forward.ServerAddress = $ServerAddress }
$global:LASTEXITCODE = 0
& (Join-Path $PSScriptRoot 'client\publish.ps1') @forward
exit $LASTEXITCODE
