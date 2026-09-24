# HANDOFF — Audiplex Windows "follow-me" client (#2021 / assignment #5038)

**For:** the replacement worker (label `win-client`, Opus 5.5) executing signed plan-back #19815.
**From:** worker-audiplex-win-client--20260924-203352-632b (Opus 4.8), parked per #5046 (Todd's move-to-5.5 order 13:47). No work lost.
**Branch:** `duck-deeper`. WIP committed + pushed: `5315877` ("device registry + active-device targeting on the playback bus (slice 1 WIP)").

## Plan is APPROVED — do not re-plan. Execute it.
Plan-back #19815, signed by Jarvis + Karen (see assignment #5042 for full text). Locked answers/riders:
- Q1 **headless tray**; never pop/steal focus; a minimal now-playing/transfer window opens ONLY on tray-icon click (Todd hates MusicBee's cluttered UI — keep it minimal).
- Q2 **per-device service tokens**: `pc-solace`, `pc-athena`.
- Q3 slice 1 = **music only**; audiobook follow-me deferred to slice 2.
- Q4 **Android deactivate/reactivate = slice 2 in THIS cascade**, immediately after slice 1 verifies. Don't let it drift — it's the "follows me" part Todd actually asked for.
- R1 back-compat test: phone-alone byte-identical with no PC registered.
- R2 stale renderer must not strand playback: `last_seen` timeout → fall back to phone; `dj_now_playing` says which device is active.
- R3 autostart on Solace/Athena ONLY with Todd's explicit OK — **ask Jarvis/Todd before adding autostart**.
- Never test the APK on Todd's phone (the in-app update chip is his gate). Report slice-1 test with the actual device + track heard.

## DONE (committed in 5315877, back-compat verified: 43 existing playback tests pass)
Server-side of slice 1 is functional:
- **`server/audiplex/playback_bus.py`**: `LEGACY_DEVICE_ID="phone"`; `DeviceRecord` dataclass; `self._devices` + `self._active_device_id`; `register_device()`, `touch_device()`, `_target_device_id()` (None when no active device OR active is stale → any live poller, R2), `set_active_device()` (phone always valid; wakes pollers), `active_device_id` property, `devices()`. `next()` now takes `device_id/device_name/device_type`, registers the poller (paramless → "phone"), and `_claim(now, poller_id)` only delivers when the poller is the target. `device_status()` now includes `active_device_id`, `effective_target_device_id`, `devices`.
- **`server/audiplex/routers/playback.py`**: `GET /api/playback/devices`, `POST /api/playback/devices/{id}/activate`; `command/next` accepts `device_id/device_name/device_type` query params; `post_state` accepts optional `device_id` to touch liveness.
- **`audiplex_mcp/server.py`**: new `dj_devices` and `dj_transfer(device)` (matches id or friendly name, case-insensitive; 'phone' always valid); `dj_now_playing` now reports the active device + flags stale fallback.

## NOT DONE — pick up here
1. **Tests (do first, cheap, no windows).** Add to `server/tests/test_playback_registry.py` (or a new `test_playback_devices.py`) — fixtures/style are in that file, `reset_bus` autouse fixture already resets the singleton:
   - R1: with no PC registered, phone (paramless `command/next`) gets its command exactly as today; `/devices` shows only "phone"; `active_device_id` is None.
   - Targeting: register `pc-solace` via `command/next?device_id=pc-solace`; `POST /devices/pc-solace/activate`; then a paramless (phone) poll gets 204 while a `?device_id=pc-solace` poll gets the command. (Set `LONGPOLL_TIMEOUT_SECONDS` low via monkeypatch like the existing redelivery tests.)
   - R2 stale fallback: monkeypatch `DEVICE_STALE_AFTER_SECONDS` to ~0; after activating pc-solace, once it's stale a phone poll is served again; `device_status()['effective_target_device_id']` is None.
   - `activate` unknown device → 404.
2. **PC tray client (CODEX-FIRST — this is the bulk of new code; draft via `bash Q:/Pantheon/scripts/codex_draft.sh <name> <spec-file>`, review/test/install every line, then `python Q:/Pantheon/scripts/codex_ledger.py verdict <token> ...`).** Suggested location: a new top-level `windows_client/` (or `pc_client/`). Components:
   - Config: server base URL (`http://solace:8000`? NO — server is **:8100**; on-LAN `http://192.168.50.139:8100`, off-LAN `http://solace:8100`), device id (`pc-solace`/`pc-athena`), per-device JWT token.
   - Auth token: mint per-device via `python -m audiplex.create_service_token` — BUT that script hardcodes user `dj-agent`; you'll likely need to parametrize it (or add `pc-solace`/`pc-athena` users) so each PC has its own identity. Check `server/audiplex/create_service_token.py` and `settings.dj_owner_username` interplay — owner-scoped taste reads assume the caller maps to the configured owner; a plain new service user is fine for a renderer (it only needs to stream + drive the bus).
   - **Local auth proxy**: tiny `http.server`/`aiohttp` on `127.0.0.1:<ephemeral>` that injects `Authorization: Bearer <token>` and forwards **range requests** to `:8100`. Point VLC at the loopback URL. This is the whole reason the client works without a server `?token=` change. Watch: forward `Range` + return 206 + `Content-Range`/`Accept-Ranges` faithfully for seek on large files.
   - **Bus loops** (mirror `android/app/src/main/java/com/audiplex/app/playback/DjCommandClient.kt`): `commandLoop` long-polls `GET /api/playback/command/next?device_id=pc-solace&device_name=Solace&device_type=windows` (25s server long-poll, ~30s read timeout), dispatches to the VLC wrapper, then `POST /command/{id}/ack` (`{"status":"ok"}` or a failure status+detail). Dedupe executed command ids (delivery is at-least-once). `reportLoop`: every 5s `POST /api/playback/state?device_id=pc-solace` with the `PlaybackState` shape (see `server/audiplex/schemas.py` `PlaybackState`/`NowPlayingQueueItem`/`NowPlayingTrack`).
   - **Command types to handle** (music slice 1): resolve them from what the DJ MCP enqueues — `play_now`, `queue`, `play_next`, `skip`, `previous`, `pause`, `resume`, `seek`, `volume`, `reorder`, plus `announce`/voice-break clips. Read `audiplex_mcp/server.py` (dj_* tools, ~line 100-270 build the command payloads) for exact `type`+`payload` shapes, and the Android `PlaybackManager`/command dispatch for the reference semantics.
   - **VLC wrapper**: python-vlc MediaListPlayer for a queue (gapless), volume 0..1 to match the bus's `volume` field, position/duration in ms. Streams music tracks via `/api/music/stream/track/{track_id}` through the local proxy.
   - **Tray** (`pystray` + `Pillow`): background, no window; menu/left-click opens a minimal now-playing + "transfer here"/"transfer to phone" window (transfer here = `POST /devices/{this}/activate`). Never steal focus.
   - Ship player errors/process-exit up via `POST /api/playback/client-log` (optional but matches the phone).
3. **Slice 2** (after slice 1 verified): Android honors a `deactivate`/`activate` command (pause + report final position on deactivate; resume on activate) so transfer auto-pauses the phone and resumes at the exact position; true bidirectional handoff; audiobook follow-me via `/api/progress`. `dj_transfer` currently does NOT pause the previous device (by design for slice 1) — slice 2 adds the deactivate command emission in `set_active_device`/`dj_transfer`.

## Gotchas
- Server runs on **:8100** (`launch.bat`, `launch-hidden.vbs`). The DJ MCP defaults `AUDIPLEX_URL` to `:8000` — env must override; your client must target :8100.
- JWT is **Bearer header only, host-gated** (no `?token=`). That's why the local proxy exists.
- `CLAUDE.md` shows as modified in git — that's the orchestrator-generated boot block; **do not commit it**.
- Codex leash: headless, read-only, you read/test/install every line, record ledger verdict.
- Commit via `python Q:/Pantheon/scripts/orch_safe_commit.py --marker '#2021' [--allow-unmarked <file> per file if hunks lack the marker] --message-file <f> <paths>`; then `git -C Q:/Development/audiplex push` as its OWN bare command. Don't commit `project-reports/` or `CLAUDE.md`.

## Verify env before you start
`orch_get_assignments(project='audiplex', team_name='<your team>', limit=3)`. The signed plan is assignment #5042 (has full reviewer notes). This handoff + `notes/2026-09-24-win-follow-me-plan.md` are the design record.
