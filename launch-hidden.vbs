' Launch Audiplex server without a visible console window.
' Used by Task Scheduler for auto-start on boot.
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
WshShell.CurrentDirectory = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "server")
WshShell.Run "C:\Python311\python.exe -m uvicorn audiplex.main:app --host 0.0.0.0 --port 8100", 0, False
