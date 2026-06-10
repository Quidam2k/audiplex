' Launch Audiplex server without a visible console window.
' Used by Task Scheduler for auto-start on boot.
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "Q:\Development\audiplex\server"
WshShell.Run "C:\Python311\python.exe -m uvicorn audiplex.main:app --host 0.0.0.0 --port 8000", 0, False
