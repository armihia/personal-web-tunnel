@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0stop-tunnel.ps1" -ShowResult
exit /b %errorlevel%
