$global:LASTEXITCODE = 0
& (Join-Path $PSScriptRoot 'client\stop-publish.ps1')
exit $LASTEXITCODE
