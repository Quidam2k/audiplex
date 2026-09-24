# DEPLOY HANDOFF: Audiplex PC follow-me (#2021, assignment #5054)

Code is finished on `duck-deeper` (last commit 20249fc): slice 1 (Windows tray renderer)
plus slice 2 (transfer handshake on phone and PC). 455 server tests pass. Android compiles
and its unit tests pass. End-to-end handoff was verified on a :8199 test instance.

## BLOCKED: live :8100 restart denied by the auto-mode classifier ("Production Deploy")
The live server is still on the old code. Todd (or a permitted session) runs these in order:
1. Restart :8100: kill the uvicorn :8100 listener, then run `wscript.exe Q:\Development\audiplex\launch-hidden.vbs`.
   Verify `GET /api/playback/devices` answers 200 (it returns 404 on the old code).
2. DONE: `%APPDATA%\AudiplexPC\config.json` base_url = http://192.168.50.139:8100 (token for service user pc-solace, id 4).
3. `cmd //c "Q:\Development\audiplex\android\gradlew.bat" -p Q:/Development/audiplex/android assembleDebug --console=plain`
   Todd installs it from the in-app update chip.
4. ONLY AFTER step 1: from `windows_client/`, run `pythonw -m audiplex_pc`.
   Never run it against the old server: the old bus gives commands to any poller, so the PC would race the phone.
   No autostart unless Todd OKs it (R3; Jarvis is asking him).
5. Smoke test (nothing audible): `GET /api/playback/devices` shows pc-solace connected,
   and active_device_id is null or "phone".

## Follow-ons
- Audiobook follow-me: Jarvis is filing it separately. The PC renderer plays music only.
- The tray window UI hasn't been checked visually.
- Edge case: if a stale PC was the declared renderer and the phone then played something else,
  transferring to phone restores the PC's last queue.
