' Launch the DJ-bridge watcher (#2858) without a visible console window.
' Read-only against Audiplex; on/off lives in data\dj_patter.json.
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "Q:\Development\audiplex"
WshShell.Run "C:\Python311\python.exe -m audiplex_mcp.dj_bridge_watcher run", 0, False
