@echo off
REM Launch the Audiplex server. Close this window to stop.
cd /d "%~dp0server"

REM Kill anything already listening on port 8100 (taskkill /T kills the whole process tree)
for /f "tokens=5" %%p in ('netstat -aon ^| findstr "LISTENING" ^| findstr ":8100 "') do (
    echo Killing process tree rooted at PID %%p
    taskkill /F /T /PID %%p >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo Starting Audiplex server on http://0.0.0.0:8100 ...
echo (LAN URL: http://192.168.50.139:8100)
echo.
C:\Python311\python.exe -m uvicorn audiplex.main:app --host 0.0.0.0 --port 8100
echo.
echo Server stopped.
pause
