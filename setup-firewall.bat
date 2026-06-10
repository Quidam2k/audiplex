@echo off
REM One-time setup: open inbound TCP 8000 for the Audiplex server, scoped to the home LAN.
REM Run this AS ADMINISTRATOR (right-click -> Run as administrator).

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo This script must be run as Administrator.
    echo Right-click setup-firewall.bat and choose "Run as administrator".
    pause
    exit /b 1
)

powershell -NoProfile -Command "New-NetFirewallRule -DisplayName 'Audiplex Server (TCP 8000)' -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -RemoteAddress 192.168.50.0/24 -Profile Any -Description 'Allow inbound TCP 8000 from home LAN for the Audiplex audiobook server'"

if %errorLevel% equ 0 (
    echo.
    echo Firewall rule added. Audiplex is now reachable from your LAN.
) else (
    echo.
    echo Firewall rule add failed. See error above.
)
pause
