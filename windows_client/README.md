# Audiplex PC renderer (#2021)

Headless tray client that makes a Windows PC a playback target on the Audiplex
playback bus, Spotify-Connect style. The DJ (or the tray menu) transfers
playback to it; while it is the active device, bus commands play here instead
of on the phone. When it isn't active it just polls and never takes commands.

- Needs VLC installed (64-bit, matching Python) and `pip install --user -r requirements.txt`.
- Config: `%APPDATA%\AudiplexPC\config.json`
  `{"base_url": "http://192.168.50.139:8100", "token": "...", "device_id": "pc-solace", "device_name": "Solace"}`
  (env overrides: `AUDIPLEX_PC_URL`, `AUDIPLEX_PC_TOKEN`, `AUDIPLEX_DEVICE_ID`, `AUDIPLEX_DEVICE_NAME`).
- Token: `cd server && python -m audiplex.create_service_token --username pc-solace` (one account per PC).
- Run: `pythonw -m audiplex_pc` from this folder (`--debug` for verbose logging).
  Log: `%APPDATA%\AudiplexPC\audiplex_pc.log`.
- UI: tray icon only. Left-click opens a small now-playing / transfer window.

Audio streams through a loopback proxy (`proxy.py`) that adds the Bearer
header, since the server takes no `?token=` query auth.
Music only for now: sleep beds/timers ack as `unsupported`.
