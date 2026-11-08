$global:LASTEXITCODE = 0
& (Join-Path $PSScriptRoot 'client\status.ps1')
exit $LASTEXITCODE
