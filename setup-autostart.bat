@echo off
REM Register Audiplex server to start automatically on Windows boot.
REM Run this AS ADMINISTRATOR (right-click -> Run as administrator).

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo This script must be run as Administrator.
    echo Right-click setup-autostart.bat and choose "Run as administrator".
    pause
    exit /b 1
)

powershell -NoProfile -Command "Register-ScheduledTask -TaskName 'Audiplex Server' -Description 'Start the Audiplex audiobook server on boot' -Trigger (New-ScheduledTaskTrigger -AtStartup) -Action (New-ScheduledTaskAction -Execute 'wscript.exe' -Argument '\"Q:\Development\audiplex\launch-hidden.vbs\"') -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit 0) -RunLevel Highest -User '%USERNAME%' -Force"

if %errorLevel% equ 0 (
    echo.
    echo Task 'Audiplex Server' registered successfully.
    echo The server will start automatically on next boot.
    echo.
    echo To start it now without rebooting, run:
    echo   schtasks /run /tn "Audiplex Server"
    echo.
    echo To remove auto-start later:
    echo   schtasks /delete /tn "Audiplex Server" /f
) else (
    echo.
    echo Failed to register task. Check the error above.
)
pause
